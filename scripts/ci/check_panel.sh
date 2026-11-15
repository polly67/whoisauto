#!/usr/bin/env bash
set -euo pipefail
: "${PANEL_URL:?Panel URL is required}"
: "${PANEL_TOKEN:?Panel token is required}"

# A runner cannot always reach its own quick tunnel (egress or DNS),
# so this is a warning: the URL still works from outside.
code=""
for _ in $(seq 6); do
  code=$(curl -s --max-time 20 -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer $PANEL_TOKEN" "$PANEL_URL/api/state" || true)
  echo "public /api/state -> $code"
  if [ "$code" = "200" ]; then break; fi
  sleep 3
done
if [ "$code" != "200" ]; then
  echo "::warning::runner could not reach $PANEL_URL; try it from your machine"
  tail -40 /tmp/cloudflared.log || true
fi
