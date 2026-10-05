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


def main():
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
                cookie_dict[c["name"]] = c["value"]

        # 确保关键 cookie 在
        needed = ["tk", "JSESSIONID", "BIGipServerotn", "RAIL_DEVICEID"]
        missing = [n for n in needed if n not in cookie_dict]
        if missing:
            print("[警告] 以下关键 Cookie 未捕获: {0}（不影响保存，但下单可能失败）".format(missing))

        with open(COOKIE_PATH, "w", encoding="utf-8") as f:
            json.dump(cookie_dict, f, ensure_ascii=False, indent=2)

        browser.close()
        print("\n[完成] 已保存 {0} 个 Cookie 到 {1}".format(len(cookie_dict), COOKIE_PATH))
        print("之后直接运行：python monitor.py")
        sys.exit(0)


if __name__ == "__main__":
    main()