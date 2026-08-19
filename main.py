#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 多账号每日积分领取工具 - 命令行入口。

用法示例：
    python main.py add-account account1           # 添加账号并扫码登录
    python main.py list-accounts                   # 列出账号
    python main.py checkin                         # 执行全部账号签到+领礼包
    python main.py checkin --account account1      # 只执行指定账号
    python main.py install-schedule --time 09:00   # 注册每日 9 点定时任务
    python main.py uninstall-schedule              # 卸载定时任务
    python main.py status                          # 查看状态与最近记录
"""
import argparse
import os
import sys
import time

# 保证从任意工作目录 / 定时任务环境都能找到 wb_checkin 包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wb_checkin import __version__  # noqa: E402
from wb_checkin.config import ConfigManager  # noqa: E402
from wb_checkin import logger as logger_mod  # noqa: E402
from wb_checkin import login, rotator, scheduler  # noqa: E402
from wb_checkin.notify import send_all  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wb-checkin",
        description="WorkBuddy 多账号自动签到与领取每日礼包工具 v%s" % __version__,
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="数据目录（配置文件/登录态/日志存放处），默认 ~/.wb_checkin",
    )
    sub = parser.add_subparsers(dest="command", metavar="命令")

    p_add = sub.add_parser("add-account", help="添加账号：打开浏览器扫码登录并保存登录态")
    p_add.add_argument("name", help="账号名称（唯一标识，如 account1）")
    p_add.add_argument(
        "--auto",
        action="store_true",
        help="自动检测登录（无需按回车，检测到 URL/Cookie 变化自动保存）",
    )
    p_add.add_argument("--wait-minutes", type=int, default=10, help="自动检测最长等待分钟数（默认 10）")

    p_remove = sub.add_parser("remove-account", help="删除账号（含登录态文件）")
    p_remove.add_argument("name")

    sub.add_parser("list-accounts", help="列出所有账号及状态")

    p_enable = sub.add_parser("enable-account", help="启用账号")
    p_enable.add_argument("name")
    p_disable = sub.add_parser("disable-account", help="停用账号（不参与轮换）")
    p_disable.add_argument("name")

    p_checkin = sub.add_parser("checkin", help="执行签到与领取礼包（默认处理全部启用账号）")
    p_checkin.add_argument("--account", default=None, help="仅处理指定账号")
    p_checkin.add_argument("--headful", action="store_true", help="使用可见浏览器窗口执行（默认无头）")
    p_checkin.add_argument(
        "--delay-max-minutes", type=int, default=0,
        help="执行前随机延迟 0~N 分钟（配合定时任务错峰，默认 0 不延迟）",
    )

    p_sched = sub.add_parser("install-schedule", help="注册系统每日定时任务")
    p_sched.add_argument("--time", default="09:00", help="每日执行时间 HH:MM（默认 09:00）")
    p_sched.add_argument("--python", default=None, help="Python 解释器完整路径（默认当前解释器）")
    p_sched.add_argument(
        "--random-delay", type=int, default=0,
        help="任务启动后随机延迟 0~N 分钟再签到（错峰执行，默认 0 准点）",
    )
    p_sched.add_argument(
        "--mode", choices=["checkin", "travel"], default="checkin",
        help="任务执行内容：checkin=签到（默认）| travel=猫猫旅行",
    )
    p_sched.add_argument(
        "--name", default="WBCheckinDaily",
        help="任务名（默认 WBCheckinDaily；注册多个旅行任务时需不同名字）",
    )

    p_travel = sub.add_parser("cat-travel", help="猫猫旅行：派出/收取旅行奖励")
    p_travel.add_argument("--account", default=None, help="仅处理指定账号")
    p_travel.add_argument(
        "--claim-delay-minutes", type=int, default=30,
        help="到点后随机延迟 0~N 分钟再收取（默认 30，降低准点特征）",
    )
    p_travel.add_argument("--no-redepart", action="store_true", help="收取后不自动重新派出")
    p_travel.add_argument(
        "--mode", choices=["full", "claim", "depart"], default="full",
        help="full=完整状态机（默认）| claim=仅收取已到达 | depart=仅派出空闲",
    )

    p_auto = sub.add_parser("auto", help="一键全自动：随机延迟 → 签到 → 猫猫旅行（收/派）")
    p_auto.add_argument("--account", default=None, help="仅处理指定账号")
    p_auto.add_argument(
        "--delay-max-minutes", type=int, default=30,
        help="执行前随机延迟 0~N 分钟（默认 30）",
    )

    p_un = sub.add_parser("uninstall-schedule", help="卸载系统每日定时任务")
    p_un.add_argument("--name", default="WBCheckinDaily", help="任务名（默认 WBCheckinDaily）")
    sub.add_parser("status", help="查看配置、定时任务与最近执行记录")

    p_logs = sub.add_parser("logs", help="查看运行日志与执行历史")
    p_logs.add_argument("--lines", type=int, default=50, help="显示最近 N 行（默认 50）")
    p_logs.add_argument("--date", default=None, help="指定日志日期 YYYY-MM-DD（默认今天）")
    p_logs.add_argument("--history", action="store_true", help="显示结构化执行历史（history.jsonl）")

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cm = ConfigManager(args.data_dir)
    log = logger_mod.setup_logging(cm.log_dir)

    if not args.command:
        build_parser().print_help()
        return 0

    # ---------- 账号管理 ----------
    if args.command == "add-account":
        from wb_checkin import credentials
        from wb_checkin.executor import CheckinExecutor

        # 方式一（推荐）：本机 WorkBuddy 客户端已登录 → 自动备份凭证，无需扫码
        client_file = credentials.find_client_auth_file()
        if client_file:
            log.info("检测到本机 WorkBuddy 客户端登录凭证：%s", client_file)
            backup = credentials.save_backup_credential(cm, args.name, client_file)
            if backup:
                # 用真实接口验证凭证有效性
                try:
                    cm.add_account(args.name)
                except RuntimeError as e:
                    log.error("%s", e)
                    return 1
                cfg = cm.load()
                for acc in cfg["accounts"]:
                    if acc["name"] == args.name:
                        acc["credential_file"] = os.path.basename(backup)
                        break
                cm.save(cfg)
                cred = credentials.read_auth_file(backup)
                if cred:
                    log.info("账号：%s（%s）", cred.get("nickname") or args.name, cred.get("uid", "")[:8])
                    exec_ = CheckinExecutor(api_base=cfg.get("api_base") or "https://www.codebuddy.cn", timeout=20)
                    exec_.auth_headers = credentials.build_auth_headers(cred)
                    try:
                        status, text = exec_.http_caller()("/v2/billing/meter/checkin-status", exec_._build_headers())
                        if 200 <= status < 300:
                            log.info("✅ 账号 %r 凭证有效（接口返回 HTTP %d），无需扫码。", args.name, status)
                            log.info("验证签到：python main.py checkin --account %s", args.name)
                            return 0
                        log.warning("凭证文件已备份，但接口返回 HTTP %d：%s", status, text[:120])
                        return 0
                    except Exception as e:
                        log.warning("凭证文件已备份，但验证接口调用失败：%s", e)
                        return 0
            log.warning("凭证备份失败，回退到扫码登录方式。")

        # 方式二（备选）：扫码登录保存浏览器登录态
        try:
            account = cm.add_account(args.name)
        except RuntimeError as e:
            log.error("%s", e)
            return 1
        storage_path = cm.storage_path(account["storage_file"])
        cfg = cm.load()
        web_base = cfg.get("web_base")
        if not web_base:
            log.error(
                "请先编辑 %s 配置 web_base（登录页地址）。",
                cm.config_path,
            )
            log.info("配置模板参考：checkin_config.example.json")
            return 1
        login.interactive_login(
            web_base,
            storage_path,
            args.name,
            auto_wait=args.auto,
            wait_minutes=args.wait_minutes,
        )
        log.info("完成。验证签到：python main.py checkin --account %s", args.name)
        return 0

    if args.command == "remove-account":
        try:
            removed = cm.remove_account(args.name)
            log.info("已删除账号 %r（登录态文件已清理）", removed["name"])
        except RuntimeError as e:
            log.error("%s", e)
            return 1
        return 0

    if args.command == "list-accounts":
        _print_accounts(cm)
        return 0

    if args.command in ("enable-account", "disable-account"):
        try:
            cm.set_account_enabled(args.name, enabled=(args.command == "enable-account"))
            log.info("账号 %r 已%s", args.name, "启用" if args.command == "enable-account" else "停用")
        except RuntimeError as e:
            log.error("%s", e)
            return 1
        return 0

    # ---------- 签到执行 ----------
    if args.command == "checkin":
        cfg = cm.load()
        if not cfg.get("api_base"):
            log.error("api_base 未配置。请编辑 %s 填入后端接口地址。", cm.config_path)
            return 1
        # 无头模式由 rotator 内部决定；--headful 预留：当前实现固定无头（不干扰桌面使用）
        if args.headful:
            log.info("提示：签到接口走 HTTP 调用，无头模式更稳定，headful 参数将被忽略。")
        # 随机延迟错峰（配合定时任务，避免每天准点触发被识别）
        if getattr(args, "delay_max_minutes", 0) and args.delay_max_minutes > 0:
            import random as _random

            delay = _random.randint(0, args.delay_max_minutes * 60)
            log.info("随机延迟 %d 秒后开始执行...", delay)
            time.sleep(delay)
        summary = rotator.run_all(cm, cfg, only_account=args.account)
        if summary:
            text = rotator.build_summary_text(summary)
            report = logger_mod.append_summary_report(cm.log_dir, text)
            if report:
                log.info("执行汇总已写入：%s", report)
            send_all(cfg, text)
        # 退出码（借鉴 codeLong1024 的退出码约定）：
        # 0=全部成功/已领取/跳过  1=有业务失败  2=存在凭证失效（需重新登录刷新）
        if any(
            r.get("cred_invalid")
            for s in summary for r in s.get("results", [])
        ):
            return 2
        if any(
            r.get("result") == "failed"
            for s in summary for r in s.get("results", [])
        ):
            return 1
        return 0

    # ---------- 猫猫旅行 ----------
    if args.command == "cat-travel":
        from wb_checkin import cat_travel

        cfg = cm.load()
        summary = cat_travel.run(
            cm, cfg,
            only_account=args.account,
            claim_delay_minutes=args.claim_delay_minutes,
            auto_redepart=not args.no_redepart,
            mode=getattr(args, "mode", "full"),
        )
        if summary:
            text = cat_travel.build_summary_text(summary)
            report = logger_mod.append_summary_report(cm.log_dir, text)
            if report:
                log.info("执行汇总已写入：%s", report)
            send_all(cfg, text)
        # 退出码：0=全部成功/等待/跳过  1=有失败  2=凭证失效
        if any(s.get("cred_invalid") for s in summary):
            return 2
        if any(s.get("result") == "failed" for s in summary):
            return 1
        return 0

    # ---------- 一键全自动 ----------
    if args.command == "auto":
        from wb_checkin import cat_travel

        cfg = cm.load()
        if not cfg.get("api_base"):
            log.error("api_base 未配置。请编辑 %s 填入后端接口地址。", cm.config_path)
            return 1
        # 执行前随机延迟（错峰，模拟真实操作节奏）
        if getattr(args, "delay_max_minutes", 0) and args.delay_max_minutes > 0:
            import random as _random

            d = _random.randint(0, args.delay_max_minutes * 60)
            log.info("一键全自动：随机延迟 %d 秒后开始...", d)
            time.sleep(d)
        # 1) 签到（当日已签会自动跳过）
        summary = rotator.run_all(cm, cfg, only_account=args.account)
        if summary:
            text = rotator.build_summary_text(summary)
            logger_mod.append_summary_report(cm.log_dir, text)
            send_all(cfg, text)
        # 2) 猫猫旅行（空闲派出 / 到点收取 / 未到点等待）
        tsummary = cat_travel.run(cm, cfg, only_account=args.account)
        if tsummary:
            ttext = cat_travel.build_summary_text(tsummary)
            logger_mod.append_summary_report(cm.log_dir, ttext)
            send_all(cfg, ttext)
        log.info("一键全自动完成。")
        # 退出码：0=成功  1=有失败  2=凭证失效
        cred_invalid = any(
            r.get("cred_invalid") for s in summary for r in s.get("results", [])
        ) or any(s.get("cred_invalid") for s in tsummary)
        failed = any(
            r.get("result") == "failed" for s in summary for r in s.get("results", [])
        ) or any(s.get("result") == "failed" for s in tsummary)
        if cred_invalid:
            return 2
        if failed:
            return 1
        return 0

    # ---------- 定时任务 ----------
    if args.command == "install-schedule":
        main_path = os.path.abspath(__file__)
        ok, msg = scheduler.install_schedule(
            cm.data_dir, main_path, args.time, args.python,
            random_delay=getattr(args, "random_delay", 0),
            mode=getattr(args, "mode", "checkin"),
            task_name=getattr(args, "name", "WBCheckinDaily"),
        )
        log.info("%s", msg)
        return 0 if ok else 1

    if args.command == "uninstall-schedule":
        ok, msg = scheduler.uninstall_schedule(getattr(args, "name", "WBCheckinDaily"))
        log.info("%s", msg)
        return 0 if ok else 1

    # ---------- 日志查看 ----------
    if args.command == "logs":
        files = logger_mod.list_log_files(cm.log_dir)
        if not files:
            log.info("日志目录为空：%s（尚无运行记录）", cm.log_dir)
        else:
            log.info("日志文件（共 %d 个）：", len(files))
            for fp in files[:10]:
                log.info("  %s（%.1f KB）", fp, os.path.getsize(fp) / 1024)
            log.info("")
            log.info("最近执行汇总：")
            summary_files = [f for f in files if "summary-" in os.path.basename(f)]
            if summary_files:
                for sf in summary_files[:3]:
                    log.info("  >>> %s", sf)
                    log.info("%s", logger_mod.read_tail(sf, args.lines))
            else:
                log.info("  （暂无汇总报告，执行 checkin 后生成）")
            if not args.history:
                log.info("")
                log.info("提示：加 --history 查看结构化执行历史，--date YYYY-MM-DD 指定某天日志。")
        if args.history:
            records = logger_mod.read_recent_history(cm.history_path, limit=50)
            if records:
                log.info("结构化执行历史（最近 %d 条）：", len(records))
                for r in records:
                    log.info(
                        "  %s | %s | %s | %s | %s",
                        r.get("ts", "?"), r.get("account", "?"),
                        r.get("label", "?"), r.get("result", "?"),
                        (r.get("detail") or "")[:60],
                    )
            else:
                log.info("暂无执行历史。")
        return 0

    # ---------- 状态 ----------
    if args.command == "status":
        cfg = cm.load()
        log.info("数据目录：%s", cm.data_dir)
        log.info("配置文件：%s", cm.config_path)
        log.info("api_base：%s", cfg.get("api_base") or "（未配置）")
        log.info("web_base：%s", cfg.get("web_base") or "（未配置）")
        log.info("通知：%s", "开启" if cfg.get("notify", {}).get("enabled") else "关闭")
        log.info("定时任务：%s", scheduler.task_status())
        _print_accounts(cm)
        records = logger_mod.read_recent_history(cm.history_path, limit=10)
        if records:
            log.info("最近执行记录（%d 条）：", len(records))
            for r in records:
                log.info(
                    "  %s | %s | %s | %s | %s",
                    r.get("ts", "?"), r.get("account", "?"),
                    r.get("label", "?"), r.get("result", "?"),
                    (r.get("detail") or "")[:80],
                )
        else:
            log.info("暂无执行记录（尚未执行过 checkin）。")
        return 0

    build_parser().print_help()
    return 0


def _print_accounts(cm: ConfigManager) -> None:
    log = logger_mod.get_logger()
    accounts = cm.list_accounts()
    if not accounts:
        log.info("账号列表为空。添加账号：python main.py add-account <名称>")
        return
    log.info("账号列表：")
    for acc in accounts:
        status = "启用" if acc.get("enabled", True) else "停用"
        cred_file = (acc.get("credential_file") or "").strip()
        if cred_file:
            path = os.path.join(cm.tokens_dir, cred_file)
            if os.path.exists(path):
                from wb_checkin.credentials import read_auth_file

                cred = read_auth_file(path)
                exp = (" | 有效期至 %s" % cred["expires_at"]) if cred and cred.get("expires_at") else ""
                nick = (" | %s" % cred["nickname"]) if cred and cred.get("nickname") else ""
                cred_desc = "凭证存在%s%s" % (nick, exp)
            else:
                cred_desc = "凭证缺失(需add-account)"
        elif (acc.get("token") or "").strip():
            cred_desc = "内联token"
        else:
            storage = os.path.join(cm.storage_dir, acc.get("storage_file", ""))
            cred_desc = "浏览器登录态" if os.path.exists(storage) else "无凭证/登录态"
        log.info("  [%s] %s | %s", status, acc["name"], cred_desc)


if __name__ == "__main__":
    sys.exit(main())
