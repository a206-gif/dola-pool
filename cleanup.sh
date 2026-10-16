#!/usr/bin/env bash
# Dola Pool 自动清理脚本（建议每 5 小时由 cron / systemd timer 执行一次）
#
# 只清理本项目自己的数据，不会动系统日志或 /tmp 里的其他文件：
#   1. 删除 N 小时前的成片
#   2. 删除 N 小时前的上传参考图
#   3. 压缩 SQLite 数据库并清理 30 天前的错误 / 审计日志
#   4. 打印磁盘占用
#
# 用法：
#   DOLA_DATA_DIR=/opt/dola-pool/data VIDEO_KEEP_HOURS=24 ./cleanup.sh
set -euo pipefail

DATA_DIR="${DOLA_DATA_DIR:-$(cd "$(dirname "$0")" && pwd)/data}"
OUTPUT_DIR="${DOLA_OUTPUT_DIR:-$DATA_DIR/output}"
UPLOAD_DIR="${DOLA_UPLOAD_DIR:-$DATA_DIR/uploads}"
DB="${DOLA_DB:-$DATA_DIR/dola.db}"
KEEP_HOURS="${VIDEO_KEEP_HOURS:-24}"
LOG_FILE="${DOLA_CLEANUP_LOG:-$DATA_DIR/cleanup.log}"

mkdir -p "$DATA_DIR"
{
  echo "========================================"
  echo "清理开始: $(date '+%Y-%m-%d %H:%M:%S')  (保留 ${KEEP_HOURS} 小时内文件)"

  if [ -d "$OUTPUT_DIR" ]; then
    before=$(find "$OUTPUT_DIR" -type f -name '*.mp4' | wc -l)
    find "$OUTPUT_DIR" -type f -name '*.mp4' -mmin "+$((KEEP_HOURS * 60))" -delete
    after=$(find "$OUTPUT_DIR" -type f -name '*.mp4' | wc -l)
    echo "成片: 删除 $((before - after)) 个（剩余 $after 个）"
  fi

  if [ -d "$UPLOAD_DIR" ]; then
    before=$(find "$UPLOAD_DIR" -type f | wc -l)
    find "$UPLOAD_DIR" -type f -mmin "+$((KEEP_HOURS * 60))" -delete
    after=$(find "$UPLOAD_DIR" -type f | wc -l)
    echo "上传参考图: 删除 $((before - after)) 个（剩余 $after 个）"
  fi

  if [ -f "$DB" ] && command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB" "delete from errors where created_at < datetime('now','-30 day');" 2>/dev/null || true
    sqlite3 "$DB" "delete from audits where created_at < datetime('now','-30 day');" 2>/dev/null || true
    sqlite3 "$DB" "vacuum;" 2>/dev/null || true
    echo "数据库: 已清理 30 天前日志并压缩"
  fi

  echo "--- 磁盘占用 ---"
  df -h "$DATA_DIR" | tail -1
  du -sh "$DATA_DIR" 2>/dev/null || true
  echo "清理完成: $(date '+%Y-%m-%d %H:%M:%S')"
  echo ""
} >> "$LOG_FILE"

# 日志只保留最近 200 行
tail -200 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
echo "清理完成，日志：$LOG_FILE"
