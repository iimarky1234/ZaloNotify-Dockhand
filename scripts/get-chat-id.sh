#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

if [[ -z "${ZALO_BOT_TOKEN:-}" && -f "${ROOT_DIR}/.env" ]]; then
  token_line="$(grep -E '^ZALO_BOT_TOKEN=' "${ROOT_DIR}/.env" | tail -n 1 || true)"
  token_val="${token_line#ZALO_BOT_TOKEN=}"
  token_val="${token_val%\"}"
  token_val="${token_val#\"}"
  token_val="${token_val%\'}"
  token_val="${token_val#\'}"
  ZALO_BOT_TOKEN="${token_val}"
fi

if [[ -z "${ZALO_BOT_TOKEN:-}" ]]; then
  echo "Error: ZALO_BOT_TOKEN is not set (export it or set it in .env)." >&2
  exit 1
fi

for cmd in curl jq; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Error: '$cmd' is required." >&2
    exit 1
  fi
done

TIMEOUT="${ZALO_POLL_TIMEOUT:-5}"
if ! [[ "$TIMEOUT" =~ ^[0-9]+$ ]]; then
  echo "Error: ZALO_POLL_TIMEOUT must be an integer (got '$TIMEOUT')." >&2
  exit 1
fi

response="$(
  curl -sS -X POST "https://bot-api.zaloplatforms.com/bot${ZALO_BOT_TOKEN}/getUpdates" \
    -H 'Content-Type: application/json' \
    -d "{\"timeout\":\"${TIMEOUT}\"}"
)"

ok="$(printf '%s' "$response" | jq -r '.ok // false')"
error_code="$(printf '%s' "$response" | jq -r '.error_code // 0')"
if [[ "$ok" != "true" ]]; then
  if [[ "$error_code" == "408" ]]; then
    echo "No updates returned (timeout). Send a message to your bot in Zalo (or @mention it in a group) and run this script again." >&2
    exit 0
  fi
  printf 'Zalo API error: %s\n' "$response" >&2
  exit 1
fi

rows="$(
  printf '%s' "$response" | jq -r '
    .result
    | if . == null then empty
      elif type == "array" then .[]
      else .
      end
    | .message? // empty
    | [
        (.chat.id // ""),
        (.chat.chat_type // .chat.type // ""),
        (.from.display_name // .chat.display_name // .chat.title // "")
      ]
    | @tsv
  '
)"

if [[ -z "$rows" ]]; then
  echo "No updates returned. Send a message to your bot in Zalo (note: getUpdates does not return events if a webhook is active) and run this script again." >&2
  exit 0
fi

printf "CHAT_ID\tTYPE\tNAME\n"
printf '%s\n' "$rows"
