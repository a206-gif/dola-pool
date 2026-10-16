#!/bin/sh
# 代理刷新脚本：向代理服务商申请新的出口 IP，写入 $DOLA_PROXY_ENV_FILE（默认 $DOLA_DATA_DIR/proxy.env），
# 然后可选重启服务让新代理生效。
#
# 用法（cron 示例，每 50 分钟刷新一次，与住宅 IP 粘性时长对齐）：
#   */50 * * * * DOLA_PROXY_REFRESH_URL='http://.../update?...' /opt/dola-pool/scripts/refresh-proxy.sh
set -eu

URL="${DOLA_PROXY_REFRESH_URL:-}"
DATA_DIR="${DOLA_DATA_DIR:-$(cd "$(dirname "$0")/.." && pwd)/data}"
OUT="${DOLA_PROXY_ENV_FILE:-$DATA_DIR/proxy.env}"

if [ -z "$URL" ]; then
  echo "未配置 DOLA_PROXY_REFRESH_URL，跳过刷新"
  exit 0
fi

RESP=$(curl --max-time 15 -fsS "$URL" || true)
PROXY=$(printf '%s' "$RESP" | tr -d '\r\n' | sed -n 's#^\(https\?://[^ ]*\)$#\1#p')

if [ -z "$PROXY" ]; then
  echo "代理服务返回内容无法解析：$RESP"
  exit 0
fi

mkdir -p "$(dirname "$OUT")"
printf 'DOLA_PROXY=%s\nHTTPS_PROXY=%s\nHTTP_PROXY=%s\n' "$PROXY" "$PROXY" "$PROXY" > "$OUT"
chmod 600 "$OUT"
echo "已写入 $OUT -> $PROXY"

# 可选：刷新后重启服务以加载新代理
if [ "${DOLA_PROXY_RESTART_SERVICE:-}" != "" ]; then
  systemctl restart "$DOLA_PROXY_RESTART_SERVICE" && echo "已重启 $DOLA_PROXY_RESTART_SERVICE"
fi
