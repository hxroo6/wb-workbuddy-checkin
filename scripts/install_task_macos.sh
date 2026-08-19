#!/usr/bin/env bash
# 安装 WorkBuddy 每日签到定时任务（macOS launchd）
# 用法：./scripts/install_task_macos.sh [09:00]
set -euo pipefail

TIME="${1:-09:00}"
LABEL="com.wb.checkin.daily"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MAIN_PY="$PROJECT_DIR/main.py"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"

if [[ ! -f "$MAIN_PY" ]]; then
  echo "错误：找不到 $MAIN_PY" >&2
  exit 1
fi
if [[ -z "$PYTHON_BIN" ]]; then
  echo "错误：未找到 python3，请设置 PYTHON_BIN 环境变量" >&2
  exit 1
fi

HOUR="${TIME%%:*}"
MINUTE="${TIME##*:}"
if [[ -z "$HOUR" || -z "$MINUTE" ]]; then
  echo "错误：时间格式应为 HH:MM，例如 09:00" >&2
  exit 1
fi

DATA_DIR="$HOME/.wb_checkin"
mkdir -p "$DATA_DIR/logs" "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_BIN}</string>
        <string>${MAIN_PY}</string>
        <string>checkin</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>${HOUR}</integer>
        <key>Minute</key><integer>${MINUTE}</integer>
    </dict>
    <key>RunAtLoad</key><false/>
    <key>StandardOutPath</key><string>${DATA_DIR}/logs/launchd.out.log</string>
    <key>StandardErrorPath</key><string>${DATA_DIR}/logs/launchd.err.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"
echo "✔ 已注册 launchd 任务 ${LABEL}，每天 ${TIME} 执行"
echo "  查看：launchctl list | grep ${LABEL}"
echo "  卸载：./scripts/uninstall_task_macos.sh"
