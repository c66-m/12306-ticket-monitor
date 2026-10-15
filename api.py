# -*- coding: utf-8 -*-
"""12306 桌面版 API 服务（Electron 后端）——取代 gui.py 的界面层。

协议：stdin/stdout 逐行 JSON-RPC 2.0。
  请求：{"jsonrpc":"2.0","id":N,"method":"<name>","params":{...}}
  响应：{"jsonrpc":"2.0","id":N,"result":...} 或 {"error":{"message":...}}
  推送：{"jsonrpc":"2.0","method":"log"/"status","params":{...}}（无 id）

业务层全部复用 launcher / ticket / engine / passengers / order / notify，
本文件只做：JSON-RPC 分发 + LOGQ 日志泵 + 引擎线程管理。零业务逻辑。
"""
import json
import os
import queue
import re
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import launcher
import launcher_utils
import ticket
import engine as engine_mod
import passengers as passengers_mod
import appcommon
import filelock
import order as order_mod
import browser_order
import notify as notify_mod

CONFIG_PATH = os.path.join(HERE, "config.json")

# 中文 Windows 下 stdin/stdout 默认走 ANSI 代码页（GBK），而 Electron 侧写的是 UTF-8 字节，
# 中文参数会被按 GBK 解码成乱码（症状：searchStation 查「长葛」返回空、拼音正常）。
# 协议两端统一 UTF-8，本文件是协议端点，编码属于它的契约。
for _stream in (sys.stdin, sys.stdout):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception as e:
        _warn("stdio 切 UTF-8", e)

OUT_LOCK = threading.Lock()
WRITE = sys.stdout.write
FLUSH = sys.stdout.flush


def _warn(what, exc):
    """静默 except 的观测出口：不改控制流，只在 stderr 留一行。

    进程 stderr 由 electron/main.js:74 转发到 electron/_eerr.txt。"""
    try:
        print("[api] %s: %r" % (what, exc), file=sys.stderr)
    except Exception:
        pass


def emit(obj):
    """线程安全写一行 JSON 到 stdout（Electron 读取）。"""
    line = json.dumps(obj, ensure_ascii=False, default=str)
    with OUT_LOCK:
        WRITE(line + "\n")
        FLUSH()


def push(method, params):
    emit({"jsonrpc": "2.0", "method": method, "params": params})


def reply(id_, result=None, error=None):
    obj = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        obj["error"] = {"message": str(error)}
    else:
        obj["result"] = result
    emit(obj)


# ---------------- 纯函数（自 gui.py 内联，避免 import tkinter） ----------------

STATUS_COLOR = {"monitoring": "#0969da", "retrying": "#d97706", "paused": "#6e7781",
                "cancelled": "#6e7781", "success": "#1a7f37", "failed": "#cf222e"}

HISTORY_RESULT_LABEL = {"success": "下单成功", "dup": "防重跳过",
                        "notified": "已通知", "failed": "下单失败", "error": "异常"}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_CONFIG_CORRUPT_WARNED = False

# 乐观并发检查的哨兵：update_config_locked 的 expect_stamp 取此值表示"不检查"
#（旧调用方式）；None 是合法指纹，表示"调用方 load 时文件不存在"。
# 与 passengers._STAMP_UNSET / monitor._STAMP_UNSET 同口径。
_STAMP_UNSET = object()


def _config_stamp():
    """config.json 当前内容的字节指纹（sha256），与 monitor._config_stamp 同义。

    两份都只是各自模块 CONFIG_PATH 对 appcommon.stat_fingerprint 的绑定，
    真实现已单源；不要为「去重」再抽一层（CONFIG_PATH 是各模块自己的常量）。

    直接复用 appcommon.stat_fingerprint（同一份 sha256 内容哈希实现，文件读不出
    返回 None），不再造第二份。用内容哈希而非 mtime：他进程重写相同内容不误报，
    任何字节变化必检出。
    """
    return appcommon.stat_fingerprint(CONFIG_PATH)


def load_config():
    """读 config.json（gui.py 116 原版语义，messagebox 换日志）。"""
    global _CONFIG_CORRUPT_WARNED
    try:
        with open(CONFIG_PATH, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        if not _CONFIG_CORRUPT_WARNED:
            _CONFIG_CORRUPT_WARNED = True
            print("[api] config.json 解析失败，按空配置", file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save_config(config):
    with filelock.file_lock(CONFIG_PATH + ".lock"):
        appcommon.atomic_write_json(CONFIG_PATH, config, mode=0o600)


def load_state():
    config = load_config()
    return engine_mod.load_state_file(
        os.path.join(HERE, config.get("state_file", "state.json")))


def remove_task_from_config(config, task):
    tasks = config.get("tasks")
    if isinstance(tasks, list):
        key = task.get("name")
        config["tasks"] = [t for t in tasks if t.get("name") != key]
    return config


def update_config_locked(fn, expect_stamp=_STAMP_UNSET):
    """config.json 读-改-写原子接口（gui.py 201 原版语义，损坏拒绝覆盖）。

    expect_stamp 为 _config_stamp() 在调用方 load 之后取到的指纹时做乐观并发
    检查（与 passengers.save_passengers / monitor.save_config 同口径）：在锁内
    重读当前指纹，不一致说明两次操作之间有外部写入（抢票进程 / 另一个窗口）——
    放弃本次保存并返回 False，不静默覆写对方的修改；一致则写入并返回 True。
    不传 expect_stamp 时保持旧行为（直接写）。返回值自本轮起统一为布尔
    （旧行为是返回 mutator 的返回值，仓内两个调用点均未消费）。
    """
    with filelock.file_lock(CONFIG_PATH + ".lock"):
        if expect_stamp is not _STAMP_UNSET and _config_stamp() != expect_stamp:
            return False
        try:
            with open(CONFIG_PATH, encoding="utf-8-sig") as f:
                config = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise RuntimeError("config.json 已损坏：本次保存已取消，旧文件未被覆盖")
        if not isinstance(config, dict):
            raise RuntimeError("config.json 内容非 dict：本次保存已取消，旧文件未被覆盖")
        fn(config)
        appcommon.atomic_write_json(CONFIG_PATH, config, mode=0o600)
        return True


def update_state_locked(fn):
    """state.json 读-改-写原子接口（gui.py 245 原版语义）。"""
    config = load_config()
    path = os.path.join(HERE, config.get("state_file", "state.json"))
    with filelock.file_lock(path + ".lock"):
        state = engine_mod.load_state_file(path)
        if not isinstance(state, dict):
            state = {}
        fn(state)
        appcommon.write_state(path, state, tmp_kind="apisave")


def delete_task_everywhere(app, task):
    """config 移除 + state 条目清除 + 引擎运行时通知（gui.py 409 原版逻辑）。"""
    name = task.get("name") or ""
    update_config_locked(lambda config: remove_task_from_config(config, task))
    if app is not None and getattr(app, "engine_thread", None) is not None \
            and app.engine_thread.is_alive():
        try:
            app.get_ops_engine().note_task_deleted(name)
            return
        except Exception as e:
            _warn("note_task_deleted", e)

    def _clear(state):
        tasks = state.get("tasks")
        if isinstance(tasks, dict):
            tasks.pop(name, None)
    update_state_locked(_clear)


def normalize_email_settings(email):
    """校验清洗邮件配置（gui.py 437 原版逻辑）。"""
    user = (email.get("username") or "").strip()
    if not EMAIL_RE.match(user):
        raise ValueError("发件邮箱格式不正确：「%s」" % user)
    from_addr = (email.get("from") or "").strip()
    if not EMAIL_RE.match(from_addr):
        from_addr = user
    email["username"] = user
    email["from"] = from_addr
    to_list = [x.strip() for x in (email.get("to") or []) if x.strip()]
    bad = [x for x in to_list if not EMAIL_RE.match(x)]
    if bad:
        raise ValueError("收件人格式不正确：%s" % "、".join(bad))
    if not to_list:
        raise ValueError("请至少填写一个收件人邮箱")
    email["to"] = to_list
    return email


def read_history_records(limit=200):
    """读购票历史（gui.py 1686 原版逻辑）。"""
    path = os.path.join(HERE, load_config().get("history_file", "order_history.json"))
    try:
        with open(path, encoding="utf-8-sig") as f:
            history = json.load(f)
        if not isinstance(history, list):
            return []
        return list(reversed(history[-limit:]))
    except Exception as e:
        _warn("读取历史文件", e)
        return []


# ---------------- 引擎管理（单例） ----------------

_engine = None
_engine_lock = threading.Lock()
_engine_thread = None
_engine_stop = None  # 当前引擎线程的 stop_event，stop_engine() 用它请求优雅退出


def get_engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = engine_mod.MonitorEngine(setup_logging=False)
        return _engine


def engine_running():
    global _engine_thread
    return _engine_thread is not None and _engine_thread.is_alive()


def start_engine():
    """启动监控引擎线程（幂等）。"""
    global _engine_thread, _engine_stop
    if engine_running():
        return True
    eng = get_engine()
    stop_evt = threading.Event()
    t = threading.Thread(target=eng.run, args=(stop_evt,), daemon=True)
    t.start()
    _engine_thread = t
    _engine_stop = stop_evt
    launcher.log("[启动] 监控引擎已启动")
    return True


def stop_engine():
    """停止监控引擎线程：置位 stop_event，等主循环收尾（默认最长 10s）。
    引擎真正退出后，把状态文件里仍在 monitoring/retrying 的任务标为 paused，
    避免重启后「监控中」的假象（引擎都没了，任务不可能还在监控）。"""
    global _engine_thread, _engine_stop
    if _engine_thread is None or not _engine_thread.is_alive():
        _engine_thread = None
        _engine_stop = None
        return True
    if _engine_stop is not None:
        _engine_stop.set()
    _engine_thread.join(timeout=10)
    alive = _engine_thread.is_alive()
    if not alive:
        _engine_thread = None
        _engine_stop = None
    if not alive:
        # T6：引擎停止后，状态停留 monitoring 的任务实际上已停摆——落盘标 paused
        _mark_running_tasks_paused()
    launcher.log("[停止] 监控引擎%s" % ("仍在收尾（在途任务未结束）" if alive else "已停止"))
    return not alive


def _mark_running_tasks_paused():
    """引擎停摆后把 monitoring/retrying 任务统一标 paused，message 注明原因。
    纯文件操作，不碰引擎对象（引擎线程已死，调它反而危险）。"""
    changed = [0]

    def _set(state):
        tasks = state.get("tasks")
        if not isinstance(tasks, dict):
            return
        for name, rec in tasks.items():
            if isinstance(rec, dict) and rec.get("status") in ("monitoring", "retrying"):
                rec["status"] = "paused"
                rec["message"] = "引擎已停止"
                changed[0] += 1

    try:
        update_state_locked(_set)
        if changed[0]:
            launcher.log("[停止] %d 个任务已标记暂停（引擎已停止）" % changed[0])
    except Exception as e:
        launcher.log("[停止] 标记任务暂停失败：%s" % e)
    return changed[0]


# ---------------- 日志泵 ----------------

def log_pump():
    """把 launcher.LOGQ 的日志推给前端。"""
    while True:
        try:
            record = launcher.LOGQ.get(timeout=0.3)
        except queue.Empty:
            continue
        tag = "info"
        if "[有票]" in record or "[抢到]" in record or "[成功]" in record:
            tag = "ok"
        elif "[错误]" in record or "[失败]" in record:
            tag = "err"
        elif "[提醒]" in record or "[重试]" in record or "[暂停]" in record:
            tag = "warn"
        elif "[会话]" in record:
            tag = "warn"
        push("log", {"line": record, "tag": tag})


# ---------------- RPC 方法 ----------------

def rpc_list_monitor_tasks(_p):
    cfg = load_config()
    try:
        state = load_state() or {}
    except Exception as e:
        _warn("读取任务状态", e)
        state = {}
    if not isinstance(state, dict):
        state = {}
    # 编辑弹窗要用 code 回填 station-box 的选中态：任务里只存站名
    try:
        _name2code, _code2name = ticket.load_station_map()
    except Exception as e:
        _warn("加载车站映射", e)
        _name2code, _code2name = {}, {}
    tasks_out = []
    for t in cfg.get("tasks") or []:
        name = t.get("name", "")
        rec = (state.get("tasks") or {}).get(name) or {}
        tasks_out.append({
            "name": name,
            "from": t.get("from") or "",
            "to": t.get("to") or "",
            "fromCode": _name2code.get(t.get("from") or "", ""),
            "toCode": _name2code.get(t.get("to") or "", ""),
            "dates": t.get("dates") or [],
            "trains": t.get("trains") or [],
            "seat_types": t.get("seat_types") or [],
            "seat_priority": t.get("seat_priority") or "",
            # 落盘字段名是 purpose_code / passenger_names（见 launcher_utils.build_monitor_task）；
            # 早先这里读 t["purpose"] / t["passengers"] 恒为空 → 编辑弹窗回填不出票种和乘车人，
            # 用户保存一次就把乘车人清空了
            "purpose": t.get("purpose_code") or "ADULT",
            "passengers": t.get("passenger_names") or [],
            "priority": t.get("priority") or "",
            "desired_status": t.get("status") or "paused",
            "status": rec.get("status") or t.get("status") or "paused",
            "message": rec.get("message") or "",
            "status_label": engine_mod.STATUS_LABELS.get(rec.get("status") or t.get("status") or "paused", "?"),
        })
    # engineRunning=线程活着；engine_alive=引擎在真监控（线程活着且存在
    # monitoring/retrying 任务）。引擎空跑时任何任务都不是 ACTIVE，engine_alive=False，
    # 前端据此把「是否监控」徽章关掉。
    active = any(t["status"] in ("monitoring", "retrying") for t in tasks_out)
    return {"tasks": tasks_out, "engineRunning": engine_running(),
            "engine_alive": engine_running() and active}


# 12306 预售期：2026-10-14 二分实测，今天+14 天（2026-10-28）仍能查到结果，
# 今天+15 天（2026-10-29）起四个端点全部返回 HTML 错误页（非 JSON）。窗口随官方
# 政策变动，故此天数只用于提前给出可读提示，真实成败仍由查询本身决定。
PRESALE_DAYS = 14


def _split_date_input(p):
    """把前端传来的日期输入归一成 (dates, date_range) 契约形状。

    契约（appcommon.parse_date_range / engine.expand_dates 一致）：dates 是逐日
    ISO 列表，date_range 是 [起, 止] 或空列表。前端曾把 date_range 传成
    "起~止" 字符串，落盘后被 engine 判为形状非法整段丢弃（任务靠 dates 还能跑，
    区间语义却丢了，日志里那行「date_range 形状非法」就是它）。"""
    dates = [str(x) for x in (p.get("dates") or [])
             if isinstance(x, str) and x.strip()]
    raw = p.get("date_range")
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return dates, [str(raw[0]), str(raw[1])]
    text = str(raw or "").strip()
    if not text:
        return dates, []
    try:
        parsed, span = appcommon.parse_date_range(text)
    except ValueError:
        return dates, []
    return dates or parsed, span


def _req_on(p, key):
    """任务开关取值：前端显式传了就用传的，没传就用设置里的全局默认（缺省全开）。

    这四个开关（auto_order / stop_after_order / notify / start_now）已从新建任务
    弹窗移除，改到设置页「任务默认值」；编辑旧任务时前端会显式回传任务原值，
    免得编辑一次就把「本来关着自动下单」的任务悄悄打开。"""
    if p.get(key) is not None:
        return bool(p[key])
    return _task_defaults(load_config())[key]


def rpc_create_task(p):
    """前端传 NewTaskDialog 收集的字段，映射到 build_monitor_task 位置参数。"""
    prio = p.get("priority")
    try:
        prio = int(prio) if str(prio).strip() else 5
    except (TypeError, ValueError):
        prio = 5
    if prio < 1 or prio > 10:
        prio = 5
    pri_raw = str(p.get("seat_priority") or "").strip()
    raw_trains = p.get("trains")
    if isinstance(raw_trains, str):
        raw_trains = re.split(r"[,，\s]+", raw_trains)
    # 前端传的是数组；早先 str(list) 落成 "['K225']" 这种字符串，被当成单个车次码
    trains = [t.strip().upper() for t in (raw_trains or [])
              if isinstance(t, str) and t.strip()]
    seats = [str(s) for s in (p.get("seat_types") or [])]
    auto_order = _req_on(p, "auto_order")
    if auto_order and seats:
        # 与 gui 同规则：自动下单剔除不支持下单的席别
        bad = [s for s in seats if s not in ticket.SEAT_NAME_TO_CODE]
        if bad:
            seats = [s for s in seats if s in ticket.SEAT_NAME_TO_CODE]
            push("log", {"line": "[提醒] 席别 %s 暂不支持自动下单，已从该监控任务移除" % "、".join(bad), "tag": "warn"})
    dates, date_range = _split_date_input(p)
    task = launcher.build_monitor_task(
        str(p.get("from") or ""), str(p.get("to") or ""),
        dates, date_range,
        trains, seats, list(p.get("passengers") or []),
        p.get("purpose") or "ADULT", auto_order,
        _req_on(p, "stop_after_order"), prio,
        pax_purpose=dict(p.get("pax_purpose") or {}),
        seat_priority=pri_raw)
    task["notify_channels"] = ["email"] if _req_on(p, "notify") else []
    if pri_raw:
        try:
            parsed = ticket.seat_rules_parse(pri_raw)
            for warn in parsed["warnings"]:
                push("log", {"line": "[提醒] 首选席别：%s" % warn, "tag": "warn"})
        except Exception as e:
            _warn("首选席别解析", e)
    launcher.append_monitor_task(task)
    if _req_on(p, "start_now"):
        start_engine()
        get_engine().set_task_status(task, "monitoring", "新建任务立即启动", force=True)
    return {"ok": True, "name": task.get("name")}


def rpc_set_task_status(p):
    """p = {name, status, message?}"""
    name = p.get("name")
    status = p.get("status")
    cfg = load_config()
    task = next((t for t in cfg.get("tasks") or [] if t.get("name") == name), None)
    if not task:
        return {"ok": False, "error": "任务不存在：" + str(name)}
    if engine_running():
        get_engine().set_task_status(task, status, p.get("message") or "", force=True)
    else:
        # 引擎未运行：直接写 state
        def _set(state):
            tasks = state.setdefault("tasks", {})
            rec = tasks.setdefault(name, {})
            rec["status"] = status
            rec["message"] = p.get("message") or ""
        update_state_locked(_set)
    return {"ok": True}


def rpc_reset_task(p):
    """p = {name}：清该任务本地防重记录并恢复监控（GUI「重置防重」等价）。"""
    name = p.get("name")
    cfg = load_config()
    task = next((t for t in cfg.get("tasks") or [] if t.get("name") == name), None)
    if not task:
        return {"ok": False, "error": "任务不存在：" + str(name)}
    try:
        get_engine().empty_task_dedup(task)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if engine_running():
        get_engine().set_task_status(task, "monitoring", "手动重置", force=True)
    else:
        def _set(state):
            rec = state.setdefault("tasks", {}).setdefault(name, {})
            rec["status"] = "monitoring"
            rec["message"] = "手动重置"
        update_state_locked(_set)
    return {"ok": True}


def rpc_resume_all(_p):
    """全部恢复：把暂停中的任务批量转回 monitoring。"""
    cfg = load_config()
    n = 0
    for t in cfg.get("tasks") or []:
        rpc_set_task_status({"name": t.get("name"), "status": "monitoring", "message": "全部恢复"})
        n += 1
    return {"ok": True, "count": n}


def rpc_pause_all(_p):
    cfg = load_config()
    n = 0
    for t in cfg.get("tasks") or []:
        rpc_set_task_status({"name": t.get("name"), "status": "paused", "message": "批量暂停"})
        n += 1
    return {"ok": True, "count": n}


def rpc_delete_task(p):
    name = p.get("name")
    cfg = load_config()
    task = next((t for t in cfg.get("tasks") or [] if t.get("name") == name), None)
    if not task:
        return {"ok": False, "error": "任务不存在：" + str(name)}
    delete_task_everywhere(None, task)  # app=None → 文件路径清 state
    return {"ok": True}


def rpc_save_grab_task(p):
    """p = 完整任务 dict（含 id/name/status）。"""
    tasks = launcher.load_grab_tasks()
    tid = p.get("id")
    found = False
    for i, t in enumerate(tasks):
        if t.get("id") == tid:
            tasks[i] = p
            found = True
            break
    if not found:
        tasks.append(p)
    launcher.save_grab_tasks(tasks)
    return {"ok": True}


def rpc_rename_grab_task(p):
    """p = {id, name}：抢票任务改名。"""
    tid = p.get("id")
    name = (p.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "名称不能为空"}
    tasks = launcher.load_grab_tasks()
    for t in tasks:
        if t.get("id") == tid:
            t["name"] = name
            launcher.save_grab_tasks(tasks)
            return {"ok": True, "name": name}
    return {"ok": False, "error": "抢票任务不存在"}


def rpc_delete_grab_task(p):
    tid = p.get("id")
    tasks = [t for t in launcher.load_grab_tasks() if t.get("id") != tid]
    launcher.save_grab_tasks(tasks)
    return {"ok": True}


# ---------------- 抢票引擎 ----------------

_grabbers = {}  # id -> Grabber


def rpc_start_grab(p):
    tid = p.get("id")
    tasks = launcher.load_grab_tasks()
    task = next((t for t in tasks if t.get("id") == tid), None)
    if not task:
        return {"ok": False, "error": "抢票任务不存在"}
    g = _grabbers.get(tid)
    if g and g.is_alive():
        return {"ok": False, "error": "任务已在抢票中"}
    g = launcher.Grabber(dict(task))
    g.start()
    _grabbers[tid] = g
    return {"ok": True}


def rpc_stop_grab(p):
    tid = p.get("id")
    g = _grabbers.get(tid)
    if g:
        g.stop()
        return {"ok": True}
    return {"ok": False, "error": "任务未在抢票"}


def rpc_grab_ticker(_p):
    """抢票页每 1s 轮询：倒计时（Python 时间源）+ 运行状态 + 结束结果落盘。"""
    import datetime as _dt
    now = _dt.datetime.now()
    out = []
    for t in launcher.load_grab_tasks():
        tid = t.get("id")
        g = _grabbers.get(tid)
        # 编辑弹窗回填依赖 from/to/fromCode/date/start_time 等全量字段，
        # 投影只给 id/name/status 会导致编辑一次就把这些字段清空
        item = dict(t)
        item["name"] = t.get("name") or tid
        item["status"] = t.get("status") or "idle"
        item["running"] = bool(g and g.is_alive())
        item["auto_fired"] = tid in _armed_fired
        # 已落盘的上一轮结果（供前端在任务结束后继续展示）
        if t.get("result_msg"):
            item["result_ok"] = bool(t.get("result_ok"))
            item["result_msg"] = t.get("result_msg") or ""
            item["result_at"] = t.get("result_at") or ""
        if g and not g.is_alive() and g.result is not None:
            ok, msg = g.result
            item["result_ok"] = bool(ok)
            item["result_msg"] = msg or ""
            item["result_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
            del _grabbers[tid]
            # 状态 + 结果落盘（与 gui._tick 同逻辑：成功/失败回写任务库）
            t["status"] = "ok" if ok else "idle"
            t["result_ok"] = bool(ok)
            t["result_msg"] = msg or ""
            t["result_at"] = item["result_at"]
            try:
                tasks = launcher.load_grab_tasks()
                for i, x in enumerate(tasks):
                    if x.get("id") == tid:
                        tasks[i] = t
                        break
                launcher.save_grab_tasks(tasks)
            except Exception as e:
                _warn("抢票结果回写任务库", e)
        st = launcher_utils.parse_dt(str(t.get("start_time") or ""))
        armed = bool(st and st > now)
        item["armed"] = armed
        if st:
            diff = (st - now).total_seconds()
            try:
                lead = max(0, int(t.get("warm_minutes") or 10)) * 60
            except (TypeError, ValueError):
                lead = 0
            cd = launcher_utils.fmt_countdown(abs(diff))
            if diff > lead:
                item["countdown"] = ("距开抢 " + cd) + (" (自动开抢)" if armed else "")
            elif diff > 0:
                item["countdown"] = "⏰ 预热窗口 · 距开抢 " + cd
            elif tid in _armed_fired:
                item["countdown"] = "已到点，自动开抢"
            else:
                item["countdown"] = "开抢时间已过，请手动开抢"
        else:
            item["countdown"] = "未设置开抢时间"
        out.append(item)
    return {"tasks": out}


def rpc_grab_result(p):
    tid = p.get("id")
    g = _grabbers.get(tid)
    if not g:
        return {"result": None}
    return {"result": getattr(g, "result", None)}


# ---------------- 查票（免登录） ----------------

def rpc_query_tickets(p):
    """真实查票（免登录）：query_tickets + parse_row → 前端渲染友好的结构。"""
    from_code = p.get("fromCode") or ""
    to_code = p.get("toCode") or ""
    date = p.get("date") or ""
    purpose = p.get("purpose") or "ADULT"
    if not (from_code and to_code and date):
        return {"error": "缺少 fromCode/toCode/date"}
    if date < time.strftime("%Y-%m-%d"):
        # 12306 对过期日期返回非 JSON，直接给出可读原因（查询历史里的旧日期会走到这里）
        return {"error": "该日期已过期，12306 不再提供余票查询"}
    limit_day = time.strftime("%Y-%m-%d",
                              time.localtime(time.time() + PRESALE_DAYS * 86400))
    if date > limit_day:
        # 未开售的日期同样拿不到 JSON（实测今天+15 天起四个端点全返 HTML 错误页），
        # 与其把 "Expecting value: line 1 column 1" 抛给用户，不如直说没开售
        return {"error": "该日期尚未开售（12306 预售期约 %d 天，最远可查到 %s）"
                         % (PRESALE_DAYS, limit_day)}
    try:
        rows = ticket.query_tickets(from_code, to_code, date, purpose=purpose)
    except ValueError as e:
        # 非 JSON 响应（未开售边界日、临时限流、风控页）都会走到这里
        if "Expecting value" in str(e):
            return {"error": "12306 未返回余票数据（该日可能未开售或查询被限流），稍后再试"}
        return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}
    _name2code, code2name = ticket.load_station_map()
    trains = []
    for row in rows:
        try:
            info = ticket.parse_row(row, code2name, query_date=date)
        except Exception as e:
            _warn("车次行解析，跳过该行", e)
            continue
        seats = []
        # seats_all 是该车次全部席别（含无票），available_seats 只含有票——合成三态
        avail = info.get("available_seats") or {}
        for name in (info.get("seats_all") or []):
            v = avail.get(name)
            if v:
                seats.append({"name": name, "count": str(v), "state": "ok"})
            elif info.get("houbu"):
                seats.append({"name": name, "count": "", "state": "hb"})
            else:
                seats.append({"name": name, "count": "", "state": "sold"})
        trains.append({
            "trainCode": info.get("train_code"),
            "fromName": info.get("from_name"),
            "toName": info.get("to_name"),
            "startTime": info.get("start_time"),
            "arriveTime": info.get("arrive_time"),
            "duration": info.get("duration"),
            "canBuy": info.get("can_buy") == "Y",
            "houbu": bool(info.get("houbu")),
            "seats": seats,
        })
    return {"trains": trains, "date": date}


def rpc_seat_priority_feedback(p):
    """首选席别输入框即时反馈（与 gui.NewTaskDialog 同源）。"""
    text = p.get("text") or ""
    checked = p.get("checked") or []
    trains = p.get("trains") or []
    txt, bad = ticket.seat_priority_feedback(text, checked, trains)
    return {"text": txt or "不填 = 只按下面勾选的席别顺序", "warn": bool(bad)}


_armed_fired = set()  # 已自动触发的抢票任务 id（防重）


def _armed_loop():
    """原 gui.GrabTaskForm._tick 的到点自动开抢职责：每 2s 扫一遍抢票任务。"""
    import datetime as _dt
    while True:
        time.sleep(2)
        try:
            now = _dt.datetime.now()
            for t in launcher.load_grab_tasks():
                tid = t.get("id")
                if tid in _armed_fired:
                    continue
                st = launcher_utils.parse_dt(str(t.get("start_time") or ""))
                if not st:
                    continue
                if st > now:
                    continue
                try:
                    lead = max(0, int(t.get("warm_minutes") or 10)) * 60
                except (TypeError, ValueError):
                    lead = 0
                if (now - st).total_seconds() > max(90, lead):
                    _armed_fired.add(tid)  # 窗口已过，缴械
                    push("log", {"line": "[提示] 任务 %s 开抢窗口已过，自动开抢已缴械" % (t.get("name") or tid), "tag": "warn"})
                    continue
                g = _grabbers.get(tid)
                if g and g.is_alive():
                    _armed_fired.add(tid)  # 已在抢，视为已触发
                    continue
                _armed_fired.add(tid)
                push("log", {"line": "[自动] 已到点，自动开始抢票 %s（%s）" % ((t.get("name") or tid), st.strftime("%H:%M:%S")), "tag": "info"})
                rpc_start_grab({"id": tid})
        except Exception as e:
            _warn("自动开抢轮询", e)


def rpc_search_station(p):
    """p = {q} → 本地模糊搜索车站（launcher.search_stations：拼音/简拼/电报码）。"""
    q = (p.get("q") or "").strip()
    if not q:
        return {"stations": []}
    hits = launcher.search_stations(q, limit=8)
    # kind：该站探测到的车次类型（"高铁"/"普速"/组合），没探测过是空串，仅作展示
    return {"stations": [{"name": h["name"], "code": h["code"],
                          "kind": launcher.station_kind(h["code"])} for h in hits]}


def rpc_list_presets(_p):
    cfg = launcher.load_launcher_config()
    return {"presets": cfg.get("presets") or [],
            "station_history": cfg.get("station_history") or [],
            "query_history": cfg.get("query_history") or []}


def rpc_delete_preset(p):
    """p = {name}：删除常用行程预设。"""
    cfg = launcher.load_launcher_config()
    name = p.get("name")
    before = cfg.get("presets") or []
    cfg["presets"] = [x for x in before if not (isinstance(x, dict) and x.get("name") == name)]
    if len(cfg["presets"]) == len(before):
        return {"ok": False, "error": "预设不存在：" + str(name)}
    appcommon.atomic_write_json(launcher.LAUNCHER_CFG_PATH, cfg)
    return {"ok": True}


def rpc_push_query_history(p):
    """p = {from, to, date}：记录一次查票历史（去重、最多 10 条，与 gui 同口径）。"""
    item = {"from": (p.get("from") or "").strip(),
            "to": (p.get("to") or "").strip(),
            "date": (p.get("date") or "").strip()}
    if not (item["from"] and item["to"]):
        return {"ok": False, "error": "缺少 from/to"}
    cfg = launcher.load_launcher_config()
    hist = [h for h in (cfg.get("query_history") or [])
            if not (isinstance(h, dict)
                    and h.get("from") == item["from"] and h.get("to") == item["to"]
                    and h.get("date") == item["date"])]
    hist.insert(0, item)
    cfg["query_history"] = hist[:10]
    appcommon.atomic_write_json(launcher.LAUNCHER_CFG_PATH, cfg)
    return {"ok": True, "query_history": cfg["query_history"]}


def rpc_ip_locate(_p):
    """本机 IP 归属地（百度公开接口）。"""
    return {"text": launcher_utils.ip_locate()}


def rpc_check_update(_p):
    """检查更新（读 launcher_config.json 的 update_url；未配置时 has_new=False）。"""
    lc = launcher.load_launcher_config()
    has_new, ver, notes, download = launcher.check_update(lc)
    return {"has_new": bool(has_new), "version": ver or "",
            "notes": notes or "", "download": download or "",
            "configured": bool((lc.get("update_url") or "").strip()),
            "current": getattr(launcher, "__version__", "")}


def rpc_save_preset(p):
    """p = {name, from, to, ...}"""
    cfg = launcher.load_launcher_config()
    presets = cfg.setdefault("presets", [])
    name = p.get("name") or ("预设 %d" % (len(presets) + 1))
    p["name"] = name
    for i, x in enumerate(presets):
        if x.get("name") == name:
            presets[i] = p
            break
    else:
        presets.append(p)
    appcommon.atomic_write_json(launcher.LAUNCHER_CFG_PATH, cfg)
    return {"ok": True}


# ---------------- 乘车人 ----------------

def rpc_list_passengers(_p):
    """列表接口：证件号 / 手机号一律脱敏，明文只在 passengerDetail 按需取。"""
    pax = passengers_mod.load_passengers()
    stamp = passengers_mod.passengers_stamp()
    safe = [dict(x, id_no=launcher_utils.mask_id(x.get("id_no")),
                 mobile=launcher_utils.mask_mobile(x.get("mobile"))) for x in pax]
    return {"passengers": safe, "stamp": stamp}


def rpc_passenger_detail(p):
    """p = {name}：取单个乘车人的明文记录（编辑弹窗专用）。"""
    for x in passengers_mod.load_passengers():
        if x.get("name") == p.get("name"):
            return {"ok": True, "passenger": x}
    return {"ok": False, "error": "乘车人不存在"}


def rpc_save_passengers(p):
    """p = {passengers: [...], expect_stamp: ...}（乐观并发）。

    列表接口（listPassengers）下发的是脱敏值，前端全量回写会把「****」
    写回文件 —— 含 '*' 的 id_no/mobile 一律视为「未修改」，用磁盘上的
    明文还原（证件号/手机号不会含 '*'，掩码串不可能与真实值撞车）。
    """
    try:
        old = {x.get("name"): x for x in passengers_mod.load_passengers()}
        rows = []
        for x in (p.get("passengers") or []):
            x = dict(x)
            prev = old.get(x.get("name")) or {}
            for k in ("id_no", "mobile"):
                v = x.get(k)
                if isinstance(v, str) and "*" in v:
                    x[k] = prev.get(k, "")
            rows.append(x)
        passengers_mod.save_passengers(rows, expect_stamp=p.get("expect_stamp"))
        return {"ok": True, "stamp": passengers_mod.passengers_stamp()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def rpc_12306_passengers(_p):
    """从 12306 会话拉真实乘车人（需登录）。"""
    try:
        session = current_session()
        if session is None:
            return {"error": "未登录"}
        pax = order_mod.get_passengers(session)
        return {"passengers": pax}
    except Exception as e:
        return {"error": str(e)}


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
    if (load_config().get("order_mode") or "http") != "browser":
        try:
            ok, msg, permanent = order_mod.verify_session(order_mod.load_session())
        except Exception as e:
            return {"ok": False, "unknown": False, "permanent": True,
                    "message": str(e), "name": ""}
        return {"ok": bool(ok), "unknown": not ok and not permanent,
                "permanent": bool(permanent), "message": str(msg or ""),
                "name": str(msg or "") if ok else ""}
    now = time.time()
    if (not force and _SESSION_CACHE["res"] is not None
            and now - _SESSION_CACHE["at"] < _SESSION_TTL):
        return _SESSION_CACHE["res"]
    res = _probe_browser_session()
    _SESSION_CACHE["at"] = now
    _SESSION_CACHE["res"] = res
    return res


def rpc_check_session(p):
    p = p or {}
    try:
        return probe_session(force=bool(p.get("force")))
    except Exception as e:
        return {"ok": False, "unknown": True, "permanent": False,
                "message": str(e), "name": ""}


def rpc_relogin(_p):
    """browser 模式：拉起真实 Edge 滑块（保持旧行为）。"""
    import threading as _th

    def _do():
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

    _th.Thread(target=_do, daemon=True).start()
    return {"ok": True, "message": "已拉起浏览器，等待人工完成验证"}


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


# ---------------- 历史 ----------------

def rpc_list_history(p):
    limit = int(p.get("limit") or 200)
    return {"records": read_history_records(limit)}


# ---------------- 设置（SMTP + 会话 + order_mode） ----------------

# 新建任务弹窗的四个开关（自动下单 / 抢到后停止 / 邮件通知 / 立即启动）
# 不再在弹窗里显示，改由这里给全局默认：缺省全开，只认显式 false。
TASK_DEFAULTS_DEF = {"auto_order": True, "stop_after_order": True, "notify": True, "start_now": True}


def _task_defaults(cfg):
    raw = cfg.get("task_defaults") or {}
    out = {}
    for k, v in TASK_DEFAULTS_DEF.items():
        val = raw.get(k)
        out[k] = v if val is None else bool(val)
    return out


def rpc_get_settings(_p):
    cfg = load_config()
    email = dict((cfg.get("notify") or {}).get("email") or {})
    # 授权码脱敏显示：有密码 → 掩码占位，前端保存时才回写
    out = dict(email)
    if out.get("password"):
        try:
            pw = notify_mod.secret_of(out["password"])
            out["password"] = "******" if pw else out["password"]
        except Exception as e:
            _warn("邮箱授权码脱敏", e)
            out["password"] = "******"
    return {
        "order_mode": cfg.get("order_mode") or "http",
        "email": out,
        "notify_enabled": bool((cfg.get("notify") or {}).get("email", {}).get("enabled")),
        "poll_interval_seconds": cfg.get("poll_interval_seconds"),
        "task_defaults": _task_defaults(cfg),
        # 乐观并发基线：设置页保存时原样带回（与 listPassengers 的 stamp 同口径）
        "stamp": _config_stamp(),
    }


def rpc_save_settings(p):
    """p = {email?, order_mode?, poll_interval_seconds?, task_defaults?, expect_stamp?}。

    expect_stamp 为 getSettings 下发的 stamp 时做乐观并发检查：抢票进程（launcher/
    monitor）与设置页并发写 config.json 时，后写方不再静默覆盖前写方。
    """
    try:
        def _upd(cfg):
            if p.get("email") is not None:
                email = dict(p["email"])
                # 掩码占位不动原密码；明文才加密回写
                pw = email.get("password") or ""
                if pw == "******":
                    email["password"] = (cfg.get("notify") or {}).get("email", {}).get("password", "")
                elif pw:
                    email["password"] = notify_mod.protect_secret(pw)
                if not (cfg.get("notify") or {}).get("email", {}).get("password"):
                    email.pop("password", None)
                email = normalize_email_settings(email)
                cfg.setdefault("notify", {})["email"] = email
            if p.get("order_mode"):
                cfg["order_mode"] = p["order_mode"]
            if p.get("poll_interval_seconds") is not None:
                cfg["poll_interval_seconds"] = int(p["poll_interval_seconds"])
            if p.get("task_defaults") is not None:
                # 合并而非整块替换：只认已知键（其余丢弃），漏传的键保持原值。
                # 整块替换会让「前端只传一个键」把另外三个悄悄重置回默认。
                merged = _task_defaults(cfg)
                for k, v in dict(p["task_defaults"]).items():
                    if k in TASK_DEFAULTS_DEF:
                        merged[k] = bool(v)
                cfg["task_defaults"] = merged
        stamp = p["expect_stamp"] if "expect_stamp" in p else _STAMP_UNSET
        if not update_config_locked(_upd, expect_stamp=stamp):
            return {"ok": False, "error": "配置已被其它窗口或抢票进程修改，本次保存已取消，请刷新后重试",
                    "stamp": _config_stamp()}
        return {"ok": True, "stamp": _config_stamp()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def rpc_test_email(_p):
    try:
        cfg = load_config()
        email = (cfg.get("notify") or {}).get("email") or {}
        ok, msg = notify_mod.send_email(dict(email), "测试邮件：12306 监控系统",
                                        "配置验证（桌面版设置页）")
        return {"ok": bool(ok), "message": msg or ""}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------- RPC 分发表 ----------------

METHODS = {
    "listMonitorTasks": rpc_list_monitor_tasks,
    "createTask": rpc_create_task,
    "setTaskStatus": rpc_set_task_status,
    "resetTask": rpc_reset_task,
    "pauseAll": rpc_pause_all,
    "resumeAll": rpc_resume_all,
    "deleteTask": rpc_delete_task,
    "saveGrabTask": rpc_save_grab_task,
    "deleteGrabTask": rpc_delete_grab_task,
    "renameGrabTask": rpc_rename_grab_task,
    "startGrab": rpc_start_grab,
    "stopGrab": rpc_stop_grab,
    "grabResult": rpc_grab_result,
    "grabTicker": rpc_grab_ticker,
    "queryTickets": rpc_query_tickets,
    "searchStation": rpc_search_station,
    "seatPriorityFeedback": rpc_seat_priority_feedback,
    "listPresets": rpc_list_presets,
    "savePreset": rpc_save_preset,
    "deletePreset": rpc_delete_preset,
    "pushQueryHistory": rpc_push_query_history,
    "ipLocate": rpc_ip_locate,
    "checkUpdate": rpc_check_update,
    "listPassengers": rpc_list_passengers,
    "savePassengers": rpc_save_passengers,
    "passengerDetail": rpc_passenger_detail,
    "sync12306Passengers": rpc_12306_passengers,
    "checkSession": rpc_check_session,
    "relogin": rpc_relogin,
    "getAccount": rpc_get_account,
    "listHistory": rpc_list_history,
    "getSettings": rpc_get_settings,
    "saveSettings": rpc_save_settings,
    "testEmail": rpc_test_email,
    "startEngine": lambda _p: {"ok": start_engine()},
    "stopEngine": lambda _p: {"ok": stop_engine()},
}


def rpc_dispatch(line):
    """处理一行 JSON-RPC 请求并回包。

    从 main() 的循环体抽出（逻辑一字未改），让协议层可以脱离真管道被测试。
    副作用只有 reply()（写 stdout）与方法异常时 traceback 到 stderr。
    """
    line = line.strip()
    if not line:
        return
    try:
        msg = json.loads(line)
    except Exception as e:
        reply(None, error="JSON 解析失败：%s" % e)
        return
    if not isinstance(msg, dict):
        # stdin 是信任边界：非对象载荷原来会 AttributeError 冒泡出主循环，
        # 直接带崩后端进程（前端仍会以为后端活着）。
        reply(None, error="请求必须是 JSON 对象")
        return
    id_ = msg.get("id")
    method = msg.get("method") or ""
    params = msg.get("params") or {}
    fn = METHODS.get(method)
    if fn is None:
        reply(id_, error="未知方法：" + method)
        return
    try:
        result = fn(params)
        reply(id_, result=result)
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        reply(id_, error="%s: %s" % (type(e).__name__, e))


def main():
    # 日志泵线程
    threading.Thread(target=log_pump, daemon=True).start()
    # 抢票任务到点自动开抢调度（原 gui._tick）
    threading.Thread(target=_armed_loop, daemon=True).start()
    push("ready", {"pid": os.getpid()})
    for line in sys.stdin:
        rpc_dispatch(line)


if __name__ == "__main__":
    main()