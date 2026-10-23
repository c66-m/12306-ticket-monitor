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
import logging
import json
import os
import re
import sys
import threading
import time
import weakref

import appcommon

try:
    import msvcrt  # Windows 文件字节锁：跨进程互斥靠它
except ImportError:  # 非 Windows 平台没有 msvcrt，改用 fcntl
    msvcrt = None

try:
    import fcntl  # POSIX 跨进程文件锁（Linux/macOS）；Windows 下没有这个模块
except ImportError:
    fcntl = None

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(HERE, ".browser_profile")
# 注意：PROFILE_DIR 故意不迁进 data/——Playwright 持久化 profile 换路径等于
# 换浏览器身份，登录态与设备指纹全丢；挪它的收益（目录整洁）远小于代价。
STATE_PATH = appcommon.data_path(".browser_state.json")
LEFT_TICKET_URL = "https://kyfw.12306.cn/otn/leftTicket/init"
LOGIN_URL = "https://kyfw.12306.cn/otn/resources/login.html"
CHECK_URL = "https://kyfw.12306.cn/otn/index/initMy12306Api"

# 滑块出现后保留浏览器等用户手动完成的时长（脚本不绕过验证码）
_SLIDE_WAIT_SEC = 180


def _log(msg):
    """进度提示走 stderr。

    API 进程的 stdout 是 JSON-RPC 通道：非 JSON 行会被 electron/main.js 的
    JSON.parse 静默丢弃，登录/预热/下单进度因此在界面上永远看不到（"点了登录
    没反应"的观感来源）。stderr 由主进程接住转成界面日志，CLI 下也照常可见。
    另一个好处：与 api.emit() 的 OUT_LOCK 不共享 stdout，不会互相截断。"""
    print(msg, file=sys.stderr, flush=True)


# Task 101h：_log 的 PII 脱敏（与 Task 61/65a/84c 同口径）——用户名/乘车人姓名
# 不再明文进控制台日志。只用于日志路径，不碰下单逻辑与取证结构。
# 姓名口径单源在 appcommon.mask_one_name，别名保留（原实现逐字相同）。
_mask_name = appcommon.mask_one_name


def _mask_scalar(value):
    """单个标量值脱敏（与 probe_login._mask_scalar 同口径）：有值记为 ***。"""
    return "***" if value else value


def _mask_ticket_names(tickets):
    """limit_tickets 取证 JSON：只脱敏 name 字段，席别/票种等结构保留。"""
    out = []
    for t in tickets or []:
        t = dict(t)
        if t.get("name"):
            t["name"] = _mask_name(t["name"])
        out.append(t)
    return out


def _mask_text_names(text, names):
    """自由文本里的已知姓名替换为脱敏形（核对窗原文取证用）。"""
    for n in sorted((n for n in names or [] if n), key=len, reverse=True):
        text = (text or "").replace(n, _mask_name(n))
    return text


# 同一时刻只允许一个浏览器实例占用 profile。Playwright 的持久化 profile 是独占的：
# 引擎会话体检 / 弹窗刷新 / 侧边栏刷新 / 重新登录 并发时会互相把对方挤掉，
# 症状是浏览器 exitCode=21 启动即退、报 "browser has been closed"。
_BROWSER_LOCK = threading.RLock()

# 启动器（launcher.py）与监控系统（gui.py/engine.py）是各自独立的进程，却共用同一个
# .browser_profile，RLock 只管得住本进程的线程，跨进程必须再上一层文件锁。
_LOCK_PATH = os.path.join(HERE, ".browser_profile.lock")


class _ProfileLock:
    """跨进程互斥：Windows 用 msvcrt.locking 对锁文件首字节加独占锁，
    POSIX（Linux/macOS）用 fcntl.flock；两者都没有才退化为空操作
    （跨进程这一层让位，仅保留进程内 RLock）。

    用文件字节锁而不是"锁文件存在即占用"：进程被强杀或崩溃时操作系统会自动
    释放，不会留下删不掉、又没人认领的死锁。"""

    def __init__(self, path):
        self._path = path
        self._fd = None
        # 同线程重入计数：文件字节锁本身不支持重入，同一个线程第二次 acquire
        # 会自我死锁（拿不到自己已经持有的那把），必须先按线程判断。
        self._owner = None
        self._depth = 0

    def acquire(self, timeout=0):
        if msvcrt is None and fcntl is None:
            return True  # 无跨进程后端：跨进程这一层让位，仅保留进程内锁
        tid = threading.get_ident()
        if self._depth and self._owner == tid:
            # 同线程重入：直接计数放行。没有这条，busy() 在持锁栈内会把自己
            # 判成"别的程序正占用浏览器"，调用方据此永久跳过会话体检。
            self._depth += 1
            return True
        try:
            fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError:
            return False
        # monotonic：系统时钟回拨（NTP 校时 / VM 快照恢复）会让 time.time()
        # 版本的 deadline 永不成立，抢不到锁时变成 0.2s 空转的死循环。
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if msvcrt is not None:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    # LOCK_NB 抢不到时抛 BlockingIOError（OSError 子类）
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return False
                time.sleep(0.2)
            else:
                self._fd = fd
                self._owner = tid
                self._depth = 1
                return True

    def release(self):
        tid = threading.get_ident()
        if self._depth and self._owner == tid:
            self._depth -= 1
            if self._depth:
                return
        fd, self._fd = self._fd, None
        self._owner = None
        self._depth = 0
        if fd is None:
            return
        try:
            if msvcrt is not None:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


_PROFILE_LOCK = _ProfileLock(_LOCK_PATH)


# profile 独占锁等待者登记（审查第 10 条）：exclusive() 在真正阻塞等锁前
# 登记、拿到锁或放弃后注销。付款页留窗（_hold_pay_page）每轮检查这张表：
# 有等待者说明已有下一个任务在排队等锁，留窗占着锁只会让对方等锁超时、
# 错过放票窗口——提前结束留窗把锁让出去；无人等待时留窗时长不变。
# 登记只覆盖本进程内的等待者（跨进程等待在对方进程的登记表里，本进程
# 看不见，属已知边界）。
_LOCK_WAITERS = {}
_LOCK_WAITERS_GUARD = threading.Lock()


def _register_lock_waiter(label=None):
    """登记一个独占锁等待者，返回注销用的 token。"""
    token = object()
    with _LOCK_WAITERS_GUARD:
        _LOCK_WAITERS[token] = label or threading.current_thread().name
    return token


def _unregister_lock_waiter(token):
    with _LOCK_WAITERS_GUARD:
        _LOCK_WAITERS.pop(token, None)


def _lock_waiter_count():
    with _LOCK_WAITERS_GUARD:
        return len(_LOCK_WAITERS)


# Task 85d：depth 记录 owner 可寻址。
# 背景：原先用 threading.local 存 depth（本线程重入标记）；WarmSession.close()
# 若在非创建线程被调用（Task 65h 路径），owner 线程的 depth 残留为 1——owner
# 线程后续 exclusive() 走重入捷径，跳过跨进程文件锁的获取，profile 互斥静默
# 失效（P2）。threading.local 无法跨线程清理，故 depth 改按 Thread 对象寻址：
# WeakKeyDictionary 按 Thread 对象存 cell（Thread 对象不复用，无 ident 回收
# 误判；线程消亡后条目自动清理）。_LOCK_LOCAL 保留为兼容代理，既有读写
# ._LOCK_LOCAL.depth 的代码/测试语义不变（读写当前线程的 cell）。
_DEPTH_CELLS = weakref.WeakKeyDictionary()
_DEPTH_CELLS_GUARD = threading.Lock()


def _depth_cell(thread=None):
    t = thread if thread is not None else threading.current_thread()
    with _DEPTH_CELLS_GUARD:
        cell = _DEPTH_CELLS.get(t)
        if cell is None:
            cell = [0]
            _DEPTH_CELLS[t] = cell
        return cell


def _get_depth(thread=None):
    return _depth_cell(thread)[0]


def _set_depth(value, thread=None):
    _depth_cell(thread)[0] = value


class _DepthProxy:
    """_LOCK_LOCAL 的兼容外形：.depth 读写当前线程的 depth cell。"""

    @property
    def depth(self):
        return _get_depth()

    @depth.setter
    def depth(self, value):
        _set_depth(value)


_LOCK_LOCAL = _DepthProxy()

# 浏览器自动探测顺序：Edge → Chrome → Chromium → Playwright 自带 Chromium(None)。
# 上一个候选启动失败会在 profile 目录留下 Singleton* 锁，必须先清掉再试下一个。
_BROWSER_CHANNELS = ("msedge", "chrome", "chromium", None)

_SINGLETON_NAMES = ("SingletonLock", "SingletonSocket", "SingletonCookie")


def _snapshot_profile_locks():
    """launch() 每次启动尝试前对 Singleton* 做快照。

    只删"自己创建"的锁：快照之后新出现 / 被改动的文件才可能是本次尝试留下
    的；尝试前已存在且未被改动的，是别人的（或对端浏览器的），不动。
    用 lstat 而不用 exists/stat：Chromium 的 SingletonLock 是悬空软链接，
    os.path.exists 会跟随链接返回 False，根本看不见它。"""
    snap = {}
    for name in _SINGLETON_NAMES:
        path = os.path.join(PROFILE_DIR, name)
        try:
            snap[path] = os.lstat(path)
        except OSError:
            snap[path] = None
    return snap


def _same_stat(a, b):
    return (a.st_ino, a.st_mtime_ns, a.st_size) == \
        (b.st_ino, b.st_mtime_ns, b.st_size)


def _singleton_lock_owner_alive(path):
    """判断 SingletonLock 的持有者是否还活着。

    Chromium 在 POSIX 下把 SingletonLock 做成指向 "<hostname>-<pid>" 的
    软链接；目标 pid 存活 ⇒ 某个浏览器实例（可能是对端正在下单 / 体检的）
    还活着。返回 True（活着）/ False（已死）/ None（无法判定，
    如 Windows 下是普通文件、或无权限探查该 pid）。
    """
    try:
        target = os.readlink(path)
    except OSError:
        return None
    m = re.search(r"-(\d+)\s*$", os.path.basename(target))
    if not m:
        return None
    try:
        os.kill(int(m.group(1)), 0)
    except ProcessLookupError:
        return False
    except OSError:
        return None  # 无权限等：保守起见视为未知
    return True


def _clear_profile_locks(baseline=None):
    """清理 profile 锁，但只删"确定是自己留下的、或持有者已死"的。

    旧行为：launch() 某通道启动失败就无条件删 SingletonLock/Socket/Cookie。
    危险在于删掉对端（引擎体检 / 下单中）浏览器还活着的锁——等于把对方正在
    下单的浏览器掐死。

    规则：
      1. SingletonLock 的持有者 pid 还活着 ⇒ 对端浏览器还在跑，
         三个文件一个都不动，直接返回；
      2. baseline 给出时：尝试前已存在且未被改动的文件不是自己创建的，不动；
      3. 其余（本次尝试新产生的文件、持有者已死的僵尸锁、baseline 未给出
         时归属未知的文件）按旧行为删除。
    baseline 为空表示"不知道哪些是自己创建的"（兼容旧调用），此时只做
    规则 1 的存活校验，其余按旧行为处理。
    """
    baseline = baseline or {}
    lock_path = os.path.join(PROFILE_DIR, "SingletonLock")
    try:
        os.lstat(lock_path)
    except OSError:
        pass
    else:
        if _singleton_lock_owner_alive(lock_path):
            return  # 对端浏览器还活着：一个都不动
    for name in _SINGLETON_NAMES:
        path = os.path.join(PROFILE_DIR, name)
        try:
            st = os.lstat(path)
        except OSError:
            continue  # 不存在
        base = baseline.get(path)
        if base is not None and _same_stat(st, base):
            continue  # 尝试前已存在且未被改动 ⇒ 不是自己创建的，不动
        try:
            os.remove(path)
        except OSError:
            pass


def _acquire_locks_sliced(timeout, should_stop):
    """exclusive 的分片等锁路径：两把锁都按约 1s 一片获取，每片之间检查
    should_stop，被要求停时抛 RuntimeError 提前退出。总时长仍以 timeout
    封顶，超时/被占的错误信息与单次阻塞路径完全一致。任一步失败都保证
    已拿到的锁被释放（与单次路径同口径）。"""
    deadline = time.monotonic() + max(0.0, timeout)

    def _left():
        return deadline - time.monotonic()

    def _check_stop():
        if should_stop():
            raise RuntimeError(
                "等待浏览器独占锁期间收到停止信号，已中止本次下单等待")

    while True:
        _check_stop()
        if _BROWSER_LOCK.acquire(timeout=min(1.0, max(0.0, _left()))):
            break
        if _left() <= 0:
            raise RuntimeError("另一处正在使用浏览器（登录/体检/下单），请稍后重试")
    try:
        while True:
            _check_stop()
            if _PROFILE_LOCK.acquire(timeout=min(1.0, max(0.0, _left()))):
                break
            if _left() <= 0:
                raise RuntimeError("另一个程序正在使用浏览器（登录/体检/下单），请稍后重试")
    except BaseException:
        _BROWSER_LOCK.release()
        raise


@contextlib.contextmanager
def exclusive(timeout=90, should_stop=None):
    """串行化 profile 访问：本进程线程锁 + 跨进程文件锁，拿不到就抛错。

    Playwright 的持久化 profile 是独占的。启动器与监控系统分属不同进程，
    只靠 RLock 挡不住——一边下单、另一边开浏览器体检，会把下单的浏览器掐死，
    报 TargetClosedError: Target page, context or browser has been closed。

    should_stop（可调用，或带 is_set 的 Event）：传入时等锁改为分片等待
    （约 1s 一片），每片检查一次停止信号，被要求停时立即抛 RuntimeError
    退出，不再整段睡满 timeout（旧行为：用户点停止后下单线程还在锁队列
    里睡最多 180s，界面像「点了停止没反应」）。总等待上限仍是 timeout，
    超时错误信息不变；不传 should_stop 的调用点保持原单次阻塞语义。"""
    if should_stop is not None and not callable(should_stop):
        should_stop = getattr(should_stop, "is_set", None)
    depth = _get_depth()
    if depth:
        # 同线程重入：外层已持有两把锁，直接放行（保留 RLock 语义）
        _set_depth(depth + 1)
        try:
            yield
        finally:
            # Task 70a：体内若触发 stale warm 的 close()（见 order_via_browser），
            # close 已释放两把锁并把 depth 清零；此时若恢复旧 depth，会造成
            # "本线程仍持有锁"的假象，本线程后续 exclusive() 永久走重入捷径、
            # 跨进程 profile 互斥静默失效（P2）。depth 为 0 即视为锁已释放，
            # 不再恢复；正常路径 depth 非零，行为不变。
            if _get_depth():
                _set_depth(depth)
        return
    # 阻塞等锁前先登记等待者：持锁方的付款页留窗据此提前结束（见
    # _LOCK_WAITERS 注释）。拿到两把锁或任一步失败后立即注销。
    waiter = _register_lock_waiter()
    try:
        if should_stop is None:
            if not _BROWSER_LOCK.acquire(timeout=timeout):
                raise RuntimeError("另一处正在使用浏览器（登录/体检/下单），请稍后重试")
            try:
                if not _PROFILE_LOCK.acquire(timeout=timeout):
                    raise RuntimeError("另一个程序正在使用浏览器（登录/体检/下单），请稍后重试")
            except BaseException:
                _BROWSER_LOCK.release()
                raise
        else:
            _acquire_locks_sliced(timeout, should_stop)
    finally:
        _unregister_lock_waiter(waiter)
    _set_depth(1)
    try:
        yield
    finally:
        _set_depth(0)
        _PROFILE_LOCK.release()
        _BROWSER_LOCK.release()


def _exclusive(lock_timeout=150):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            # 允许调用方用 lock_timeout= 覆盖等待上限（如抢票线程不想等 420 秒）
            # should_stop 只借读给 exclusive 做分片等锁的停止检查，仍原样
            # 传给被装饰函数（其签名内的同名参数语义不变）。
            with exclusive(timeout=kwargs.pop("lock_timeout", lock_timeout),
                           should_stop=kwargs.get("should_stop")):
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


def launch(p, headless=False, restore=True, extra_args=None):
    """打开持久化 profile，并把上次保存的会话注入回去。

    注意：tk / uamtk / JSESSIONID 都是 session cookie，浏览器一关就清空
    （persistent context 关闭等同关浏览器），只有设备指纹类的持久 cookie 能留下。
    而 launch_persistent_context 不接受 storage_state 参数，所以会话只能
    读文件后用 add_cookies 手动注回，否则下次启动就是"未登录"。

    extra_args: 本次启动追加的 Chromium 参数；登录窗口用 --app= 让第一个窗口
    直接落在登录页上（见 login()），避免「先空白再跳转」那一下。"""
    errs = []
    for ch in _BROWSER_CHANNELS:
        kwargs = dict(
            headless=headless,
            viewport={"width": 1440, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                # 12306 静态资源首次 1.1 秒、二次 78ms：给磁盘缓存留足空间，
                # 反复进同一页面少拉一遍
                "--disk-cache-size=268435456",
                # 持久化 profile 的后台同步 / 预取会跟抢票抢带宽，关掉
                "--disable-background-networking",
                "--disable-sync",
                "--no-first-run",
                "--no-default-browser-check",
            ] + list(extra_args or []),
        )
        if ch:
            kwargs["channel"] = ch
        # 快照：失败时只删本次尝试自己留下的锁，不动对端的
        baseline = _snapshot_profile_locks()
        try:
            ctx = p.chromium.launch_persistent_context(PROFILE_DIR, **kwargs)
            break
        except Exception as e:
            errs.append("%s: %s" % (ch or "chromium", type(e).__name__))
            _clear_profile_locks(baseline)
            time.sleep(0.5)
    else:
        raise RuntimeError("浏览器启动失败（已尝试 %s）：%s" % (
            "/".join(str(c) if c else "chromium" for c in _BROWSER_CHANNELS), "; ".join(errs)))
    if restore and os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8-sig") as f:
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
        # 原子写：写一半被杀会留下半截 JSON，下次 launch() 注不回 cookie，
        # 开抢时刻变成"未登录"
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"cookies": cookies}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
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
        try:
            # wait_until 必须是 domcontentloaded：12306 的 load 事件要等一票第三方
            # 资源，默认的 "load" 经常把超时耗光，表现为"校验卡住不动"。
            page.goto(CHECK_URL, timeout=20000, wait_until="domcontentloaded")
            raw = page.evaluate("() => document.body.innerText") or ""
            final_url = page.url or ""
        except Exception as e:
            return False, "校验异常: %s" % str(e)[:100]
        try:
            # 先看落点再看正文：踢会话时 12306 是 302 到 /otn/passport?redirect=...，
            # 落点页是登录门户 HTML，json.loads 必然抛 JSONDecodeError。旧代码让这条
            # 真失效落进通用的"校验异常"桶，而引擎把"校验异常"一律当瞬时故障 →
            # 任务永不标 failed、无限重试、永不下单。
            # 判据与 order.verify_session（纯 HTTP 版）同口径。
            if "/login" in final_url or "/passport" in final_url:
                return False, "已跳转登录门户：%s" % final_url
            if "请先登录" in raw or "未登录" in raw[:500]:
                return False, "已跳转登录门户：%s" % final_url
            data = json.loads(raw)
        except Exception as e:
            return False, "校验异常: %s" % str(e)[:100]
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


# 在已打开的页面里做同源 fetch 校验：不开窗口、不导航、不刷新。
# 未登录时 12306 会把请求 302 到 /otn/passport?...，落点 URL 就是「没登录」的
# 直接证据；已登录时返回 initMy12306Api 的 JSON（status=true）。
_FETCH_SESSION_JS = """async () => {
  try {
    const r = await fetch('/otn/index/initMy12306Api',
                          {credentials: 'include', cache: 'no-store'});
    const text = await r.text();
    return {status: r.status, url: r.url, body: text.slice(0, 1000)};
  } catch (e) {
    return {status: -1, url: '', body: 'ERR:' + String(e)};
  }
}"""


def _page_session_ok(page):
    """在已打开的页面上用同源 fetch 校验登录态，返回 (ok, who)。

    判据与 session_ok 一致，但**不做任何导航**：登录过程中用户正对着登录页
    扫码，一次整页导航就会清掉二维码与"已扫描"状态。旧实现另开探针页导航
    CHECK_URL，未登录时被 302 到登录门户——用户看到的就是「第二个登录页」
    和「页面一直刷新」（2026-10-18 两次反馈）。
    """
    r = page.evaluate(_FETCH_SESSION_JS) or {}
    url = r.get("url") or ""
    body = (r.get("body") or "").strip()
    if "/login" in url or "/passport" in url:
        return False, ""
    if "请先登录" in body[:500] or "未登录" in body[:500]:
        return False, ""
    # 非 JSON（风控页 / 空白页）由 json 抛异常，调用方按「没验成」处理
    data = json.loads(body)
    if data.get("status"):
        return True, (data.get("data") or {}).get("user_name") or ""
    return False, ""


# 下单票据校验：initMy12306Api 只证明 uamtk 有效（页头能显示姓名），真正决定
# 「点预订会不会被放行」的是 /otn/login/checkUser 的 data.flag。两者会不一致：
# 会话从 cookie 重放回来时，常见「页头还是你的名字，checkUser 却 flag=false」，
# 此时 12306 只弹「您尚未登录」、页面不跳转（2026-10-23 实测，见 HANDOFF §34）。
_CHECKUSER_JS = """async () => {
  try {
    const r = await fetch('/otn/login/checkUser', {
      method: 'POST', credentials: 'include', cache: 'no-store',
      headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest'},
      body: '_json=true'
    });
    const t = await r.text();
    return {status: r.status, url: r.url, body: t.slice(0, 400)};
  } catch (e) {
    return {status: -1, url: '', body: 'ERR:' + String(e)};
  }
}"""

# 12306 判定未登录时页面上的提示词（弹窗 / 顶部条都可能出现）
_LOGIN_MODAL_WORDS = ("您尚未登录", "请先登录", "登录已过期")


def _page_booking_ok(page):
    """页面上「能不能下单」的真实判定：checkUser 的 data.flag。

    返回 (ok, 原因)。**验不成时返回 True**（不阻断下单）：这里只是提前
    发现"半残登录态"，真正的兜底是点「预订」后的弹窗检测与 30 秒等待——
    宁可多试一次点击，也不要因为一次校验异常把有票的机会放过。
    """
    try:
        r = page.evaluate(_CHECKUSER_JS)
    except Exception:
        return True, ""
    if not isinstance(r, dict):
        return True, ""
    url = r.get("url") or ""
    # 注意：checkUser 自己的路径就叫 /otn/login/checkUser，所以不能按 "/login"
    # 一刀切判门户（那样每次都会误报）；只认真正的 passport 落点或路径被换掉。
    if "/passport" in url or ("/login" in url and "checkUser" not in url):
        return False, "checkUser 被跳到登录门户"
    body = (r.get("body") or "").strip()
    try:
        data = json.loads(body)
    except Exception:
        return True, ""          # 风控页 / 空响应：不作为结论
    if (data.get("data") or {}).get("flag"):
        return True, ""
    return False, "下单票据已失效（页头仍显示已登录，但 checkUser flag=false）"


def _page_body(page):
    """取页面正文（取不到就返回空串，绝不抛）。"""
    try:
        text = page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:
        return ""
    return text if isinstance(text, str) else ""


# 会话失效判据：只有这两类算"确认失效"，其余（goto 超时 / 网络抖动 / 风控
# 非 JSON 页）都是瞬时故障。默认方向取"重试"——误把任务标 failed 要人工恢复，
# 代价比多试几轮大，所以拿白名单而不是把"校验异常"整类当永久。
_PERMANENT_FAILURE_MARKS = ("已跳转登录门户", "接口返回 status=false")


def session_failure_is_permanent(who):
    """失败文案是否代表"确认失效"（而非瞬时故障）。引擎据此决定把自动下单
    任务标 failed 还是只走冷却重试。"""
    text = str(who or "")
    return any(mark in text for mark in _PERMANENT_FAILURE_MARKS)


# lock_timeout 90 而不是 420：登录是交互动作，等对方放锁期间界面没有任何反馈，
# 干等 7 分钟只会得到「点了没反应」。90 秒后明确报「浏览器正忙」，用户重试或等流程结束。
# 排队提示是硬需求：不加日志就是静默卡死（2026-10-17 实测 46 秒零输出）。
def login(timeout_sec=300, stop_event=None, lock_timeout=90):
    """打开浏览器让用户完成登录；profile 会自动持久化，之后不必重复。

    stop_event: threading.Event，置位后尽快关浏览器返回 False（抢票线程的
    「停止」按钮不必再干等登录超时）。
    lock_timeout: 抢 profile 锁的上限（launcher 传 120 给自己的抢票节奏）。
    抢锁排队期间界面零反馈，所以进锁前先把「在排队」说出来。
    """
    if busy():
        _log("[浏览器] 浏览器正被占用（登录/下单/体检中），排队等待，最多 %d 秒…"
             % lock_timeout)
    return _login_locked(timeout_sec=timeout_sec, stop_event=stop_event,
                         lock_timeout=lock_timeout)


@_exclusive(lock_timeout=90)
def _login_locked(timeout_sec=300, stop_event=None):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = launch(p, headless=False, extra_args=["--app=" + LOGIN_URL])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        _log("请在打开的浏览器里完成登录（扫码或账号密码+滑块），最多等 %d 秒。" % timeout_sec)
        # domcontentloaded：12306 登录页的 load 事件要等滑块脚本和第三方资源，
        # 默认等到 load 就是窗口白屏好几秒（session_ok 早因同一原因改过）
        # --app= 已经让第一个窗口直接落在登录页上，这里只在没落上时补一次导航：
        # 用户要的是「别再刷这一下」，不是把窗口停在 about:blank。
        need_nav = not (page.url or "").startswith(LOGIN_URL)
        # 不挂任何文档级拦截：登录页里的「账号登录」会正常导航到
        # /otn/login/userLogin，一律 abort 会把窗口停在 ERR_FAILED 错误页
        # （2026-10-18 反馈，截图为证）。此前那条「禁止整页刷新」是给
        # 「扫码时页面一直刷」打的补丁，但真正的原因是探针页每 2 秒导航一次
        # CHECK_URL——探针页已删除，页面本身 30 秒内零导航（实测），
        # 拦截只剩副作用，去掉。
        if need_nav:
            # 显式 10 秒上限：不设 timeout 吃 Playwright 默认 30 秒，登录页
            # 资源卡住时窗口半天没动静；超时抛错走既有失败路径，不另加等待。
            page.goto(LOGIN_URL, timeout=10000, wait_until="domcontentloaded")
        # 登录期间一律不开新页面、不做整页导航：用户正对着登录页扫码，多一个
        # 窗口或一次导航都会打断（2026-10-18 反馈「两个登录页」「页面一直刷新」）。
        # 校验改在登录页内用同源 fetch 完成（_page_session_ok）：零窗口、零导航。
        deadline = time.time() + timeout_sec
        next_fast = 0.0   # 有登录信号时的校验节拍（2 秒）
        # 无信号时的兜底节拍：12306 改版导致 tk Cookie 没落地时靠它兜住。
        # 推迟启动给扫码留安静时间（生产 5 秒；短超时按一半缩短，测试仍覆盖）。
        # 曾是 20 秒：改版无 tk 时用户登完要干等十几秒才被识别，和快信号的
        # 1~2 秒形成两个世界，5 秒起兜、之后仍每 15 秒一探。
        next_slow = time.time() + min(5.0, timeout_sec / 2.0)
        miss = 0          # 校验连续抛异常的次数（页面被关 / 被导航走）
        ok, who = False, ""
        while time.time() < deadline:
            if stop_event is not None:
                if stop_event.wait(1):
                    ctx.close()
                    _log("[停止] 登录已被手动停止。")
                    return False
            else:
                # rpc_relogin 调 login() 不传 stop_event：旧代码这个循环零 sleep，
                # 空转一个核，还每 2 秒打一次官方校验接口（登录途中被 12306 限流，
                # 识别反而更慢）。没有 stop_event 也要有 1 秒节拍。
                time.sleep(1)
            now = time.time()
            # 快信号：Cookie 出现 tk = 登录成功（capture_session.py 同款判据，
            # 读 Cookie 无需导航，识别延迟从最坏 30 秒降到 1~2 秒）。登录后
            # 12306 的 uam 中转跳转不在旧 URL 白名单里，之前只能等 30 秒兜底
            # ——那就是"登录后识别太慢"的主因。URL 落点降为辅助信号。
            signal = False
            try:
                signal = any(str(c.get("name", "")).startswith("tk")
                             for c in ctx.cookies())
            except Exception:
                pass
            if not signal:
                try:
                    for pg in ctx.pages:
                        u = pg.url or ""
                        if "initMy12306" in u or "/otn/view/" in u or "/otn/index" in u:
                            signal = True
                            break
                except Exception:
                    pass
            # 有信号：每 2 秒校验一次（uam 中转没走完时会暂时不通过，短间隔重试
            # 直到通过）；无信号：15 秒兜底探测。两者都不碰页面本身。
            if signal and now >= next_fast:
                next_fast = now + 2
                next_slow = now + 15
                try:
                    ok, who = _page_session_ok(page)
                    miss = 0
                except Exception:
                    ok, who = False, ""
                    miss += 1
                if ok:
                    break
                if miss >= 10:      # 约 20 秒都读不到页面：窗口已被关闭/接管
                    _log("[停止] 登录窗口已关闭或页面不可用。")
                    break
            elif not signal and now >= next_slow:
                next_slow = now + 15
                try:
                    ok, who = _page_session_ok(page)
                    miss = 0
                except Exception:
                    ok, who = False, ""
                    miss += 1
                if ok:
                    break
                if miss >= 10:
                    _log("[停止] 登录窗口已关闭或页面不可用。")
                    break
        if ok:
            save_state(ctx)
            _log("[成功] 已登录：%s" % _mask_scalar(who))
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


# 下单成功后「席位已锁定」付款页的保留时长（秒）。12306 的座位锁定是 20 分钟，
# 这里默认 5 分钟：足够核对订单、完成支付，又不至于长期占住独占锁。
# config.json：keep_pay_page=false 关掉；pay_page_keep_seconds 自定义时长。
_PAY_PAGE_DEFAULT_HOLD = 300


def _pay_page_hold_seconds():
    """读 config.json 的付款页保留配置，返回秒数（0 = 不保留）。"""
    try:
        cfg = appcommon.read_config_or_empty(os.path.join(HERE, "config.json"))
    except Exception:
        return _PAY_PAGE_DEFAULT_HOLD
    if cfg.get("keep_pay_page") is False:
        return 0
    raw = cfg.get("pay_page_keep_seconds", _PAY_PAGE_DEFAULT_HOLD)
    try:
        return max(0, min(int(raw), 1200))     # 上限 20 分钟 = 座位锁定窗口
    except (TypeError, ValueError):
        return _PAY_PAGE_DEFAULT_HOLD


def _hold_pay_page(page, seconds, should_stop=None):
    """把「席位已锁定」付款页留在屏幕上，直到关窗 / 手动停止 / 超时 /
    有其它任务在等独占锁。

    留窗全程占着 profile 独占锁：若已有任务在 exclusive() 里排队等锁，
    继续留窗只会让对方等锁超时、错过放票窗口（审查第 10 条）——每轮
    检查等待者登记，有等待者即提前结束留窗；无人等待时保持原时长。
    订单结论在进留窗前已经落定，提前结束不影响本次下单结果。

    只对真实 Playwright 页面生效：测试替身没有 is_closed，直接返回，
    避免单元测试被这段等待挂住。
    """
    try:
        if page.is_closed():
            return
    except Exception:
        return
    _log("  [浏览器] 席位已锁定：付款页已保留，请核对订单并完成支付；"
         "关闭窗口或 %d 秒后程序继续（未支付订单在「未完成订单」里可继续支付）。"
         % seconds)
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if page.is_closed():
                _log("  [浏览器] 付款页已关闭，继续后续流程。")
                return
        except Exception:
            return
        if _lock_waiter_count() > 0:
            _log("  [浏览器] 已有其它任务在等待使用浏览器，提前结束付款页"
                 "保留（订单已生成，可在「未完成订单」里继续支付）。")
            return
        if should_stop is not None and should_stop():
            _log("  [浏览器] 已手动停止，关闭付款页。")
            return
        try:
            page.wait_for_timeout(500)
        except Exception:
            return
    _log("  [浏览器] 付款页保留 %d 秒后自动关闭；订单仍可在「未完成订单」里继续支付。"
         % seconds)


# 耗时记录落点：测试在 TempDirCase 里 patch 成临时文件，避免 mock 下单
# 把 G101 之类的假记录写进真实 logs/order_timing.jsonl 淹没真实数据。
TIMING_PATH = os.path.join(HERE, "logs", "order_timing.jsonl")


def _record_timing(tm, total, ok, msg, info, seat_name, warm):
    """把一次下单的各阶段耗时追加到 logs/order_timing.jsonl。

    「记录速度最快的路径和方法」的落地：每次下单都留一条，事后比对冷启动与
    预热、各车次各席别的阶段耗时，找出真正拖后腿的那一步。纯旁路，
    写不进去也绝不影响下单本身。"""
    try:
        os.makedirs(os.path.dirname(TIMING_PATH) or ".", exist_ok=True)
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
        with open(TIMING_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# 查询命中判定，两档：
#   _QUERY_ROW_JS —— 只要车次行渲染出来即可（预热用，此时多半无票，没有「预订」）
#   _QUERY_HIT_JS —— 还要有「预订」按钮，才是真能下单的信号
_QUERY_ROW_JS = r"""(code) => {
    const tb = document.querySelector('#queryLeftTable');
    if (!tb) return false;
    const ld = document.querySelector('#queryLoading');
    if (ld && ld.offsetParent !== null) return false;
    const rows = Array.from(tb.querySelectorAll('tr'));
    if (!code) return rows.length > 0;
    for (const tr of rows) {
        const s = tr.querySelector('strong');
        if (s && s.textContent.trim() === code) return true;
    }
    // 词界正则兜底：G1 不得命中 G11/G100（此前用子串匹配曾可能认错车次行）
    const re = new RegExp('(^|[^A-Za-z0-9])' +
        code.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '($|[^0-9])');
    return rows.some(tr => re.test(tr.innerText || ''));
}"""

_QUERY_HIT_JS = r"""(code) => {
    const tb = document.querySelector('#queryLeftTable');
    if (!tb) return false;
    const ld = document.querySelector('#queryLoading');
    if (ld && ld.offsetParent !== null) return false;
    const rows = Array.from(tb.querySelectorAll('tr'));
    for (const tr of rows) {
        const s = tr.querySelector('strong');
        if (!(s && s.textContent.trim() === code)) continue;
        if (Array.from(tr.querySelectorAll('a,button')).some(
                a => (a.innerText || '').indexOf('预订') >= 0)) return true;
    }
    const re = new RegExp('(^|[^A-Za-z0-9])' +
        code.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '($|[^0-9])');
    return rows.some(tr => re.test(tr.innerText || '')
        && Array.from(tr.querySelectorAll('a,button')).some(
            a => (a.innerText || '').indexOf('预订') >= 0));
}"""


def _goto_and_query(page, info, date, want_hit=True, timeout=20000, wait_code=None, should_stop=None):
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
    if should_stop is None:
        should_stop = lambda: False
    try:
        # 500ms 分片轮询：stop 置位可中断（坑 2 贯穿 DETECT 阶段）；
        # 窗口死（TargetClosed 等）快速失败，不拖满 20s
        deadline = time.time() + timeout / 1000
        while time.time() < deadline:
            if should_stop():
                return False
            try:
                page.wait_for_function(js, arg=arg, timeout=500)
                return True
            except Exception as e:
                if _window_dead_exc(e):
                    return False
    except Exception:
        return False


def _dump_booking_failure(page, info, diag):
    """把「找不到预订按钮」的现场落盘（JSON + 截图），供离线定位。

    只在真实 Playwright 页面生效：测试替身没有 screenshot，直接跳过，
    不会往项目 logs/ 里写垃圾。任何异常都吞掉——取证不能反过来打断失败上报。
    """
    if not hasattr(page, "screenshot"):
        return ""
    try:
        d = os.path.join(HERE, "logs")
        os.makedirs(d, exist_ok=True)
        base = os.path.join(d, "booking_fail_%s_%s" % (
            info.get("train_code") or "x", time.strftime("%Y%m%d-%H%M%S")))
        snap = {"train": info.get("train_code"),
                "from": info.get("from_name"), "to": info.get("to_name"),
                "url": getattr(page, "url", ""), "diag": diag}
        try:
            snap["table"] = page.evaluate(r"""() => {
                const tb = document.querySelector('#queryLeftTable');
                const out = {url: location.href, title: document.title,
                             tables: Array.from(document.querySelectorAll('table'))
                                       .map(t => t.id || t.className || '').slice(0, 10)};
                if (!tb) return out;
                out.rows = Array.from(tb.querySelectorAll('tr')).slice(0, 40).map(tr => ({
                    text: (tr.innerText || '').replace(/\s+/g, ' ').slice(0, 160),
                    links: Array.from(tr.querySelectorAll('a,button'))
                              .map(e => (e.innerText || '').trim()).slice(0, 8)}));
                out.html = tb.outerHTML.slice(0, 20000);
                return out;
            }""")
        except Exception:
            pass
        with open(base + ".json", "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
        try:
            page.screenshot(path=base + ".png", full_page=False)
        except Exception:
            pass
        _log("  [浏览器] 预订按钮失败现场已存：%s.json/.png" % base)
        return base
    except Exception:
        return ""


def _cls_has(cls_text, name):
    """className 精确 token 匹配（Task 65g）。

    确认按钮的启用态类名是 btn92s、禁用态是 btn92；倒计时中的按钮类名可能
    以 btn92s 为子串（如 btn92s-countdown）——旧代码 `"btn92s" in cls` 子串
    匹配会误点倒计时中的按钮。这里按空白切分后精确匹配独立 token。
    className 在 SVG 元素上可能是对象，做 str 兜底。
    """
    if not cls_text:
        return False
    text = cls_text if isinstance(cls_text, str) else str(
        getattr(cls_text, "baseVal", cls_text))
    return name in text.split()


def _slide_up(page):
    """当前页面是否有可见的滑块验证框。返回非空列表表示有（取证用）。"""
    try:
        return page.evaluate(
            """() => Array.from(document.querySelectorAll(
                  '#slide_passcode, .nc-container, .yzm, #randCodeForm_id'))
                  .filter(e => e.getBoundingClientRect().width > 0
                            && getComputedStyle(e).display !== 'none')
                  .map(e => e.id || e.className)""")
    except Exception:
        return []


_PAY_TIME_RE = re.compile(r"支付剩余时间[:：]\s*(\d{1,2}时\d{1,2}分\d{1,2}秒)")


def _window_dead_exc(e):
    """浏览器窗口/上下文已死的异常类判定（小写类名子串匹配）。

    覆盖 Playwright 关闭类异常（TargetClosedError、BrowserContextClosedError、
    Error 基类）与 websocket/协议层断开（ProtocolError、ConnectionClosedError）。
    判定失败（鬼魂异常）一律 False，不吞真实失败。
    """
    return any(k in type(e).__name__.lower()
               for k in ("targetclosed", "closed", "protocolerror", "connectionclosed"))


def _extract_pay_evidence(body):
    """付款页正文→支付证据串。无证据返回空串。纯函数便于单测。"""
    m = _PAY_TIME_RE.search(body)
    locked = "席位已锁定" in body
    if locked and m:
        return "席位已锁定+支付剩余时间%s" % m.group(1)
    if locked:
        return "席位已锁定"
    if m:
        return "支付剩余时间%s" % m.group(1)
    return ""


# 「下单成功」的唯一口径：页面上真的出现「席位已锁定」的订单界面。
# 只看 URL 不够——payOrder 加载失败 / 被踢 / 只渲染一半时 URL 是对的，
# 页面却是空白或错误页，按 URL 判成功会把失败当成功（用户 2026-10-18 要求
# 以「弹出锁定席位的界面」为准）。
_PAY_LOCK_TEXT = "席位已锁定"
_PAY_LOCK_WAIT_SEC = 10.0


def _wait_pay_locked(page, wait_sec=_PAY_LOCK_WAIT_SEC, stop=None):
    """轮询页面正文直到出现「席位已锁定」界面，返回 (ok, body, evidence)。

    页面从 payOrder 跳到渲染完成有 1~3 秒延迟，所以给一个短窗口轮询；
    stop 置位（用户点停止）立即放弃，交上层按「结果未知」处理。
    """
    deadline = time.time() + wait_sec
    body = ""
    while True:
        try:
            body = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        except Exception:
            return False, body, ""
        if _PAY_LOCK_TEXT in body:
            return True, body, _extract_pay_evidence(body)
        if stop is not None and stop():
            return False, body, ""
        if time.time() >= deadline:
            return False, body, _extract_pay_evidence(body) if body else ""
        try:
            page.wait_for_timeout(400)
        except Exception:
            return False, body, ""


def _wait_slide_gone(page, sec, stop=None):
    """等用户在浏览器窗口手动完成滑块（脚本不绕过验证码）。

    浏览器和锁都保持原状。成功返回实际等待的秒数（float），超时返回 False。
    调用方必须用返回值补偿业务倒计时——滑块是用户手动完成的，这段时间
    不应消耗「确认窗 15 秒」或「结果等待」倒计时（Task 42）：确认窗循环在
    滑块消失后重置 t_confirm，结果等待循环 deadline += elapsed。
    stop: threading.Event 或等效 callable，置位时中止等待，返回字符串
    "user_stopped"（调用方据此把本次下单标成手动停止）。
    """
    t_slide = time.time()
    while time.time() - t_slide < sec:
        page.wait_for_timeout(500)
        if stop is not None and stop():
            return "user_stopped"
        try:
            if not _slide_up(page):
                return time.time() - t_slide
        except Exception:
            return time.time() - t_slide
    return False


# 核对窗席别词表（Task 70c）：与原 _order_impl 内联元组逐字一致，抽出以便复用与单测
_SEAT_WORDS = ("无座", "硬座", "硬卧", "软卧", "二等座", "一等座", "商务座")


def _dialog_seat_words(dlg_text):
    """核对窗原文里出现的所有席别词（按 _SEAT_WORDS 顺序）；无则 []。"""
    text = dlg_text or ""
    return [w for w in _SEAT_WORDS if w in text]


def _audit_dialog_seat(dlg_text, seat_name, alias_name):
    """核对窗席别审计（Task 70c）。返回 (dlg_seat, abort_seat)：
    - dlg_seat: 原文里出现的第一个席别词，无则 ""（取证用，
      结果页 extra["seat_in_dialog"]）。
    - abort_seat: 与所选/改判席别都不符的第一个席别词，无则 None（应中止提交）。
    """
    shown = _dialog_seat_words(dlg_text)
    dlg_seat = shown[0] if shown else ""
    bad = [w for w in shown if w not in (seat_name, alias_name or "")]
    return dlg_seat, (bad[0] if bad else None)


def _run_fill_submit_confirm(ctx, page, info, date, seat_name, seat_code,
                             passenger_names, purpose="ADULT", alias_name=None,
                             purpose_map=None, should_stop=None, mark=None,
                             stage_cb=None):
    """填单→提交→确认 共享序列：旧单体链与阶段化新链同源，选择器 2026-10 实测勿改。

    覆盖旧 _order_impl 的步骤 3-7：点预订→勾乘车人→席别(limit_tickets 四步强写)→
    票种(静默改值)→提交→核对窗取证→点确认。返回 dict：ok/msg/extra/clicked/
    picked/dlg_seat/t_submit。clicked=True 表示确认已点出（此后异常=ambiguous）；
    t_submit=点确认时刻（官方回读 180s 归因窗口起点）。mark(name)=耗时回调；
    stage_cb(name)=阶段节点回调（orderflow 打 SUBMIT/CONFIRM 节点日志）。
    """
    if mark is None:
        def mark(name):
            return None
    clicked = False
    picked = []
    dlg_seat = ""
    t_submit = 0.0
    def _fail(msg, extra=None):
        # 旧 tuple return 归一为共享 res dict（clicked 等由 try 前预初始化兜底）
        return {"ok": False, "msg": msg, "extra": extra, "clicked": clicked,
                "picked": picked, "dlg_seat": dlg_seat, "t_submit": t_submit}

    try:
        # 2.5) 点「预订」之前先确认这张票还允许下单。页头显示姓名只说明 uamtk
        #      有效；会话由 cookie 重放回来时经常「名字还在、下单票据已废」，
        #      这时点下去 12306 只弹「您尚未登录」、页面不跳转。
        #      提前判定 → 不点、不创建订单草稿，直接给出可执行的结论。
        booking_ok, booking_why = _page_booking_ok(page)
        if not booking_ok:
            return _fail("12306 判定未登录（%s）：本次未点「预订」、未提交订单。"
                         "请重新登录后再试" % booking_why,
                         {"reason": "need_relogin"})

        # 3) 点该车次的「预订」——精确匹配车次行。:has-text() 是子串匹配，
        #    查 G1 曾可能点中排在前面的 G11/G100 的「预订」，把订单下给别的车次
        book = page.evaluate(
            r"""(code) => {
                const tb = document.querySelector('#queryLeftTable');
                if (!tb) return {ok: false, why: 'no_table', url: location.href};
                const rows = Array.from(tb.querySelectorAll('tr'));
                const re = new RegExp('(^|[^A-Za-z0-9])' +
                    code.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '($|[^0-9])');
                // a / button 都认：12306 前端灰度可能把「预订」从链接换成按钮
                const bookOf = (tr) => Array.from(tr.querySelectorAll('a,button')).find(
                    a => (a.innerText || '').indexOf('预订') >= 0);
                // 同车次可能有多行（主行/展开行）：取第一条「真有预订」的。
                // 旧代码找到第一条 strong 命中就 break，那一行没有预订就直接判失败
                // （2026-10-19 实测：网页明明有预订，脚本报没有）。
                for (const tr of rows) {
                    const s = tr.querySelector('strong');
                    if (s && s.textContent.trim() === code) {
                        const a = bookOf(tr);
                        if (a) { a.click(); return {ok: true}; }
                    }
                }
                for (const tr of rows) {
                    if (!re.test(tr.innerText || '')) continue;
                    const a = bookOf(tr);
                    if (a) { a.click(); return {ok: true}; }
                }
                const same = rows.filter(tr => {
                    const s = tr.querySelector('strong');
                    return s && s.textContent.trim() === code;
                });
                return {ok: false, why: 'no_book', url: location.href,
                        title: document.title, rows: rows.length,
                        codeRows: same.length,
                        sample: (same[0] ? same[0].innerText : '').slice(0, 120)};
            }""", info["train_code"])
        # 兼容旧测试替身：它们对这段 JS 直接返回 True/False
        book_ok = book.get("ok") if isinstance(book, dict) else bool(book)
        if not book_ok:
            detail = json.dumps(book, ensure_ascii=False)[:400] if isinstance(
                book, dict) else str(book)
            base = _dump_booking_failure(page, info, book)
            tip = ("；失败现场已存 %s.json/.png" % base) if base else ""
            return _fail("页面上没有 %s 的『预订』按钮（可能已无票或不可预订）；"
                         "页面取证：%s%s" % (info["train_code"], detail, tip), None)
        _log("  [浏览器] 已点 %s 的『预订』" % info["train_code"])
        mark("book")

        # 4) 确认订单页。官方进页后会异步填 limit_tickets（乘车人/席别/票种
        #    的内部状态），等这个数组就绪即可，不必死睡 2.5 秒。
        # commit：导航一发起就走，不必等页面 load（后面有 limit_tickets 兜底）
        # 账号有未支付/未处理订单时，12306 会拦住预订、页面留在列表页并弹
        # 「您还有未处理的订单」——这里识别它，别再白等 40 秒超时。
        arrived = False
        try:
            # 先只等 3 秒：未登录时 12306 根本不跳转，只在列表页弹
            # 「您尚未登录」。旧实现一次性等 30 秒，窗口看起来就是卡死。
            page.wait_for_url("**/confirmPassenger/initDc**",
                              wait_until="commit", timeout=3000)
            arrived = True
        except Exception:
            body = _page_body(page)
            if any(k in body for k in _LOGIN_MODAL_WORDS):
                mark("result")
                return _fail("12306 判定未登录（页面弹「您尚未登录」）：本次未提交订单，"
                             "登录票据已失效，请重新登录后再试",
                             {"reason": "need_relogin"})
        if not arrived:
            try:
                page.wait_for_url("**/confirmPassenger/initDc**",
                                  wait_until="commit", timeout=27000)
            except Exception:
                body = ""
                for _ in range(3):   # 弹窗渲染可能晚于超时瞬间:复核 3 次
                    body = _page_body(page)
                    if any(k in body for k in ("未处理", "未支付", "未完成订单", "行程冲突")):
                        break
                    page.wait_for_timeout(500)
                if any(k in body for k in ("未处理", "未支付", "未完成订单", "行程冲突")):
                    i = max(0, body.find("订单") - 30)
                    mark("result")
                    return _fail(body[i:i + 200].replace("\n", " "), {"reason": "dup"})
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
            return _fail("确认页没有可用乘车人（账号里没保存常用联系人？）", None)

        targets, missed = [], []
        for name in (passenger_names or []):
            exact = [p for p in people if p["text"] == name]
            if exact:
                targets.append(exact[0])
                continue
            # 前缀兜底取最短命中（适配"张三（学生）"这类带标注的展示文本），
            # 避免「张三」按前缀匹配到排在更前面的「张三丰」
            pref = sorted((p for p in people if p["text"].startswith(name)),
                          key=lambda p: len(p["text"]))
            if pref:
                targets.append(pref[0])
            else:
                missed.append(name)
        if missed:
            # 点了名但没匹配到：绝不能悄悄改勾别人（此前会静默勾第一位）
            return _fail(
                    "乘车人 %s 在确认页没匹配到（可用：%s）。已中止，未提交订单——"
                    "请核对姓名或账号常用联系人" % (
                        "、".join(missed),
                        "、".join(p["text"] for p in people[:8])),
                    None)
        if not targets:
            targets = [people[0]]  # 未点名：沿用账号第一位（既有约定）

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
        # 读回勾选状态：12306 会把未核验/证件过期的联系人置灰，click 无效；
        # 勾不上还往下走就是给页面残留的勾选下单
        unchecked_ids = page.evaluate(
            """(ids) => ids.filter(i => { const e = document.getElementById(i);
                                          return !e || !e.checked; })""",
            [t["id"] for t in targets])
        if unchecked_ids:
            bad_names = [t["text"] for t in targets if t["id"] in unchecked_ids]
            return _fail(
                    "乘车人 %s 勾选未生效（可能未核验/证件过期）。已中止，未提交订单"
                    % "、".join(bad_names), None)
        _log("  [浏览器] 乘车人：%s" % "、".join(_mask_name(n) for n in picked))
        mark("passenger")

        if alias_name:
            _log("  [浏览器] 席别：勾选 %s 网页端不下发，按同价改判为 %s（%s）下单"
                 % (seat_name, alias_name, seat_code))
        # 6) 选席别：确认页的席别是 <select id="seatType_1">，
        #    选项由服务端按该车次可售席别下发（网页端不一定有无座）。
        seat = page.locator("#seatType_1").first
        if seat.count() == 0:
            return _fail("确认页找不到席别下拉 #seatType_1（页面结构可能已变）", None)
        opts = seat.evaluate(
            "e => Array.from(e.options).map(o => ({v: o.value, t: o.text.trim()}))")
        codes = [o["v"] for o in opts]
        if seat_code not in codes:
            readable = "、".join("%s(%s)" % (o["t"].split("（")[0], o["v"]) for o in opts)
            if alias_name:
                return _fail(
                        "勾选 %s，已按同价改判为 %s，但该车次网页端下单页不下发 %s（%s）；可选：%s"
                        % (seat_name, alias_name, alias_name, seat_code, readable),
                        {"reason": "alias_no_stock", "seat_codes": codes,
                         "seat_options": readable, "alias_seat": alias_name})
            return _fail(
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
        _log("  [浏览器] 内部状态 limit_tickets = %s" % json.dumps(_mask_ticket_names(tickets), ensure_ascii=False))
        seats_now = [t.get("seat") for t in tickets]
        if seats_now:
            if any(s != seat_code for s in seats_now):
                return _fail(
                        "席别未同步到页面内部状态（期望 %s，实际 %s）。已中止，未提交订单。" % (
                            seat_code, seats_now),
                        {"limit_tickets": tickets})
            _log("  [浏览器] 席别已确认：%s（%s）× %d" % (seat_name, seat_code, len(seats_now)))
        else:
            # 读不到内部状态（可能是私有变量）时退回 DOM 校验
            now_seat = seat.input_value()
            if now_seat != seat_code:
                return _fail(
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
        # 读回校验（与席别段同一纪律）：内部状态可读时不一致即中止——
        # 学生票没核验时盲提交必被拒，白耗抢票窗口（此前只 best-effort 等待）
        try:
            tt_now = page.evaluate(
                """() => (window.limit_tickets || []).map(
                    t => ({name: t.name, tt: String(t.ticket_type)}))""")
        except Exception:
            tt_now = []
        if tt_now:
            tt_bad = [t for t in tt_now
                      if t["tt"] != str((tt_args["map"] or {}).get(t["name"]) or tt_args["code"])]
            if tt_bad:
                return _fail(
                        "票种未同步到页面内部状态（期望 %s，实际 %s）。已中止，未提交订单"
                        % (json.dumps(tt_args, ensure_ascii=False),
                           json.dumps(tt_bad, ensure_ascii=False)),
                        {"limit_ticket_types": tt_now})
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
        if stage_cb is not None:
            stage_cb("SUBMIT")

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
        captcha_waited = False

        def _stopped():
            return should_stop is not None and should_stop()

        dlg_seat = ""     # 12306 核对窗里显示的席别（取证：万一与所选不一致，日志里有原文）
        t_confirm = time.time()
        while time.time() - t_confirm < 15.0 and not _stopped():
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
            # Task 65g：精确匹配启用态类名 btn92s（独立 token），不用子串——
            # 倒计时中的按钮类名可能含 btn92s 子串，子串匹配会误点
            if st["found"] and st["shown"] and _cls_has(st["cls"], "btn92s"):
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
                    _log("  [浏览器] 核对窗原文：%s" % _mask_text_names(dlg_text, picked))
                    # Task 70c：记录核对窗显示的席别（取证）。此前 dlg_seat
                    # 从未被赋值，结果页的席别警告与 extra["seat_in_dialog"]
                    # 是死代码；现在正确赋值。
                    dlg_seat, abort_seat = _audit_dialog_seat(
                        dlg_text, seat_name, alias_name)
                    if abort_seat:
                        # 服务端渲染的核对窗与页面内部状态不一致：按 RULES.md
                        # 的纪律"不一致就中止、不提交"——此前照常提交，可能
                        # 买到与所选不符（更贵）的席别
                        return _fail(
                                "核对窗显示席别 %s，与所选 %s 不一致。已中止，未提交订单（原文：%s）"
                                % (abort_seat, seat_name, dlg_text[:160]),
                                {"seat_in_dialog": abort_seat})
                page.evaluate(
                    "() => { const e = document.querySelector('#qr_submit_id'); if (e) e.click(); }")
                _log("  [浏览器] qr_submit 倒计时结束已启用（%.1fs），已点确认" % (
                    time.time() - t_confirm))
                clicked = True
                break
            if _slide_up(page):
                # 浏览器和锁都保持原状等用户完成滑块；此前直接返回，
                # 毫秒级关窗让"请手动完成滑块"根本做不到
                _log("  [浏览器] 触发滑块验证：请在浏览器窗口手动完成（最多等 %d 秒）…" % _SLIDE_WAIT_SEC)
                slide_elapsed = _wait_slide_gone(page, _SLIDE_WAIT_SEC, stop=_stopped)
                if slide_elapsed == "user_stopped":
                    return _fail("手动停止：滑块等待期间用户已停止任务",
                            {"reason": "user_stopped"})
                if slide_elapsed is False:
                    return _fail("滑块验证等待超时（%d 秒），本次下单中止" % _SLIDE_WAIT_SEC,
                            {"need_captcha": True})
                # 滑块是用户手动完成的：等待期间不消耗确认窗倒计时，
                # 重置计时起点（Task 42；此前倒计时在滑块前定死，回来后
                # 按钮永远点不下去，却只记一条普通失败日志）
                t_confirm = time.time()
                _log("  [浏览器] 滑块已消失（人工等待 %.1fs，不计入确认倒计时），"
                     "继续等确认按钮…" % slide_elapsed)
            page.wait_for_timeout(200)
        if _stopped():
            # 用户点了停止：clicked 与否分开处置——已点确认的结果未知，
            # 交上层官方回读；未点确认的直接按手动停止收尾（问题二）
            if clicked:
                return _fail("手动停止：确认已提交但结果未知，"
                        "请先到 12306 核对未完成订单",
                        {"reason": "ambiguous"})
            return _fail("手动停止：提交前用户已停止任务",
                    {"reason": "user_stopped"})
        if not clicked:
            # 旧版页面回退：只点文本确认为「确定/确认/是/好的」的可见控件；
            # #qr_submit_id 交给主轮询管（此刻可能还在倒计时禁用态），并且
            # 不再盲点第一个控件——点错按钮置 clicked=True 会白白烧掉一次重试
            btns = _visible_ok_btns()
            _log("  [浏览器] 未等到 qr_submit，回退旧确认控件：%s" % json.dumps(
                btns, ensure_ascii=False))
            for b in btns:
                # 空 id 会让 getElementById("") 恒返回 null：点了等于没点，却把
                # clicked 记成 True，上层据此判 ambiguous「已提交但结果未知」，
                # 实际是漏单（订单根本没提交）。
                if not b["id"] or b["id"] == "qr_submit_id":
                    continue
                if b["txt"] in ("确定", "确认", "是", "好的"):
                    ok_click = page.evaluate(
                        "(id) => { const e = document.getElementById(id);"
                        " if (!e) return false; e.click(); return true; }", b["id"])
                    if not ok_click:
                        continue
                    _log("  [浏览器] 已点确认框：%s" % b["txt"])
                    clicked = True
                    break
        if not clicked:
            # 确认按钮始终未启用、回退控件也没有：订单根本没提交。旧代码仍
            # 返回 ok=True（HOLD 空等 → 首轮无单 → 重下），现在如实上报，
            # 上层按普通失败处理（2026-10-19 审查）。
            return _fail("未点成确认（确认按钮始终未启用），本次未提交订单",
                         {"reason": "not_submitted"})
        mark("submit")
        t_submit = time.time()
        if stage_cb is not None:
            stage_cb("CONFIRM")
        return {"ok": True, "msg": "", "extra": None, "clicked": clicked,
                "picked": picked, "dlg_seat": dlg_seat, "t_submit": t_submit}
    except Exception as e:
        if clicked:
            return {"ok": False,
                    "msg": "提交确认后异常（结果未知，请先到 12306 核对未完成订单）：%s: %s"
                           % (type(e).__name__, str(e)[:150]),
                    "extra": {"reason": "ambiguous"}, "clicked": clicked,
                    "picked": [], "dlg_seat": "", "t_submit": 0.0}
        if _window_dead_exc(e):
            return {"ok": False, "msg": "浏览器窗口异常关闭: %s: %s"
                    % (type(e).__name__, str(e)[:180]),
                    "extra": {"reason": "browser_dead"}, "clicked": False,
                    "picked": [], "dlg_seat": "", "t_submit": 0.0}
        return {"ok": False, "msg": "浏览器下单异常: %s: %s"
                % (type(e).__name__, str(e)[:180]),
                "extra": None, "clicked": False,
                "picked": [], "dlg_seat": "", "t_submit": 0.0}


def _order_impl(info, seat_name, seat_code, passenger_names, date,
                headless=False, verify_timeout=90, purpose="ADULT", warm=None, tm=None,
                alias_name=None, purpose_map=None, should_stop=None):
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
    warm: WarmSession 实例。命中则跳过开窗口 / 导航 / 填条件这几步，
        直接在这张已经预热好的列表页上点「预订」（需求 2）。
        注：会话有效性复验已改在 order_via_browser（wrapper 层）执行，
        此处收到的 warm 均已通过复验。
    tm: 调用方给的耗时字典，边跑边填各阶段秒数（需求 3）。
    alias_name: 同价改判后的席别名（勾选无座 → 硬座）。非空表示 seat_code 已经是
        改判后的代码，失败时按「改判后仍不可售」上报，不要当成配置错误永久跳过。
    """
    from playwright.sync_api import sync_playwright

    tm = tm if tm is not None else {}
    t_last = [time.perf_counter()]
    # Task 70b：外层 except 会引用 clicked（提交后异常→ambiguous 判定）；
    # 它在 try 内确认段（:1014）才赋值，步骤 1–6.5 抛异常时此前是
    # UnboundLocalError 掩盖原始错误。在此预初始化。
    clicked = False

    def mark(name):
        now = time.perf_counter()
        tm[name] = round(now - t_last[0], 3)
        t_last[0] = now

    if warm is not None and not warm.usable():
        # Task 101g：已在 @_exclusive 锁内——不能调完整 close()（会释放装饰器
        # 仍需要的 profile/线程锁并清零 depth，本次下单后半程失去互斥）。
        # 预热等待拿锁期间现场失效（页面被关等）的亚毫秒竞速走这里：只关浏览器
        # 资源，锁的释放留给外层调用方的 finally 做完整 close()。
        try:
            warm.close(release_locks=False)
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
                _log("  [浏览器] 会话有效：%s" % _mask_scalar(who))
            else:
                # Task 95：会话有效性已在 order_via_browser（锁外）复验，
                # 这里跳过的是开窗口与完整导航（warm.refresh 只做一次重查）。
                _log("  [浏览器] 复用预热现场（会话已复验，跳过开窗口 / 导航）")
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

            # 3-7) 填单→提交→确认：共享序列（阶段化新链同源；选择器勿改）
            res = _run_fill_submit_confirm(
                ctx, page, info, date, seat_name, seat_code,
                passenger_names, purpose=purpose, alias_name=alias_name,
                purpose_map=purpose_map, should_stop=should_stop, mark=mark)
            if not res["ok"]:
                return False, res["msg"], res["extra"]
            clicked = res["clicked"]
            picked = res["picked"]
            dlg_seat = res["dlg_seat"]
            t_submit = res["t_submit"]
            _stopped = (lambda: should_stop is not None and should_stop())
            _log("  [浏览器] 等待出票结果...")
            # 8) 判定结果
            deadline = time.time() + verify_timeout
            tick = 0
            captcha_waited = False   # 结果循环内滑块已等过标记（提取前在确认段初始化）
            _dup_keys = ("未支付", "未完成订单", "未处理", "已预订", "行程冲突", "已购买")
            _fail_keys = ("订票失败", "出票失败", "余票不足", "系统繁忙") + _dup_keys
            while time.time() < deadline:
                if _stopped():
                    # 用户点了停止：clicked 与否结果都未知——已点确认的
                    # 交上层官方回读，未点确认的按手动停止（问题二）
                    if clicked:
                        return (False, "手动停止：确认已提交但结果未知，"
                                "请先到 12306 核对未完成订单",
                                {"reason": "ambiguous"})
                    return (False, "手动停止：等待出票结果期间用户已停止任务",
                            {"reason": "user_stopped"})
                # 250ms 一轮：出票结果通常 1-3 秒内 URL 就会变
                page.wait_for_timeout(250)
                tick += 1
                # 滑块检测要两次 IPC，降频到每 4 轮（约 1 秒）查一次；第 1 轮先查
                if tick % 4 == 1:
                    sp = page.locator("#slide_passcode").first
                    if sp.count() and sp.is_visible():
                        if not captcha_waited:
                            captcha_waited = True
                            _log("  [浏览器] 触发滑块验证：请在浏览器窗口手动完成（最多等 %d 秒）…" % _SLIDE_WAIT_SEC)
                            slide_elapsed = _wait_slide_gone(page, _SLIDE_WAIT_SEC, stop=_stopped)
                            if slide_elapsed == "user_stopped":
                                if clicked:
                                    return (False, "手动停止：确认已提交但结果未知，"
                                            "请先到 12306 核对未完成订单",
                                            {"reason": "ambiguous", "need_captcha": True})
                                return (False, "手动停止：滑块等待期间用户已停止任务",
                                        {"reason": "user_stopped"})
                            if slide_elapsed is False:
                                extra = {"need_captcha": True}
                                if clicked:
                                    # 确认已点出去：订单可能已在服务端生成。
                                    # 滑块超时≠没提交，标记 ambiguous 走官方回读
                                    # 确认（Task 52；此前漏标，上层盲重试可能重复下单）
                                    extra["reason"] = "ambiguous"
                                return (False, "滑块验证等待超时（%d 秒），请重试" % _SLIDE_WAIT_SEC,
                                        extra)
                            # 滑块是用户手动完成的：等待期间不消耗结果等待倒计时，
                            # 顺延 deadline（Task 42；此前倒计时在滑块前定死，
                            # 回来后很快报超时）
                            deadline += slide_elapsed
                            _log("  [浏览器] 滑块已消失（人工等待 %.1fs，不计入结果倒计时），"
                                 "继续等出票结果…" % slide_elapsed)
                        else:
                            extra = {"need_captcha": True}
                            if clicked:
                                # 同上：确认已点出时滑块重现，结果同样未知，
                                # 标 ambiguous 走官方回读而非盲重试
                                extra["reason"] = "ambiguous"
                            return (False, "滑块验证再次出现，已中止（脚本不绕过验证码）",
                                    extra)
                url = page.url
                if any(k in url for k in ("payOrder", "MyOrderNoComplete", "order/init")):
                    _log("  [浏览器] 结果页用时 %.1fs（%d 轮）" % (time.time() - t_submit, tick))
                    # 只有付款页才是「本次下单成功」；未完成订单列表/订单初始化页
                    # 可能是账号里更早的旧单，不能当成功直接停机，标 ambiguous
                    # 交上层官方接口回读（旧代码三 URL 一锅烩误报成功）
                    if "payOrder" not in url:
                        mark("result")
                        return (False,
                                "订单结果落在 %s（非付款页）——无法确认本次下单结果，"
                                "交官方订单接口回读" % url, {"reason": "ambiguous"})
                    # 成功口径：页面上真的出现「席位已锁定」的订单界面。
                    # 只看 URL 不够——payOrder 加载失败时 URL 对、页面是空白/错误页。
                    locked, body_raw, pay_evidence = _wait_pay_locked(
                        page, _PAY_LOCK_WAIT_SEC, _stopped)
                    if not locked:
                        mark("result")
                        return (False,
                                "已跳到付款页但没出现「席位已锁定」界面"
                                "（页面可能仍在加载、被踢或已失效）——"
                                "无法确认本次下单成功，交官方订单接口回读",
                                {"reason": "ambiguous", "page_kind": "payOrder"})
                    # 页面正文已拿到：核对真实显示的席别，防止静默买错
                    seat_on_page = ""
                    body = body_raw.replace("\n", " ")
                    for kw in (seat_name, "无座", "硬座", "硬卧", "软卧", "二等座", "一等座", "商务座"):
                        if kw and kw in body:
                            seat_on_page = kw
                            break
                    warn = ""
                    if seat_on_page and seat_on_page != seat_name:
                        warn = "（注意：页面显示席别为 %s，与期望的 %s 不一致，请核对订单）" % (
                            seat_on_page, seat_name)
                    if dlg_seat and dlg_seat != seat_name:
                        warn += "（核对窗曾显示席别 %s，请以订单详情为准）" % dlg_seat
                    mark("result")
                    # 成功页就是「席位已锁定」的付款页：先别关窗，留给用户核对与
                    # 支付（关窗 / 手动停止 / 超时才继续）。headless 没有窗口可看，
                    # 且自动化场景不需要人工确认，直接跳过。
                    if not headless:
                        _hold = _pay_page_hold_seconds()
                        if _hold > 0:
                            _hold_pay_page(page, _hold, _stopped)
                    return True, "已提交订单（未支付）：%s%s" % (url, warn), {
                        "passengers": "、".join(picked), "seat_on_page": seat_on_page,
                        "seat_in_dialog": dlg_seat,
                        "pay_evidence": pay_evidence, "page_kind": "payOrder"}
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
                            # 再重试只会得到同样的拒绝，必须停下让用户去支付。
                            # dup_kind 让上层区分"行程冲突"和"本行程已有订单"
                            return False, body, {"reason": "dup",
                                                 "dup_kind": next(k for k in _dup_keys if k in _body)}
                        if clicked:
                            # 确认已点出：网页的失败提示可能是误报（实跑：页面
                            # 报「出票失败」但订单已生成待付款）——不当普通失败
                            # 让上层盲重试，标 ambiguous 交上层官方回读定生死
                            # （回读到本次未支付订单=成功，确认无单才安全重试）。
                            return (False,
                                    "网页提示下单失败（%s）但确认已提交——无法确认"
                                    "本次下单结果，交官方订单接口回读" % body,
                                    {"reason": "ambiguous"})
                        return False, body, None
            mark("result")
            if clicked:
                # 确认已点出去：订单可能已在服务端生成。结果未知时绝不能让上层
                # 当普通失败盲目重试（重复下单窗口），标记 ambiguous 交人工核对
                return (False,
                        "提交后 %d 秒未收到明确结果（当前页：%s）——结果未知，请先到 12306 核对未完成订单"
                        % (int(time.time() - t_submit), page.url), {"reason": "ambiguous"})
            return False, "提交后 %d 秒未收到明确结果（当前页：%s）" % (
                int(time.time() - t_submit), page.url), None
        except Exception as e:
            mark("result")
            if clicked:
                return (False,
                        "提交确认后异常（结果未知，请先到 12306 核对未完成订单）：%s: %s"
                        % (type(e).__name__, str(e)[:150]), {"reason": "ambiguous"})
            # 浏览器窗口/上下文死亡（TargetClosedError 等）不再是普通失败：
            # 显式标 browser_dead，上层据此回读官方订单接口定生死，绝不盲重试
            # （问题一：旧行为伪装普通失败→每轮重开窗口→无限刷）
            if _window_dead_exc(e):
                return False, "浏览器窗口异常关闭: %s: %s" % (
                    type(e).__name__, str(e)[:180]), {"reason": "browser_dead"}
            return False, "浏览器下单异常: %s: %s" % (type(e).__name__, str(e)[:180]), None
        # 无 finally：冷启动路径的收尾（save_state + ctx.close）挂在 ExitStack
        # 回调上，预热路径不关窗、留给 WarmSession 继续用。


def order_via_browser(info, seat_name, seat_code, passenger_names, date,
                      headless=False, verify_timeout=90, purpose="ADULT", warm=None,
                      alias_name=None, purpose_map=None, **kwargs):
    """用真实浏览器完成一次下单。返回 (ok, msg, extra)。

    每次下单都往 logs/order_timing.jsonl 追加一条分阶段耗时（需求 3），
    传 warm=WarmSession 可复用预热现场（需求 2）。
    alias_name: 同价改判目标席别名（勾选无座 → 硬座）；非空时会在 extra 里标注
        alias_seat / selected_seat，方便上层如实记账。
    purpose_map: {姓名: 票种代码}，按每个乘车人分别对齐成人票 / 学生票。

    Task 70a：stale warm 的 close() 会释放两把锁并把 _LOCK_LOCAL.depth 清零，
    因此必须在进入 @_exclusive 装饰器之前处理——若在装饰器体内 close，重入
    分支的 finally 会把 depth 恢复为进入前的值，造成"本线程仍持有锁"的假象，
    此后本线程的 exclusive() 永久走重入捷径、跨进程 profile 互斥静默失效（P2）。
    """
    try:
        if warm is not None and not warm.usable():
            # 预热现场已失效（页面被关 / 跨了线程）：先释放它占着的锁再进锁
            try:
                warm.close()
            except Exception:
                pass
            warm = None
        if warm is not None:
            # Task 95：warm 复用前复验会话有效性。usable() 只看页面存活，
            # 看不到预热等待期（默认 10 分钟）内被踢/过期的会话；若直接复用，
            # warm.refresh() 会在死会话上烧 ~20-30 秒抛误导性 TimeoutError，
            # 连续几次后任务被误判自停（漏单，P2）。
            # 会话已失效 → 关闭预热现场，改走冷启动路径（内含诚实的会话校验
            # 与"请先运行 login"指引），不烧误导性超时、不误判自停。
            # 复用 warm 自带的 page 做校验（不新开标签页）；校验在进锁前完成，
            # 与 Task 70a 的"close 必须在装饰器外"同理。
            try:
                ok, who = session_ok(warm.ctx, page=warm.page)
            except Exception as e:
                ok, who = False, "预热会话校验异常: %s" % str(e)[:100]
            if not ok:
                _log("  [浏览器] 预热期会话已失效（%s），关闭预热现场改走冷启动" % who)
                try:
                    warm.close()
                except Exception:
                    pass
                warm = None
    except Exception as e:
        if _window_dead_exc(e):
            return False, "浏览器窗口异常关闭: %s: %s" % (
                type(e).__name__, str(e)[:180]), {"reason": "browser_dead"}
        return False, "浏览器下单异常: %s: %s" % (type(e).__name__, str(e)[:180]), None
    return _order_via_browser_locked(
        info, seat_name, seat_code, passenger_names, date,
        headless=headless, verify_timeout=verify_timeout, purpose=purpose,
        warm=warm, alias_name=alias_name, purpose_map=purpose_map, **kwargs)


@_exclusive(lock_timeout=180)
def _order_via_browser_locked(info, seat_name, seat_code, passenger_names, date,
                              headless=False, verify_timeout=90, purpose="ADULT", warm=None,
                              alias_name=None, purpose_map=None, should_stop=None):
    """order_via_browser 的锁内本体（stale warm 已在外层 wrapper 处理，见上）。"""
    tm = {}
    t0 = time.perf_counter()
    try:
        ok, msg, extra = _order_impl(info, seat_name, seat_code, passenger_names,
                                     date, headless=headless,
                                     verify_timeout=verify_timeout,
                                     purpose=purpose, warm=warm, tm=tm,
                                     alias_name=alias_name, purpose_map=purpose_map,
                                     should_stop=should_stop)
    except RuntimeError:
        raise  # 抢锁失败等：交给调用方决定重试节奏
    except Exception as e:
        if _window_dead_exc(e):
            ok, msg, extra = False, "浏览器窗口异常关闭: %s: %s" % (
                type(e).__name__, str(e)[:180]), {"reason": "browser_dead"}
        else:
            ok, msg, extra = False, "浏览器下单异常: %s: %s" % (type(e).__name__, str(e)[:180]), None
    if alias_name:
        extra = dict(extra or {})
        extra.setdefault("alias_seat", alias_name)
        extra.setdefault("selected_seat", seat_name)
        if ok:
            msg = "%s（勾选 %s，网页端同价按 %s 下单）" % (msg, seat_name, alias_name)
    _record_timing(tm, time.perf_counter() - t0, ok, msg, info, seat_name, warm)
    return ok, msg, extra


def _recover_browser_lock(owner_thread, probe_timeout=5):
    """WarmSession 跨线程收尾时进程内浏览器锁的恢复路径（Task 65h）。

    背景：threading.RLock 只能由持锁线程 release；close() 若在非创建线程被
    调用，旧代码只打一条警告就把 _local_locked 置 False——锁实际仍被（可能已
    死的）持锁线程占着，本进程后续所有 exclusive() 永久超时。

    恢复步骤：
    1. 短超时探针：锁若当前可获取，说明并未泄漏（或已被释放），拿走再释放
       即可，无需重建；
    2. 超时仍拿不到：只有当持锁线程已死才重建 _BROWSER_LOCK。本模块所有调用
       方都经模块全局名引用它，重建后新的 acquire 走新锁；旧锁由已死线程持
       有、永不可再释放，重建是唯一的出路；
    3. 持锁线程仍存活：不重建（会破坏它后续的正常释放），记 error 返回 False，
       调用方应提示用户重启该程序。

    返回 True 表示锁已不再泄漏（可正常释放或已重建）。
    """
    global _BROWSER_LOCK
    lock = _BROWSER_LOCK
    try:
        if lock.acquire(timeout=probe_timeout):
            try:
                lock.release()
            except RuntimeError:
                pass
            return True
    except Exception:
        pass
    if owner_thread is not None and owner_thread.is_alive():
        _log("[错误] WarmSession 由非创建线程收尾，持锁线程仍存活："
             "进程内浏览器锁无法安全释放，本进程后续 exclusive() 可能超时"
             "——建议重启该程序")
        return False
    _log("[错误] WarmSession 由非创建线程收尾且持锁线程已退出："
         "强制重建进程内浏览器锁（旧锁由已死线程持有，永不可释放）")
    _BROWSER_LOCK = threading.RLock()
    return True


class WarmSession:
    """开抢前预热好的「下单现场」：浏览器已开、已登录、车票页条件已填、查询已发。

    到点时 order_via_browser(warm=...) 只做「刷新 → 点预订 → 提交」，把冷启动
    的 10~15 秒准备压到 2 秒上下。生命周期内一直持有 profile 锁（手动 acquire，
    不走 with exclusive，因为要跨函数长持有）；close() 必须被调用，否则别的
    进程会被一直挡在门外。进程被强杀时 OS 自动释放文件锁，不会留死锁。"""

    def __init__(self, info, date, headless=False, timeout=120, wait_code=None,
                 stop_event=None):
        self.info = info
        self.date = date
        self._p = None
        self._ctx = None
        self._page = None
        self._closed = False
        self._local_locked = False
        self._file_locked = False
        self._owner = threading.get_ident()
        # Task 65h：持锁线程对象（非 ident，避免 ident 被系统回收复用导致误判）
        self._owner_thread = threading.current_thread()
        self._timeout = timeout
        self._stop_event = stop_event
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
        def _stopped():
            return self._stop_event is not None and self._stop_event.is_set()
        try:
            if _stopped():
                raise RuntimeError("预热已手动停止")
            if not _BROWSER_LOCK.acquire(timeout=self._timeout):
                raise RuntimeError("另一处正在使用浏览器（登录/体检/下单），预热失败")
            self._local_locked = True
            if _stopped():
                raise RuntimeError("预热已手动停止")
            if not _PROFILE_LOCK.acquire(timeout=self._timeout):
                raise RuntimeError("另一个程序正在使用浏览器（登录/体检/下单），预热失败")
            self._file_locked = True
            _set_depth(1)   # 同线程重入标记：order_via_browser 的装饰器会放行
            if _stopped():
                raise RuntimeError("预热已手动停止")
            from playwright.sync_api import sync_playwright
            self._p = sync_playwright().start()
            self._ctx = launch(self._p, headless=headless)
            self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
            self._page.set_default_timeout(20000)
            ok, who = session_ok(self._ctx, page=self._page)
            if not ok:
                raise RuntimeError("预热时会话不可用：%s" % who)
            if _stopped():
                raise RuntimeError("预热已手动停止")
            hit = _goto_and_query(self._page, self.info, self.date,
                                  want_hit=False, timeout=30000, wait_code=wait_code)
            if not hit:
                # 掩盖预热失败没有意义：到点下单时 refresh() 会重查，如实记录
                _log("  [预热] 目标车次行未出现（可能无票/未放票），到点下单时会重新查询")
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

    def close(self, release_locks=True):
        """关闭预热现场。

        release_locks=False：只关闭浏览器资源（ctx/page），不释放 profile/线程锁、
        不碰 depth——供已在 @_exclusive 锁内的调用点（_order_impl 的 stale-warm
        处理，Task 101g）使用：完整 close() 会释放装饰器仍需要的互斥并清零 depth，
        本次下单后半程失去 profile 互斥。锁的释放留给外层调用方的 finally
        做完整 close()（launcher 抢票主循环的 finally 即如此）。
        """
        if not self._closed:
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
        if not release_locks:
            return
        # Task 85d：owner-aware——无论哪条线程执行 close()，都清理 owner 线程
        # 的 depth 记录。跨线程 close 后 owner 的 depth 若残留 1，其后续
        # exclusive() 会走重入捷径跳过跨进程文件锁（P2）。owner 存活且仍持有
        # _BROWSER_LOCK 时（_recover_browser_lock 返回 False 的路径），depth
        # 清零使其下次 exclusive() 走正常获取路径：RLock 同线程重入安全，
        # 文件锁已被本函数释放故可重获，无死锁。
        if _get_depth(self._owner_thread):
            _set_depth(0, self._owner_thread)
        if self._file_locked:
            _PROFILE_LOCK.release()
            self._file_locked = False
        if self._local_locked:
            if self._owner == threading.get_ident():
                _BROWSER_LOCK.release()
            else:
                # Task 65h：RLock 只能由持锁线程释放；跨线程收尾走
                # "超时检测 + 强制恢复"路径，不再永久泄漏
                _recover_browser_lock(self._owner_thread)
            self._local_locked = False


def warm_up(info, date, headless=False, timeout=120, wait_code=None, stop_event=None):
    """开抢前预热：打开浏览器、确认登录、停在目标车次的列表页。

    stop_event: threading.Event，置位后预热尽快中止（ RuntimeError）。
    返回 WarmSession；失败抛 RuntimeError（调用方回退到普通下单）。"""
    return WarmSession(info, date, headless=headless, timeout=timeout,
                       wait_code=wait_code, stop_event=stop_event)


def order_ticket_via_browser(config, task, ticket, seat_name, should_stop=None):
    """order_ticket 的浏览器实现，签名与 order.py 的 HTTP 版一致。

    供 order.py 在 config["order_mode"] == "browser" 时直接转发调用。
    should_stop: 引擎/启动器的停止事件，贯穿下单链（滑块与付款页驻留
    期间也据此尽快收手，2026-10-19 审查）。
    """
    alias_name = None
    try:
        from ticket import SEAT_NAME_TO_CODE, order_seat_code
        seat_code = SEAT_NAME_TO_CODE.get(seat_name)
        # 网页端下单页不下发「无座」：按同价席别改判（记账仍按勾选的席别）
        seat_code, alias_name = order_seat_code(seat_name, seat_code, ticket.get("train_code"))
    except Exception:
        seat_code = None
    if not seat_code:
        return False, "未知席别: %s" % seat_name, None

    date = ticket.get("query_date")
    if not date:
        # start_date(p13) 是无横线的始发日期，跨夜车与乘车日可能差一天——
        # 宁可拒绝下单也不能静默订错日期
        return False, "查询结果缺少乘车日期（query_date），拒绝下单以防订错日期", None
    names = list(task.get("passenger_names") or [])
    # 按人票种：任务里存了 {姓名: 票种代码} 就逐人下发，覆盖到的走本人票种
    purpose_map = dict(task.get("pax_purpose") or {})
    purpose = task.get("purpose_code") or "ADULT"
    if purpose_map and names:
        _codes = [purpose_map.get(n) or purpose for n in names]
        purpose = "0X00" if all(c == "0X00" for c in _codes) else "ADULT"
    if config.get("order_flow_mode", True):
        # 新链：阶段化管线 orderflow（默认开启）。旧链 order_via_browser
        # 保留到实跑验证成功后退役（双链并存，REBUILD_PLAN.md）。
        try:
            import orderflow
        except ImportError:
            pass
        else:
            ok, msg, extra = orderflow.run_orderflow(
                ticket, seat_name, seat_code, names, date,
                headless=bool(config.get("browser_headless", False)),
                alias_name=alias_name, purpose=purpose, purpose_map=purpose_map,
                should_stop=should_stop,
                flow_cfg=config.get("order_flow") or {},
                logger=logging.getLogger("monitor"))
            if ok:
                extra = dict(extra or {})
                extra.setdefault("passengers", "、".join(names))
            return ok, msg, extra
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
    p = TIMING_PATH
    if not os.path.exists(p):
        print("还没有下单耗时记录（logs/order_timing.jsonl）")
        return 1
    recs = []
    with open(p, "r", encoding="utf-8-sig") as f:
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
