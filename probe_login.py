# -*- coding: utf-8 -*-
"""
12306 登录链路可行性探测脚本（只做诊断，不做验证码绕过）

目的
    确认"扫码登录 + 会话持久化"这条路在 2026 年是否还能走通。
    这是整个"自动下单"方案的地基：拿不到并保持登录态，后面查票/下单都无从谈起。

它做什么
    1. 探测关键域名/接口的连通性（看是否被封 IP、是否可达）
    2. 获取基础 Cookie（JSESSIONID）
    3. 申请登录二维码，保存成图片，供你用 12306 App 扫描
    4. 轮询二维码状态
    5. 登录成功后，调用一个"需要登录"的接口来验证会话是否真的有效
    6. 把会话 Cookie 存到本地独立的 probe_cookies.json，用于验证持久化
       （不碰生产 session_cookies.json）

它不做什么
    - 不处理账号密码登录的滑块验证（那属于绕过安全验证，不做）
    - 不查票、不下单

用法
    pip install requests
    python probe_login.py

说明
    - 扫码登录需要你用 12306 App 亲手扫一次码；这是它"干净"的原因，也是多账号
      方案的代价：每个账号需要人工扫一次，之后靠持久化的 Cookie 维持。
    - 脚本会把每一步的原始响应打印出来，方便判断 12306 当前到底改了什么。
"""

import base64
import json
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    import requests
except ImportError:
    print("[FATAL] 缺少依赖：请先执行  pip install requests")
    sys.exit(1)

# ----------------------------- 配置区 -----------------------------

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://kyfw.12306.cn/otn/resources/login.html",
    "Origin": "https://kyfw.12306.cn",
    "X-Requested-With": "XMLHttpRequest",
}

QR_IMAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "login_qr.png")
# Task 44: 诊断脚本不得写生产 session_cookies.json。拍平的 {name: value}
# 会丢 domain/path（增大风控特征风险），还会覆盖生产会话。
# 诊断 Cookie 一律落到独立的 probe_cookies.json，生产文件永不触碰。
PROBE_COOKIE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_cookies.json")
POLL_TIMEOUT_SEC = 180          # 扫码轮询最长等待时间
POLL_INTERVAL_SEC = 2

SESSION = requests.Session()
SESSION.headers.update(BASE_HEADERS)


def banner(title):
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)


def show(resp, limit=300):
    """打印一次响应的关键信息 + 原始内容片段（截断）。"""
    if resp is None:
        print("    [无响应]")
        return
    print("    HTTP {status}  耗时 {elapsed:.0f}ms  长度 {length}  Content-Type={ctype}".format(
        status=resp.status_code,
        elapsed=resp.elapsed.total_seconds() * 1000,
        length=len(resp.content),
        ctype=resp.headers.get("Content-Type", "-"),
    ))
    body = resp.text.strip().replace("\n", " ")
    if len(body) > limit:
        body = body[:limit] + " ...(已截断)"
    print("    body: " + (body if body else "<空>"))


# ----------------------------- STEP 1：连通性 -----------------------------

def step1_connectivity():
    banner("STEP 1  连通性探测（判断是否可达 / 是否被限）")
    targets = [
        "https://www.12306.cn/index/",
        "https://kyfw.12306.cn/otn/login/conf",
        "https://kyfw.12306.cn/otn/index12306/getLoginBanner",
        "https://kyfw.12306.cn/passport/web/create-qr64",
    ]
    for url in targets:
        print("\n  -> " + url)
        try:
            # create-qr64 是 POST，这里先用 GET 探活即可（返回 405 也说明域名可达）
            r = SESSION.get(url, timeout=10)
            show(r)
        except Exception as e:
            print("    [异常] {0}: {1}".format(type(e).__name__, e))


# ----------------------------- STEP 2：取基础 Cookie -----------------------------

def step2_bootstrap_cookies():
    banner("STEP 2  获取基础 Cookie（JSESSIONID 等）")
    try:
        r = SESSION.get("https://kyfw.12306.cn/otn/index12306/getLoginBanner", timeout=10)
        show(r, limit=200)
    except Exception as e:
        print("    [异常] {0}: {1}".format(type(e).__name__, e))

    print("\n  当前 Cookie：")
    for c in SESSION.cookies:
        print("    {0} = {1}".format(c.name, (c.value[:40] + "...") if len(c.value) > 40 else c.value))

    if not any(c.name == "JSESSIONID" for c in SESSION.cookies):
        print("\n  [注意] 未拿到 JSESSIONID。后续扫码接口可能失败——请把上面的原始响应发出来。")
    return True


# ----------------------------- STEP 3：申请二维码 -----------------------------

def create_qr_raw():
    """静默申请二维码并覆盖保存图片，返回 uuid（失败返回 None）。用于过期后自动刷新。"""
    try:
        r = SESSION.post("https://kyfw.12306.cn/passport/web/create-qr64",
                         data={"appid": "otn"}, timeout=10)
        data = r.json()
        uuid = data.get("uuid")
        image_b64 = data.get("image")
    except Exception as e:
        print("    [异常] {0}: {1}".format(type(e).__name__, e))
        return None

    if not uuid:
        return None
    if image_b64:
        try:
            with open(QR_IMAGE_PATH, "wb") as f:
                f.write(base64.b64decode(image_b64))
        except Exception:
            pass
    return uuid


def step3_create_qr():
    banner("STEP 3  申请登录二维码")
    url = "https://kyfw.12306.cn/passport/web/create-qr64"
    try:
        r = SESSION.post(url, data={"appid": "otn"}, timeout=10)
    except Exception as e:
        print("    [异常] {0}: {1}".format(type(e).__name__, e))
        return None

    show(r)
    try:
        data = r.json()
    except Exception:
        print("\n  [失败] 返回不是 JSON。接口可能已变更，上面原始响应就是证据。")
        return None

    uuid = data.get("uuid")
    image_b64 = data.get("image")

    if not uuid:
        print("\n  [失败] 响应里没有 uuid 字段，字段列表：{0}".format(list(data.keys())))
        return None

    print("\n  uuid = " + str(uuid))
    print("  result_code = {0}  result_message = {1}".format(
        data.get("result_code"), data.get("result_message")))

    if image_b64:
        try:
            with open(QR_IMAGE_PATH, "wb") as f:
                f.write(base64.b64decode(image_b64))
            print("\n  二维码已保存到：" + QR_IMAGE_PATH)
            print("  >>> 请打开该图片，用【铁路12306 App】扫一扫，并在手机上确认登录。")
        except Exception as e:
            print("\n  [警告] 二维码图片保存失败：{0}".format(e))
    else:
        print("\n  [警告] 响应里没有 image 字段（可能改为前端 JS 生成二维码）。")

    return uuid


# ----------------------------- STEP 4：轮询扫码状态 -----------------------------

def step4_poll_qr(uuid):
    banner("STEP 4  轮询扫码状态（请现在去扫码，最长等待 {0}s）".format(POLL_TIMEOUT_SEC))
    url = "https://kyfw.12306.cn/passport/web/checkqr"
    deadline = time.time() + POLL_TIMEOUT_SEC
    last_msg = None

    while time.time() < deadline:
        try:
            r = SESSION.post(url, data={"uuid": uuid, "appid": "otn"}, timeout=10)
            data = r.json()
        except Exception as e:
            print("    [异常] {0}: {1}".format(type(e).__name__, e))
            time.sleep(POLL_INTERVAL_SEC)
            continue

        code = data.get("result_code")
        msg = data.get("result_message")

        if msg != last_msg:
            print("    result_code={0}  result_message={1}".format(code, msg))
            last_msg = msg

        # 2 = 已确认登录，响应里会带 uamtk
        if code == 2:
            print("\n  [成功] 二维码已确认，uamtk = {0}".format(data.get("uamtk")))
            return data.get("uamtk")
        # 3 = 二维码过期 —— 自动刷新，避免用户还没扫就失效
        if code == 3:
            print("\n  [刷新] 二维码已过期，正在重新申请...")
            uuid = create_qr_raw()
            if not uuid:
                print("  [失败] 刷新二维码失败，终止。")
                return None
            print("  新二维码已覆盖保存到：" + QR_IMAGE_PATH + "（请扫最新这一张）")
            last_msg = None
            continue

        time.sleep(POLL_INTERVAL_SEC)

    print("\n  [超时] 未在 {0}s 内完成扫码。".format(POLL_TIMEOUT_SEC))
    return None


# ----------------------------- STEP 5：完成登录 -----------------------------

def step5_finish_login(uamtk):
    banner("STEP 5  完成登录（换取会话 Cookie）")
    try:
        r = SESSION.post(
            "https://kyfw.12306.cn/passport/web/auth/uamtk",
            data={"appid": "otn"},
            timeout=10,
        )
        show(r)
        tk = None
        try:
            tk = r.json().get("newapptk")
        except Exception:
            pass
    except Exception as e:
        print("    [异常] {0}: {1}".format(type(e).__name__, e))
        tk = None

    if not tk:
        print("    [回退] 未能从 auth/uamtk 取到 newapptk，改用二维码返回的 uamtk 尝试。")
        tk = uamtk

    if not tk:
        return False

    try:
        r2 = SESSION.post(
            "https://kyfw.12306.cn/otn/uamauthclient",
            data={"tk": tk},
            timeout=10,
        )
        show(r2)
        try:
            j = r2.json()
            print("\n  登录返回：result_code={0}  username={1}".format(
                j.get("result_code"), j.get("username")))
            return j.get("result_code") == 0
        except Exception:
            return False
    except Exception as e:
        print("    [异常] {0}: {1}".format(type(e).__name__, e))
        return False


# ----------------------------- STEP 6：验证会话有效性 -----------------------------

def step6_verify_session():
    banner("STEP 6  验证会话是否真的有效（调用需登录接口）")
    checks = [
        ("初始化我的12306", "GET", "https://kyfw.12306.cn/otn/index/initMy12306Api", None),
        ("查询乘车人列表", "POST", "https://kyfw.12306.cn/otn/passengers/query",
         {"pageIndex": 1, "pageSize": 10}),
    ]
    ok = False
    for name, method, url, payload in checks:
        print("\n  -> {0}  ({1})".format(name, url))
        try:
            if method == "GET":
                r = SESSION.get(url, timeout=10)
            else:
                r = SESSION.post(url, data=payload, timeout=10)
            show(r)
            if r.status_code == 200 and ("passengers" in r.text or "user_name" in r.text
                                         or '"data"' in r.text):
                ok = True
        except Exception as e:
            print("    [异常] {0}: {1}".format(type(e).__name__, e))
    return ok


# ----------------------------- STEP 7：保存会话 -----------------------------

def step7_save_cookies():
    banner("STEP 7  保存会话 Cookie（验证持久化）")
    cookies = {c.name: c.value for c in SESSION.cookies}
    try:
        with open(PROBE_COOKIE_PATH, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        print("  已保存 {0} 个 Cookie 到诊断文件：{1}".format(len(cookies), PROBE_COOKIE_PATH))
        print("  （生产 session_cookies.json 未被触碰）")
        print("  Cookie 列表：" + ", ".join(cookies.keys()))
        print("\n  下一步可以验证：重开一个进程，用这些 Cookie 直接请求 STEP 6 的接口，")
        print("  如果仍然返回登录态数据，说明会话可持久化，多账号方案在地基上是成立的。")
    except Exception as e:
        print("  [失败] 保存 Cookie 出错：{0}".format(e))


def main():
    print("12306 登录链路探测 —— 开始时间 " + time.strftime("%Y-%m-%d %H:%M:%S"))
    print("Python " + sys.version.split()[0] + " / requests " + requests.__version__)
    print("提示：本机 IP 若被 12306 限制，STEP 1 就会表现为超时或非 200。")

    step1_connectivity()
    step2_bootstrap_cookies()

    uuid = step3_create_qr()
    if not uuid:
        banner("结论")
        print("  二维码接口不可用 —— 直接原因是上面 STEP 3 的原始响应。")
        print("  把 STEP 1~3 的输出发出来，就能判断是接口变更、Cookie 缺失，还是 IP 被限。")
        return

    uamtk = step4_poll_qr(uuid)
    if not uamtk:
        banner("结论")
        print("  未完成登录。若是超时，说明只是没扫码；若是过期/报错，请把输出发出来。")
        step7_save_cookies()  # 仍保存当前 Cookie，便于排查
        return

    logged_in = step5_finish_login(uamtk)
    session_ok = step6_verify_session()
    step7_save_cookies()

    banner("结论")
    if logged_in and session_ok:
        print("  [通过] 扫码登录链路可用，且会话能调用需登录接口。")
        print("  => 多账号方案的地基成立：每个账号人工扫一次码，之后靠持久化 Cookie 维持。")
        print("  => 下一步可以在这之上做「多账号会话管理 + 余票查询」。")
    elif logged_in:
        print("  [半通过] 拿到了登录态，但需登录接口没返回预期数据。")
        print("  可能是接口已变更（STEP 6 的原始响应就是证据），需要按新接口适配。")
    else:
        print("  [未通过] 扫码确认了，但换取会话失败了。STEP 5 的原始响应是关键证据。")


if __name__ == "__main__":
    main()