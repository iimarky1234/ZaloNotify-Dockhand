#!/usr/bin/env python3
"""
Zalo Bot Platform long-polling watcher sidecar for Dockhand + Apprise.

Polls https://bot-api.zaloplatforms.com/bot<TOKEN>/getUpdates and updates
/config/dockhand.yml (Apprise APPRISE_STATEFUL_MODE=simple) whenever a chat
subscribes or unsubscribes.

Security model (fail-closed by default):
  - If ZALO_SUBSCRIBE_PASSCODE is set: chats must send `/subscribe <passcode>`
    (or `@BotName /subscribe <passcode>` in groups).
  - If ZALO_AUTO_SUBSCRIBE=true: any incoming chat/group mention is added
    automatically without a passcode (only use if the bot is private).
  - If neither is set, the watcher waits and refuses subscriptions rather than
    exposing container alerts to strangers.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import requests
import yaml

ZALO_API_BASE = os.environ.get("ZALO_API_BASE", "https://bot-api.zaloplatforms.com").rstrip("/")
TOKEN_RE = re.compile(r"^[0-9]+:[A-Za-z0-9_-]+$")
CHAT_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
PLACEHOLDER_RE = re.compile(r"^<[^>]+>$")
COMMAND_RE = re.compile(r"(?:^|\s)/([a-zA-Z]+)(?:\s+(.*))?$")

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format=LOG_FORMAT)
logger = logging.getLogger("zalo-watcher")


def redact_token(text: str, token: str | None) -> str:
    """Mask the bot token in any log or error string."""
    if not text:
        return text
    if token:
        text = text.replace(token, "***")
    return re.sub(r"/bot[0-9]+:[A-Za-z0-9_-]+/", "/bot***/", text)


def is_valid_token(token: str | None) -> bool:
    return bool(token and TOKEN_RE.match(token.strip()))


def is_valid_chat_id(chat_id: str | None) -> bool:
    return bool(chat_id and not PLACEHOLDER_RE.match(chat_id) and CHAT_ID_RE.match(chat_id))


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_zalo_url(url: str) -> tuple[str, list[str]] | None:
    """
    Parse a plain string `zalo://<token>/<chat_id>[/<chat_id>...]`.
    Returns (token, [chat_ids]) or None if not a plain zalo:// URL.
    """
    if not isinstance(url, str):
        return None
    stripped = url.strip()
    if not stripped.lower().startswith("zalo://"):
        return None
    rest = stripped[7:]
    if "?" in rest:
        rest = rest.split("?", 1)[0]
    parts = [p for p in rest.split("/") if p]
    if not parts:
        return None
    token = parts[0]
    chat_ids = [p for p in parts[1:] if is_valid_chat_id(p)]
    return token, chat_ids


def build_zalo_url(token: str, chat_ids: list[str]) -> str:
    return f"zalo://{token}/" + "/".join(chat_ids)


def load_config_data(config_path: Path) -> dict[str, Any]:
    """Load /config/dockhand.yml safely, returning {'urls': [...]}."""
    if not config_path.exists():
        return {"urls": []}
    raw = config_path.read_text(encoding="utf-8")
    if not raw.strip():
        return {"urls": []}
    loaded = yaml.safe_load(raw)
    if not isinstance(loaded, dict):
        return {"urls": []}
    urls = loaded.get("urls")
    if not isinstance(urls, list):
        loaded["urls"] = []
    return loaded


def extract_token_from_config(config_path: Path) -> str | None:
    """Fall back to finding a valid Zalo bot token already saved in dockhand.yml."""
    try:
        data = load_config_data(config_path)
    except Exception:
        return None
    for entry in data.get("urls", []):
        if isinstance(entry, str):
            parsed = parse_zalo_url(entry)
            if parsed and is_valid_token(parsed[0]):
                return parsed[0]
        elif isinstance(entry, dict):
            for key in entry:
                parsed = parse_zalo_url(str(key))
                if parsed and is_valid_token(parsed[0]):
                    return parsed[0]
    return None


def get_subscribed_chats(config_path: Path, token: str) -> list[str]:
    """Return deduplicated chat IDs currently configured for this bot token."""
    data = load_config_data(config_path)
    seen: list[str] = []
    for entry in data.get("urls", []):
        if isinstance(entry, str):
            parsed = parse_zalo_url(entry)
            if parsed and parsed[0] == token:
                for cid in parsed[1]:
                    if cid not in seen:
                        seen.append(cid)
    return seen


def write_config_atomic(config_path: Path, data: dict[str, Any]) -> None:
    """Atomically write Apprise YAML config so apprise-api never sees a partial file."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Managed by zalo-watcher (safe to edit; other URLs and tagged entries are preserved)\n"
    )
    body = yaml.safe_dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".dockhand.", suffix=".tmp", dir=str(config_path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(header)
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, config_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def update_dockhand_config(
    config_path: Path,
    token: str,
    chat_id: str,
    action: str,
) -> tuple[bool, list[str]]:
    """
    Add or remove `chat_id` for `token` in `config_path`.

    Preserves:
      - other notification URLs (Discord, ntfy, etc.)
      - tagged YAML dict entries (e.g. `- zalo://...: {tag: ops}`)
      - other existing chat_ids on the untagged `zalo://` entry
    Drops placeholder entries like `zalo://<BOT_TOKEN>/<CHAT_ID>`.
    Returns (changed, current_chat_ids).
    """
    if action not in {"add", "remove"}:
        raise ValueError(f"Unsupported action: {action}")
    if not is_valid_token(token):
        raise ValueError("Invalid ZALO_BOT_TOKEN format")
    if not is_valid_chat_id(chat_id):
        raise ValueError(f"Invalid chat_id: {chat_id!r}")

    data = load_config_data(config_path)
    existing_urls: list[Any] = data.get("urls", [])

    kept_urls: list[Any] = []
    chat_ids: list[str] = []
    insert_at: int | None = None

    for entry in existing_urls:
        if isinstance(entry, str):
            parsed = parse_zalo_url(entry)
            if parsed is not None:
                entry_token, entry_chats = parsed
                # Drop placeholder template entries copied from dockhand.yml.example
                if PLACEHOLDER_RE.match(entry_token):
                    continue
                if entry_token == token:
                    if insert_at is None:
                        insert_at = len(kept_urls)
                    for cid in entry_chats:
                        if cid not in chat_ids:
                            chat_ids.append(cid)
                    continue
        kept_urls.append(entry)

    before = list(chat_ids)
    if action == "add":
        if chat_id not in chat_ids:
            chat_ids.append(chat_id)
    elif action == "remove":
        chat_ids = [cid for cid in chat_ids if cid != chat_id]

    changed = chat_ids != before
    if not changed and config_path.exists():
        return False, chat_ids

    if chat_ids:
        new_url = build_zalo_url(token, chat_ids)
        if insert_at is None:
            kept_urls.append(new_url)
        else:
            kept_urls.insert(insert_at, new_url)

    data["urls"] = kept_urls
    write_config_atomic(config_path, data)
    return changed, chat_ids


def parse_command(text: str | None) -> tuple[str | None, str]:
    """
    Extract slash command and optional argument, tolerating a leading @mention
    (e.g. `@Bot Dockhand /subscribe mypass` -> ('subscribe', 'mypass')).
    """
    if not text:
        return None, ""
    match = COMMAND_RE.search(text.strip())
    if not match:
        return None, ""
    return match.group(1).lower(), (match.group(2) or "").strip()


class ZaloPollingTimeout(Exception):
    """Raised when Zalo getUpdates returns 408 (idle long-poll timeout)."""


class ZaloApiError(Exception):
    """Non-timeout error from Zalo Bot Platform."""


def call_zalo_api(
    session: requests.Session,
    token: str,
    method: str,
    payload: dict[str, Any] | None = None,
    timeout_s: float = 15.0,
) -> dict[str, Any]:
    url = f"{ZALO_API_BASE}/bot{token}/{method}"
    try:
        resp = session.post(url, json=payload or {}, timeout=timeout_s)
    except requests.RequestException as exc:
        raise ZaloApiError(redact_token(str(exc), token)) from None

    if resp.status_code == 408:
        raise ZaloPollingTimeout()

    try:
        body = resp.json()
    except ValueError:
        if not resp.ok:
            raise ZaloApiError(f"HTTP {resp.status_code} from {method}")
        raise ZaloApiError(f"Invalid JSON from {method} (HTTP {resp.status_code})")

    if isinstance(body, dict) and body.get("error_code") == 408:
        raise ZaloPollingTimeout()

    if not resp.ok or not isinstance(body, dict) or body.get("ok") is not True:
        code = body.get("error_code") if isinstance(body, dict) else None
        desc = (body.get("description") or body.get("message")) if isinstance(body, dict) else None
        raise ZaloApiError(
            redact_token(f"{method} failed (http={resp.status_code}, error_code={code}): {desc or 'unknown'}", token)
        )
    return body


def send_reply(
    session: requests.Session,
    token: str,
    chat_id: str,
    text: str,
) -> None:
    try:
        call_zalo_api(session, token, "sendMessage", {"chat_id": chat_id, "text": text}, timeout_s=10.0)
    except Exception as exc:
        logger.warning("Failed to send reply to %s: %s", chat_id, redact_token(str(exc), token))


def handle_update(
    update: dict[str, Any],
    *,
    config_path: Path,
    token: str,
    passcode: str,
    auto_subscribe: bool,
    send_welcome: bool,
    session: requests.Session | None = None,
) -> str | None:
    """
    Process one Zalo update object. Returns a short status label for logging/testing.
    """
    msg = update.get("message")
    if not isinstance(msg, dict):
        return "ignored:no_message"

    sender = msg.get("from") or {}
    if sender.get("is_bot") is True:
        return "ignored:is_bot"

    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not is_valid_chat_id(chat_id):
        return "ignored:invalid_chat_id"

    chat_type = chat.get("chat_type") or chat.get("type") or "UNKNOWN"
    text = msg.get("text")
    cmd, arg = parse_command(text)

    def reply(reply_text: str) -> None:
        if send_welcome and session is not None:
            send_reply(session, token, chat_id, reply_text)

    if cmd == "help":
        reply(
            "Dockhand Zalo Notifier commands:\n"
            "/subscribe <passcode> - Subscribe this chat\n"
            "/unsubscribe - Stop notifications in this chat\n"
            "/status - Check subscription status"
        )
        return "replied:help"

    if cmd == "status":
        chats = get_subscribed_chats(config_path, token)
        is_sub = chat_id in chats
        reply(
            f"Chat ID: {chat_id}\n"
            f"Type: {chat_type}\n"
            f"Subscribed: {'yes' if is_sub else 'no'} (total chats: {len(chats)})"
        )
        return "replied:status"

    if cmd == "unsubscribe":
        changed, chats = update_dockhand_config(config_path, token, chat_id, "remove")
        if changed:
            logger.info("Unsubscribed chat_id=%s (%s). Total chats=%d", chat_id, chat_type, len(chats))
            reply("Unsubscribed. This chat will no longer receive Dockhand notifications.")
            return "unsubscribed"
        reply("This chat is not currently subscribed.")
        return "already_unsubscribed"

    # Subscription path: explicit /subscribe OR open auto_subscribe
    wants_subscribe = cmd == "subscribe" or auto_subscribe
    if not wants_subscribe:
        return "ignored:no_command"

    if not auto_subscribe:
        if not passcode:
            logger.warning(
                "Received /subscribe from chat_id=%s, but neither ZALO_SUBSCRIBE_PASSCODE nor ZALO_AUTO_SUBSCRIBE=true is configured.",
                chat_id,
            )
            reply("Subscription is disabled: set ZALO_SUBSCRIBE_PASSCODE or ZALO_AUTO_SUBSCRIBE=true on the server.")
            return "rejected:unconfigured"
        if not hmac.compare_digest(arg, passcode):
            logger.warning("Rejected /subscribe from chat_id=%s (%s): invalid passcode.", chat_id, chat_type)
            reply("Invalid passcode. Usage: /subscribe <passcode>")
            return "rejected:bad_passcode"

    already = chat_id in get_subscribed_chats(config_path, token)
    if already:
        if cmd == "subscribe":
            reply(f"Already subscribed (chat_id: {chat_id}).")
        return "already_subscribed"

    changed, chats = update_dockhand_config(config_path, token, chat_id, "add")
    if changed:
        logger.info("Subscribed chat_id=%s (%s). Total chats=%d", chat_id, chat_type, len(chats))
        reply(f"Subscribed! This chat ({chat_id}) will now receive Dockhand notifications.")
        return "subscribed"
    return "unchanged"


def normalize_updates(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict):
        return [result]
    return []


def resolve_token(config_path: Path) -> str | None:
    env_token = os.environ.get("ZALO_BOT_TOKEN", "").strip()
    if is_valid_token(env_token):
        return env_token
    if env_token:
        logger.error("ZALO_BOT_TOKEN has an unexpected format (expected <digits>:<secret>).")
        return None
    return extract_token_from_config(config_path)


def main() -> int:
    config_path = Path(os.environ.get("APPRISE_CONFIG_FILE", "/config/dockhand.yml"))
    passcode = os.environ.get("ZALO_SUBSCRIBE_PASSCODE", "").strip()
    auto_subscribe = parse_bool(os.environ.get("ZALO_AUTO_SUBSCRIBE"), default=False)
    send_welcome = parse_bool(os.environ.get("ZALO_SEND_WELCOME"), default=True)
    poll_timeout = max(5, min(60, int(os.environ.get("ZALO_POLL_TIMEOUT", "25"))))

    stop_event = threading.Event()
    session = requests.Session()

    def handle_sig(signum: int, _frame: Any) -> None:
        logger.info("Received signal %s, shutting down...", signum)
        stop_event.set()
        try:
            session.close()
        except Exception:
            pass

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    token = resolve_token(config_path)
    while not token and not stop_event.is_set():
        logger.warning(
            "Waiting for a valid ZALO_BOT_TOKEN (set in .env or in %s)...",
            config_path,
        )
        stop_event.wait(30)
        token = resolve_token(config_path)

    if stop_event.is_set() or not token:
        return 0

    mode_desc = "open (ZALO_AUTO_SUBSCRIBE=true)" if auto_subscribe else (
        "passcode-gated (/subscribe <passcode>)" if passcode else "disabled (set ZALO_SUBSCRIBE_PASSCODE or ZALO_AUTO_SUBSCRIBE=true)"
    )
    existing = get_subscribed_chats(config_path, token)
    logger.info(
        "Zalo watcher started | config=%s | mode=%s | subscribed_chats=%d | poll_timeout=%ds",
        config_path,
        mode_desc,
        len(existing),
        poll_timeout,
    )

    while not stop_event.is_set():
        try:
            data = call_zalo_api(
                session,
                token,
                "getUpdates",
                {"timeout": str(poll_timeout)},
                timeout_s=float(poll_timeout + 10),
            )
            for update in normalize_updates(data.get("result")):
                handle_update(
                    update,
                    config_path=config_path,
                    token=token,
                    passcode=passcode,
                    auto_subscribe=auto_subscribe,
                    send_welcome=send_welcome,
                    session=session,
                )
        except ZaloPollingTimeout:
            continue
        except ZaloApiError as exc:
            if stop_event.is_set():
                break
            logger.warning("Polling error: %s (retrying in 5s)", exc)
            stop_event.wait(5)
        except Exception as exc:
            if stop_event.is_set():
                break
            logger.exception("Unexpected error in watcher loop: %s", redact_token(str(exc), token))
            stop_event.wait(5)

    logger.info("Zalo watcher stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
