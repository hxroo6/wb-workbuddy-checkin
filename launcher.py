#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb-checkin 一键启动器：启动 Web 服务并自动打开浏览器。

打包命令（Windows）：
    pyinstaller --onefile --name wb-checkin \
        --add-data "webui/index.html;webui" \
        --exclude-module playwright \
        launcher.py

打包后双击 wb-checkin.exe 即可：
    - 自动在 127.0.0.1 选一个空闲端口启动 Web 服务
    - 自动打开默认浏览器
    - 数据目录 = exe 同目录下的 .data（无需安装 Python，token 凭证模式零第三方依赖）
"""
import os
import socket
import sys
import threading
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _default_data_dir() -> str:
    """数据目录：exe（程序）同级目录下的 .data，便于整目录迁移。"""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)  # 打包后：exe 所在目录
    else:
        base = HERE  # 脚本模式：项目目录
    return os.path.join(base, ".data")


def _find_port(start: int = 8080) -> int:
    """在 8080~8089 间找一个空闲端口。"""
    for port in range(start, start + 10):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main() -> int:
    import webui

    port = _find_port()
    data_dir = _default_data_dir()
    url = "http://127.0.0.1:%d" % port
    threading.Timer(1.5, _open_browser, args=(url,)).start()
    return webui.main(["--port", str(port), "--data-dir", data_dir])


if __name__ == "__main__":
    sys.exit(main())
