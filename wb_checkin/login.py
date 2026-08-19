"""登录态获取模块（一次性操作）：拉起可见浏览器扫码登录，保存 storage_state。

两种结束方式：
- 默认：登录完成后用户在终端按回车
- --auto：轮询检测登录状态（URL 离开登录页 / Cookie 明显增多即判定成功）
"""
import time

from .logger import get_logger

_LOGIN_PATH_KEYWORDS = ("/login", "login/", "passport", "signin")


def interactive_login(
    web_base: str,
    storage_path: str,
    account_name: str,
    auto_wait: bool = False,
    wait_minutes: int = 10,
) -> bool:
    """打开浏览器让用户手动登录，成功保存登录态到 storage_path。"""
    logger = get_logger()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("未安装 playwright，请先执行：pip install playwright && playwright install chromium")
        return False

    logger.info("正在启动浏览器并打开登录页：%s", web_base)
    logger.info("账号：%s | 若浏览器未自动打开，请检查 playwright 浏览器是否已安装。", account_name)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(web_base, timeout=60000, wait_until="domcontentloaded")
        except Exception as e:
            logger.error("打开登录页失败：%s: %s", type(e).__name__, e)
            logger.info("请确认 web_base 地址正确且网络可访问。")
            browser.close()
            return False

        if auto_wait:
            ok = _wait_login_auto(page, context, wait_minutes)
        else:
            ok = _wait_login_enter(page, context)

        if not ok:
            logger.warning("未确认登录成功，未保存登录态。")
            browser.close()
            return False

        context.storage_state(path=storage_path)
        cookies = context.cookies()
        logger.info("✅ 登录态已保存：%s（Cookie %d 个）", storage_path, len(cookies))
        logger.info("下次直接执行 python main.py checkin 即可自动签到。")
        browser.close()
        return True


def _wait_login_enter(page, context) -> bool:
    """等待用户按回车确认登录完成。"""
    logger = get_logger()
    try:
        input(">> 请在浏览器中完成登录，然后回到此窗口按【回车】保存登录态...")
    except EOFError:
        # 非交互终端（如定时任务/自动化环境）：改为等待固定时间
        logger.warning("非交互终端无法读取回车，等待 15 秒后保存（请尽快完成登录）...")
        time.sleep(15)
    return True


def _wait_login_auto(page, context, wait_minutes: int) -> bool:
    """自动轮询检测登录成功。

    判定条件（任一满足即成功）：
    1. URL 跳转到登录后的页面（离开 /login/ 路径，如 /console/ 等工作台）；
    2. 页面出现"登录成功 / 扫码成功"等成功提示文本（部分入口扫码成功后不跳转）。
    同时要求 Cookie 已具备登录态（>= 5 个），避免把页面初始加载误判为登录成功。
    """
    logger = get_logger()
    deadline = time.time() + wait_minutes * 60
    logger.info("自动检测模式：请在弹出来的浏览器中完成登录（最长等待 %d 分钟）...", wait_minutes)

    # 等页面稳定加载，避免把初始跳转误判为登录成功
    time.sleep(5)

    success_text_keywords = ("登录成功", "扫码成功", "登录已成功", "扫码已成功")

    while time.time() < deadline:
        time.sleep(2)
        try:
            url = page.url or ""
            lower = url.lower()
            cookies = context.cookies()
            on_login_page = any(kw in lower for kw in _LOGIN_PATH_KEYWORDS)
            left_login = lower.startswith("https://") and not on_login_page
            body_text = ""
            if left_login:
                try:
                    body_text = page.inner_text("body")[:200]
                except Exception:
                    body_text = ""
            text_success = any(kw in body_text for kw in success_text_keywords)
            has_cookies = len(cookies) >= 5

            if (left_login or text_success) and has_cookies:
                # 判定成功后再观察 3 秒，确保跳转/文本稳定
                time.sleep(3)
                logger.info("检测到登录成功（URL=%s），正在保存登录态...", page.url or url)
                return True
        except Exception:
            continue
    logger.warning("等待 %d 分钟仍未检测到登录成功，已超时。", wait_minutes)
    return False
