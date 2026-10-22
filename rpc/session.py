# -*- coding: utf-8 -*-
"""rpc.session —— RPC 域 session.*：登录会话探测、看门狗、重新登录与账号信息。

A-3a 自 api.py 逐字拆分：函数体未改动，仅补 import 与模块限定。
"""
import datetime
import json
import os
import queue
import re
import sys
import threading
import time
import traceback

from rpc.common import (_warn, engine_running, get_engine, load_config, push)

import grabber_utils as launcher_utils
import order as order_mod
import browser_order

# ---------------- 会话 / 登录 ----------------

def current_session():
    """当前登录会话（requests.Session），已登录时同时重置会话探测缓存。

    浏览器模式的真实会话在 .browser_profile / .browser_state.json；session_cookies.json
    是 capture_session 时代的旧载体，浏览器模式下读它只会得到一个跟现实无关的结果
    —— 界面显示「未登录」，浏览器与日志里却明明已登录（checkSession 恒 false）。

    注意：返回的 Session 只在调用线程内用（requests 的连接池不跨线程共享）。
    """
    reset_session_cache()
    if (load_config().get("order_mode") or "http") == "browser":
        return order_mod.session_from_browser_state()
    return order_mod.load_session()

_SESSION_CACHE = {"at": 0.0, "res": None}
_SESSION_TTL = 20.0   # checkSession 与 getAccount 每轮总是一起调用，缓存一份免得验两遍

def reset_session_cache():
    _SESSION_CACHE["at"] = 0.0
    _SESSION_CACHE["res"] = None

def _probe_browser_session():
    """浏览器模式的会话探测。

    快路用 .browser_state.json 的 cookie 直接打校验接口（约 0.6 秒，不碰浏览器）；
    只有它**明确报失效**时才开一次真实浏览器复核 —— 12306 对没有浏览器指纹的请求
    会回风控页，拿快路的下线结论直接报「未登录」就是本 bug 的镜像。真实浏览器校验
    要抢 profile 锁（引擎正在登录/下单时抢不到），所以只留给确认环节，抢不到就返回
    unknown：这不是掉线的证据。
    """
    if not os.path.exists(browser_order.STATE_PATH):
        # 从没登录过：这是明确的未登录，不是「没验成」
        return {"ok": False, "unknown": False, "permanent": True, "name": "",
                "message": "尚未登录（没有会话存档），点「重新登录」"}
    try:
        sess = order_mod.session_from_browser_state()
    except Exception as e:
        return {"ok": False, "unknown": True, "permanent": False,
                "message": str(e), "name": ""}
    ok, msg, permanent = order_mod.verify_session(sess)
    if ok:
        return {"ok": True, "unknown": False, "permanent": True,
                "message": str(msg or ""), "name": str(msg or "")}
    if not permanent:
        return {"ok": False, "unknown": True, "permanent": False,
                "message": str(msg or ""), "name": ""}
    if browser_order.busy():
        return {"ok": False, "unknown": True, "permanent": False,
                "message": "浏览器正忙（登录/下单中），稍后自动重试", "name": ""}
    try:
        ok2, who = browser_order.check_session(timeout=6)
    except Exception as e:
        return {"ok": False, "unknown": True, "permanent": False,
                "message": str(e), "name": ""}
    if ok2:
        return {"ok": True, "unknown": False, "permanent": True,
                "message": str(who or ""), "name": str(who or "")}
    return {"ok": False, "unknown": False, "permanent": True,
            "message": str(who or msg or ""), "name": ""}

def probe_session(force=False):
    """当前登录态 {ok, unknown, permanent, message, name}。

    unknown=True 表示这一轮没验成（浏览器忙 / 风控 / 网络），前端据此保留上一次
    结论，而不是把有效会话显示成「未登录」。force=True 走真实浏览器校验。
    """
    now = time.time()
    if (not force and _SESSION_CACHE["res"] is not None
            and now - _SESSION_CACHE["at"] < _SESSION_TTL):
        return _SESSION_CACHE["res"]
    if (load_config().get("order_mode") or "http") != "browser":
        try:
            ok, msg, permanent = order_mod.verify_session(order_mod.load_session())
        except Exception as e:
            res = {"ok": False, "unknown": False, "permanent": True,
                   "message": str(e), "name": ""}
        else:
            res = {"ok": bool(ok), "unknown": not ok and not permanent,
                   "permanent": bool(permanent), "message": str(msg or ""),
                   "name": str(msg or "") if ok else ""}
    else:
        res = _probe_browser_session()
    # 两种模式共用缓存：http 的 3×15s 校验不能被前端与看门狗每 60s 各打一遍
    _SESSION_CACHE["at"] = time.time()
    _SESSION_CACHE["res"] = res
    return res

_SESSION_WATCH = {"expired_pushed": False}

def _session_watch_step(res):
    """会话看门狗单步：res 为 probe_session() 的结果。
    返回 (should_push, delay_seconds)。只有「确认失效」的边沿才推一次弹窗事件，
    推过之后转 30 分钟长节拍（失效期少开真实浏览器复核），恢复（ok）即复位。"""
    if res.get("ok"):
        _SESSION_WATCH["expired_pushed"] = False
        return False, 60
    if res.get("unknown") or not res.get("permanent"):
        return False, 60
    if not _SESSION_WATCH["expired_pushed"]:
        _SESSION_WATCH["expired_pushed"] = True
        return True, 1800
    return False, 1800

def _session_watch_loop():
    """会话看门狗线程：探测 12306 登录态，确认失效时向前端推 sessionExpired
    弹窗事件（不重复推，直到会话恢复）。启动即探测一次，之后按 step 的节拍。"""
    while True:
        try:
            res = probe_session()
        except Exception:
            time.sleep(60)
            continue
        should_push, delay = _session_watch_step(res)
        if should_push:
            push("sessionExpired", {"message": res.get("message") or "登录已失效"})
        time.sleep(delay)

def rpc_check_session(p):
    p = p or {}
    try:
        return probe_session(force=bool(p.get("force")))
    except Exception as e:
        return {"ok": False, "unknown": True, "permanent": False,
                "message": str(e), "name": ""}

# 登录单飞：设置页「重新登录」/ 失效弹窗「立即登录」/ 头像 doLogin 三个入口都能发
# relogin。登录窗口还开着时再点一次，旧代码会白开第二支浏览器（还要抢同一把
# profile 锁，login 的 lock_timeout=90，把真正那次一起拖慢）。非阻塞占用即互斥。
_LOGIN_GUARD = threading.Lock()

def rpc_relogin(p):
    """browser 模式：会话还活着就先说清楚，真需要人工验证才拉起真实 Edge 滑块。

    为什么先预检：会话有效时 login() 2 秒就判定成功并关窗，用户只看到窗口一闪，
    会当成「拉起登录失败」；而 login() 抢不到 profile 锁要干等 lock_timeout 秒，
    期间界面没有任何反馈。预检走快路（cookie 打接口），不碰浏览器。
    p.force=True 跳过预检强制重登（界面「重新登录」按钮确认后走这条）。

    同一时刻只允许一次登录：进行中再点返回 running=True，由界面提示等待。
    """
    import threading as _th
    p = p or {}
    if not _LOGIN_GUARD.acquire(blocking=False):
        return {"ok": True, "running": True,
                "message": "登录窗口已经打开了，请先在浏览器里完成验证"}

    started = False
    try:
        if not p.get("force"):
            try:
                res = probe_session(force=True)
            except Exception as e:
                _warn("重新登录预检", e)
                res = {}
            if res.get("ok"):
                try:
                    who = launcher_utils.mask_mobile(res.get("name") or "") or "已登录"
                except Exception:
                    who = res.get("name") or "已登录"
                push("log", {"line": "[会话] 会话仍然有效（%s），无需重新登录" % who, "tag": "ok"})
                push("session", {"ok": True})
                return {"ok": True, "already": True, "name": who,
                        "message": "当前已登录（%s），无需重新登录" % who}

        def _do():
            try:
                push("log", {"line": "[会话] 正在拉起登录窗口，请在浏览器里扫码或账密+滑块完成验证…",
                             "tag": "info"})
                try:
                    # login() 返回 False 是「超时/取消」，别把没成功也报成已完成
                    ok = bool(browser_order.login())
                    push("log", {"line": "[会话] 重新登录成功" if ok else "[错误] 重新登录未完成（超时或已取消）",
                                 "tag": "ok" if ok else "err"})
                except Exception as e:
                    ok = False
                    push("log", {"line": "[错误] 重新登录失败：%s" % e, "tag": "err"})
                reset_session_cache()          # 下一次探测重新验，不用等缓存过期
                push("session", {"ok": ok})
                if ok:
                    try:
                        if engine_running():
                            # 立刻触发引擎的会话体检（主循环 1s 内会读到）：
                            # 正常节流 20 分钟，不触发的话用户登录完仍看到「已失败」，
                            # 只能手动点恢复——再点又因会话刚恢复而重复失败（实测）。
                            get_engine()._last_session_check = 0
                    except Exception as e:
                        _warn("登录成功后触发会话恢复", e)
            finally:
                _LOGIN_GUARD.release()         # 必须复位，否则再也登不上

        _th.Thread(target=_do, daemon=True).start()
        started = True
        return {"ok": True, "message": "已拉起浏览器，等待人工完成验证"}
    finally:
        if not started:
            _LOGIN_GUARD.release()

def rpc_get_account(_p):
    """账号显示名（脱敏）。

    旧实现把 load_session() 的返回值当 dict 取 "login_user"，但它其实是
    requests.Session —— 取键会抛 MissingSchema，于是恒返回「未登录」。
    名字一律走 verify_session 的真实 user_name（浏览器模式下本地没有该字段）。
    """
    try:
        res = probe_session()
        if not res.get("ok"):
            return {"name": "", "logged_in": False}
        name = res.get("name") or ""
        if name:
            try:
                name = launcher_utils.mask_mobile(name)
            except Exception as e:
                _warn("手机号掩码", e)
        return {"name": name or "已登录", "logged_in": True}
    except Exception as e:
        _warn("探测登录态", e)
        return {"name": "", "logged_in": False}
