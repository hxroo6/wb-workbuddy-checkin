"""定时任务注册模块。

- Windows：schtasks 注册每日任务（或使用 scripts/install_task_windows.ps1）
- macOS：launchd LaunchAgent plist
- Linux：crontab
"""
import os
import shutil
import subprocess
import sys
from typing import List, Optional, Tuple

from .logger import get_logger

TASK_NAME = "WBCheckinDaily"
MAC_LABEL = "com.wb.checkin.daily"
MAC_PLIST = os.path.join(
    os.path.expanduser("~"), "Library", "LaunchAgents", MAC_LABEL + ".plist"
)


def install_schedule(
    data_dir: str,
    main_path: str,
    time_str: str = "09:00",
    python_path: Optional[str] = None,
    random_delay: int = 0,
    mode: str = "checkin",
    task_name: str = "WBCheckinDaily",
) -> Tuple[bool, str]:
    """安装每日定时任务，返回 (是否成功, 说明/错误信息)。

    random_delay: 任务启动后随机延迟 0~N 分钟再签到（错峰执行，避免每天准点触发）。
    mode: checkin=每日签到 | travel=猫猫旅行（可注册多个不同时间的旅行任务）
    task_name: 任务名（macOS 会映射为 com.wb.<task_name>.daily）
    """
    logger = get_logger()
    if not _valid_time(time_str):
        return False, "时间格式无效，应为 HH:MM（如 09:00）"
    if random_delay < 0:
        return False, "random_delay 不能为负数"
    if mode not in ("checkin", "travel"):
        return False, "mode 无效：%s" % mode

    python_path = python_path or sys.executable
    if not os.path.exists(python_path):
        return False, "Python 解释器不存在：%s" % python_path
    if not os.path.exists(main_path):
        return False, "main.py 不存在：%s" % main_path

    if sys.platform.startswith("win"):
        return _install_windows(data_dir, main_path, python_path, time_str, logger, random_delay, mode, task_name)
    if sys.platform == "darwin":
        return _install_macos(data_dir, main_path, python_path, time_str, logger, random_delay, mode, task_name)
    if sys.platform.startswith("linux"):
        return _install_linux(data_dir, main_path, python_path, time_str, logger, random_delay, mode, task_name)
    return False, "不支持的平台：%s" % sys.platform


def uninstall_schedule(task_name: str = "WBCheckinDaily") -> Tuple[bool, str]:
    """卸载定时任务。"""
    logger = get_logger()
    if sys.platform.startswith("win"):
        return _uninstall_windows(logger, task_name)
    if sys.platform == "darwin":
        return _uninstall_macos(logger, task_name)
    if sys.platform.startswith("linux"):
        return _uninstall_linux(logger)
    return False, "不支持的平台：%s" % sys.platform


def task_status() -> str:
    """查询定时任务是否已安装（尽力而为，失败返回未知）。"""
    try:
        if sys.platform.startswith("win"):
            r = subprocess.run(
                ["schtasks", "/Query", "/TN", TASK_NAME],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=15,
            )
            if r.returncode == 0 and "ERROR" not in r.stdout.upper():
                return "已安装 (Windows 任务计划程序: %s)" % TASK_NAME
            return "未安装"
        if sys.platform == "darwin":
            if os.path.exists(MAC_PLIST):
                return "已安装 (launchd: %s)" % MAC_LABEL
            return "未安装"
        if sys.platform.startswith("linux"):
            r = subprocess.run(
                ["crontab", "-l"], capture_output=True, text=True, errors="replace", timeout=15
            )
            if "wb_checkin" in (r.stdout or ""):
                return "已安装 (crontab)"
            return "未安装"
    except Exception as e:
        return "查询失败：%s" % e
    return "未知"


def _valid_time(t: str) -> bool:
    try:
        hh, mm = t.split(":")
        hh, mm = int(hh), int(mm)
        return 0 <= hh <= 23 and 0 <= mm <= 59
    except (ValueError, AttributeError):
        return False


# ---------------- Windows ----------------
def _build_command(main_path, data_dir, mode, random_delay=0):
    """构造定时任务执行命令。"""
    if mode == "travel":
        cmd = "{} --data-dir {} cat-travel".format(_quote(main_path), _quote(data_dir))
        if random_delay > 0:
            cmd += " --claim-delay-minutes %d" % random_delay
        else:
            cmd += " --claim-delay-minutes 30"
    else:
        cmd = "{} --data-dir {} checkin".format(_quote(main_path), _quote(data_dir))
        if random_delay > 0:
            cmd += " --delay-max-minutes %d" % random_delay
    return cmd


def _install_windows(data_dir, main_path, python_path, time_str, logger, random_delay=0, mode="checkin", task_name=TASK_NAME):
    cmd = "{} {}".format(_quote(python_path), _build_command(main_path, data_dir, mode, random_delay))
    args = [
        "schtasks", "/Create", "/F",
        "/TN", task_name,
        "/TR", cmd,
        "/SC", "DAILY",
        "/ST", time_str,
    ]
    logger.info("注册任务：schtasks /Create /TN %s /TR %s /SC DAILY /ST %s", task_name, cmd, time_str)
    r = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=30)
    if r.returncode == 0:
        return True, "已注册：任务计划程序中的 %s，每天 %s 执行（%s）" % (task_name, time_str, mode)
    # schtasks 中文系统错误信息是 GBK，已在 errors="replace" 处理
    return False, "schtasks 失败：%s" % (r.stdout.strip() or r.stderr.strip())


def _uninstall_windows(logger, task_name=TASK_NAME):
    args = ["schtasks", "/Delete", "/F", "/TN", task_name]
    r = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=30)
    if r.returncode == 0 or "不存在" in (r.stdout or "") or "not found" in (r.stdout or "").lower():
        return True, "已删除定时任务 %s" % task_name
    return False, "删除失败：%s" % (r.stdout.strip() or r.stderr.strip())


# ---------------- macOS (launchd) ----------------
def _install_macos(data_dir, main_path, python_path, time_str, logger, random_delay=0, mode="checkin", task_name=TASK_NAME):
    hh, mm = time_str.split(":")
    label = "com.wb." + task_name.lower().replace("_", "") + ".daily"
    plist_path = os.path.join(
        os.path.expanduser("~"), "Library", "LaunchAgents", label + ".plist"
    )
    args_array = [python_path, main_path, "--data-dir", data_dir, "cat-travel" if mode == "travel" else "checkin"]
    if mode == "travel":
        args_array += ["--claim-delay-minutes", str(random_delay or 30)]
    elif random_delay > 0:
        args_array += ["--delay-max-minutes", str(random_delay)]
    args_xml = "\n".join("        <string>%s</string>" % a for a in args_array)
    plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{args}
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>{hour}</integer>
        <key>Minute</key><integer>{minute}</integer>
    </dict>
    <key>RunAtLoad</key><false/>
    <key>StandardOutPath</key><string>{out}</string>
    <key>StandardErrorPath</key><string>{err}</string>
</dict>
</plist>
""".format(
        label=label,
        args=args_xml,
        hour=int(hh),
        minute=int(mm),
        out=os.path.join(data_dir, "logs", "launchd.out.log"),
        err=os.path.join(data_dir, "logs", "launchd.err.log"),
    )
    os.makedirs(os.path.dirname(plist_path), exist_ok=True)
    with open(plist_path, "w", encoding="utf-8") as f:
        f.write(plist)
    r = subprocess.run(
        ["launchctl", "load", "-w", plist_path],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode == 0:
        return True, "已注册 launchd 任务 %s（每天 %s，%s）" % (label, time_str, mode)
    return False, "launchctl load 失败：%s" % (r.stderr.strip() or r.stdout.strip())


def _uninstall_macos(logger, task_name=TASK_NAME):
    label = "com.wb." + task_name.lower().replace("_", "") + ".daily"
    plist_path = os.path.join(
        os.path.expanduser("~"), "Library", "LaunchAgents", label + ".plist"
    )
    if not os.path.exists(plist_path):
        return True, "任务不存在（无需卸载）"
    subprocess.run(["launchctl", "unload", plist_path], capture_output=True, timeout=30)
    try:
        os.remove(plist_path)
    except OSError:
        pass
    return True, "已卸载 launchd 任务 %s" % label


# ---------------- Linux (crontab) ----------------
def _install_linux(data_dir, main_path, python_path, time_str, logger, random_delay=0, mode="checkin", task_name=TASK_NAME):
    hh, mm = time_str.split(":")
    tag = "cat-travel" if mode == "travel" else "checkin"
    extra = ""
    if mode == "travel":
        extra = " --claim-delay-minutes %d" % (random_delay or 30)
    elif random_delay > 0:
        extra = " --delay-max-minutes %d" % random_delay
    marker = "wb_%s" % task_name.lower()
    line = "%s %s * * * %s %s --data-dir %s %s%s >> %s 2>&1 # %s" % (
        mm, hh, _quote(python_path), _quote(main_path), _quote(data_dir), tag, extra,
        os.path.join(data_dir, "logs", "cron.log"), marker,
    )
    cur = ""
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=15)
    if r.returncode == 0:
        cur = r.stdout or ""
    if marker in cur:
        return True, "crontab 已存在该任务（%s），跳过（如需改时间请先 uninstall-schedule）" % marker
    new_cron = (cur.rstrip() + "\n" + line + "\n") if cur.strip() else (line + "\n")
    p = subprocess.run(["crontab", "-"], input=new_cron, text=True, capture_output=True, timeout=15)
    if p.returncode == 0:
        return True, "已注册 crontab 任务（每天 %s，%s）" % (time_str, mode)
    return False, "crontab 写入失败：%s" % (p.stderr.strip() or "")


def _uninstall_linux(logger):
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        return True, "无 crontab（无需卸载）"
    lines = [ln for ln in (r.stdout or "").splitlines() if "wb_checkin" not in ln]
    new_cron = "\n".join(lines) + ("\n" if lines else "")
    p = subprocess.run(["crontab", "-"], input=new_cron, text=True, capture_output=True, timeout=15)
    if p.returncode == 0:
        return True, "已从 crontab 移除任务"
    return False, "crontab 更新失败：%s" % (p.stderr.strip() or "")


def _quote(path: str) -> str:
    return '"%s"' % path.replace('"', '\\"')
