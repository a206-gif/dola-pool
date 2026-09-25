#!/bin/sh
set -eu
URL='http://us.udealproxy.com:7777/update?custom=17941&session=EeApl&password=alMLlU'
OUT=/opt/dola-pool/proxy.env
RESP=$(curl --max-time 15 -fsS "$URL")
PROXY=$(printf '%s' "$RESP" | tr -d '\r\n' | sed -n 's#^\(https\?://[^ ]*\)$#\1#p')
[ -n "$PROXY" ] || exit 0
printf 'HTTP_PROXY=%s\nHTTPS_PROXY=%s\n' "$PROXY" "$PROXY" >"$OUT"
chmod 600 "$OUT"
systemctl restart dola-pool
