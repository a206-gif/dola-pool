#!/bin/bash
# Dola Pool 自动清理脚本
# 每5小时执行一次

LOG_FILE="/opt/dola-pool/cleanup.log"
OUTPUT_DIR="/opt/dola-pool/output"
TMP_DIR="/tmp"

echo "========================================" >> $LOG_FILE
echo "清理开始: $(date '+%Y-%m-%d %H:%M:%S')" >> $LOG_FILE

# 1. 删除24小时前的视频文件
echo "--- 1. 清理24小时前的视频文件 ---" >> $LOG_FILE
BEFORE=$(ls $OUTPUT_DIR/*.mp4 2>/dev/null | wc -l)
find $OUTPUT_DIR -name "*.mp4" -type f -mtime +1 -delete 2>/dev/null
AFTER=$(ls $OUTPUT_DIR/*.mp4 2>/dev/null | wc -l)
DELETED=$((BEFORE - AFTER))
echo "删除视频文件: $DELETED 个 (清理前: $BEFORE, 清理后: $AFTER)" >> $LOG_FILE

# 2. 清理/tmp临时文件
echo "--- 2. 清理/tmp临时文件 ---" >> $LOG_FILE
TMP_BEFORE=$(du -sh $TMP_DIR 2>/dev/null | cut -f1)
find $TMP_DIR -type f -mtime +1 -delete 2>/dev/null
find $TMP_DIR -type d -empty -delete 2>/dev/null
TMP_AFTER=$(du -sh $TMP_DIR 2>/dev/null | cut -f1)
echo "/tmp目录: $TMP_BEFORE -> $TMP_AFTER" >> $LOG_FILE

# 3. 清理旧日志（系统日志保留7天）
echo "--- 3. 清理旧日志 ---" >> $LOG_FILE
LOG_BEFORE=$(du -sh /var/log 2>/dev/null | cut -f1)
find /var/log -type f -name "*.log" -mtime +7 -delete 2>/dev/null
find /var/log -type f -name "*.gz" -mtime +7 -delete 2>/dev/null
find /var/log -type f -name "*.[0-9]" -mtime +7 -delete 2>/dev/null
# 清理journal日志
journalctl --vacuum-time=7d 2>/dev/null
LOG_AFTER=$(du -sh /var/log 2>/dev/null | cut -f1)
echo "/var/log目录: $LOG_BEFORE -> $LOG_AFTER" >> $LOG_FILE

# 4. 清理apt缓存
echo "--- 4. 清理apt缓存 ---" >> $LOG_FILE
apt-get clean 2>/dev/null
echo "apt缓存已清理" >> $LOG_FILE

# 5. 显示当前磁盘使用情况
echo "--- 5. 当前磁盘使用情况 ---" >> $LOG_FILE
df -h / >> $LOG_FILE

echo "清理完成: $(date '+%Y-%m-%d %H:%M:%S')" >> $LOG_FILE
echo "========================================" >> $LOG_FILE
echo "" >> $LOG_FILE

# 只保留最近100行清理日志
tail -100 $LOG_FILE > $LOG_FILE.tmp
mv $LOG_FILE.tmp $LOG_FILE

echo "清理完成！"

