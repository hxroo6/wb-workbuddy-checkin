"""积分余额模块：查询账号总积分（剩余可用），记录历史用于增量汇报。

通过计费接口获取各积分账户（CapacityUnit=credits），字段语义：
  - CapacitySizePrecise   账户总额
  - CapacityRemainPrecise 剩余可用
  - CapacityUsedPrecise   已用
  - Status                0=有效 3=过期
总积分 = 所有剩余>0 账户的 CapacityRemainPrecise 之和（过期账户剩余恒为 0，自然排除）。

接口路径与域名请结合客户端行为自行研究，本模块不承担接口探测说明。
"""
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from .logger import get_logger

BALANCE_PATH = "get-user-resource"


def _to_int(v: Any) -> int:
    """积分字段可能是整数或浮点字符串（如 '91.0800002'），统一转 int 取整。"""
    if v is None:
        return 0
    try:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return 0
            if "." in v:
                return int(float(v))
            return int(v)
        return int(v)
    except (TypeError, ValueError):
        return 0


def query_total_balance(caller, headers: Dict[str, str]) -> Optional[int]:
    """查询账号总积分（剩余 credits），失败返回 None。"""
    logger = get_logger()
    try:
        status, text = caller("/billing/meter/" + BALANCE_PATH, headers)
        if not (200 <= status < 300):
            logger.warning("积分查询失败（HTTP %d）", status)
            return None
        data = json.loads(text)
        accounts = (
            data.get("data", {})
            .get("Response", {})
            .get("Data", {})
            .get("Accounts", [])
        )
        total = 0
        for acc in accounts:
            remain = _to_int(
                acc.get("CapacityRemainPrecise") or acc.get("CapacityRemain")
            )
            if remain > 0:
                total += remain
        return total
    except Exception as e:
        logger.warning("积分查询异常：%s: %s", type(e).__name__, e)
        return None


def load_balances(path: str) -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_balances(path: str, balances: Dict[str, Dict[str, Any]]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(balances, f, ensure_ascii=False, indent=2)
    except OSError as e:
        get_logger().warning("写入积分记录失败：%s", e)


def record_balance(path: str, account: str, balance: int) -> Dict[str, Any]:
    """记录当前积分，返回包含增量的记录。

    返回: {"balance": 当前总积分, "prev": 上次总积分, "delta": 与上次差值}
    """
    balances = load_balances(path)
    prev = balances.get(account, {}).get("balance")
    record = {
        "balance": balance,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    balances[account] = record
    save_balances(path, balances)
    delta = None
    if prev is not None:
        delta = balance - prev
    return {"balance": balance, "prev": prev, "delta": delta}
