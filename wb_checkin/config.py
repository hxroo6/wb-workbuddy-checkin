"""配置管理模块：账号增删改查、配置文件读写、目录初始化。"""
import json
import os

from typing import Any, Dict, List, Optional

DEFAULT_DATA_DIR = os.path.join(os.path.expanduser("~"), ".wb_checkin")
CONFIG_FILE = "checkin_config.json"
STORAGE_DIR = "storage"
LOG_DIR = "logs"
HISTORY_FILE = "history.jsonl"

DEFAULT_CONFIG: Dict[str, Any] = {
    "api_base": "",
    "web_base": "",
    "request_timeout": 30,
    "retry_times": 3,
    "extra_headers": {},
    # 风控规避（低调行为）：账号间随机间隔（秒，[min, max]）
    "account_interval": [2, 5],
    # 当日去重：当天已成功签到/已领取的账号，本次执行直接跳过（避免重复请求）
    "skip_if_done_today": True,
    # 每个账号固定分配一个 UA（按账号名确定性选择，同一账号永远相同；可在账号配置中显式指定 ua 覆盖）
    "ua_pool": [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    ],
    "notify": {
        "enabled": False,
        "system_notify": True,
        "pushplus_token": "",
        "wecom_webhook_key": "",
    },
    "accounts": [],
}


def _merge_defaults(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """把用户配置与默认配置做一层合并，缺省字段补默认值。"""
    merged = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝
    for key, value in cfg.items():
        if key == "notify" and isinstance(value, dict):
            merged["notify"].update(value)
        else:
            merged[key] = value
    return merged


def _chmod_600(path: str) -> None:
    """尽量将敏感文件权限设为仅本人可读写（POSIX 有效，Windows 上尽力而为）。"""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class ConfigManager:
    """管理数据目录、配置文件与账号列表。"""

    def __init__(self, data_dir: Optional[str] = None):
        raw = data_dir or os.environ.get("WB_CHECKIN_DATA_DIR", DEFAULT_DATA_DIR)
        # 统一转绝对路径：定时任务/日志引用时不会因工作目录变化而失效
        self.data_dir = os.path.abspath(raw)
        self.config_path = os.path.join(self.data_dir, CONFIG_FILE)
        self.storage_dir = os.path.join(self.data_dir, STORAGE_DIR)
        self.tokens_dir = os.path.join(self.data_dir, "tokens")
        self.log_dir = os.path.join(self.data_dir, LOG_DIR)
        self.history_path = os.path.join(self.data_dir, HISTORY_FILE)
        self.balances_path = os.path.join(self.data_dir, "balances.json")
        os.makedirs(self.storage_dir, exist_ok=True)
        os.makedirs(self.tokens_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)
        # 历史记录/日志目录默认 700，登录态文件单独 600
        try:
            os.chmod(self.storage_dir, 0o700)
        except OSError:
            pass

    # ---------- 配置文件 ----------
    def load(self) -> Dict[str, Any]:
        if os.path.exists(self.config_path):
            with open(self.config_path, "r", encoding="utf-8") as f:
                try:
                    raw = json.load(f)
                except json.JSONDecodeError as e:
                    raise RuntimeError(
                        "配置文件 %s 解析失败：%s" % (self.config_path, e)
                    )
            return _merge_defaults(raw)
        return _merge_defaults({})

    def save(self, cfg: Dict[str, Any]) -> None:
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        _chmod_600(self.config_path)

    # ---------- 账号管理 ----------
    def add_account(self, name: str) -> Dict[str, Any]:
        cfg = self.load()
        for acc in cfg["accounts"]:
            if acc["name"] == name:
                raise RuntimeError("账号 %r 已存在" % name)
        storage_file = "%s.json" % name
        account = {
            "name": name,
            "storage_file": storage_file,
            "enabled": True,
        }
        cfg["accounts"].append(account)
        self.save(cfg)
        return account

    def remove_account(self, name: str) -> Dict[str, Any]:
        cfg = self.load()
        removed = None
        for acc in cfg["accounts"]:
            if acc["name"] == name:
                removed = acc
                break
        if removed is None:
            raise RuntimeError("账号 %r 不存在" % name)
        cfg["accounts"].remove(removed)
        self.save(cfg)
        # 顺带清理该账号的登录态文件与凭证备份（删除失败不致命，仅警告）
        storage_file = os.path.join(self.storage_dir, removed.get("storage_file", ""))
        if removed.get("storage_file") and os.path.exists(storage_file):
            try:
                os.remove(storage_file)
            except OSError as e:
                print("警告：删除登录态文件失败（可手动清理）：%s" % e)
        cred_file = os.path.join(self.tokens_dir, removed.get("credential_file", ""))
        if removed.get("credential_file") and os.path.exists(cred_file):
            try:
                os.remove(cred_file)
            except OSError as e:
                print("警告：删除凭证备份失败（可手动清理）：%s" % e)
        return removed

    def set_account_enabled(self, name: str, enabled: bool) -> None:
        cfg = self.load()
        for acc in cfg["accounts"]:
            if acc["name"] == name:
                acc["enabled"] = bool(enabled)
                self.save(cfg)
                return
        raise RuntimeError("账号 %r 不存在" % name)

    def list_accounts(self) -> List[Dict[str, Any]]:
        return self.load()["accounts"]

    def storage_path(self, storage_file: str) -> str:
        return os.path.join(self.storage_dir, storage_file)

    # ---------- 校验 ----------
    def validate(self, cfg: Dict[str, Any]) -> List[str]:
        """返回配置问题列表，空列表表示配置基本可用。"""
        problems: List[str] = []
        if not cfg.get("api_base"):
            problems.append("api_base 未配置（后端接口地址）")
        if not cfg.get("web_base"):
            problems.append("web_base 未配置（登录页地址）")
        for acc in cfg.get("accounts", []):
            storage_file = os.path.join(self.storage_dir, acc["storage_file"])
            if not os.path.exists(storage_file):
                problems.append(
                    "账号 %r 的登录态文件不存在：%s（请先执行 add-account 扫码登录）"
                    % (acc["name"], storage_file)
                )
        return problems
