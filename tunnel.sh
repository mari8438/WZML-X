#!/bin/sh
set -eu

URL_FILE="${TUNNEL_URL_FILE:-/data/tunnel_url.txt}"
mkdir -p "$(dirname "$URL_FILE")"
touch "$URL_FILE"

cleanup() {
  if [ -n "${CFPID:-}" ]; then
    kill -9 "$CFPID" 2>/dev/null || true
  fi
  rm -f /tmp/tunnel_log
  exit 0
}
trap cleanup INT TERM HUP

while true; do
  rm -f /tmp/tunnel_log
  mkfifo /tmp/tunnel_log || true

  cloudflared tunnel --protocol http2 --no-autoupdate --url "${1:-http://127.0.0.1:8080}" > /tmp/tunnel_log 2>&1 &
  CFPID=$!

  while IFS= read -r line; do
    printf '%s\n' "$line" >&2
    url=$(printf '%s' "$line" | grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' | head -1 || true)
    if [ -n "$url" ]; then
      current=$(cat "$URL_FILE" 2>/dev/null || true)
      if [ "$url" != "$current" ]; then
        printf '%s' "$url" > "$URL_FILE"
        printf 'tunnel_url: %s\n' "$url" >&2
      fi
    fi
  done < /tmp/tunnel_log

  if [ -n "${CFPID:-}" ]; then
    kill -9 "$CFPID" 2>/dev/null || true
  fi
  sleep 3
done
