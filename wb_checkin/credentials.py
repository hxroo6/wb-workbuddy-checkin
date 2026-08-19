"""凭证管理模块：读取 WorkBuddy 客户端登录凭证（accessToken）。

凭证来源（按优先级）：
1. 账号配置中显式指定的 credential_file（用于多账号：登录 A → 备份凭证 → 登录 B → 备份凭证）
2. 本机 WorkBuddy 客户端当前登录态文件（自动检测，单账号最省事）

凭证文件为 JSON，结构（字段名以实际文件为准，取近似匹配）：
{
  "account": {"uid": "...", "nickname": "..."},
  "auth": {"accessToken": "eyJ...", "domain": "www.codebuddy.cn", "tokenType": "Bearer"}
}

部分实现思路致敬开源前辈 codeLong1024/workbuddy-checkin：
- JWT payload 预检 token 有效期（提前发现过期，而非等到 401）
- 同时兼容 workbuddy-desktop.info / Tencent-Cloud.coding-copilot.info 两个认证文件名
"""
import base64
import json
import os
import time
from typing import Any, Dict, Optional

from .logger import get_logger

# 本机客户端凭证文件名（兼容 CodeBuddy / WorkBuddy 客户端的不同命名）
# 灵感来源：codeLong1024/workbuddy-checkin 的多文件兼容做法
_AUTH_FILENAMES = (
    "workbuddy-desktop.info",
    "Tencent-Cloud.coding-copilot.info",
)

# 本机客户端凭证文件路径（Windows / macOS）
_CLIENT_AUTH_CANDIDATES = [
    os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "CodeBuddyExtension", "Data", "Public", "auth", name,
    )
    for name in _AUTH_FILENAMES
] + [
    os.path.join(
        os.path.expanduser("~"),
        "Library", "Application Support", "CodeBuddyExtension",
        "Data", "Public", "auth", name,
    )
    for name in _AUTH_FILENAMES
]


def jwt_payload(token: str) -> Optional[Dict[str, Any]]:
    """解析 JWT 的 payload（中段），失败返回 None。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def jwt_sub(token: str) -> str:
    """从 JWT 提取 sub（uid 兜底来源）。"""
    payload = jwt_payload(token)
    return str((payload or {}).get("sub") or "")


def token_expiry_ts(token: str) -> Optional[float]:
    """token 过期时间戳（秒），解析失败返回 None。"""
    payload = jwt_payload(token)
    if not payload:
        return None
    exp = payload.get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None


def validate_token(cred: Dict[str, Any], skew: int = 300) -> tuple:
    """预检 token 是否有效（含有效期判断）。

    借鉴 codeLong1024/workbuddy-checkin 的 JWT 预检思路：
    提前解 exp，过期/临期直接提示，避免白白发起请求打到 401。

    返回 (ok: bool, reason: str)
    """
    token = (cred or {}).get("access_token", "")
    if not token:
        return False, "凭证缺失"
    exp = token_expiry_ts(token)
    if exp is None:
        # 解析不出 exp 就交给服务端判定（兼容非标准 JWT）
        return True, ""
    remaining = exp - time.time()
    if remaining <= 0:
        return False, "凭证已过期，请在客户端重新登录后 add-account 刷新"
    if remaining < skew:
        return False, "凭证即将过期（剩余 %d 秒），请在客户端重新登录后 add-account 刷新" % int(remaining)
    return True, ""


def find_client_auth_file() -> Optional[str]:
    """定位本机 WorkBuddy 客户端当前登录凭证文件（多文件名兼容）。"""
    for path in _CLIENT_AUTH_CANDIDATES:
        if path and os.path.exists(path):
            return path
    return None


def read_auth_file(path: str) -> Optional[Dict[str, Any]]:
    """读取凭证文件，规范化为统一结构。

    返回: {"access_token", "uid", "domain", "nickname", "token_type"} 或 None
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        get_logger().warning("读取凭证文件失败 %s：%s", path, e)
        return None
    return normalize_auth(raw)


def normalize_auth(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从任意字段形态的凭证 JSON 提取 token 信息（字段名做兼容匹配）。"""
    auth = raw.get("auth") if isinstance(raw.get("auth"), dict) else {}
    account = raw.get("account") if isinstance(raw.get("account"), dict) else {}

    access_token = (
        auth.get("accessToken")
        or auth.get("access_token")
        or auth.get("token")
        or raw.get("accessToken")
        or raw.get("access_token")
        or raw.get("token")
    )
    if not access_token:
        return None
    token_type = auth.get("tokenType") or auth.get("token_type") or "Bearer"
    uid = (
        auth.get("uid")
        or account.get("uid")
        or account.get("userId")
        or raw.get("uid")
        or ""
    )
    if not uid and access_token:
        # uid 缺失时从 JWT sub 兜底（借鉴 codeLong1024 的做法）
        uid = jwt_sub(str(access_token))
    domain = auth.get("domain") or raw.get("domain") or "www.codebuddy.cn"
    nickname = account.get("nickname") or account.get("name") or ""
    # token 到期时间（毫秒时间戳，兼容秒）
    expires_at_raw = (
        auth.get("expiresAt") or auth.get("expires_at") or raw.get("expiresAt") or 0
    )
    expires_at = None
    if isinstance(expires_at_raw, (int, float)) and expires_at_raw > 0:
        ts = expires_at_raw / 1000 if expires_at_raw > 10**11 else expires_at_raw
        try:
            from datetime import datetime, timezone

            expires_at = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        except (OSError, ValueError, OverflowError):
            expires_at = None
    return {
        "access_token": str(access_token),
        "token_type": str(token_type),
        "uid": str(uid or ""),
        "domain": str(domain),
        "nickname": str(nickname),
        "expires_at": expires_at,
        "source": "client",
    }


def build_auth_headers(cred: Dict[str, Any]) -> Dict[str, str]:
    """根据凭证构建请求头（Bearer Token 认证）。"""
    headers = {
        "Authorization": "%s %s" % (cred.get("token_type", "Bearer"), cred["access_token"]),
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "WorkBuddy-Checkin/1.2",
    }
    if cred.get("uid"):
        headers["X-User-Id"] = cred["uid"]
    if cred.get("domain"):
        headers["X-Domain"] = cred["domain"]
    return headers


def load_account_credential(cm, account: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """按账号配置加载凭证。

    优先级：
    1. account["credential_file"]：显式指定凭证文件（相对 tokens 目录或绝对路径）
    2. account["token"]：直接内联的 access_token
    3. 本机客户端当前登录凭证
    """
    cred_file = (account.get("credential_file") or "").strip()
    if cred_file:
        path = cred_file
        if not os.path.isabs(path):
            tokens_dir = os.path.join(cm.data_dir, "tokens")
            path = os.path.join(tokens_dir, cred_file)
        cred = read_auth_file(path)
        if cred:
            return cred
        get_logger().warning("账号 %r 的凭证文件不可用：%s", account.get("name"), path)

    inline_token = (account.get("token") or "").strip()
    if inline_token:
        return normalize_auth(
            {"auth": {"accessToken": inline_token, "tokenType": "Bearer"},
             "account": {"uid": account.get("uid", "")}}
        )

    client_file = find_client_auth_file()
    if client_file:
        cred = read_auth_file(client_file)
        if cred:
            return cred
    return None


def save_backup_credential(cm, account_name: str, source_path: str) -> Optional[str]:
    """把客户端凭证备份到 tokens/<name>.info，返回备份路径。"""
    tokens_dir = os.path.join(cm.data_dir, "tokens")
    os.makedirs(tokens_dir, exist_ok=True)
    dest = os.path.join(tokens_dir, "%s.info" % account_name)
    try:
        with open(source_path, "r", encoding="utf-8") as src, open(dest, "w", encoding="utf-8") as dst:
            dst.write(src.read())
        try:
            os.chmod(dest, 0o600)
        except OSError:
            pass
        return dest
    except OSError as e:
        get_logger().warning("备份凭证失败：%s", e)
        return None
