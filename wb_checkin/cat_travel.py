"""猫猫旅行模块（WorkBuddy 成长中心活动，自动赚积分）。

旅行状态机（接口路径见下方常量，域名/路径请结合客户端行为自行研究）：
- 状态查询：state 取值 idle / traveling / arrived / finished
- 派出旅行：随机选地点（携带地点编号）
- 收取奖励：到点（可加随机延迟）后领取，奖励为积分

流程（自动状态机）：
- idle      → 随机选地点，派出旅行
- 已到点    → 随机延迟（0~N 分钟）→ 收取奖励 → 自动重新派出（未达每日上限时）
- 未到点    → 记录剩余时间，下次执行再收
"""
import json
import os
import random
import time
import zlib
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib import request as ur
from urllib.error import HTTPError, URLError

from .balance import query_total_balance, record_balance
from .config import ConfigManager
from .credentials import build_auth_headers, load_account_credential, validate_token
from .executor import CheckinExecutor
from .logger import append_history, get_logger, read_recent_history

BASE = "https://www.workbuddy.cn"
STATUS_PATH = "/activity/growth/buddy/travel/status"
CONFIG_PATH = "/activity/growth/buddy/travel/config"
DEPART_PATH = "/activity/growth/buddy/travel/depart"
CLAIM_PATH = "/activity/growth/buddy/travel/claim"


def _request(headers: Dict[str, str], path: str, method: str = "GET", body: Optional[dict] = None) -> tuple:
    """通用请求，返回 (status, text)。"""
    req = ur.Request(BASE + path, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode("utf-8")
    try:
        with ur.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        return e.code, (e.read().decode("utf-8", errors="replace") if e.fp else "")
    except URLError as e:
        return 0, "URLError: %s" % e


def _parse(text: str) -> Dict[str, Any]:
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {"code": -1, "msg": "非JSON"}
    except (ValueError, TypeError):
        return {"code": -1, "msg": text[:200]}


def get_status(headers: Dict[str, str]) -> Dict[str, Any]:
    status_code, text = _request(headers, STATUS_PATH)
    data = _parse(text)
    if status_code == 401:
        # 凭证被服务端拒绝（可能已在客户端重新登录换发新 token）
        return {"state": "unknown", "error": "凭证无效（HTTP 401），请重新 add-account 刷新凭证"}
    return data.get("data", {}) if data.get("code") in (0, 200) else {}


def get_locations(headers: Dict[str, str]) -> List[Dict[str, Any]]:
    _, text = _request(headers, CONFIG_PATH)
    data = _parse(text)
    return data.get("data", {}).get("locations", []) if data.get("code") in (0, 200) else []


def _daily_limit_reached_today(history_path: str, account_name: str) -> bool:
    """当天是否已触发"每日旅行上限"（避免后续任务重复请求）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    for r in read_recent_history(history_path, limit=500):
        if (r.get("ts") or "").startswith(today) and r.get("account") == account_name:
            if r.get("result") == "failed" and "daily limit" in str(r.get("detail", "")).lower():
                return True
    return False


def run(
    cm: ConfigManager,
    cfg: Dict[str, Any],
    only_account: Optional[str] = None,
    claim_delay_minutes: int = 30,
    auto_redepart: bool = True,
    mode: str = "full",
) -> List[Dict[str, Any]]:
    """对启用账号执行猫猫旅行状态机，返回汇总。

    mode: full=完整状态机（默认）| claim=仅收取已到达的旅行 | depart=仅派出空闲的猫猫
    """
    logger = get_logger()
    accounts = [a for a in cfg.get("accounts", []) if a.get("enabled", True)]
    if only_account:
        accounts = [a for a in accounts if a["name"] == only_account]

    summary: List[Dict[str, Any]] = []
    ts = datetime.now().isoformat(timespec="seconds")

    for acc in accounts:
        name = acc["name"]
        logger.info("===== 猫猫旅行：账号 %r =====", name)
        cred = load_account_credential(cm, acc)
        if not cred:
            logger.warning("  账号 %r 无凭证，跳过", name)
            summary.append({"ts": ts, "account": name, "action": "skip", "detail": "无凭证"})
            continue
        # token 有效期预检（JWT exp）
        tok_ok, tok_reason = validate_token(cred)
        if not tok_ok:
            logger.warning("  %s", tok_reason)
            summary.append(
                {"ts": ts, "account": name, "action": "skip", "result": "failed", "cred_invalid": True, "detail": tok_reason}
            )
            continue
        # 当日已达旅行上限 → 跳过（减少无效请求）
        if _daily_limit_reached_today(cm.history_path, name):
            logger.info("  %s 今日旅行已达上限，跳过（当日去重）", name)
            record = {
                "ts": ts, "account": name, "action": "skip-limit",
                "result": "skip", "detail": "今日旅行已达上限，跳过",
            }
            append_history(
                cm.history_path,
                {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "account": name,
                    "op": "cat-travel",
                    "label": "猫猫旅行",
                    "result": "skip",
                    "detail": "今日旅行已达上限，跳过",
                },
            )
            summary.append(record)
            continue

        executor = CheckinExecutor(api_base=BASE, timeout=20)
        executor.auth_headers = build_auth_headers(cred)
        # 每个账号固定一个常用 UA（与签到逻辑一致，配置可显式指定 ua）
        ua = (acc.get("ua") or "").strip()
        if not ua:
            pool = cfg.get("ua_pool") or []
            if pool:
                ua = pool[zlib.crc32((name or "").encode("utf-8")) % len(pool)]
        if ua:
            executor.auth_headers["User-Agent"] = ua
        headers = executor._build_headers()

        st = get_status(headers)
        state = st.get("state", "unknown")
        now = int(st.get("server_now") or 0)
        arrive_at = int(st.get("arrive_at") or 0)
        daily_limit = bool(st.get("daily_limit_reached"))
        record: Dict[str, Any] = {"ts": ts, "account": name, "state": state, "detail": ""}

        try:
            if state == "idle":
                if mode == "claim":
                    logger.info("  🐱 空闲中，无奖励可收取")
                    record.update({"action": "claim", "result": "skip", "detail": "猫猫空闲，无旅行奖励可收"})
                else:
                    # 随机选地点派出（像真人一样不定点）
                    locations = get_locations(headers)
                    loc_id = None
                    if locations:
                        loc_id = random.choice(locations).get("id")
                    loc_id = loc_id or 1
                    status, text = _request(headers, DEPART_PATH, method="POST", body={"location_id": loc_id})
                    resp = _parse(text)
                    if resp.get("code") in (0, 200):
                        d = resp.get("data", {})
                        arrive = int(d.get("arrive_at") or 0)
                        loc_name = (d.get("location") or {}).get("name", "")
                        arrive_txt = datetime.fromtimestamp(arrive).strftime("%H:%M") if arrive else "?"
                        logger.info("  ✅ 已派出旅行：%s（%s），预计 %s 到达", loc_name, d.get("duration_hours", "?"), arrive_txt)
                        record.update({"action": "depart", "result": "success", "detail": "已派出：%s，到达 %s" % (loc_name, arrive_txt)})
                    else:
                        msg = resp.get("msg", text[:100])
                        if "no active buddy" in str(msg).lower():
                            msg = "没有可旅行的猫猫（需先在成长中心用能量开启盲盒获得猫猫）"
                        logger.warning("  派出失败：%s", msg)
                        record.update({"action": "depart", "result": "failed", "detail": str(msg)})
            elif state in ("traveling", "finished", "arrived"):
                # arrived = 已到达可收取（8/14 实测出现的状态）
                if state in ("finished", "arrived") or (arrive_at and now >= arrive_at):
                    # 到点 → 随机延迟 → 收取
                    if claim_delay_minutes > 0:
                        delay = random.randint(0, claim_delay_minutes * 60)
                        if delay > 0:
                            logger.info("  旅行已到达，随机延迟 %d 秒后收取...", delay)
                            time.sleep(delay)
                    status, text = _request(headers, CLAIM_PATH, method="POST", body={})
                    resp = _parse(text)
                    if resp.get("code") in (0, 200):
                        d = resp.get("data", {})
                        reward = d.get("reward_credit") or d.get("credit") or "?"
                        logger.info("  ✅ 收取旅行奖励：%s 积分", reward)
                        record.update({"action": "claim", "result": "success", "detail": "收取奖励：%s 积分" % reward})
                        # 刷新积分
                        try:
                            bal = query_total_balance(executor.http_caller(), headers)
                            if bal is not None:
                                rec = record_balance(cm.balances_path, name, bal)
                                record["balance"] = rec["balance"]
                                record["delta"] = rec["delta"]
                                logger.info("  积分：当前 %d（较上次 %s）", rec["balance"], rec["delta"])
                        except Exception:
                            pass
                        # 未达每日上限且为完整模式 → 自动重新派出
                        if auto_redepart and mode == "full" and not daily_limit:
                            locations = get_locations(headers)
                            loc_id = random.choice(locations).get("id") if locations else 1
                            s2, t2 = _request(headers, DEPART_PATH, method="POST", body={"location_id": loc_id})
                            r2 = _parse(t2)
                            if r2.get("code") in (0, 200):
                                d2 = r2.get("data", {})
                                arrive2 = datetime.fromtimestamp(int(d2.get("arrive_at") or 0)).strftime("%H:%M") if d2.get("arrive_at") else "?"
                                logger.info("  🔁 已自动重新派出旅行，预计 %s 到达", arrive2)
                                record["detail"] += "；已重新派出（到达 %s）" % arrive2
                            else:
                                logger.info("  未重新派出（可能已达每日上限）：%s", r2.get("msg", ""))
                        elif daily_limit:
                            logger.info("  已达每日旅行上限，不再派出")
                    else:
                        msg = resp.get("msg", text[:100])
                        logger.warning("  收取失败：%s", msg)
                        record.update({"action": "claim", "result": "failed", "detail": str(msg)})
                else:
                    if mode == "depart":
                        logger.info("  🐱 已在旅行中，无需重复派出")
                        remain = arrive_at - now
                        hh, mm = remain // 3600, (remain % 3600) // 60
                        record.update({"action": "depart", "result": "skip", "detail": "已在旅行中，剩余 %dh%02dm" % (hh, mm)})
                    else:
                        remain = arrive_at - now
                        hh, mm = remain // 3600, (remain % 3600) // 60
                        logger.info("  旅行中，还需 %dh%02dm 到达（%s）", hh, mm, datetime.fromtimestamp(arrive_at).strftime("%H:%M"))
                        record.update({"action": "wait", "result": "waiting", "detail": "旅行中，剩余 %dh%02dm" % (hh, mm)})
            else:
                err = st.get("error") or ""
                if err:
                    logger.warning("  状态查询失败：%s", err)
                    record.update({"action": "unknown", "result": "failed", "detail": err})
                else:
                    logger.warning("  未知状态：%s", state)
                    record.update({"action": "unknown", "result": "failed", "detail": "未知状态 %s" % state})
        except Exception as e:
            logger.warning("  执行异常：%s: %s", type(e).__name__, e)
            record.update({"action": "error", "result": "failed", "detail": "%s: %s" % (type(e).__name__, e)})

        append_history(
            cm.history_path,
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "account": name,
                "op": "cat-travel",
                "label": "猫猫旅行",
                "result": record.get("result", "unknown"),
                "detail": record.get("detail", ""),
            },
        )
        summary.append(record)
        time.sleep(random.uniform(1.5, 3.5))  # 账号间随机间隔

    return summary


def build_summary_text(summary: List[Dict[str, Any]]) -> str:
    if not summary:
        return "本次猫猫旅行无任何账号被处理"
    lines = ["WorkBuddy 猫猫旅行结果", "——————————"]
    for s in summary:
        lines.append("%s：%s" % (s.get("account", "?"), s.get("detail", s.get("action", "?"))))
    lines.append("——————————")
    return "\n".join(lines)
