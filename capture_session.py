# -*- coding: utf-8 -*-
"""
会话抓取模块：用 Playwright 驱动本机 Edge，你在弹出的浏览器里登录 12306，
登录成功后自动把会话 Cookie 保存到 session_cookies.json，供下单模块使用。

为什么用这个方式
    - 密码登录现在有网易易盾滑块验证，脚本无法绕过（也不应该绕过）
    - 扫码/手动登录 = 用户主动登录，合规且稳定
    - 登录一次后 Cookie 持久化，平时只跑 monitor.py 即可

用法
    1. 确保已安装：pip install playwright  &&  playwright install msedge（或本机已有 Edge）
    2. python capture_session.py
    3. 在弹出的 Edge 窗口里完成登录（用户名密码 + 滑块，或 App 扫码）
    4. 看到"已保存 N 个 Cookie"即成功，之后可关闭窗口

出口约定（Task 65c）：唯一的成败出口是「退出码」——0=成功，1=失败。
子进程调用方只看 returncode；在进程内调用 main() 时看它的 int 返回值。
"""

import appcommon
import json
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
# 默认落盘路径（历史值）。main() 内以 _cookie_path() 为准：优先读
# config.json 的 session_cookies_file（Task 69a）。
COOKIE_PATH = os.path.join(HERE, "session_cookies.json")

# 同名 Cookie 多 path 条目的落盘复合键分隔符（Task 69e）。
# \x1f 是 Cookie 名里不可能出现的控制字符；旧文件（纯 name 键）无此分隔符，
# order.load_session 按"有分隔符=复合键 / 无=旧格式"兼容读取。
COOKIE_KEY_SEP = "\x1f"

# _wait_for_login 的三种出口
LOGIN_OK = "ok"
LOGIN_TIMEOUT = "timeout"
LOGIN_BROWSER_CLOSED = "browser_closed"

LOGIN_URL = "https://kyfw.12306.cn/otn/resources/login.html"
CHECK_URL = "https://kyfw.12306.cn/otn/index/initMy12306Api"
MAX_WAIT_SEC = 300   # 等用户登录的最长时间


def _order_mode():
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
            return json.load(f).get("order_mode") or "http"
    except Exception:
        return "http"


def _cookie_path(config_path=None):
    """Cookie 落盘路径：优先 config.json 的 session_cookies_file。

    与 order.py / engine.py / gui.py / monitor.py 同口径
    （os.path.join(HERE, name)，绝对路径天然透传）；读不到配置时
    回退旧硬编码值 session_cookies.json（Task 69a）。
    """
    try:
        with open(config_path or os.path.join(HERE, "config.json"), encoding="utf-8") as f:
            name = json.load(f).get("session_cookies_file") or "session_cookies.json"
    except Exception:
        name = "session_cookies.json"
    if not isinstance(name, str):
        name = "session_cookies.json"
    return os.path.join(HERE, name)


def _browser_mode_login():
    """浏览器下单模式转交 browser_order 登录。

    playwright 缺失 / Edge 启动失败等异常给友好提示并返回 False，
    不把原始 traceback 抛给调用方（Task 69b，与 65e 同口径）。
    """
    sys.path.insert(0, HERE)
    try:
        import browser_order
        return bool(browser_order.login())
    except Exception as e:
        print("[失败] 浏览器登录失败：{0}".format(e))
        print("       请检查 Playwright / Edge 是否安装正确后重试。")
        return False


def _launch_browser(p):
    """启动 Edge：失败给友好提示并返回 None（Task 69c，与 65e 同口径）。"""
    try:
        return p.chromium.launch(channel="msedge", headless=False)
    except Exception as e:
        print("[失败] Edge 启动失败：{0}".format(e))
        print("       请先执行：playwright install msedge（或确认本机已安装 Edge）后重试。")
        return None


def _wait_for_login(browser, context, deadline):
    """轮询登录 Cookie，直到 tk 出现 / 超时 / 浏览器被关闭。

    浏览器被用户提前关闭时不再空转烧完 300s，而是早退并提示（Task 69d）。
    """
    while time.time() < deadline:
        time.sleep(2)
        try:
            connected = browser.is_connected()
        except Exception:
            connected = False
        if not connected:
            print("\n[失败] 检测到浏览器窗口已被关闭，停止等待登录。")
            return LOGIN_BROWSER_CLOSED
        try:
            cookies = context.cookies()
            names = [c.get("name") for c in cookies]
        except Exception:
            continue
        # 登录后 12306 会种下 tk 且带签名（有效会话的必要条件之一）
        if "tk" in names or any(n.startswith("tk") for n in names if n):
            return LOGIN_OK
    return LOGIN_TIMEOUT


def _build_cookie_dict(cookies):
    """按 (name, path, domain) 三元组去重分组，生成落盘字典。

    12306 可能对同一 Cookie 名下发多个 path（如 JSESSIONID 在 /otn 与
    /passport）：旧代码以 name 为键，后者覆盖前者（Task 69e）。
    序列化规则：某 name 只有一条目时保持纯 name 键（旧文件/旧版本可读）；
    有多条目时用复合键 "name\\x1fpath\\x1fdomain" 全部保留。
    """
    grouped = {}
    order = []
    for c in cookies or []:
        # 只保留 12306 相关域（含 .12306.cn），过滤第三方
        if "12306.cn" not in (c.get("domain") or "") or not c.get("name"):
            continue
        triple = (c["name"], c.get("path") or "/", c.get("domain") or ".12306.cn")
        if triple not in grouped:
            order.append(triple)
        # 同三元组重复出现取最后一次的值
        grouped[triple] = {
            "value": c.get("value") or "",
            "domain": triple[2],
            "path": triple[1],
        }
    hits = {}
    for t in order:
        hits[t[0]] = hits.get(t[0], 0) + 1
    out = {}
    for t in order:
        key = t[0] if hits[t[0]] == 1 else COOKIE_KEY_SEP.join(t)
        out[key] = grouped[t]
    return out


def _goto_login_page(page):
    """打开登录页：失败给友好提示并返回 False（Task 65e）。"""
    try:
        page.goto(LOGIN_URL)
        return True
    except Exception as e:
        print("[失败] 打不开 12306 登录页：{0}".format(e))
        print("       请检查网络连接 / 代理设置后重试。")
        return False


def _final_verify(page):
    """最终验证：拉取需登录接口，当且仅当拿到登录态数据返回 True。

    通过才允许保存 Cookie（Task 65d）：校验失败就存盘会把"未登录"的
    Cookie 写进 session_cookies.json，下次下单直接判"未登录"还查不出原因。
    """
    try:
        page.goto(CHECK_URL)
        time.sleep(2)
        body = page.content()
    except Exception as e:
        print("[警告] 最终验证请求失败：{0}".format(e))
        return False
    return "user_name" in body


def main():
    """抓取会话并保存 Cookie。

    出口约定（Task 65c）：唯一的成败出口是「退出码」——0=成功，1=失败。
    直接运行时 __main__ 会 sys.exit(main())；子进程调用方
    （gui._relogin_ok_via_script / monitor.menu_session）只看 returncode；
    若在进程内调用 main()，同样看它的 int 返回值——main() 内部不再 sys.exit，
    避免 import 后调用时 SystemExit 炸掉调用方进程。
    """
    # 浏览器下单模式的会话活在 .browser_profile 里，本脚本那套 session_cookies.json
    # 已经用不上了。要是还照旧开一个独立的临时浏览器，就会和 browser_order 抢
    # 同一个 profile：两个窗口你开我关，用户看到的就是"网页一直在开启关闭"。
    if _order_mode() == "browser":
        print("[提示] 当前是浏览器下单模式，转交 browser_order 登录（会话存进 .browser_profile）")
        return 0 if _browser_mode_login() else 1

    cookie_path = _cookie_path()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[FATAL] 缺少 playwright，请先执行：pip install playwright")
        return 1

    print("正在启动 Edge（channel=msedge）打开 12306 登录页...")
    print("请在弹出的浏览器窗口里登录。脚本会自动检测登录状态。")
    print("登录页面：" + LOGIN_URL)
    print()

    with sync_playwright() as p:
        browser = _launch_browser(p)
        if browser is None:
            return 1
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()
        if not _goto_login_page(page):
            browser.close()
            return 1

        # 轮询判断：登录成功后 Cookie 里会出现 tk / RAIL_EXPIRATION 等关键字段
        status = _wait_for_login(browser, context, time.time() + MAX_WAIT_SEC)
        if status == LOGIN_BROWSER_CLOSED:
            return 1
        if status != LOGIN_OK:
            print("\n[失败] 未检测到登录状态（{0}s 超时）。窗口已自动关闭，请重试。".format(MAX_WAIT_SEC))
            browser.close()
            return 1

        # 最终验证：通过才存 Cookie、才打印成功（Task 65d）。
        # 旧代码这里先打印"[成功]"、校验失败也照存——存的是未登录态 Cookie。
        print("\n[验证] 已检测到登录 Cookie，正在拉取需登录接口做最终验证...")
        if not _final_verify(page):
            print("[失败] 最终验证未通过：没取到登录态数据，Cookie 不保存。")
            print("       请确认浏览器里确实已登录成功后重试。")
            browser.close()
            return 1
        print("[成功] 最终验证通过。")

        cookies = context.cookies()
        cookie_dict = _build_cookie_dict(cookies)

        # 确保关键 cookie 在（RAIL_DEVICEID 服务端已不再下发，勿再误报）
        needed = ["tk", "JSESSIONID", "BIGipServerotn"]
        present = {k.split(COOKIE_KEY_SEP)[0] for k in cookie_dict}
        missing = [n for n in needed if n not in present]
        if missing:
            print("[警告] 以下关键 Cookie 未捕获: {0}（不影响保存，但下单可能失败）".format(missing))

        # 原子写：写一半被杀会留下半截 JSON，下次下单直接"未登录"
        appcommon.atomic_write_json(cookie_path, cookie_dict)

        browser.close()
        print("\n[完成] 已保存 {0} 个 Cookie 到 {1}".format(len(cookie_dict), cookie_path))
        print("之后直接运行：python monitor.py")
        return 0


if __name__ == "__main__":
    sys.exit(main())