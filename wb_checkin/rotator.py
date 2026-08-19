"""多账号轮换执行模块：遍历启用账号 → 逐个加载凭证 → 签到 → 汇总。

两种执行模式（按账号配置自动选择）：
- token 模式（推荐）：读取客户端凭证文件（accessToken），urllib 直连接口，零浏览器依赖；
- 登录态模式（备选）：Playwright 加载 storage_state，用 context.request 调用（携带 Cookie）。

多账号凭证准备：登录账号 A → 复制凭证文件到 tokens/A.info → 登录账号 B → 复制 B.info ...
（本机同时只有一个 WorkBuddy 客户端登录态，需逐个账号备份凭证）
"""
import os
import random
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import ConfigManager
from .credentials import (
    build_auth_headers,
    load_account_credential,
    read_auth_file,
    validate_token,
)
from .executor import CheckinExecutor
from .logger import append_history, get_logger, read_recent_history
from .balance import query_total_balance, record_balance


def _done_today(history_path: str, account_name: str) -> bool:
    """判断账号今天是否已成功签到/已领取（用于当日去重）。

    仅统计 daily-checkin 操作，避免把猫猫旅行等其它操作的成功记录误判为已签到。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    for r in read_recent_history(history_path, limit=500):
        if (r.get("ts") or "").startswith(today) and r.get("account") == account_name:
            if r.get("op") == "daily-checkin" and r.get("result") in ("success", "already"):
                return True
    return False


def run_all(
    cfg_manager: ConfigManager,
    cfg: Dict[str, Any],
    only_account: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """按顺序为每个启用账号执行签到，返回汇总结果。"""
    logger = get_logger()

    accounts = [a for a in cfg.get("accounts", []) if a.get("enabled", True)]
    if only_account:
        accounts = [a for a in accounts if a["name"] == only_account]
        if not accounts:
            logger.error("未找到启用的账号 %r", only_account)
            return []

    if not accounts:
        logger.warning("没有可用的启用账号，请先 add-account 添加账号。")
        return []

    api_base = cfg.get("api_base", "")
    if not api_base:
        logger.error("api_base 未配置，请先编辑配置文件。")
        return []

    executor = CheckinExecutor(
        api_base=api_base,
        timeout=cfg.get("request_timeout", 30),
        retry_times=cfg.get("retry_times", 3),
        extra_headers=cfg.get("extra_headers") or {},
    )

    # 是否有账号走 Playwright 登录态模式（需要浏览器）
    need_browser = any(
        not _account_has_credential(cfg_manager, acc) and _account_has_storage(cfg_manager, acc)
        for acc in accounts
    )

    browser = None
    playwright_ctx = None
    if need_browser:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error("需要 Playwright 处理登录态账号，但未安装。pip install -r requirements.txt && playwright install chromium")
            need_browser = False
        if need_browser:
            p = sync_playwright().start()
            browser = p.chromium.launch(headless=True)

    # 账号间随机间隔（秒范围，避免固定节奏被风控识别）
    interval_range = cfg.get("account_interval") or [2, 5]
    if isinstance(interval_range, (int, float)):
        interval_range = [float(interval_range), float(interval_range)]
    # UA 池（每个账号随机取一个，避免多账号相同 UA 指纹）
    ua_pool = cfg.get("ua_pool") or []

    summary: List[Dict[str, Any]] = []
    ts = datetime.now().isoformat(timespec="seconds")

    for idx, acc in enumerate(accounts):
        if idx > 0:
            sleep_sec = random.uniform(interval_range[0], interval_range[1])
            logger.info("账号间间隔 %.1f 秒...", sleep_sec)
            time.sleep(sleep_sec)

        name = acc["name"]
        logger.info("===== 开始处理账号 %r =====", name)
        results: List[Dict[str, Any]] = []
        balance_rec = None

        # 当日去重：当天已成功签到/已领取的账号，本次直接跳过（避免重复请求）
        if cfg.get("skip_if_done_today", True) and _done_today(cfg_manager.history_path, name):
            logger.info("  %s 今日已完成签到，跳过（当日去重）", name)
            results = [
                {
                    "op": "daily-checkin",
                    "label": "每日签到/今日礼包",
                    "result": "skip",
                    "detail": "今日已完成签到/领取，跳过（当日去重）",
                    "attempts": 1,
                }
            ]
            # 跳过时仍刷新积分，保持界面数据新鲜
            cred = load_account_credential(cfg_manager, acc)
            if cred:
                executor.auth_headers = build_auth_headers(cred)
                caller = executor.http_caller()
                b = query_balance(caller, executor)
                if b is not None:
                    balance_rec = _make_balance_record(cfg_manager, name, None, b)
            append_history(
                cfg_manager.history_path,
                {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "account": name,
                    "op": "daily-checkin",
                    "label": "每日签到/今日礼包",
                    "result": "skip",
                    "detail": "今日已完成签到/领取，跳过（当日去重）",
                },
            )
            summary.append(
                {"ts": ts, "account": name, "results": results, "balance": balance_rec}
                if balance_rec
                else {"ts": ts, "account": name, "results": results}
            )
            continue

        # 模式一：token 凭证
        cred = load_account_credential(cfg_manager, acc)
        if cred:
            # token 有效期预检（JWT exp，借鉴 codeLong1024/workbuddy-checkin）
            ok, reason = validate_token(cred)
            if not ok:
                logger.warning("  %s", reason)
                results = [
                    {
                        "op": "daily-checkin",
                        "label": "每日签到/今日礼包",
                        "result": "failed",
                        "detail": reason,
                        "attempts": 1,
                        "cred_invalid": True,
                    }
                ]
            else:
                auth_headers = build_auth_headers(cred)
                if ua_pool:
                    auth_headers["User-Agent"] = random.choice(ua_pool)
                if cred.get("expires_at"):
                    logger.info("  凭证有效期至 %s", cred["expires_at"])
                executor.auth_headers = auth_headers
                caller = executor.http_caller()

                # 签到前查询积分（用于计算本次获得）
                b_before = query_balance(caller, executor)
                results = _safe_run(executor, caller, acc)
                # 签到后查询积分并记录（含与上次的增量）
                b_after = query_balance(caller, executor)
                balance_rec = _make_balance_record(cfg_manager, name, b_before, b_after)

        # 模式二：Playwright 登录态
        elif _account_has_storage(cfg_manager, acc):
            storage_path = cfg_manager.storage_path(acc["storage_file"])
            if browser is not None:
                context = browser.new_context(storage_state=storage_path)
                try:
                    # context 模式：caller 直接接收完整 URL
                    api = api_base.rstrip("/")
                    def caller(path: str, headers: Dict[str, str]) -> Tuple[int, str]:
                        from urllib.parse import urljoin
                        full = api + path
                        return CheckinExecutor.context_caller(context, executor.timeout * 1000)(full, headers)

                    results = _safe_run(executor, caller, acc)
                finally:
                    context.close()
            else:
                results = [
                    {
                        "op": "unknown", "label": "整体执行", "result": "failed",
                        "detail": "登录态模式需要 Playwright 但未初始化", "attempts": 1,
                    }
                ]
        else:
            results = [
                {
                    "op": "unknown", "label": "整体执行", "result": "failed",
                    "detail": "该账号既无 token 凭证，也无登录态文件（请 add-account 或配置 credential_file）",
                    "attempts": 1,
                }
            ]

        acc_record = {"ts": ts, "account": name, "results": results}
        if balance_rec:
            acc_record["balance"] = balance_rec
        summary.append(acc_record)
        for r in results:
            append_history(
                cfg_manager.history_path,
                {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "account": name,
                    "op": r["op"],
                    "label": r["label"],
                    "result": r["result"],
                    "detail": r["detail"],
                },
            )

    if browser is not None:
        browser.close()
        playwright_ctx = None

    _log_summary(summary)
    return summary


def _account_has_credential(cm: ConfigManager, acc: Dict[str, Any]) -> bool:
    """判断账号是否有可用凭证（token 直连模式）。"""
    return load_account_credential(cm, acc) is not None


def _account_has_storage(cm: ConfigManager, acc: Dict[str, Any]) -> bool:
    storage_file = acc.get("storage_file") or ""
    if not storage_file:
        return False
    return os.path.exists(cm.storage_path(storage_file))


def _safe_run(executor: CheckinExecutor, caller, acc: Dict[str, Any]) -> List[Dict[str, Any]]:
    logger = get_logger()
    try:
        return executor.run_all(caller, acc)
    except Exception as e:
        logger.error("账号 %r 执行异常：%s: %s", acc.get("name"), type(e).__name__, e)
        return [
            {
                "op": "unknown",
                "label": "整体执行",
                "result": "failed",
                "detail": "%s: %s" % (type(e).__name__, e),
                "attempts": 1,
            }
        ]


def query_balance(caller, executor: CheckinExecutor) -> Optional[int]:
    """查询账号当前总积分，失败返回 None（不中断签到）。"""
    try:
        return query_total_balance(caller, executor._build_headers())
    except Exception:
        return None


def _make_balance_record(cm: ConfigManager, name: str, b_before, b_after) -> Optional[Dict[str, Any]]:
    """生成积分记录（含与上次的增量），写入 balances.json。"""
    logger = get_logger()
    if b_after is None:
        return None
    rec = record_balance(cm.balances_path, name, b_after)
    gained = (b_after - b_before) if (b_before is not None and b_after is not None) else None
    rec["gained"] = gained
    if rec.get("prev") is not None:
        logger.info(
            "  积分：当前 %d | 本次获得 %s | 较上次 %s%d",
            rec["balance"],
            ("+%d" % gained) if gained is not None else "未知",
            "+" if rec["delta"] is not None and rec["delta"] >= 0 else "",
            rec["delta"] if rec["delta"] is not None else "?",
        )
    else:
        logger.info("  积分：当前 %d（首次记录）", rec["balance"])
    return rec


def _log_summary(summary: List[Dict[str, Any]]) -> None:
    logger = get_logger()
    if not summary:
        logger.warning("本次执行无任何账号被处理。")
        return
    total = len(summary)
    ok = sum(1 for s in summary if s["results"] and all(r["result"] == "success" for r in s["results"]))
    already = sum(
        1
        for s in summary
        if s["results"]
        and all(r["result"] in ("success", "already", "skip") for r in s["results"])
        and not all(r["result"] == "success" for r in s["results"])
    )
    failed = sum(1 for s in summary if not s["results"] or any(r["result"] == "failed" for r in s["results"]))
    logger.info(
        "===== 汇总：共 %d 个账号 | 全部成功 %d | 已领取/已完成 %d | 失败 %d =====",
        total, ok, already, failed,
    )
    for s in summary:
        line = "  [%s] " % s["account"]
        line += " | ".join("%s:%s" % (r["label"], r["result"]) for r in s["results"])
        b = s.get("balance")
        if b and b.get("balance") is not None:
            line += " | 积分:%d" % b["balance"]
            if b.get("gained") is not None:
                line += "(本次%+d)" % b["gained"]
            elif b.get("delta") is not None:
                line += "(较上次%+d)" % b["delta"]
        logger.info(line)


def build_summary_text(summary: List[Dict[str, Any]]) -> str:
    """生成用于通知的纯文本摘要（含积分与增量）。"""
    if not summary:
        return "本次执行无任何账号被处理"
    lines = ["WorkBuddy 每日签到结果", "——————————"]
    for s in summary:
        parts = ["%s：" % s["account"]]
        for r in s["results"]:
            parts.append("%s=%s" % (r["label"], r["result"]))
        b = s.get("balance")
        if b and b.get("balance") is not None:
            bal = "积分 %d" % b["balance"]
            if b.get("gained") is not None:
                bal += "（本次 %+d）" % b["gained"]
            elif b.get("delta") is not None:
                bal += "（较上次 %+d）" % b["delta"]
            parts.append(bal)
        lines.append("  ".join(parts))
    total = len(summary)
    failed = sum(1 for s in summary if not s["results"] or any(r["result"] == "failed" for r in s["results"]))
    lines.append("——————————")
    lines.append("共 %d 个账号，失败 %d 个" % (total, failed))
    return "\n".join(lines)