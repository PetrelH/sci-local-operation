#!/bin/bash
# Shell Agent 卸载脚本
# 用法：sudo bash uninstall.sh

set -e

echo "🗑  卸载 Shell Agent..."

# 停止并卸载服务
launchctl bootout system/com.shellagent 2>/dev/null \
  && echo "✓ 服务已停止" \
  || echo "  服务未在运行，跳过"

# 删除文件
FILES=(
  "/usr/local/bin/shellagent"
  "/Library/LaunchDaemons/com.shellagent.plist"
  "/usr/local/share/shellagent"
  "/var/log/shellagent.log"
  "/var/log/shellagent.err"
)

for f in "${FILES[@]}"; do
  if [ -e "$f" ]; then
    rm -rf "$f"
    echo "✓ 已删除 $f"
  fi
done

# 清除 pkgutil 记录
pkgutil --forget com.shellagent.agent 2>/dev/null \
  && echo "✓ 安装记录已清除" \
  || true

echo ""
echo "✅  Shell Agent 已完全卸载"
