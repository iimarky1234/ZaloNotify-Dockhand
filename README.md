# Zalo Notifications for Dockhand (No Source Changes)

Deliver [Dockhand](https://github.com/Finsys/dockhand) container, stack, and system notifications to **Zalo** (users or group chats) without modifying Dockhand's source code.

## How It Works

Dockhand has a built-in `apprise://host:port/key` notification channel that forwards notifications (`{title, body, type, format}`) to a self-hosted [`caronc/apprise-api`](https://github.com/caronc/apprise-api) sidecar. Apprise `2.2.0` (shipped in `caronc/apprise:2.1.0`) includes a native `zalo://{bot_token}/{chat_id}` plugin for the **Zalo Bot Platform** (`bot-api.zaloplatforms.com`).

```text
Dockhand (unchanged)
  └─ channel URL: apprise://apprise:8000/dockhand[?format=markdown]
        │  POST http://apprise:8000/notify/dockhand
        ▼
apprise-api sidecar (this compose stack)
  └─ /config/dockhand.yml: zalo://<BOT_TOKEN>/<CHAT_ID>
        │  POST https://bot-api.zaloplatforms.com/bot<BOT_TOKEN>/sendMessage
        ▼
Zalo user or group chat

zalo-watcher sidecar (optional auto-registration via long polling)
  └─ polls getUpdates ──▶ adds/removes chat_ids in /config/dockhand.yml (no restart needed)
```

### Security & Networking Notes

- **Do not publish port `8000` publicly:** `apprise-api` has no authentication enabled by default and stores your Zalo bot token in `./apprise/dockhand.yml`. Keep it reachable only over Dockhand's Docker network (`docker-compose.yml` exposes no ports by default).
- **Do not use `localhost` / `127.0.0.1` in Dockhand:** Dockhand's notification SSRF guard blocks loopback addresses. Connecting both containers to the same Docker network lets Dockhand reach `apprise://apprise:8000/dockhand`.
- **Keep secrets out of git:** `.gitignore` excludes `.env` and `apprise/*.yml`.

---

## Setup

### 1. Create a Zalo Bot and get your `chat_id`

1. In the Zalo app, search for **Zalo Bot Manager** and choose **Create bot** (the bot name must start with `Bot`).
2. Copy the **Bot Token** (`123456789:abcdefghijKLMNOPqrst`).
3. Send a message to your bot in Zalo (or add it to a group chat and mention/message it).
4. Make the helper script executable and run it to retrieve your `chat_id` (requires `curl` and `jq`):

   ```bash
   chmod +x scripts/get-chat-id.sh
   ZALO_BOT_TOKEN="123456789:your_token_here" ./scripts/get-chat-id.sh
   ```

   > **Note:** `getUpdates` uses long polling and returns nothing if you have previously configured a webhook on the bot.

### 2. Find Dockhand's Docker network

Find the Docker network attached to your running Dockhand container:

```bash
docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' <dockhand-container>
```

### 3. Configure `.env` and `apprise/dockhand.yml`

```bash
cp .env.example .env
cp apprise/dockhand.yml.example apprise/dockhand.yml
```

1. Edit `.env` and set `DOCKHAND_NETWORK` to the network name from Step 2 (for example `dockhand_default`), plus your `TZ` / `PUID` / `PGID` if needed.
2. Edit `apprise/dockhand.yml` and replace `<BOT_TOKEN>` and `<CHAT_ID>`:

   ```yaml
   urls:
     - zalo://123456789:your_token_here/your_chat_id
   ```

   To notify multiple chats at once, list them separated by `/`:

   ```yaml
   urls:
     - zalo://123456789:your_token_here/chat_id_1/chat_id_2
   ```

### 4. Start the Apprise sidecar

```bash
docker compose up -d
```

Check health:

```bash
docker compose ps
docker compose logs -f apprise
```

### 5. Add the channel in Dockhand

Open Dockhand → **Settings → Notifications → Add**:

- **Type:** Apprise / Webhook
- **URL (choose plain text, Markdown, or create separate channels for both):**
  - **Plain text (default):**
    ```text
    apprise://apprise:8000/dockhand
    ```
  - **Markdown (`parse_mode: markdown`):**
    ```text
    apprise://apprise:8000/dockhand?format=markdown
    ```
- Click **Test**, then select the events (globally or per environment) you want forwarded to Zalo.

> **Both plain text & Markdown work with the same `apprise/dockhand.yml`:** `docker-compose.yml` sets `APPRISE_DEFAULT_FORMAT: text` so `apprise://apprise:8000/dockhand` sends clean plain text (preventing underscores in container names from being parsed as Markdown italics), while `apprise://apprise:8000/dockhand?format=markdown` explicitly overrides it to send Markdown (`parse_mode: markdown`).

---

## Automatic Chat Registration (`zalo-watcher`)

`docker-compose.yml` includes a `zalo-watcher` sidecar ([`watcher/zalo_watcher.py`](watcher/zalo_watcher.py)) that long-polls Zalo's `getUpdates` endpoint and atomically updates `apprise/dockhand.yml` whenever chats subscribe or unsubscribe (no `apprise` restart needed).

> **Group chat note:** Zalo Bots do not receive passive "bot joined group" events. After inviting your bot into a group, send a message mentioning the bot (for example `@Bot Dockhand /subscribe <passcode>`) so Zalo delivers an event to `zalo-watcher`. Also ensure **Webhook** is not configured on the bot, because `getUpdates` and webhooks are mutually exclusive.

### Modes (configured in `.env`)

- **Passcode-gated (recommended):** Set `ZALO_SUBSCRIBE_PASSCODE=<your-secret>` and leave `ZALO_AUTO_SUBSCRIBE=false`. Only chats that send the secret passcode are added:
  - DM: `/subscribe <your-secret>`
  - Group: `@Bot Dockhand /subscribe <your-secret>`
- **Open auto-add:** Set `ZALO_AUTO_SUBSCRIBE=true` to automatically add **any** DM or group `@mention` without a passcode *(use only if your bot is private, since anyone who finds the bot will receive Dockhand alerts)*.

### Bot Commands

| Command (prefix with `@BotName ` in groups) | Action |
|---|---|
| `/subscribe <passcode>` | Subscribe this DM or group chat to `apprise/dockhand.yml` |
| `/unsubscribe` | Remove this chat from `apprise/dockhand.yml` |
| `/status` | Show this chat's `chat_id` and subscription status |
| `/help` | List available commands |

Check watcher logs:

```bash
docker compose logs -f zalo-watcher
```

---

## Verification

1. **Test `apprise-api` → Zalo directly from the sidecar:**

   Plain text:
   ```bash
   docker compose exec apprise curl -s -o /dev/null -w '%{http_code}\n' \
     -X POST http://localhost:8000/notify/dockhand \
     -H 'Content-Type: application/json' \
     -d '{"title":"Apprise plain test","body":"Hello from apprise-api","type":"info"}'
   # Expect: 200
   ```

   Markdown:
   ```bash
   docker compose exec apprise curl -s -o /dev/null -w '%{http_code}\n' \
     -X POST http://localhost:8000/notify/dockhand \
     -H 'Content-Type: application/json' \
     -d '{"title":"Apprise markdown test","body":"**Bold** and `code`","type":"info","format":"markdown"}'
   # Expect: 200
   ```

2. **Test Dockhand → `apprise-api` network reachability:**

   ```bash
   docker exec <dockhand-container> curl -fs http://apprise:8000/status
   ```

3. **Test end-to-end in Dockhand UI:** Click **Test** on your notification channel (`Dockhand Test Notification` should arrive in Zalo).
4. **Verify failure reporting:** If you temporarily put an invalid `chat_id` in `apprise/dockhand.yml` and click **Test**, Dockhand reports *"Apprise: at least one downstream provider failed"*.

## Limitations (Zalo Bot Platform)

- **2,000-character message limit:** Apprise's `zalo` plugin automatically splits messages exceeding 2,000 characters into multiple messages.
- **No file attachments:** Zalo Bot Platform `sendMessage` does not accept uploaded file attachments (`attachments` from Dockhand won't be sent through Zalo).
- **Bot interaction prerequisite:** Each recipient user or group must message the bot first (and share their `chat_id`) before the bot can send messages to them.
