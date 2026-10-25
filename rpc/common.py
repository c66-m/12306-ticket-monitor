# -*- coding: utf-8 -*-
"""rpc.common —— 共享基础设施：协议出口（emit/push/reply）、config/state 读写与锁、
引擎生命周期、日志泵，及被多个 RPC 域共用的辅助。

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

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import grabber
import grabber_utils
import ticket
import engine as engine_mod
import passengers as passengers_mod
import appcommon
import filelock
import logutil
import order as order_mod
import browser_order
import notify as notify_mod

CONFIG_PATH = os.path.join(HERE, "config.json")

# 中文 Windows 下 stdin/stdout 默认走 ANSI 代码页（GBK），而 Electron 侧写的是 UTF-8 字节，
# 中文参数会被按 GBK 解码成乱码（症状：searchStation 查「长葛」返回空、拼音正常）。
# 协议两端统一 UTF-8，本文件是协议端点，编码属于它的契约。
def _warn(what, exc):
    """静默 except 的观测出口：不改控制流，只在 stderr 留一行。

    进程 stderr 由 electron/main.js:74 转发到 electron/_eerr.txt。
    必须定义在下面的 stdio reconfigure 之前——那里就是它的第一个调用点，
    顺序反过来时 reconfigure 一旦失败就是 NameError，整个模块导入失败、后端起不来。"""
    try:
        print("[api] %s: %r" % (what, exc), file=sys.stderr)
    except Exception:
        pass


for _stream in (sys.stdin, sys.stdout):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception as e:
        _warn("stdio 切 UTF-8", e)

OUT_LOCK = threading.Lock()
WRITE = sys.stdout.write
FLUSH = sys.stdout.flush


def emit(obj):
    """线程安全写一行 JSON 到 stdout（Electron 读取）。"""
    line = json.dumps(obj, ensure_ascii=False, default=str)
    with OUT_LOCK:
        WRITE(line + "\n")
        FLUSH()


def push(method, params):
    emit({"jsonrpc": "2.0", "method": method, "params": params})


def reply(id_, result=None, error=None, code=None, evidence=None):
    """回 JSON-RPC 响应。error 给定时走 A-3b 统一错误协议：
    {"code": ..., "message": ..., "evidence": {...}}。

    message 是用户可见文案，与旧 {"message": ...} 形状逐字一致（前端只
    消费 message）；code 是机器可判的错误类别；evidence 只许放方法名 /
    异常类名 / 简短原因类别——禁止请求 params 原文（参数含乘车人等 PII，
    脱敏红线）。处理函数返回值内的 {"error": ...} 是业务结果载荷，不经
    本函数包装，与信封 error 无关。"""
    obj = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        obj["error"] = {"code": code or "error",
                        "message": str(error),
                        "evidence": evidence if evidence is not None else {}}
    else:
        obj["result"] = result
    emit(obj)


# ---------------- 纯函数（自 gui.py 内联，避免 import tkinter） ----------------

STATUS_COLOR = {"monitoring": "#0969da", "retrying": "#d97706", "paused": "#6e7781",
                "cancelled": "#6e7781", "success": "#1a7f37", "failed": "#cf222e"}

# 邮箱正则单源到 appcommon.EMAIL_RE（严格口径）。旧版这里另有一份宽松正则，
# 会让 gui 拒收的地址从桌面版设置页落进 config.json。

_CONFIG_CORRUPT_WARNED = False

# 乐观并发检查的哨兵：update_config_locked 的 expect_stamp 取此值表示"不检查"
#（旧调用方式）；None 是合法指纹，表示"调用方 load 时文件不存在"。
# 与 passengers._STAMP_UNSET / monitor._STAMP_UNSET 同口径。
_STAMP_UNSET = appcommon._STAMP_UNSET


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
    """读 config.json（gui.py 116 原版语义，messagebox 换 stderr）。解析与
    降级策略单源在 appcommon.read_config_or_empty（缺失/损坏/非 dict 都给 {}）。"""
    global _CONFIG_CORRUPT_WARNED

    def _broken(exc):
        global _CONFIG_CORRUPT_WARNED
        if not _CONFIG_CORRUPT_WARNED:
            _CONFIG_CORRUPT_WARNED = True
            print("[api] config.json 解析失败，按空配置", file=sys.stderr)

    return appcommon.read_config_or_empty(CONFIG_PATH, broken=_broken)


def load_state():
    return engine_mod.load_state_file(
        appcommon.state_path_of(load_config(), HERE))


def remove_task_from_config(config, task):
    """删除任务（单源在 appcommon：有 uid 按 uid 删，无 uid 只删第一个同名）。"""
    return appcommon.remove_task_from_config(config, task)


def update_config_locked(fn, expect_stamp=_STAMP_UNSET):
    """config.json 读-改-写原子接口（gui.py 201 原版语义，损坏拒绝覆盖）。

    expect_stamp 为 _config_stamp() 在调用方 load 之后取到的指纹时做乐观并发
    检查（与 passengers.save_passengers / monitor.save_config 同口径）：在锁内
    重读当前指纹，不一致说明两次操作之间有外部写入（抢票进程 / 另一个窗口）——
    放弃本次保存并返回 False，不静默覆写对方的修改；一致则写入并返回 True。
    不传 expect_stamp 时保持旧行为（直接写）。返回值自本轮起统一为布尔
    （旧行为是返回 mutator 的返回值，仓内两个调用点均未消费）。
    """
    return appcommon.update_config_locked(
        CONFIG_PATH, fn, expect_fingerprint=expect_stamp)


def update_state_locked(fn):
    """state.json 读-改-写原子接口（gui.py 245 原版语义；机械层单源 appcommon）。"""
    path = appcommon.state_path_of(load_config(), HERE)
    return appcommon.update_state_locked(
        path, fn, read_state=engine_mod.load_state_file, tmp_kind="apisave")


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
    """校验清洗邮件配置（单源 appcommon.normalize_email_settings：正则取严格口径，
    旧版 api 的宽松版会让 gui 拒收的地址从桌面版设置页偷偷落进 config.json）。"""
    return appcommon.normalize_email_settings(email)


def read_history_records(limit=200):
    """读购票历史（单源 appcommon.read_history_records；失败只留 stderr 一行）。"""
    path = appcommon.history_path_of(load_config(), HERE)
    return appcommon.read_history_records(
        path, limit=limit, on_error=lambda e: _warn("读取历史文件", e))


# ---------------- 引擎管理（单例） ----------------

_engine = None
_engine_lock = threading.Lock()
_engine_start_lock = threading.Lock()  # 启动幂等：判定+建线程+赋值必须原子
_engine_thread = None
_engine_stop = None  # 当前引擎线程的 stop_event，stop_engine() 用它请求优雅退出


def get_engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            # setup_logging=True：引擎按天写 logs/monitor_YYYYMMDD.log。它的
            # StreamHandler 默认走 stderr（stdout 是 JSON-RPC 通道，碰不得），
            # 与界面的 py-push 推送并行不冲突。关掉的话事后只有
            # order_timing.jsonl 能查，13:0x「有票却没下单」就是这么瞎猜的。
            _engine = engine_mod.MonitorEngine(setup_logging=True)
        return _engine


def engine_running():
    global _engine_thread
    return _engine_thread is not None and _engine_thread.is_alive()


def start_engine():
    """启动监控引擎线程（幂等）。

    判定+建线程+赋值在同一把锁内：否则自启与 RPC 并发时会在
    t.start() 与 _engine_thread 赋值之间各起一个循环，同一任务双路下单
    （2026-10-19 审查）。
    """
    global _engine_thread, _engine_stop
    with _engine_start_lock:
        if _engine_thread is not None and _engine_thread.is_alive():
            return True
        eng = get_engine()
        stop_evt = threading.Event()
        t = threading.Thread(target=eng.run, args=(stop_evt,), daemon=True)
        t.start()
        _engine_thread = t
        _engine_stop = stop_evt
    grabber.log("[启动] 监控引擎已启动")
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
    # 在途任务的两种收尾：run() 主循环已在 10s 内退出（_run_task 每轮查
    # stop_event，下一轮头即返回），或主循环本身还没轮到 break。前者之外
    # 的「仍在收尾」不等于任务会跑完一整轮（旧文案「在途任务未结束」会
    # 让人以为日志还要刷很久）。
    grabber.log("[停止] 监控引擎%s" % ("仍在收尾（在途任务跑完当轮即停）" if alive else "已停止"))
    return not alive


def rpc_shutdown(_p):
    """Electron 退出前的优雅收尾：停引擎（状态落盘）后返回，由主进程 kill。

    直接强杀会跳过 finally：引擎节流的 state 丢失，Playwright 起的浏览器
    子进程变孤儿并占住 .browser_profile（2026-10-19 审查）。
    """
    try:
        stop_engine()
    except Exception as e:
        _warn("退出前停止引擎", e)
    return {"ok": True}


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
            grabber.log("[停止] %d 个任务已标记暂停（引擎已停止）" % changed[0])
    except Exception as e:
        grabber.log("[停止] 标记任务暂停失败：%s" % e)
    return changed[0]


def _pause_all_tasks_on_startup():
    """软件启动时把所有任务统一标 paused（2026-10-25 用户口径：重新打开
    窗口一律关闭所有任务，是否继续监控由用户手动恢复）。

    覆盖三态：monitoring/retrying（上次在跑，含进程被硬杀没走
    _mark_running_tasks_paused 的残态）、failed 且 message 含「登录会话失效」
    （engine 的会话恢复逻辑会在登录成功后自动拉起它们——不先清掉，就会出现
    「一打开、一登录，任务自己开抢」）、message=="软件启动自动恢复"（旧版本
    自动恢复留下的残态）。用户手动暂停（message 为其它文案）与永久失败、
    已取消原样不动，保留用户看到的上次文案。
    """
    changed = [0]

    def _set(state):
        tasks = state.get("tasks")
        if not isinstance(tasks, dict):
            return
        for rec in tasks.values():
            if not isinstance(rec, dict):
                continue
            st, msg = rec.get("status"), rec.get("message") or ""
            if st in ("monitoring", "retrying") \
                    or (st == "failed" and "登录会话失效" in msg) \
                    or msg == "软件启动自动恢复":
                rec["status"] = "paused"
                rec["message"] = "软件重启，已全部暂停"
                changed[0] += 1

    try:
        update_state_locked(_set)
        if changed[0]:
            grabber.log("[启动] %d 个任务已按「启动即暂停」标记，需要继续监控"
                        "请手动恢复" % changed[0])
    except Exception as e:
        grabber.log("[启动] 启动暂停任务失败：%s" % e)
    return changed[0]


def _auto_start_engine():
    """启动软件即启动监控引擎；config.json 里 auto_start_engine=false 可关。

    先暂停所有任务再起引擎（2026-10-25 用户口径：重开窗口一律不自动恢复）：
    get_engine() 首次创建时会读一遍 state 文件，顺序反了引擎会把上次在跑的
    任务直接捡起来开抢。
    """
    if not load_config().get("auto_start_engine", True):
        grabber.log("[启动] auto_start_engine=false，跳过自动启动引擎")
        return False
    _pause_all_tasks_on_startup()
    return start_engine()


# ---------------- 日志泵 ----------------

def _log_tag(record):
    """日志分级（与前端颜色映射一致）。"""
    if "[有票]" in record or "[抢到]" in record or "[成功]" in record:
        return "ok"
    if "[错误]" in record or "[失败]" in record:
        return "err"
    if ("[提醒]" in record or "[重试]" in record or "[暂停]" in record
            or "[会话]" in record):
        return "warn"
    return "info"


def log_pump():
    """把 grabber.LOGQ 的日志成批推给前端（150ms 一窗）。

    逐行推送时每条日志都是一次 IPC + 前端一次同步 reflow，日志密集时会
    一起卡；攒批后一次 DOM 插入只滚一次（2026-10-19 审查）。
    """
    while True:
        try:
            first = grabber.LOGQ.get(timeout=0.3)
        except queue.Empty:
            continue
        batch = [first]
        deadline = time.time() + 0.15
        while time.time() < deadline:
            try:
                batch.append(grabber.LOGQ.get_nowait())
            except queue.Empty:
                time.sleep(0.02)
        items = [{"line": r, "tag": _log_tag(r)} for r in batch]
        # 单条保持旧形状（line/tag），多条用 lines 数组，前端两态都认。
        # 前端断开（stdout 关闭）时日志泵不能跟着死，否则后续日志全丢。
        try:
            push("log", {"lines": items} if len(items) > 1 else items[0])
        except Exception:
            pass

# 新建任务弹窗的四个开关（自动下单 / 抢到后停止 / 邮件通知 / 立即启动）
# 不再在弹窗里显示，改由这里给全局默认：缺省全开，只认显式 false。
TASK_DEFAULTS_DEF = {"auto_order": True, "stop_after_order": True, "notify": True, "start_now": True}

def _task_defaults(cfg):
    raw = cfg.get("task_defaults") or {}
    if not isinstance(raw, dict):
        # task_defaults 被手改成 list/字符串时按无自定义处理：缺省全开的
        # 口径不变，不许 raw.get 抛 AttributeError 连带 createTask /
        # saveSettings 一并失效（深审第 5 条）
        raw = {}
    out = {}
    for k, v in TASK_DEFAULTS_DEF.items():
        val = raw.get(k)
        out[k] = v if val is None else bool(val)
    return out
