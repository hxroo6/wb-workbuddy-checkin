#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 积分管理 Web 界面（零第三方依赖，标准库 http.server）

用法：
    python webui.py [--data-dir .data] [--port 8080]
启动后访问 http://127.0.0.1:8080

功能：
    - 查看所有账号：凭证状态、有效期、当前积分、与上次差值、最近结果
    - 一键签到（全部账号）
    - 添加账号（需先在 WorkBuddy 客户端登录目标账号）
    - 启用 / 停用 / 删除账号
    - 定时任务状态、执行日志查看
"""
import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wb_checkin.config import ConfigManager  # noqa: E402
from wb_checkin import credentials, rotator, scheduler  # noqa: E402
from wb_checkin.balance import (  # noqa: E402
    load_balances,
    query_total_balance,
    record_balance,
)
from wb_checkin.executor import CheckinExecutor  # noqa: E402
from wb_checkin.logger import read_recent_history, setup_logging  # noqa: E402

WEBUI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui")
_CM = None  # type: ConfigManager
_LOCK = threading.Lock()


def _load_index_html() -> str:
    path = os.path.join(WEBUI_DIR, "index.html")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>webui/index.html 缺失</h1>"


class Handler(BaseHTTPRequestHandler):
    server_version = "WB-WebUI/1.0"

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    # ---------------- 工具 ----------------
    def _send(self, code: int, body: str, content_type: str):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False, indent=2), "application/json")

    def send_html(self, html: str):
        self._send(200, html, "text/html")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    # ---------------- 路由 ----------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self.send_html(_load_index_html())
        elif path == "/api/status":
            self.api_status()
        elif path == "/api/logs":
            self.api_logs()
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_json()
        try:
            if path == "/api/checkin":
                self.api_checkin(body)
            elif path == "/api/auto":
                self.api_auto(body)
            elif path == "/api/travel":
                self.api_travel(body)
            elif path == "/api/account/add":
                self.api_add_account(body)
            elif path == "/api/account/toggle":
                self.api_toggle_account(body)
            elif path == "/api/account/remove":
                self.api_remove_account(body)
            else:
                self.send_json({"error": "not found"}, 404)
        except Exception as e:  # 兜底：任何异常返回明确错误
            self.send_json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)

    # ---------------- API 实现 ----------------
    def api_status(self):
        cm = _CM
        cfg = cm.load()
        balances = load_balances(cm.balances_path)
        history = read_recent_history(cm.history_path, limit=100)
        accounts = []
        for acc in cm.list_accounts():
            item = {
                "name": acc["name"],
                "enabled": acc.get("enabled", True),
                "nickname": "",
                "cred_desc": "无凭证",
                "expires_at": None,
                "balance": None,
                "delta": None,
                "last": None,
            }
            cred_file = (acc.get("credential_file") or "").strip()
            cred = None
            if cred_file:
                path = os.path.join(cm.tokens_dir, cred_file)
                if os.path.exists(path):
                    cred = credentials.read_auth_file(path)
                    if cred:
                        item["nickname"] = cred.get("nickname", "")
                        item["expires_at"] = cred.get("expires_at")
                        # JWT 预检（借鉴 codeLong1024）：提前发现过期/临期
                        tok_ok, tok_reason = credentials.validate_token(cred)
                        if tok_ok:
                            item["cred_desc"] = "凭证有效"
                        else:
                            item["cred_desc"] = "⚠ " + tok_reason
                else:
                    item["cred_desc"] = "凭证缺失"
            elif (acc.get("token") or "").strip():
                item["cred_desc"] = "内联 token"
            else:
                storage = os.path.join(cm.storage_dir, acc.get("storage_file", ""))
                item["cred_desc"] = "浏览器登录态" if os.path.exists(storage) else "无凭证/登录态"
            b = balances.get(acc["name"])
            prev_balance = b.get("balance") if b else None
            # 实时查询积分（签到入账可能延迟几分钟，以真实接口为准），失败回退缓存
            live = None
            live_error = None
            if cred and cred.get("access_token"):
                try:
                    executor = CheckinExecutor(api_base=cfg.get("api_base") or "https://www.codebuddy.cn", timeout=20)
                    executor.auth_headers = credentials.build_auth_headers(cred)
                    live = query_total_balance(executor.http_caller(), executor._build_headers())
                except Exception as e:
                    live = None
                    live_error = "%s: %s" % (type(e).__name__, e)
            item["live_error"] = live_error
            if live is not None:
                item["balance"] = live
                item["balance_live"] = True
                item["balance_ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                # 与上次缓存比较得出增量，并同步缓存
                rec = record_balance(cm.balances_path, acc["name"], live)
                item["delta"] = rec["delta"]
                item["gained"] = rec.get("gained")
            elif prev_balance is not None:
                item["balance"] = prev_balance
                item["balance_live"] = False
                item["balance_ts"] = b.get("ts")
            # 猫猫旅行状态
            item["travel"] = None
            if cred and cred.get("access_token"):
                try:
                    from wb_checkin import cat_travel

                    t = cat_travel.get_status(credentials.build_auth_headers(cred))
                    if t:
                        item["travel"] = {
                            "state": t.get("state"),
                            "arrive_at": t.get("arrive_at"),
                            "duration_hours": t.get("duration_hours"),
                            "reward_credit": t.get("reward_credit"),
                            "daily_limit_reached": t.get("daily_limit_reached"),
                        }
                except Exception:
                    pass
            # 最近一条执行记录
            for r in reversed(history):
                if r.get("account") == acc["name"]:
                    item["last"] = {
                        "ts": r.get("ts"),
                        "result": r.get("result"),
                        "label": r.get("label"),
                    }
                    break
            accounts.append(item)

        payload = {
            "ok": True,
            "data_dir": cm.data_dir,
            "api_base": cfg.get("api_base", ""),
            "schedule": scheduler.task_status(),
            "notify_enabled": bool(cfg.get("notify", {}).get("enabled")),
            "accounts": accounts,
        }
        self.send_json(payload)

    def api_checkin(self, body):
        cm = _CM
        cfg = cm.load()
        only = (body.get("account") or "").strip() or None
        with _LOCK:
            summary = rotator.run_all(cm, cfg, only_account=only)
        text = rotator.build_summary_text(summary)
        report = logger_mod_append_report(cm, text)
        self.send_json({
            "ok": True,
            "summary": summary,
            "text": text,
            "report": report,
        })

    def api_travel(self, body):
        from wb_checkin import cat_travel

        cm = _CM
        cfg = cm.load()
        only = (body.get("account") or "").strip() or None
        mode = body.get("mode") or "full"
        # 手动收取：短延迟（0~2 分钟）尽快收；完整状态机：默认 0~30 分钟
        delay = int(body.get("claim_delay_minutes") or (2 if mode == "claim" else 30))
        with _LOCK:
            summary = cat_travel.run(cm, cfg, only_account=only, claim_delay_minutes=delay, mode=mode)
        text = cat_travel.build_summary_text(summary)
        report = logger_mod_append_report(cm, text)
        self.send_json({
            "ok": True,
            "summary": summary,
            "text": text,
            "report": report,
        })

    def api_auto(self, body):
        """一键全自动：随机延迟 → 签到 → 猫猫旅行（收/派）。"""
        from wb_checkin import cat_travel, rotator

        cm = _CM
        cfg = cm.load()
        only = (body.get("account") or "").strip() or None
        delay_max = int(body.get("delay_max_minutes") or 2)  # 界面按钮默认短延迟，不长时间卡住
        import random

        if delay_max > 0:
            d = random.randint(0, delay_max * 60)
            time.sleep(d)
        parts = []
        with _LOCK:
            summary = rotator.run_all(cm, cfg, only_account=only)
            if summary:
                parts.append(rotator.build_summary_text(summary))
            tsummary = cat_travel.run(cm, cfg, only_account=only)
            if tsummary:
                parts.append(cat_travel.build_summary_text(tsummary))
        text = "\n\n".join(parts) if parts else "无可执行操作"
        report = logger_mod_append_report(cm, text)
        self.send_json({"ok": True, "text": text, "report": report})

    def api_add_account(self, body):
        cm = _CM
        name = (body.get("name") or "").strip()
        if not name:
            self.send_json({"ok": False, "error": "请提供账号名"}, 400)
            return
        if any(a["name"] == name for a in cm.list_accounts()):
            self.send_json({"ok": False, "error": "账号 %r 已存在（可在配置中删掉后重加）" % name}, 400)
            return
        client_file = credentials.find_client_auth_file()
        if not client_file:
            self.send_json({
                "ok": False,
                "error": "未检测到 WorkBuddy 客户端登录凭证，请先在客户端登录目标账号后重试",
            }, 400)
            return
        cred = credentials.read_auth_file(client_file)
        if not cred:
            self.send_json({"ok": False, "error": "客户端凭证文件读取失败"}, 400)
            return
        backup = credentials.save_backup_credential(cm, name, client_file)
        if not backup:
            self.send_json({"ok": False, "error": "凭证备份失败"}, 500)
            return
        cfg = cm.load()
        if not any(a["name"] == name for a in cfg["accounts"]):
            cm.add_account(name)
            cfg = cm.load()
        for acc in cfg["accounts"]:
            if acc["name"] == name:
                acc["credential_file"] = os.path.basename(backup)
                acc.pop("storage_file", None)
                break
        cm.save(cfg)
        # 验证
        executor = CheckinExecutor(api_base=cfg.get("api_base") or "https://www.codebuddy.cn", timeout=20)
        executor.auth_headers = credentials.build_auth_headers(cred)
        verified = False
        try:
            status, text = executor.http_caller()("/v2/billing/meter/checkin-status", executor._build_headers())
            verified = 200 <= status < 300
        except Exception:
            verified = False
        self.send_json({
            "ok": True,
            "verified": verified,
            "nickname": cred.get("nickname", ""),
            "expires_at": cred.get("expires_at"),
            "name": name,
        })

    def api_toggle_account(self, body):
        cm = _CM
        name = (body.get("name") or "").strip()
        if not name:
            self.send_json({"ok": False, "error": "缺少账号名"}, 400)
            return
        try:
            for acc in cm.list_accounts():
                if acc["name"] == name:
                    cm.set_account_enabled(name, not acc.get("enabled", True))
                    self.send_json({"ok": True, "name": name, "enabled": not acc.get("enabled", True)})
                    return
            self.send_json({"ok": False, "error": "账号不存在"}, 404)
        except RuntimeError as e:
            self.send_json({"ok": False, "error": str(e)}, 400)

    def api_remove_account(self, body):
        cm = _CM
        name = (body.get("name") or "").strip()
        if not name:
            self.send_json({"ok": False, "error": "缺少账号名"}, 400)
            return
        try:
            removed = cm.remove_account(name)
            self.send_json({"ok": True, "name": removed["name"]})
        except RuntimeError as e:
            self.send_json({"ok": False, "error": str(e)}, 400)

    def api_logs(self):
        cm = _CM
        files = []
        import glob

        log_files = sorted(glob.glob(os.path.join(cm.log_dir, "*.log")), key=os.path.getmtime, reverse=True)[:10]
        for fp in log_files:
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    tail = "".join(f.readlines()[-30:])
            except OSError:
                tail = ""
            files.append({"path": os.path.basename(fp), "content": tail})
        history = read_recent_history(cm.history_path, limit=50)
        self.send_json({"ok": True, "files": files, "history": history})


def logger_mod_append_report(cm, text: str) -> str:
    from wb_checkin import logger as logger_mod
    return logger_mod.append_summary_report(cm.log_dir, text)


def main(argv=None) -> int:
    global _CM
    parser = argparse.ArgumentParser(prog="webui", description="WorkBuddy 积分管理 Web 界面")
    parser.add_argument("--data-dir", default=None, help="数据目录（默认 ~/.wb_checkin）")
    parser.add_argument("--port", type=int, default=8080, help="监听端口（默认 8080）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    args = parser.parse_args(argv)

    _CM = ConfigManager(args.data_dir)
    setup_logging(_CM.log_dir)
    print("WorkBuddy 积分管理界面")
    print("  数据目录：%s" % _CM.data_dir)
    print("  访问地址：http://%s:%d" % (args.host, args.port))
    print("  Ctrl+C 停止")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
