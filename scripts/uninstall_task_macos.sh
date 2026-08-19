#!/usr/bin/env bash
# 卸载 WorkBuddy 每日签到定时任务（macOS launchd）
set -euo pipefail

LABEL="com.wb.checkin.daily"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

if [[ ! -f "$PLIST" ]]; then
  echo "任务不存在，无需卸载。"
  exit 0
fi

launchctl unload "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "✔ 已卸载 launchd 任务 ${LABEL}"
