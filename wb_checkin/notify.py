"""通知模块（可选）：系统桌面通知 / PushPlus / 企业微信机器人。

全部为"尽力而为"：任一通知渠道失败不影响签到主流程，仅记日志。
requests 未安装时自动降级为 urllib。
"""
import json
import sys
from typing import Any, Dict, List
from urllib import request as _urllib

from .logger import get_logger

PUSHPLUS_URL = "https://www.pushplus.plus/send"
WECOM_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"


def send_all(cfg: Dict[str, Any], summary_text: str) -> List[str]:
    """按配置依次发送通知，返回成功发送的渠道名列表。"""
    logger = get_logger()
    notify = cfg.get("notify") or {}
    if not notify.get("enabled"):
        return []

    sent: List[str] = []
    if notify.get("system_notify"):
        try:
            _system_notify("WorkBuddy 每日签到", summary_text)
            sent.append("system")
        except Exception as e:
            logger.warning("系统通知失败：%s", e)

    token = (notify.get("pushplus_token") or "").strip()
    if token:
        try:
            _post_json(PUSHPLUS_URL, {"token": token, "title": "WorkBuddy 每日签到", "content": summary_text})
            sent.append("pushplus")
        except Exception as e:
            logger.warning("PushPlus 通知失败：%s", e)

    wecom_key = (notify.get("wecom_webhook_key") or "").strip()
    if wecom_key:
        try:
            _post_json(
                WECOM_URL + "?key=" + wecom_key,
                {"msgtype": "text", "text": {"content": summary_text}},
            )
            sent.append("wecom")
        except Exception as e:
            logger.warning("企业微信通知失败：%s", e)

    if sent:
        logger.info("通知已发送：%s", ", ".join(sent))
    return sent


def _post_json(url: str, payload: Dict[str, Any]) -> None:
    data = json.dumps(payload).encode("utf-8")
    try:
        import requests  # type: ignore

        resp = requests.post(url, data=data, timeout=15)
        resp.raise_for_status()
        return
    except ImportError:
        pass
    except Exception:
        # requests 存在但请求失败 → 抛出让上层记录
        raise

    req = _urllib.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _urllib.urlopen(req, timeout=15) as resp:
        resp.read()


def _system_notify(title: str, message: str) -> None:
    """跨平台桌面通知（尽力而为）。"""
    if sys.platform.startswith("win"):
        # Windows：优先 plyer；失败则用 PowerShell 气泡（Win10+ 的 toast 用 msg 替代）
        try:
            from plyer import notification  # type: ignore

            notification.notify(title=title, message=message, timeout=10)
            return
        except ImportError:
            pass
        # 降级：Windows 消息框会阻塞，改用控制台输出（不阻塞）
        get_logger().info("系统通知不可用（未安装 plyer），跳过桌面弹窗。")
    elif sys.platform == "darwin":
        # macOS：osascript 通知
        from subprocess import run

        msg = message.replace('"', "'")
        run(
            [
                "osascript",
                "-e",
                'display notification "%s" with title "%s"' % (msg, title),
            ],
            check=True,
        )
    else:
        get_logger().info("当前平台不支持桌面通知。")
