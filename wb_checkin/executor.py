"""签到执行模块：调用签到接口并如实解析响应。

接口路径与域名请结合客户端行为自行研究，本模块不承担接口探测说明。

响应判断原则（绝不编造结果）：
- code=0 / 200 → success
- code=10001 或文案含"已签到/请明天再来"等 → already（已领取，幂等结果）
- 其余一律 failed，保留原始响应文本
"""
import json
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .logger import get_logger

# 操作定义：标识、接口路径、中文名（v2 前缀为实测确认）
CHECKIN_STATUS_PATH = "/v2/billing/meter/checkin-status"
OPERATIONS: List[Tuple[str, str, str]] = [
    ("daily-checkin", "/v2/billing/meter/daily-checkin", "每日签到/今日礼包"),
]

# 已领取判定：业务码或关键词
ALREADY_CODES = {10001}
ALREADY_KEYWORDS = [
    "已签到",
    "已经签到",
    "已领取",
    "限领一次",
    "已领过",
    "重复领取",
    "不能重复",
    "请明天再来",
    "already",
    "duplicate",
]

# 成功业务码
SUCCESS_CODES = {0, 200, "0", "200"}


def parse_response(status_code: int, text: str) -> Tuple[str, str]:
    """解析接口响应，返回 (result, detail)。

    result ∈ {"success", "already", "failed"}
    detail 保留原始响应（截断至 500 字符），便于核对真实结果。
    """
    detail = (text or "").strip()[:500]

    # 1) 非 2xx 也可能携带业务结果（daily-checkin 已签到返回 HTTP 400），
    #    所以先尝试解析 JSON 内容判断，再决定是否按 HTTP 状态判失败。
    try:
        data = json.loads(text) if text else None
    except (ValueError, TypeError):
        data = None

    # 2) 已领取优先：业务码 10001 或文案关键词
    if isinstance(data, dict):
        code = data.get("code", data.get("errcode"))
        if code in ALREADY_CODES:
            return "already", detail
    haystack = _flatten_text(data) if data is not None else ""
    haystack = (detail + " " + haystack).lower()
    for kw in ALREADY_KEYWORDS:
        if kw.lower() in haystack:
            return "already", detail

    # 3) 显式成功标记
    if isinstance(data, dict):
        code = data.get("code", data.get("errcode", data.get("status")))
        success_flag = data.get("success", data.get("ok"))
        if code in SUCCESS_CODES:
            return "success", detail
        if success_flag is True:
            return "success", detail
        if success_flag is False:
            return "failed", detail
        if isinstance(data.get("data"), dict) and not any(
            k in data for k in ("error", "errmsg")
        ):
            return "success", detail

    # 4) HTTP 层失败兜底
    if not (200 <= status_code < 300):
        return "failed", "HTTP %d: %s" % (status_code, detail)

    # 5) 其余视为失败，保留原文
    return "failed", detail


def _flatten_text(data: Any) -> str:
    """把嵌套 dict/list 拍平成字符串，用于关键词匹配。"""
    out: List[str] = []
    if isinstance(data, dict):
        for k, v in data.items():
            out.append(str(k))
            out.append(_flatten_text(v))
    elif isinstance(data, list):
        for item in data:
            out.append(_flatten_text(item))
    elif data is not None:
        out.append(str(data))
    return " ".join(out)


class CheckinExecutor:
    """签到执行器：支持两种调用方式。

    - token 模式（推荐）：使用 urllib 直接 HTTP 调用，携带 Bearer Token，零浏览器依赖；
    - context 模式：使用 Playwright context.request（自动携带登录态 Cookie）调用。
    """

    def __init__(
        self,
        api_base: str,
        timeout: int = 30,
        retry_times: int = 3,
        extra_headers: Optional[Dict[str, str]] = None,
        auth_headers: Optional[Dict[str, str]] = None,
    ):
        self.api_base = (api_base or "").rstrip("/")
        self.timeout = timeout
        self.retry_times = max(1, retry_times)
        self.extra_headers = extra_headers or {}
        self.auth_headers = auth_headers or {}

    # ---------------- 对外统一入口 ----------------
    def run_all(self, caller: Callable[[str, Dict[str, str]], Tuple[int, str]], account: Dict[str, Any]) -> List[Dict[str, Any]]:
        """执行全部操作。caller(path, headers) -> (status_code, text)。"""
        logger = get_logger()
        results: List[Dict[str, Any]] = []
        for op_key, path, label in OPERATIONS:
            result, detail, attempts = self._call_with_retry(caller, path)
            logger.info("账号 %r | %s → %s (尝试 %d 次)", account.get("name", "?"), label, result, attempts)
            logger.info("  响应原文: %s", detail[:200])
            results.append(
                {
                    "op": op_key,
                    "label": label,
                    "result": result,
                    "detail": detail,
                    "attempts": attempts,
                }
            )
            time.sleep(1.0)
        return results

    # ---------------- 调用实现 ----------------
    def http_caller(self) -> Callable[[str, Dict[str, str]], Tuple[int, str]]:
        """生成 urllib 直连调用器（token 模式）。"""
        from urllib import request as ur
        from urllib.error import HTTPError, URLError

        def caller(path: str, headers: Dict[str, str]) -> Tuple[int, str]:
            url = self.api_base + path
            req = ur.Request(url, data=b"{}", method="POST")
            for k, v in headers.items():
                req.add_header(k, v)
            try:
                with ur.urlopen(req, timeout=self.timeout) as resp:
                    return resp.status, resp.read().decode("utf-8", errors="replace")
            except HTTPError as e:
                return e.code, (e.read().decode("utf-8", errors="replace") if e.fp else "")

        return caller

    @staticmethod
    def context_caller(context: Any, timeout_ms: int) -> Callable[[str, Dict[str, str]], Tuple[int, str]]:
        """生成 Playwright context.request 调用器（cookie 登录态模式）。"""

        def caller(path: str, headers: Dict[str, str]) -> Tuple[int, str]:
            # path 为完整 URL（context 模式由调用方拼接）
            resp = context.request.post(path, headers=headers, timeout=timeout_ms)
            return resp.status, resp.text() or ""

        return caller

    # ---------------- 私有：重试 ----------------
    def _build_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
        }
        headers.update(self.extra_headers)
        headers.update(self.auth_headers)
        return headers

    def _call_with_retry(self, caller, path: str) -> Tuple[str, str, int]:
        """调用单个接口，网络异常自动重试（默认 3 次）。业务失败不重试（结果已如实确定）。"""
        headers = self._build_headers()
        last_detail = "未知错误"
        for attempt in range(1, self.retry_times + 1):
            try:
                status, text = caller(path, headers)
                # 401 = 凭证无效/过期：给出明确提示，帮助用户快速定位
                if status == 401:
                    return (
                        "failed",
                        "凭证无效或已过期（HTTP 401），请重新执行 add-account 刷新凭证",
                        attempt,
                    )
                result, detail = parse_response(status, text)
                if result != "failed" or status < 500:
                    return result, detail, attempt
                last_detail = detail
            except Exception as e:
                last_detail = "%s: %s" % (type(e).__name__, e)
                if attempt < self.retry_times:
                    time.sleep(2 * attempt)
        return "failed", last_detail, self.retry_times
