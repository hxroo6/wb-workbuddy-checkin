#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""WorkBuddy 账号一键添加脚本（客户端凭证自动读取）

用法：
    python add_account.py                      # 交互式：提示输入账号名（默认自动编号 account2/3/...）
    python add_account.py pu                   # 指定账号名 pu
    python add_account.py --data-dir "F:\..."  # 指定数据目录（默认 ~/.wb_checkin）
    python add_account.py --dry-run            # 只检测+验证，不写入配置

流程（全程自动）：
    1. 读取 WorkBuddy 客户端当前登录账号的凭证文件（workbuddy-desktop.info）
    2. 备份凭证到 <数据目录>/tokens/<账号名>.info（权限 600）
    3. 写入配置 checkin_config.json 的账号列表
    4. 调用真实接口验证凭证有效性（只读状态接口，不会误触发签到）
    5. 输出结果

典型使用：
    1. WorkBuddy 客户端登录"账号A" → 运行本脚本（或双击 add_account.bat）
    2. 客户端切换登录"账号B" → 再运行一次
    3. ... 每个账号一次，之后交给每日定时任务自动签到
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wb_checkin.config import ConfigManager  # noqa: E402
from wb_checkin import credentials  # noqa: E402
from wb_checkin.executor import CheckinExecutor  # noqa: E402

_DEFAULT_DATA_DIR = os.path.join(os.path.expanduser("~"), ".wb_checkin")


def _guess_data_dir() -> str:
    """默认数据目录：项目场景（脚本旁有 main.py）用项目 .data，否则用用户目录。

    保证双击 .bat / 命令行运行与定时任务使用同一数据目录，避免凭证写错位置。
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.exists(os.path.join(script_dir, "main.py")):
        return os.path.join(script_dir, ".data")
    return _DEFAULT_DATA_DIR


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="add-account",
        description="一键读取 WorkBuddy 客户端登录凭证并添加账号",
    )
    parser.add_argument("name", nargs="?", default=None, help="账号名（默认自动编号）")
    parser.add_argument("--data-dir", default=None, help="数据目录（默认 ~/.wb_checkin）")
    parser.add_argument("--force", action="store_true", help="账号已存在时覆盖凭证")
    parser.add_argument("--dry-run", action="store_true", help="只检测+验证，不写入任何文件")
    args = parser.parse_args(argv)

    cm = ConfigManager(args.data_dir or _guess_data_dir())
    print("数据目录：%s" % cm.data_dir)

    # ---------- 1) 检测客户端凭证 ----------
    client_file = credentials.find_client_auth_file()
    if not client_file or not os.path.exists(client_file):
        print("\n[X] 未检测到 WorkBuddy 客户端登录凭证。")
        print("    请先打开 WorkBuddy 客户端并【登录目标账号】，再运行本脚本。")
        print("    凭证路径：%%LOCALAPPDATA%%\\CodeBuddyExtension\\Data\\Public\\auth\\workbuddy-desktop.info")
        return 1

    cred = credentials.read_auth_file(client_file)
    if not cred:
        print("\n[X] 凭证文件读取失败或内容无效：%s" % client_file)
        return 1

    print("\n[OK] 检测到客户端当前登录账号：%s (uid=%s)" % (
        cred.get("nickname") or "?",
        cred.get("uid", "")[:8] or "?",
    ))
    if cred.get("expires_at"):
        print("     凭证有效期至：%s（过期后需重新在客户端登录刷新）" % cred["expires_at"])

    # ---------- 2) 确定账号名 ----------
    accounts = cm.list_accounts()
    existing_names = [a["name"] for a in accounts]
    name = args.name
    if not name:
        default = "account%d" % (len(accounts) + 1)
        try:
            name = input("\n输入账号名（回车使用默认 %s）：" % default) or default
        except EOFError:
            name = default
    name = name.strip()
    if not name:
        name = "account%d" % (len(accounts) + 1)

    # ---------- 3) 已存在处理 ----------
    if name in existing_names:
        if args.dry_run:
            print("\n[!] 账号 %r 已存在（--dry-run 模式不处理）" % name)
        elif args.force:
            print("\n[!] 账号 %r 已存在，--force 覆盖凭证备份" % name)
        else:
            try:
                ans = input("账号 %r 已存在，是否覆盖凭证？(y/N)：" % name)
            except EOFError:
                ans = "n"
            if ans.strip().lower() not in ("y", "yes"):
                print("已取消。")
                return 0

    # ---------- 4) 备份凭证（dry-run 跳过） ----------
    if not args.dry_run:
        backup = credentials.save_backup_credential(cm, name, client_file)
        if not backup:
            print("\n[X] 凭证备份失败。")
            return 1
        print("\n[OK] 凭证已备份：%s" % backup)

        # ---------- 5) 写入配置 ----------
        cfg = cm.load()
        acc = next((a for a in cfg["accounts"] if a["name"] == name), None)
        if acc is None:
            cm.add_account(name)
            cfg = cm.load()
            acc = next((a for a in cfg["accounts"] if a["name"] == name), None)
        if acc is not None:
            acc["credential_file"] = os.path.basename(backup)
            acc.pop("storage_file", None)  # token 模式不需要浏览器登录态
            cm.save(cfg)
            print("[OK] 账号 %r 已写入配置（凭证模式）" % name)
    else:
        print("\n[DRY-RUN] 跳过备份与配置写入（仅验证凭证）")

    # ---------- 6) 真实接口验证（只读状态接口，安全） ----------
    api_base = (cm.load().get("api_base") or "https://www.codebuddy.cn").rstrip("/")
    executor = CheckinExecutor(api_base=api_base, timeout=20)
    executor.auth_headers = credentials.build_auth_headers(cred)
    try:
        status, text = executor.http_caller()("/v2/billing/meter/checkin-status", executor._build_headers())
        if 200 <= status < 300:
            print("\n[OK] 凭证验证通过（HTTP %d）——账号 %r 可正常签到！" % (status, name))
            if not args.dry_run:
                print("     验证签到：python main.py --data-dir \"%s\" checkin --account %s" % (cm.data_dir, name))
        else:
            print("\n[!] 凭证已处理，但接口返回 HTTP %d：%s" % (status, (text or "")[:120]))
            print("    提示：token 可能已过期，请重新在客户端登录该账号后重试。")
    except Exception as e:
        print("\n[!] 凭证验证调用失败：%s: %s" % (type(e).__name__, e))

    if args.dry_run:
        print("\n[DRY-RUN] 未写入任何文件。确认无误后去掉 --dry-run 再运行一次即可正式添加。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
