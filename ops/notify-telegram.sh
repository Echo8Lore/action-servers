#!/usr/bin/env bash
# notify-telegram.sh — send ONE plain-text message through the Telegram Bot API.
#
#   printf '%s\n' "text" | bash ops/notify-telegram.sh
#
# Env:  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (repo secrets; the bot is Hermes's)
# Exit: 0 sent | 2 not configured (a secret is empty) | 3 send failed
#       The caller decides whether 2/3 should fail its job; this script only warns.
#
# sendMessage ONLY. Never call getUpdates or set a webhook with this token: the Hermes
# gateway long-polls the same bot, and a second reader would steal its updates.
#
# This repo is public and its logs are world-readable, so the token must never reach
# stdout/stderr:
#   - The request URL (which contains the token) and the chat id are handed to curl
#     on stdin (-K -), not in argv, and are never printed.
#   - curl's own stderr and the response body are written to temp files and NOT
#     printed. On failure only the HTTP status (or curl exit code) is reported.
#   - No `set -x`.
# Plain text (no parse_mode), so nothing needs escaping and nothing can fail to parse.

set -euo pipefail
export LC_ALL=C.UTF-8

API_BASE="${TELEGRAM_API_BASE:-https://api.telegram.org}"   # overridden by tests only
MAX_CHARS=4000                                              # Telegram's limit is 4096

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
  echo "::warning::Telegram not configured (TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID empty) - alert not sent"
  exit 2
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

TEXT="$(cat)"
if [ -z "$TEXT" ]; then
  echo "::warning::notify-telegram: empty message, nothing sent"
  exit 3
fi
if [ "${#TEXT}" -gt "$MAX_CHARS" ]; then
  TEXT="${TEXT:0:$((MAX_CHARS - 20))}"$'\n[message truncated]'
fi
printf '%s' "$TEXT" > "$WORK/text"

rc=0
HTTP=$(printf 'url = "%s/bot%s/sendMessage"\ndata-urlencode = "chat_id=%s"\n' \
         "$API_BASE" "$TELEGRAM_BOT_TOKEN" "$TELEGRAM_CHAT_ID" \
  | curl -sS -K - -X POST --max-time 20 \
      --data-urlencode "text@$WORK/text" \
      --data-urlencode "disable_web_page_preview=true" \
      -o "$WORK/response" -w '%{http_code}' 2> "$WORK/curl.err") || rc=$?

if [ "$rc" -ne 0 ]; then
  echo "::warning::Telegram sendMessage failed (curl exit ${rc}, HTTP ${HTTP:-none})"
  exit 3
fi
if [ "$HTTP" != "200" ]; then
  echo "::warning::Telegram sendMessage failed (HTTP ${HTTP})"
  exit 3
fi
echo "Telegram message sent."
