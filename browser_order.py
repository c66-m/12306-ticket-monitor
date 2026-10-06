# -*- coding: utf-8 -*-
"""
浏览器下单模块：用 Playwright 驱动真实 Edge 完成 12306 下单。

为什么必须走浏览器
    12306 的下单请求带两类纯 HTTP 客户端生成不了的东西：
      · window.json_ua —— 网易易盾注入的设备指纹（requests 环境取不到）
      · sessionId / sig / scene=nc_login —— 阿里云滑动验证凭证
    服务端会把缺这些的请求伪装成「余票不足 / 系统繁忙 / 扣票失败」直接拒绝。
    真实浏览器具备完整运行环境，这条链路才走得通。

会话载体
    持久化浏览器 profile：.browser_profile/
    里面同时保存 cookie 与易盾设备指纹，比导出的 session_cookies.json 更稳，
    也不会因为 cookie 作用域被拍平而暴露非浏览器特征。

用法
    python browser_order.py login    # 打开浏览器登录一次（profile 持久化）
    python browser_order.py check    # 校验 profile 里的会话是否仍有效
"""

import contextlib
import functools
import json
import os
import sys
import threading
import time

try:
    import msvcrt  # Windows 文件字节锁：跨进程互斥靠它
except ImportError:  # 非 Windows 平台没有 msvcrt，跨进程锁退化为空操作
    msvcrt = None

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(HERE, ".browser_profile")
STATE_PATH = os.path.join(HERE, ".browser_state.json")
LEFT_TICKET_URL = "https://kyfw.12306.cn/otn/leftTicket/init"
LOGIN_URL = "https://kyfw.12306.cn/otn/resources/login.html"
CHECK_URL = "https://kyfw.12306.cn/otn/index/initMy12306Api"


def _log(msg):
    print(msg)
    sys.stdout.flush()


# 同一时刻只允许一个浏览器实例占用 profile。Playwright 的持久化 profile 是独占的：
# 引擎会话体检 / 弹窗刷新 / 侧边栏刷新 / 重新登录 并发时会互相把对方挤掉，
# 症状是浏览器 exitCode=21 启动即退、报 "browser has been closed"。
_BROWSER_LOCK = threading.RLock()

# 启动器（launcher.py）与监控系统（gui.py/engine.py）是各自独立的进程，却共用同一个
# .browser_profile，RLock 只管得住本进程的线程，跨进程必须再上一层文件锁。
_LOCK_PATH = os.path.join(HERE, ".browser_profile.lock")


class _ProfileLock:
    """对锁文件首字节加 Windows 独占锁（msvcrt.locking）实现跨进程互斥。

    用文件字节锁而不是"锁文件存在即占用"：进程被强杀或崩溃时操作系统会自动
    释放，不会留下删不掉、又没人认领的死锁。"""

    def __init__(self, path):
        self._path = path
        self._fd = None

    def acquire(self, timeout=0):
        if msvcrt is None:
            return True  # 非 Windows：跨进程这一层让位，仅保留进程内锁
        try:
            fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError:
            return False
        deadline = time.time() + max(0.0, timeout)
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                if time.time() >= deadline:
                    os.close(fd)
                    return False
                time.sleep(0.2)
            else:
                self._fd = fd
                return True

    def release(self):
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


_PROFILE_LOCK = _ProfileLock(_LOCK_PATH)
_LOCK_LOCAL = threading.local()

# 浏览器自动探测顺序：Edge → Chrome → Chromium → Playwright 自带 Chromium(None)。
# 上一个候选启动失败会在 profile 目录留下 Singleton* 锁，必须先清掉再试下一个。
_BROWSER_CHANNELS = ("msedge", "chrome", "chromium", None)


def _clear_profile_locks():
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        path = os.path.join(PROFILE_DIR, name)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


@contextlib.contextmanager
def exclusive(timeout=90):
    """串行化 profile 访问：本进程线程锁 + 跨进程文件锁，拿不到就抛错。

    Playwright 的持久化 profile 是独占的。启动器与监控系统分属不同进程，
    只靠 RLock 挡不住——一边下单、另一边开浏览器体检，会把下单的浏览器掐死，
    报 TargetClosedError: Target page, context or browser has been closed。"""
    depth = getattr(_LOCK_LOCAL, "depth", 0)
    if depth:
        # 同线程重入：外层已持有两把锁，直接放行（保留 RLock 语义）
        _LOCK_LOCAL.depth = depth + 1
        try:
            yield
        finally:
            _LOCK_LOCAL.depth = depth
        return
    if not _BROWSER_LOCK.acquire(timeout=timeout):
        raise RuntimeError("另一处正在使用浏览器（登录/体检/下单），请稍后重试")
    try:
        if not _PROFILE_LOCK.acquire(timeout=timeout):
            raise RuntimeError("另一个程序正在使用浏览器（登录/体检/下单），请稍后重试")
        _LOCK_LOCAL.depth = 1
        try:
            yield
        finally:
            _LOCK_LOCAL.depth = 0
            _PROFILE_LOCK.release()
    finally:
        _BROWSER_LOCK.release()


def _exclusive(lock_timeout=150):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            # 允许调用方用 lock_timeout= 覆盖等待上限（如抢票线程不想等 420 秒）
            with exclusive(timeout=kwargs.pop("lock_timeout", lock_timeout)):
                return fn(*args, **kwargs)
        return wrapper
    return deco


def busy():
    """浏览器是否正被占用（登录 / 下单 / 体检进行中）。

    GUI 与引擎用它区分"会话真的失效"和"这一轮轮不到校验"：登录窗口还开着的
    时候硬去校验，只会拿到等锁超时；若据此把侧边栏改成"未登录"，就是误报——
    明明浏览器里刚刚登录成功。

    启动器与监控系统是两个独立进程、共用同一个持久化 profile，
    所以线程锁之外还要探一次跨进程文件锁。"""
    got = _BROWSER_LOCK.acquire(blocking=False)
    if not got:
        return True
    _BROWSER_LOCK.release()
    if _PROFILE_LOCK.acquire(timeout=0):
        _PROFILE_LOCK.release()
        return False
    return True


def check_session(headless=True, timeout=90):
    """独占地开一次浏览器校验会话，返回 (ok, who)。引擎与 GUI 统一走这里。

    timeout 是抢锁上限：启动器要保证开抢前登录就绪，给足 90 秒；引擎 / GUI 的
    周期性体检是被动行为，传小值（6 秒）快速让位给正在下单的那一方。"""
    with exclusive(timeout=timeout):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx = launch(p, headless=headless)
            try:
                ok, who = session_ok(ctx)
            finally:
                save_state(ctx)
                try:
                    ctx.close()
                except Exception:
                    pass
        return ok, who


def launch(p, headless=False, restore=True):
    """打开持久化 profile，并把上次保存的会话注入回去。

    注意：tk / uamtk / JSESSIONID 都是 session cookie，浏览器一关就清空
    （persistent context 关闭等同关浏览器），只有设备指纹类的持久 cookie 能留下。
    而 launch_persistent_context 不接受 storage_state 参数，所以会话只能
    读文件后用 add_cookies 手动注回，否则下次启动就是"未登录"。"""
    errs = []
    for ch in _BROWSER_CHANNELS:
        kwargs = dict(
            headless=headless,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        if ch:
            kwargs["channel"] = ch
        try:
            ctx = p.chromium.launch_persistent_context(PROFILE_DIR, **kwargs)
            break
        except Exception as e:
            errs.append("%s: %s" % (ch or "chromium", type(e).__name__))
            _clear_profile_locks()
            time.sleep(0.5)
    else:
        raise RuntimeError("浏览器启动失败（已尝试 %s）：%s" % (
            "/".join(str(c) if c else "chromium" for c in _BROWSER_CHANNELS), "; ".join(errs)))
    if restore and os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                saved = json.load(f)
            cookies = saved.get("cookies") if isinstance(saved, dict) else saved
            now = time.time()
            alive = [c for c in (cookies or [])
                     if not c.get("expires") or c["expires"] < 0 or c["expires"] > now]
            if alive:
                ctx.add_cookies(alive)
        except Exception:
            pass
    return ctx


def save_state(ctx):
    """把当前会话（含 session cookie）落盘，供下次恢复。

    关键票据（tk / uamtk）已丢失时不覆盖已有存档：会话被踢后若不设防，
    一次坏状态就会把原本还能恢复的好存档永久盖掉。
    """
    try:
        cookies = ctx.cookies()
        if not ({"tk", "uamtk"} & {c.get("name") for c in cookies}):
            if os.path.exists(STATE_PATH):
                return False
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"cookies": cookies}, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def session_ok(ctx, page=None):
    """校验登录态。返回 (ok, who)。

    用真实导航、而不是 APIRequestContext：后者不带浏览器指纹，
    12306 会对它返回风控 HTML，把有效会话误判成失效。

    page: 传入已有的 about:blank 页即可省掉一次开标签页；不传则自己开一个
    （login() 里必须自己开，用户正占着主标签页）。"""
    own = page is None
    if own:
        page = ctx.new_page()
    try:
        # wait_until 必须是 domcontentloaded：12306 的 load 事件要等一票第三方
        # 资源，默认的 "load" 经常把超时耗光，表现为"校验卡住不动"。
        page.goto(CHECK_URL, timeout=20000, wait_until="domcontentloaded")
        raw = page.evaluate("() => document.body.innerText")
        data = json.loads(raw)
        if data.get("status"):
            return True, (data.get("data") or {}).get("user_name")
        return False, "接口返回 status=false（会话已失效）"
    except Exception as e:
        return False, "校验异常: %s" % str(e)[:100]
    finally:
        if own:
            try:
                page.close()
            except Exception:
                pass


@_exclusive(lock_timeout=420)
def login(timeout_sec=300, stop_event=None):
    """打开浏览器让用户完成登录；profile 会自动持久化，之后不必重复。

    stop_event: threading.Event，置位后尽快关浏览器返回 False（抢票线程的
    「停止」按钮不必再干等登录超时）。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = launch(p, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        _log("请在打开的浏览器里完成登录（扫码或账号密码+滑块），最多等 %d 秒。" % timeout_sec)
        page.goto(LOGIN_URL)
        deadline = time.time() + timeout_sec
        next_probe = 0.0
        ok, who = False, ""
        while time.time() < deadline:
            if stop_event is not None and stop_event.wait(2):
                ctx.close()
                _log("[停止] 登录已被手动停止。")
                return False
            now = time.time()
            landed = False
            try:
                for pg in ctx.pages:
                    u = pg.url or ""
                    if "initMy12306" in u or "/otn/view/" in u or "/otn/index" in u:
                        landed = True
                        break
            except Exception:
                pass
            # 绝对不能每 2 秒调一次 session_ok：它是"开新标签页 -> 导航 -> 关闭"，
            # 一秒钟就闪一次，用户看到的就是"网页一直开启关闭"。改成先看 URL，
            # 命中才做一次真实校验，再配 30 秒兜底。
            if landed or now >= next_probe:
                next_probe = now + 30
                ok, who = session_ok(ctx)
                if ok:
                    break
        if ok:
            save_state(ctx)
            _log("[成功] 已登录：%s" % who)
            _log("[提示] 会话已保存到 %s，之后无需重复登录。" % STATE_PATH)
            ctx.close()
            return True
        ctx.close()
        _log("[超时] 未检测到登录状态，请重试。")
        return False


def _safe_close_ctx(ctx):
    """下单收尾：先落盘会话（cookie 会滚动更新），再关窗。"""
    save_state(ctx)
    try:
        ctx.close()
    except Exception:
        pass


def _record_timing(tm, total, ok, msg, info, seat_name, warm):
    """把一次下单的各阶段耗时追加到 logs/order_timing.jsonl。

    「记录速度最快的路径和方法」的落地：每次下单都留一条，事后比对冷启动与
    预热、各车次各席别的阶段耗时，找出真正拖后腿的那一步。纯旁路，
    写不进去也绝不影响下单本身。"""
    try:
        d = os.path.join(HERE, "logs")
        os.makedirs(d, exist_ok=True)
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "train": (info or {}).get("train_code"),
            "seat": seat_name,
            "warm": bool(warm),
            "ok": bool(ok),
            "total": round(total, 3),
            "msg": (msg or "")[:140],
            "phases": tm,
        }
        with open(os.path.join(d, "order_timing.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# 查询命中判定，两档：
#   _QUERY_ROW_JS —— 只要车次行渲染出来即可（预热用，此时多半无票，没有「预订」）
#   _QUERY_HIT_JS —— 还要有「预订」按钮，才是真能下单的信号
_QUERY_ROW_JS = """(code) => {
    const tb = document.querySelector('#queryLeftTable');
    if (!tb) return false;
    const ld = document.querySelector('#queryLoading');
    if (ld && ld.offsetParent !== null) return false;
    const rows = Array.from(tb.querySelectorAll('tr'));
    if (!code) return rows.length > 0;
    return rows.some(tr => tr.innerText.indexOf(code) >= 0);
}"""

_QUERY_HIT_JS = """(code) => {
    const tb = document.querySelector('#queryLeftTable');
    if (!tb) return false;
    const ld = document.querySelector('#queryLoading');
    if (ld && ld.offsetParent !== null) return false;
    return Array.from(tb.querySelectorAll('tr')).some(
        tr => tr.innerText.indexOf(code) >= 0
           && tr.innerText.indexOf('预订') >= 0);
}"""


def _goto_and_query(page, info, date, want_hit=True, timeout=20000, wait_code=None):
    """导航到车票页、填条件、点查询，等目标车次渲染出来。返回是否命中。

    want_hit=True 要等「预订」按钮出现（真能下单）；False 只要该车次行在
    （预热用，开抢前多半无票，没有「预订」）。wait_code 缺省用 info["train_code"]，
    传 None 则任意一行都算（预热兜底）。"""
    page.goto(LEFT_TICKET_URL, wait_until="domcontentloaded")
    # attached 就够：站名是 JS 注入隐藏字段，不需要文本框可见
    page.wait_for_selector("#fromStationText", state="attached", timeout=10000)
    page.evaluate(
        """(d) => {
            const set = (sel, v) => { const e = document.querySelector(sel); if (e) e.value = v; };
            set('#fromStation', d.fc); set('#fromStationText', d.fn);
            set('#toStation', d.tc);   set('#toStationText', d.tn);
            set('#train_date', d.date);
        }""",
        {"fc": info["from_code"], "fn": info["from_name"],
         "tc": info["to_code"], "tn": info["to_name"], "date": date})
    page.evaluate("() => { const e = document.querySelector('#query_ticket'); if (e) e.click(); }")
    js = _QUERY_HIT_JS if want_hit else _QUERY_ROW_JS
    arg = info["train_code"] if wait_code is None else wait_code
    try:
        page.wait_for_function(js, arg=arg, timeout=timeout)
        return True
    except Exception:
        return False


def _order_impl(info, seat_name, seat_code, passenger_names, date,
                headless=False, verify_timeout=90, purpose="ADULT", warm=None, tm=None,
                alias_name=None, purpose_map=None):
    """
    一次下单的完整流程。返回 (ok, msg, extra)。

    info: ticket.parse_row 的结果（用 train_code / from_name / to_name / from_code / to_code）
    seat_name / seat_code: 席别中文名与代码（如 无座 / WZ）
    passenger_names: 优先勾选的乘车人姓名（为空则勾第一位）
    date: 乘车日期 YYYY-MM-DD
    purpose: 票种。ADULT=成人票（确认页 ticket_type=1），0X00=学生票（=3）。
        确认页默认按乘车人档案里的票种走，不显式写就会沿用档案；档案是学生票
        而资质未核验时提交必被拒——这正是「订票失败」最常见的真因。
    purpose_map: {姓名: 票种代码}，按每个乘车人分别对齐票种（可混选）。
        未覆盖到的乘车人用 purpose 兜底。
    warm: WarmSession 实例。命中则跳过开窗口 / 校验会话 / 导航 / 填条件这几步，
        直接在这张已经预热好的列表页上点「预订」（需求 2）。
    tm: 调用方给的耗时字典，边跑边填各阶段秒数（需求 3）。
    alias_name: 同价改判后的席别名（勾选无座 → 硬座）。非空表示 seat_code 已经是
        改判后的代码，失败时按「改判后仍不可售」上报，不要当成配置错误永久跳过。
    """
    from playwright.sync_api import sync_playwright

    tm = tm if tm is not None else {}
    t_last = [time.perf_counter()]

    def mark(name):
        now = time.perf_counter()
        tm[name] = round(now - t_last[0], 3)
        t_last[0] = now

    if warm is not None and not warm.usable():
        # 预热现场已失效（页面被关 / 跨了线程），先释放它占着的锁再冷启动
        try:
            warm.close()
        except Exception:
            pass
        warm = None
    reuse = warm is not None
    own_ctx = not reuse

    with contextlib.ExitStack() as stack:
        if reuse:
            p, ctx, page = warm.p, warm.ctx, warm.page
            page.set_default_timeout(20000)
        else:
            p = stack.enter_context(sync_playwright())
            ctx = launch(p, headless=headless)
            stack.callback(_safe_close_ctx, ctx)   # 任何 return 都会收尾
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(20000)
        mark("open")
        try:
            if own_ctx:
                ok, who = session_ok(ctx, page=page)
                if not ok:
                    return (False,
                            "浏览器会话不可用（%s）。请先运行：python browser_order.py login" % who,
                            None)
                _log("  [浏览器] 会话有效：%s" % who)
            else:
                _log("  [浏览器] 复用预热现场（跳过开窗口 / 校验 / 导航）")
            mark("session")

            # 1) 填条件 + 2) 查询。冷启动走完整导航；预热路径整页刷新一次
            #    （上次下单失败后页面可能停在结果页，refresh 会重新导航回列表页）。
            if own_ctx:
                _goto_and_query(page, info, date)
                _log("  [浏览器] 条件：%s -> %s %s" % (info["from_name"], info["to_name"], date))
            else:
                warm.refresh(info)
                _log("  [浏览器] 预热刷新：%s -> %s %s" % (info["from_name"], info["to_name"], date))
            mark("goto")
            mark("query")

            # 3) 点该车次的「预订」
            book = page.locator(
                "#queryLeftTable tr:has-text('%s') a:has-text('预订')" % info["train_code"]).first
            if book.count() == 0:
                return False, "页面上没有 %s 的『预订』按钮（可能已无票或不可预订）" % info["train_code"], None
            book.click(timeout=8000, no_wait_after=True)
            _log("  [浏览器] 已点 %s 的『预订』" % info["train_code"])
            mark("book")

            # 4) 确认订单页。官方进页后会异步填 limit_tickets（乘车人/席别/票种
            #    的内部状态），等这个数组就绪即可，不必死睡 2.5 秒。
            # commit：导航一发起就走，不必等页面 load（后面有 limit_tickets 兜底）
            # 账号有未支付/未处理订单时，12306 会拦住预订、页面留在列表页并弹
            # 「您还有未处理的订单」——这里识别它，别再白等 40 秒超时。
            try:
                page.wait_for_url("**/confirmPassenger/initDc**",
                                  wait_until="commit", timeout=30000)
            except Exception:
                try:
                    body = page.evaluate("() => document.body ? document.body.innerText : ''")
                except Exception:
                    body = ""
                if any(k in body for k in ("未处理", "未支付", "未完成订单", "行程冲突")):
                    i = max(0, body.find("订单") - 30)
                    mark("result")
                    return False, body[i:i + 200].replace("\n", " "), {"reason": "dup"}
                raise
            page.wait_for_selector("#normal_passenger_id input[type=checkbox]",
                                   state="attached", timeout=40000)
            page.wait_for_function(
                "() => Array.isArray(window.limit_tickets) && window.limit_tickets.length > 0",
                timeout=15000)
            _log("  [浏览器] 已进入确认订单页")
            mark("confirm")

            # 5) 勾选乘车人。列表是异步渲染的 <ul>，且外层可能不可见，
            #    所以用 JS 直接 click（等价用户点 label），不走 Playwright 可见性检查。
            people = page.eval_on_selector_all(
                "#normal_passenger_id input[type=checkbox]",
                """els => els.map(e => ({
                     id: e.id,
                     text: (e.nextElementSibling ? e.nextElementSibling.innerText : '').trim()
                   }))""")
            if not people:
                return False, "确认页没有可用乘车人（账号里没保存常用联系人？）", None

            targets = []
            for name in (passenger_names or []):
                for p in people:
                    if p["text"].startswith(name):
                        targets.append(p)
                        break
            if not targets:
                targets = [people[0]]

            picked = []
            for t in targets:
                page.evaluate(
                    "(id) => { const e = document.getElementById(id); if (e && !e.checked) e.click(); }",
                    t["id"])
                picked.append(t["text"])
            # 勾选是即时的，等状态落地即可（正常几十毫秒），不再固定睡 1.5 秒
            try:
                page.wait_for_function(
                    "(ids) => ids.every(i => { const e = document.getElementById(i);"
                    " return e && e.checked; })",
                    arg=[t["id"] for t in targets], timeout=5000)
            except Exception:
                pass
            _log("  [浏览器] 乘车人：%s" % "、".join(picked))
            mark("passenger")

            if alias_name:
                _log("  [浏览器] 席别：勾选 %s 网页端不下发，按同价改判为 %s（%s）下单"
                     % (seat_name, alias_name, seat_code))
            # 6) 选席别：确认页的席别是 <select id="seatType_1">，
            #    选项由服务端按该车次可售席别下发（网页端不一定有无座）。
            seat = page.locator("#seatType_1").first
            if seat.count() == 0:
                return False, "确认页找不到席别下拉 #seatType_1（页面结构可能已变）", None
            opts = seat.evaluate(
                "e => Array.from(e.options).map(o => ({v: o.value, t: o.text.trim()}))")
            codes = [o["v"] for o in opts]
            if seat_code not in codes:
                readable = "、".join("%s(%s)" % (o["t"].split("（")[0], o["v"]) for o in opts)
                if alias_name:
                    return (False,
                            "勾选 %s，已按同价改判为 %s，但该车次网页端下单页不下发 %s（%s）；可选：%s"
                            % (seat_name, alias_name, alias_name, seat_code, readable),
                            {"reason": "alias_no_stock", "seat_codes": codes,
                             "seat_options": readable, "alias_seat": alias_name})
                return (False,
                        "该车次网页端不提供席别 %s（%s）；可选：%s" % (seat_name, seat_code, readable),
                        {"reason": "seat_unavailable", "seat_codes": codes,
                         "seat_options": readable})
            # 用 jQuery 走官方事件链。官方下单读的是页面内部状态
            # limit_tickets[i].seat_type，只改 <select> 的 DOM 值不会同步它，
            # 结果就是界面看着是硬座、提交上去却是下拉的初始值（硬卧）。
            diag = page.evaluate(
                """(code) => {
                    const el = document.getElementById('seatType_1');
                    const d = {before: el ? el.value : null};
                    // 1) 先走官方 DOM + 事件链，尽量让页面自身保持一致
                    if (typeof jQuery !== 'undefined') {
                        jQuery('#seatType_1').val(code).trigger('change');
                        d.afterVal = el ? el.value : null;
                    }
                    if (typeof upadateSavePassengerInfo === 'function') {
                        upadateSavePassengerInfo();
                        d.calledSave = true;
                    }
                    d.afterSave = (window.limit_tickets || []).map(t => t.seat_type);
                    // 2) 实测官方页面不会随 change 更新 limit_tickets（seat_type 停在
                    //    初始值），而提交时 getpassengerTickets() 恰恰读它拼
                    //    passengerTickets —— 不写这里，界面显示硬座、提交却是硬卧。
                    if (Array.isArray(window.limit_tickets)) {
                        window.limit_tickets.forEach(t => { t.seat_type = code; });
                    }
                    d.lt = (window.limit_tickets || []).map(t => t.seat_type);
                    return d;
                }""", seat_code)
            _log("  [浏览器] 席别诊断：%s" % json.dumps(diag, ensure_ascii=False))
            # 直接等"内部状态已切到目标席别"——这正是下面那句权威判据要查的东西，
            # 等它成立就不用闭眼睡 1.5 秒。
            try:
                page.wait_for_function(
                    "(code) => (window.limit_tickets || []).every(t => t.seat_type === code)",
                    arg=seat_code, timeout=5000)
            except Exception:
                pass

            # 权威判据：直接读官方下单所用的内部状态数组
            tickets = page.evaluate(
                """() => (window.limit_tickets || []).map(t => ({
                     name: t.name, seat: t.seat_type, ticket_type: t.ticket_type
                   }))""")
            _log("  [浏览器] 内部状态 limit_tickets = %s" % json.dumps(tickets, ensure_ascii=False))
            seats_now = [t.get("seat") for t in tickets]
            if seats_now:
                if any(s != seat_code for s in seats_now):
                    return (False,
                            "席别未同步到页面内部状态（期望 %s，实际 %s）。已中止，未提交订单。" % (
                                seat_code, seats_now),
                            {"limit_tickets": tickets})
                _log("  [浏览器] 席别已确认：%s（%s）× %d" % (seat_name, seat_code, len(seats_now)))
            else:
                # 读不到内部状态（可能是私有变量）时退回 DOM 校验
                now_seat = seat.input_value()
                if now_seat != seat_code:
                    return (False,
                            "席别选择未生效（DOM=%s，期望 %s）。已中止，未提交订单。" % (
                                now_seat, seat_code),
                            None)
                _log("  [浏览器] 席别：%s（%s，DOM 校验）" % (seat_name, seat_code))
            mark("seat")

            # 6.5) 票种：确认页默认按乘车人档案取票种（学生档案会带出学生票 ticket_type=3），
            #    不显式写就沿用档案；学生票要资质核验，没核验提交必被拒——这正是
            #    之前连续「订票失败」的真因。这里按每个乘车人**各自的**票种强制对齐
            #    （成人票=1，学生票=3），支持同一订单里成人票 / 学生票混选。
            tt_default = "1" if purpose == "ADULT" else "3"
            tt_map = {n: ("3" if c == "0X00" else "1")
                      for n, c in (purpose_map or {}).items()}
            tt_args = {"code": tt_default, "map": tt_map}
            tt_diag = page.evaluate(
                """(args) => {
                    const code = args.code, map = args.map || {};
                    const pick = (name) => (name && map[name]) || code;
                    const d = {before: (window.limit_tickets || []).map(t => t.ticket_type),
                               map: map};
                    // 每个乘车人一行、每行一个 ticketType 下拉：优先按行内身份证/姓名对齐，
                    // 取不到就按行号兜底。静默改 select 值：不派发 change 事件——
                    // change handler 会创建「学生票询问」弹窗（dialog_xsertcj）并把票种改回去。
                    const lt = window.limit_tickets || [];
                    const sels = Array.from(
                        document.querySelectorAll('select[id^="ticketType_"]'));
                    sels.forEach((sel, i) => {
                        let nm = null;
                        const row = sel.closest('tr') || sel.parentElement;
                        if (row) {
                            const inp = row.querySelector(
                                'input[id^="passenger_name"], input[name^="passenger_name"]');
                            if (inp) nm = (inp.value || '').trim();
                        }
                        if (!nm && lt[i]) nm = lt[i].name;
                        sel.value = pick(nm);
                    });
                    if (typeof upadateSavePassengerInfo === 'function') {
                        upadateSavePassengerInfo();
                    }
                    // 内存数组才是官方下单所用的权威状态：DOM 没对齐也按姓名逐人写一遍
                    if (Array.isArray(window.limit_tickets)) {
                        window.limit_tickets.forEach(t => { t.ticket_type = pick(t.name); });
                    }
                    d.after = (window.limit_tickets || []).map(t => t.ticket_type);
                    return d;
                }""", tt_args)
            _log("  [浏览器] 票种诊断：%s" % json.dumps(tt_diag, ensure_ascii=False))
            try:
                page.wait_for_function(
                    """(args) => (window.limit_tickets || []).every(
                         t => String(t.ticket_type) === ((args.map || {})[t.name] || args.code))""",
                    arg=tt_args, timeout=3000)
            except Exception:
                pass
            mark("ticket_type")

            # 7) 提交订单。#submitOrder_id 是 <a href="javascript:">，
            #    官方启用逻辑给它加的正是 btn92s（不是禁用），Playwright 的
            #    可点击性判定会一直卡住，所以直接触发 DOM click。
            #    新版提交链（已逆向 passengerInfo_js.js）：
            #    submitOrder_id → checkOrderInfo → getQueueCount →
            #    弹核对窗(checkticketinfo_id) → #qr_submit_id 倒计时约 3 秒
            #    （btn92 禁用 → btn92s 启用并绑定 qr_submitClickEvent）→
            #    M("N") → confirmSingle(ForQueue) → payOrder/init。
            #    倒计时没结束就点 qr_submit 无效——这正是此前「提交后 90 秒
            #    无结果」卡死的根因：旧选择器永远匹配不到它。
            page.evaluate(
                "() => { const e = document.querySelector('#submitOrder_id'); if (e) e.click(); }")
            _log("  [浏览器] 已点『提交订单』，等核对窗确认按钮启用...")

            def _qr_state():
                try:
                    return page.evaluate(
                        """() => {
                            const e = document.querySelector('#qr_submit_id');
                            if (!e) return {found: false, cls: ''};
                            const r = e.getBoundingClientRect();
                            return {found: true, cls: e.className,
                                    shown: r.width > 0
                                           && getComputedStyle(e).display !== 'none'};
                        }""")
                except Exception:
                    return {"found": False, "cls": ""}

            def _slide_up():
                try:
                    return page.evaluate(
                        """() => Array.from(document.querySelectorAll(
                              '#slide_passcode, .nc-container, .yzm, #randCodeForm_id'))
                              .filter(e => e.getBoundingClientRect().width > 0
                                        && getComputedStyle(e).display !== 'none')
                              .map(e => e.id || e.className)""")
                except Exception:
                    return []

            def _visible_ok_btns():
                try:
                    return page.eval_on_selector_all(
                        "div.lay-btn a, div.lay-btn span, #i-ok, #check_ticketInfo_id,"
                        " #lay-btn_id, #confirmDiv a, #confirmDiv span, #popup a, #popup span",
                        """els => els.filter(e => e.offsetWidth || e.offsetHeight)
                              .map(e => ({id: e.id, cls: e.className,
                                          txt: (e.innerText || '').trim().slice(0, 12)}))""")
                except Exception:
                    return []

            clicked = False
            dlg_seat = ""     # 12306 核对窗里显示的席别（取证：万一与所选不一致，日志里有原文）
            t_confirm = time.time()
            while time.time() - t_confirm < 15.0:
                # 学生票询问弹窗（学生档案买成人票时出现）：点「取消」= 按成人票继续
                try:
                    page.evaluate(
                        """() => {
                            const w = document.querySelector('#dialog_xsertcj');
                            if (w && w.getBoundingClientRect().width > 0
                                  && getComputedStyle(w).display !== 'none') {
                                const c = document.querySelector('#dialog_xsertcj_cancel');
                                if (c) c.click();
                            }
                        }""")
                except Exception:
                    pass
                st = _qr_state()
                if st["found"] and st["shown"] and "btn92s" in st["cls"]:
                    # 点确认前先把 12306 自己渲染的核对窗原文抄下来：它是服务端下发
                    # 的载荷，若与我们所选席别不一致，只有日志里留了原文才能对账。
                    try:
                        dlg_text = page.evaluate(
                            r"""() => {
                                const ids = ['checkticketinfo_id', 'lay-box_id',
                                             'orderResultInfo_id', 'popup', 'confirmDiv'];
                                const out = [];
                                for (const id of ids) {
                                    const e = document.getElementById(id);
                                    if (e && e.getBoundingClientRect().width > 0)
                                        out.push(id + ': ' + (e.innerText || '')
                                                 .replace(/\s+/g, ' ').trim().slice(0, 300));
                                }
                                return out.join(' | ');
                            }""")
                    except Exception:
                        dlg_text = ""
                    if dlg_text:
                        _log("  [浏览器] 核对窗原文：%s" % dlg_text)
                        others = [w for w in ("无座", "硬座", "硬卧", "软卧", "二等座",
                                              "一等座", "商务座")
                                  if w in dlg_text and w not in (seat_name, alias_name or "")]
                        if others:
                            dlg_seat = others[0]
                            _log("  [浏览器] 注意：核对窗显示席别 %s，与本次提交的 %s 不一致"
                                 "（同价席别，照常提交，结果以订单详情为准）" % (dlg_seat, seat_name))
                    page.evaluate(
                        "() => { const e = document.querySelector('#qr_submit_id'); if (e) e.click(); }")
                    _log("  [浏览器] qr_submit 倒计时结束已启用（%.1fs），已点确认" % (
                        time.time() - t_confirm))
                    clicked = True
                    break
                if _slide_up():
                    return (False,
                            "触发滑块验证，请在浏览器窗口手动完成（脚本不绕过验证码）",
                            {"need_captcha": True})
                page.wait_for_timeout(200)
            if not clicked:
                # 旧版页面回退：点可见确认控件里的「确定/确认」
                btns = _visible_ok_btns()
                _log("  [浏览器] 未等到 qr_submit，回退旧确认控件：%s" % json.dumps(
                    btns, ensure_ascii=False))
                for b in btns:
                    if b["txt"] in ("确定", "确认", "是", "好的"):
                        page.evaluate("(id) => { const e = document.getElementById(id); if (e) e.click(); }", b["id"])
                        _log("  [浏览器] 已点确认框：%s" % b["txt"])
                        clicked = True
                        break
                if not clicked and btns:
                    page.evaluate("(b) => { const e = document.getElementById(b.id); if (e) e.click(); }", btns[0])
                    _log("  [浏览器] 已点第一个确认控件：%s" % btns[0]["txt"])
                    clicked = True
            _log("  [浏览器] 等待出票结果...")
            mark("submit")            # 8) 判定结果
            deadline = time.time() + verify_timeout
            tick = 0
            t_submit = time.time()
            _dup_keys = ("未支付", "未完成订单", "未处理", "已预订", "行程冲突", "已购买")
            _fail_keys = ("订票失败", "出票失败", "余票不足", "系统繁忙") + _dup_keys
            while time.time() < deadline:
                # 250ms 一轮：出票结果通常 1-3 秒内 URL 就会变
                page.wait_for_timeout(250)
                tick += 1
                # 滑块检测要两次 IPC，降频到每 4 轮（约 1 秒）查一次；第 1 轮先查
                if tick % 4 == 1:
                    sp = page.locator("#slide_passcode").first
                    if sp.count() and sp.is_visible():
                        return (False,
                                "触发滑块验证，请在浏览器窗口手动完成（脚本不绕过验证码）",
                                {"need_captcha": True})
                url = page.url
                if any(k in url for k in ("payOrder", "MyOrderNoComplete", "order/init")):
                    _log("  [浏览器] 结果页用时 %.1fs（%d 轮）" % (time.time() - t_submit, tick))
                    # 到付款页后核对页面上真实显示的席别，防止静默买错
                    seat_on_page = ""
                    try:
                        body = page.evaluate("() => document.body.innerText").replace("\n", " ")
                        for kw in (seat_name, "无座", "硬座", "硬卧", "软卧", "二等座", "一等座", "商务座"):
                            if kw and kw in body:
                                seat_on_page = kw
                                break
                    except Exception:
                        pass
                    warn = ""
                    if seat_on_page and seat_on_page != seat_name:
                        warn = "（注意：页面显示席别为 %s，与期望的 %s 不一致，请核对订单）" % (
                            seat_on_page, seat_name)
                    if dlg_seat and dlg_seat != seat_name:
                        warn += "（核对窗曾显示席别 %s，请以订单详情为准）" % dlg_seat
                    mark("result")
                    return True, "已提交订单（未支付）：%s%s" % (url, warn), {
                        "passengers": "、".join(picked), "seat_on_page": seat_on_page,
                        "seat_in_dialog": dlg_seat}
                # 失败提示：500ms 一轮只做一次 innerText 读。原来 8 次
                # locator.count() 放大成 8 次同步 IPC，页面跳转期排队会加重卡顿
                if tick % 2 == 0:
                    try:
                        _body = page.evaluate("() => document.body ? document.body.innerText : ''")
                    except Exception:
                        _body = ""
                    hit = next((t for t in _fail_keys if t in _body), None)
                    if hit:
                        i = max(0, _body.find(hit) - 30)
                        body = _body[i:i + 200].replace("\n", " ")
                        mark("result")
                        if any(k in _body for k in _dup_keys):
                            # 不是「没抢到」，是「账号已有（未支付）订单」——
                            # 再重试只会得到同样的拒绝，必须停下让用户去支付
                            return False, body, {"reason": "dup"}
                        return False, body, None
            mark("result")
            return False, "提交后 %d 秒未收到明确结果（当前页：%s）" % (verify_timeout, page.url), None
        except Exception as e:
            mark("result")
            return False, "浏览器下单异常: %s: %s" % (type(e).__name__, str(e)[:180]), None
        # 无 finally：冷启动路径的收尾（save_state + ctx.close）挂在 ExitStack
        # 回调上，预热路径不关窗、留给 WarmSession 继续用。


@_exclusive(lock_timeout=180)
def order_via_browser(info, seat_name, seat_code, passenger_names, date,
                      headless=False, verify_timeout=90, purpose="ADULT", warm=None,
                      alias_name=None, purpose_map=None):
    """用真实浏览器完成一次下单。返回 (ok, msg, extra)。

    每次下单都往 logs/order_timing.jsonl 追加一条分阶段耗时（需求 3），
    传 warm=WarmSession 可复用预热现场（需求 2）。
    alias_name: 同价改判目标席别名（勾选无座 → 硬座）；非空时会在 extra 里标注
        alias_seat / selected_seat，方便上层如实记账。
    purpose_map: {姓名: 票种代码}，按每个乘车人分别对齐成人票 / 学生票。"""
    tm = {}
    t0 = time.perf_counter()
    try:
        ok, msg, extra = _order_impl(info, seat_name, seat_code, passenger_names,
                                     date, headless=headless,
                                     verify_timeout=verify_timeout,
                                     purpose=purpose, warm=warm, tm=tm,
                                     alias_name=alias_name, purpose_map=purpose_map)
    except RuntimeError:
        raise  # 抢锁失败等：交给调用方决定重试节奏
    except Exception as e:
        ok, msg, extra = False, "浏览器下单异常: %s: %s" % (type(e).__name__, str(e)[:180]), None
    if alias_name:
        extra = dict(extra or {})
        extra.setdefault("alias_seat", alias_name)
        extra.setdefault("selected_seat", seat_name)
        if ok:
            msg = "%s（勾选 %s，网页端同价按 %s 下单）" % (msg, seat_name, alias_name)
    _record_timing(tm, time.perf_counter() - t0, ok, msg, info, seat_name, warm)
    return ok, msg, extra


class WarmSession:
    """开抢前预热好的「下单现场」：浏览器已开、已登录、车票页条件已填、查询已发。

    到点时 order_via_browser(warm=...) 只做「刷新 → 点预订 → 提交」，把冷启动
    的 10~15 秒准备压到 2 秒上下。生命周期内一直持有 profile 锁（手动 acquire，
    不走 with exclusive，因为要跨函数长持有）；close() 必须被调用，否则别的
    进程会被一直挡在门外。进程被强杀时 OS 自动释放文件锁，不会留死锁。"""

    def __init__(self, info, date, headless=False, timeout=120, wait_code=None):
        self.info = info
        self.date = date
        self._p = None
        self._ctx = None
        self._page = None
        self._closed = False
        self._local_locked = False
        self._file_locked = False
        self._owner = threading.get_ident()
        self._timeout = timeout
        self.error = None
        self.ready = False
        self._setup(headless, wait_code)

    # ---- 供 _order_impl 复用现场时直接取用（只读别名） ----

    @property
    def p(self):
        return self._p

    @property
    def ctx(self):
        return self._ctx

    @property
    def page(self):
        return self._page

    def _setup(self, headless, wait_code):
        try:
            if not _BROWSER_LOCK.acquire(timeout=self._timeout):
                raise RuntimeError("另一处正在使用浏览器（登录/体检/下单），预热失败")
            self._local_locked = True
            if not _PROFILE_LOCK.acquire(timeout=self._timeout):
                raise RuntimeError("另一个程序正在使用浏览器（登录/体检/下单），预热失败")
            self._file_locked = True
            _LOCK_LOCAL.depth = 1   # 同线程重入标记：order_via_browser 的装饰器会放行
            from playwright.sync_api import sync_playwright
            self._p = sync_playwright().start()
            self._ctx = launch(self._p, headless=headless)
            self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
            self._page.set_default_timeout(20000)
            ok, who = session_ok(self._ctx, page=self._page)
            if not ok:
                raise RuntimeError("预热时会话不可用：%s" % who)
            _goto_and_query(self._page, self.info, self.date,
                            want_hit=False, timeout=30000, wait_code=wait_code)
            self.ready = True
        except Exception as e:
            self.error = str(e)
            self.close()
            raise

    def usable(self):
        """还能不能用：未关闭、页面还活着、且是同一线程（锁是 thread-local 的）。"""
        if self._closed or self._page is None or not self.ready:
            return False
        if self._owner != threading.get_ident():
            return False
        try:
            return not self._page.is_closed()
        except Exception:
            return False

    def refresh(self, info):
        """重新导航到列表页并查询：下单前取最新余票 / 失败后回到列表页。"""
        _goto_and_query(self._page, info, self.date, want_hit=True, timeout=20000)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._ctx is not None:
                save_state(self._ctx)
                self._ctx.close()
        except Exception:
            pass
        try:
            if self._p is not None:
                self._p.stop()
        except Exception:
            pass
        if getattr(_LOCK_LOCAL, "depth", 0):
            _LOCK_LOCAL.depth = 0
        if self._file_locked:
            _PROFILE_LOCK.release()
            self._file_locked = False
        if self._local_locked:
            _BROWSER_LOCK.release()
            self._local_locked = False


def warm_up(info, date, headless=False, timeout=120, wait_code=None):
    """开抢前预热：打开浏览器、确认登录、停在目标车次的列表页。

    返回 WarmSession；失败抛 RuntimeError（调用方回退到普通下单）。"""
    return WarmSession(info, date, headless=headless, timeout=timeout,
                       wait_code=wait_code)


def order_ticket_via_browser(config, task, ticket, seat_name):
    """order_ticket 的浏览器实现，签名与 order.py 的 HTTP 版一致。

    供 order.py 在 config["order_mode"] == "browser" 时直接转发调用。
    """
    alias_name = None
    try:
        from ticket import SEAT_NAME_TO_CODE, order_seat_code
        seat_code = SEAT_NAME_TO_CODE.get(seat_name)
        # 网页端下单页不下发「无座」：同价改判为硬座（记账仍按勾选的席别）
        seat_code, alias_name = order_seat_code(seat_name, seat_code)
    except Exception:
        seat_code = None
    if not seat_code:
        return False, "未知席别: %s" % seat_name, None

    date = ticket.get("query_date") or ticket.get("start_date")
    names = list(task.get("passenger_names") or [])
    # 按人票种：任务里存了 {姓名: 票种代码} 就逐人下发，覆盖到的走本人票种
    purpose_map = dict(task.get("pax_purpose") or {})
    purpose = task.get("purpose_code") or "ADULT"
    if purpose_map and names:
        _codes = [purpose_map.get(n) or purpose for n in names]
        purpose = "0X00" if all(c == "0X00" for c in _codes) else "ADULT"
    ok, msg, extra = order_via_browser(
        ticket, seat_name, seat_code, names, date,
        headless=bool(config.get("browser_headless", False)), alias_name=alias_name,
        purpose=purpose, purpose_map=purpose_map)
    if ok:
        extra = dict(extra or {})
        extra.setdefault("passengers", "、".join(names))
    return ok, msg, extra


def show_timing_report():
    """聚合 logs/order_timing.jsonl：找出当前最快的下单路径与方法。

    「记录速度最快的路径和方法」的落地入口：按 预热/冷启动 两条路径分别汇总
    每次下单的总耗时与各阶段耗时，直接给出哪条路最快、最慢的一步在哪。"""
    import statistics
    p = os.path.join(HERE, "logs", "order_timing.jsonl")
    if not os.path.exists(p):
        print("还没有下单耗时记录（logs/order_timing.jsonl）")
        return 1
    recs = []
    with open(p, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                recs.append(json.loads(ln))
            except Exception:
                continue
    if not recs:
        print("耗时记录为空")
        return 1

    def stat(rows):
        if not rows:
            return None
        tots = [r.get("total") or 0 for r in rows]
        best = min(rows, key=lambda r: r.get("total") or 1e9)
        phases = {}
        for r in rows:
            for k, v in (r.get("phases") or {}).items():
                phases.setdefault(k, []).append(float(v))
        avg = {k: round(statistics.mean(v), 2) for k, v in phases.items()}
        return {
            "n": len(rows), "avg": round(statistics.mean(tots), 2),
            "min": round(min(tots), 2),
            "best": best, "phases_avg": avg,
        }

    warm_r = [r for r in recs if r.get("warm")]
    cold_r = [r for r in recs if not r.get("warm")]
    print("== 下单速度记录（logs/order_timing.jsonl）==")
    for name, rows in (("预热路径", warm_r), ("冷启动", cold_r)):
        s = stat(rows)
        if not s:
            print("  %s：暂无记录" % name)
            continue
        print("  %s：%d 次，平均 %.2fs，最快 %.2fs（%s / %s / %s）" % (
            name, s["n"], s["avg"], s["min"],
            s["best"].get("train"), s["best"].get("seat"),
            "成功" if s["best"].get("ok") else "失败"))
        ph = s["phases_avg"]
        if ph:
            slow = max(ph, key=ph.get)
            print("    平均阶段耗时：%s；最慢一步：%s（%.2fs）" % (
                " ".join("%s=%.2f" % (k, v) for k, v in ph.items()), slow, ph[slow]))
    if warm_r and cold_r:
        wa, ca = stat(warm_r), stat(cold_r)
        faster = "预热路径" if wa["avg"] <= ca["avg"] else "冷启动"
        print("  结论：%s更快（平均 %.2fs vs %.2fs）" % (faster, wa["avg"], ca["avg"]))
    return 0


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "check"
    if cmd == "login":
        sys.exit(0 if login() else 1)
    if cmd == "check":
        ok, who = check_session(headless=True)  # 走 exclusive，不跟别的进程抢 profile
        print(("[有效] %s" % who) if ok else ("[无效] %s" % who))
        sys.exit(0 if ok else 1)
    if cmd == "timing":
        sys.exit(show_timing_report())
    print(__doc__)


if __name__ == "__main__":
    main()
