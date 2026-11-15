#!/usr/bin/env bash
set -euo pipefail
: "${GITHUB_ENV:?Run this script from GitHub Actions}"

panel_pid=""
tunnel_pid=""
cleanup() {
  local status=$?
  if [ "$status" -ne 0 ]; then
    if [ -n "$tunnel_pid" ]; then kill "$tunnel_pid" 2>/dev/null || true; fi
    if [ -n "$panel_pid" ]; then kill "$panel_pid" 2>/dev/null || true; fi
  fi
}
trap cleanup EXIT

panel_token=$(python -c 'import secrets; print(secrets.token_urlsafe(18))')
export WHOISAUTO_ACCESS_TOKEN="$panel_token"
{
  echo "WHOISAUTO_ACCESS_TOKEN=$panel_token"
  echo "PANEL_TOKEN=$panel_token"
} >> "$GITHUB_ENV"

python -u -m whoisauto serve --port 8080 --no-browser > /tmp/panel.log 2>&1 &
panel_pid=$!
panel_port=""
for _ in $(seq 30); do
  if ! kill -0 "$panel_pid" 2>/dev/null; then break; fi
  panel_port=$(python - "$panel_pid" <<'PY' || true
import sys
from whoisauto.config import Paths, read_json
record = read_json(Paths.load().state / "panel.json")
if not isinstance(record, dict) or record.get("pid") != int(sys.argv[1]):
    raise SystemExit(1)
print(record["port"])
PY
)
  if [ -n "$panel_port" ]; then break; fi
  sleep 1
done
cat /tmp/panel.log 2>/dev/null || true
if [ -z "$panel_port" ]; then
  echo "panel did not start"
  exit 1
fi
echo "PANEL_PORT=$panel_port" >> "$GITHUB_ENV"

local_code=$(curl -s --max-time 30 -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $panel_token" "http://127.0.0.1:$panel_port/api/state" || true)
[ "$local_code" = "200" ] || { echo "panel is not answering locally (HTTP $local_code)"; exit 1; }
anon_code=$(curl -s --max-time 30 -o /dev/null -w "%{http_code}" "http://127.0.0.1:$panel_port/api/state" || true)
[ "$anon_code" = "401" ] || { echo "panel is exposed without auth (HTTP $anon_code)"; exit 1; }

if ! command -v cloudflared >/dev/null 2>&1; then brew install cloudflared; fi
cloudflared tunnel --url "http://127.0.0.1:$panel_port" --no-autoupdate > /tmp/cloudflared.log 2>&1 &
tunnel_pid=$!
panel_url=""
for _ in $(seq 60); do
  if ! kill -0 "$tunnel_pid" 2>/dev/null; then break; fi
  panel_url=$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/cloudflared.log | head -1 || true)
  if [ -n "$panel_url" ]; then break; fi
  sleep 2
done
if [ -z "$panel_url" ]; then
  echo "cloudflared did not publish a URL"
  tail -40 /tmp/cloudflared.log
  exit 1
fi
echo "PANEL_URL=$panel_url" >> "$GITHUB_ENV"
echo "::notice title=whoisauto panel::$panel_url/?token=$panel_token"
python - "$panel_url" "$panel_token" <<'PY'
import json, sys
from whoisauto.config import write_json
from pathlib import Path
url, token = sys.argv[1:3]
write_json(Path("panel-endpoint.json"), {
    "panel_url": url, "access_token": token, "open": f"{url}/?token={token}"
}, mode=0o600)
PY
