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
"""

import json
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE_PATH = os.path.join(HERE, "session_cookies.json")

LOGIN_URL = "https://kyfw.12306.cn/otn/resources/login.html"
CHECK_URL = "https://kyfw.12306.cn/otn/index/initMy12306Api"
MAX_WAIT_SEC = 300   # 等用户登录的最长时间


def _order_mode():
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
            return json.load(f).get("order_mode") or "http"
    except Exception:
        return "http"


def main():
    # 浏览器下单模式的会话活在 .browser_profile 里，本脚本那套 session_cookies.json
    # 已经用不上了。要是还照旧开一个独立的临时浏览器，就会和 browser_order 抢
    # 同一个 profile：两个窗口你开我关，用户看到的就是"网页一直在开启关闭"。
    if _order_mode() == "browser":
        print("[提示] 当前是浏览器下单模式，转交 browser_order 登录（会话存进 .browser_profile）")
        sys.path.insert(0, HERE)
        import browser_order
        sys.exit(0 if browser_order.login() else 1)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[FATAL] 缺少 playwright，请先执行：pip install playwright")
        sys.exit(1)

    print("正在启动 Edge（channel=msedge）打开 12306 登录页...")
    print("请在弹出的浏览器窗口里登录。脚本会自动检测登录状态。")
    print("登录页面：" + LOGIN_URL)
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=False)
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()
        page.goto(LOGIN_URL)

        # 轮询判断：登录成功后 Cookie 里会出现 tk / RAIL_EXPIRATION 等关键字段
        deadline = time.time() + MAX_WAIT_SEC
        logged_in = False
        while time.time() < deadline:
            time.sleep(2)
            try:
                cookies = context.cookies()
                names = [c["name"] for c in cookies]
                # 登录后 12306 会种下 tk 且带签名（有效会话的必要条件之一）
                if "tk" in names or any(n.startswith("tk") for n in names):
                    logged_in = True
                    break
            except Exception:
                pass

        if not logged_in:
            print("\n[失败] 未检测到登录状态（{0}s 超时）。窗口已自动关闭，请重试。".format(MAX_WAIT_SEC))
            browser.close()
            sys.exit(1)

        print("\n[成功] 检测到已登录，正在拉取乘车人信息做最终验证...")
        page.goto(CHECK_URL)
        time.sleep(2)
        body = page.content()
        if "user_name" not in body and "登录" in body[:2000]:
            print("[警告] 登录状态可能失效，但 cookie 仍将保存，下单时会再校验。")
        try:
            page.goto("https://kyfw.12306.cn/otn/passengers/query?pageIndex=1&pageSize=10")
            time.sleep(2)
            body = page.content()
        except Exception:
            pass

        cookies = context.cookies()
        cookie_dict = {}
        for c in cookies:
            # 只保留 12306 相关域（含 .12306.cn），过滤第三方
            if "12306.cn" in (c.get("domain") or "") and c.get("name"):
                # 连 domain/path 一起存：12306 的 Cookie 作用域很严
                # （_uab_collina 只属于 /otn/resources），拍平后会变成风控特征
                cookie_dict[c["name"]] = {
                    "value": c["value"],
                    "domain": c.get("domain") or ".12306.cn",
                    "path": c.get("path") or "/",
                }

        # 确保关键 cookie 在（RAIL_DEVICEID 服务端已不再下发，勿再误报）
        needed = ["tk", "JSESSIONID", "BIGipServerotn"]
        missing = [n for n in needed if n not in cookie_dict]
        if missing:
            print("[警告] 以下关键 Cookie 未捕获: {0}（不影响保存，但下单可能失败）".format(missing))

        # 原子写：写一半被杀会留下半截 JSON，下次下单直接"未登录"
        tmp = COOKIE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cookie_dict, f, ensure_ascii=False, indent=2)
        os.replace(tmp, COOKIE_PATH)

        browser.close()
        print("\n[完成] 已保存 {0} 个 Cookie 到 {1}".format(len(cookie_dict), COOKIE_PATH))
        print("之后直接运行：python monitor.py")
        sys.exit(0)


if __name__ == "__main__":
    main()