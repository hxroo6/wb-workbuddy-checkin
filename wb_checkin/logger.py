"""日志模块：控制台 + 每日日志文件 + JSONL 历史记录。"""
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

_LOG_DIR = None
_LOGGER = None


def setup_logging(log_dir: str) -> logging.Logger:
    """初始化日志：控制台输出 + logs/YYYY-MM-DD.log 文件。"""
    global _LOG_DIR, _LOGGER
    _LOG_DIR = log_dir
    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger("wb_checkin")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    log_file = os.path.join(log_dir, datetime.now().strftime("%Y-%m-%d") + ".log")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    _LOGGER = logger
    return logger


def get_logger() -> logging.Logger:
    if _LOGGER is None:
        return setup_logging(os.path.join(os.path.expanduser("~"), ".wb_checkin", "logs"))
    return _LOGGER


def append_history(history_path: str, record: Dict[str, Any]) -> None:
    """把一条执行记录追加到 history.jsonl（不覆盖，只追加）。"""
    try:
        with open(history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        get_logger().warning("写入历史记录失败：%s", e)


def read_recent_history(history_path: str, limit: int = 20) -> List[Dict[str, Any]]:
    if not os.path.exists(history_path):
        return []
    records: List[Dict[str, Any]] = []
    try:
        with open(history_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return records[-limit:]


def list_log_files(log_dir: str, pattern: str = "*.log") -> List[str]:
    """列出日志目录下的日志文件（按修改时间倒序）。"""
    if not os.path.isdir(log_dir):
        return []
    import glob

    files = glob.glob(os.path.join(log_dir, pattern))
    files.sort(key=os.path.getmtime, reverse=True)
    return files


def read_tail(path: str, lines: int = 50) -> str:
    """读取文件末尾 N 行（大日志文件时避免整读）。"""
    if not os.path.exists(path):
        return "(文件不存在: %s)" % path
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-lines:]
        return "".join(tail).rstrip()
    except OSError as e:
        return "(读取失败: %s)" % e


def append_summary_report(log_dir: str, summary_text: str) -> str:
    """把每次执行的可读汇总追加到 logs/summary-YYYY-MM-DD.log，返回文件路径。"""
    os.makedirs(log_dir, exist_ok=True)
    report_path = os.path.join(log_dir, "summary-" + datetime.now().strftime("%Y-%m-%d") + ".log")
    try:
        with open(report_path, "a", encoding="utf-8") as f:
            f.write("=" * 50 + "\n")
            f.write("[%s] 执行汇总\n" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            f.write(summary_text + "\n")
        return report_path
    except OSError as e:
        get_logger().warning("写入汇总报告失败：%s", e)
        return ""
