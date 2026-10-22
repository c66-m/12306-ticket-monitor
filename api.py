# -*- coding: utf-8 -*-
"""12306 桌面版 API 服务（Electron 后端）——取代 gui.py 的界面层。

协议：stdin/stdout 逐行 JSON-RPC 2.0。
  请求：{"jsonrpc":"2.0","id":N,"method":"<name>","params":{...}}
  响应：{"jsonrpc":"2.0","id":N,"result":...} 或
        {"error":{"code":...,"message":...,"evidence":{...}}}（A-3b 错误协议）
  推送：{"jsonrpc":"2.0","method":"log"/"status","params":{...}}（无 id）

业务层全部复用 grabber / ticket / engine / passengers / order / notify，
本文件只做：JSON-RPC 分发 + LOGQ 日志泵 + 引擎线程管理。零业务逻辑。
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

HERE = os.path.dirname(os.path.abspath(__file__))
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
from app.scheduler import Scheduler  # P2：散养线程统一收编

# ---------------- A-3a 拆分门面 ----------------
# 原实现已按域拆到 rpc/ 包（common + tasks/grab/orders/passengers/settings/
# history/session），本文件 re-export 全部原顶层符号，外部 import 路径
# （import api）与符号面零变化；进程入口仍是本文件（electron/main.js 以
# 脚本方式 spawn api.py）。
import types as _types

from rpc import common as _rpc_common
from rpc import tasks as _rpc_tasks
from rpc import grab as _rpc_grab
from rpc import orders as _rpc_orders
from rpc import passengers as _rpc_passengers
from rpc import settings as _rpc_settings
from rpc import history as _rpc_history
from rpc import session as _rpc_session

from rpc.common import (CONFIG_PATH, _warn, _stream, OUT_LOCK, WRITE, FLUSH, emit, push, reply, STATUS_COLOR, _CONFIG_CORRUPT_WARNED, _STAMP_UNSET, _config_stamp, load_config, load_state, remove_task_from_config, update_config_locked, update_state_locked, delete_task_everywhere, normalize_email_settings, read_history_records, _engine, _engine_lock, _engine_start_lock, _engine_thread, _engine_stop, get_engine, engine_running, start_engine, stop_engine, rpc_shutdown, _mark_running_tasks_paused, _resume_stopped_tasks, _auto_start_engine, _log_tag, log_pump, TASK_DEFAULTS_DEF, _task_defaults, _stream)
from rpc.tasks import (rpc_list_monitor_tasks, _split_date_input, _req_on, rpc_create_task, _TASK_STATUSES, rpc_set_task_status, rpc_resume_all, rpc_pause_all, rpc_delete_task, rpc_start_engine, rpc_stop_engine)
from rpc.grab import (PRESALE_DAYS, rpc_save_grab_task, rpc_rename_grab_task, rpc_delete_grab_task, _grabbers, rpc_start_grab, rpc_stop_grab, rpc_grab_ticker, rpc_grab_result, rpc_query_tickets, rpc_seat_priority_feedback, _armed_fired, _armed_prepare_lead, _armed_loop, rpc_search_station, rpc_list_presets, rpc_delete_preset, rpc_save_preset)
from rpc.orders import (_reconcile_update, _reconcile_tick, rpc_push_query_history, rpc_list_history, rpc_reconcile_history)
from rpc.passengers import (rpc_list_passengers, rpc_passenger_detail, rpc_save_passengers, rpc_12306_passengers)
from rpc.settings import (rpc_ip_locate, rpc_check_update, rpc_get_settings, rpc_save_settings, rpc_test_email)
from rpc.history import (rpc_list_problem_logs, rpc_clear_problem_logs)
from rpc.session import (current_session, _SESSION_CACHE, _SESSION_TTL, reset_session_cache, _probe_browser_session, probe_session, _SESSION_WATCH, _session_watch_step, _session_watch_loop, rpc_check_session, _LOGIN_GUARD, rpc_relogin, rpc_get_account)


_RPC_MODULES = (_rpc_common, _rpc_tasks, _rpc_grab, _rpc_orders,
                _rpc_passengers, _rpc_settings, _rpc_history, _rpc_session)


class _FacadeModule(_types.ModuleType):
    """属性写传播垫片（A-3a）。

    历史测试以 mock.patch.object(api, "load_config") 等方式替换本模块的
    属性，并预期被测代码看到替换值；直接赋值（api.reply = ...）同理。
    实现拆到 rpc.* 后，函数全局名解析发生在各 rpc 模块的命名空间——本
    垫片把对 api 属性的赋值同步到「持有同名绑定的 rpc 模块」，使 patch
    语义与拆分前一致。无人 patch 的正常运行路径不触发额外行为。"""

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        for _m in _RPC_MODULES:
            if name in _m.__dict__:
                _m.__dict__[name] = value


sys.modules[__name__].__class__ = _FacadeModule

_SCHED = None  # P2 统一调度器（main() 初始化）

# ---------------- RPC 分发表 ----------------

METHODS = {
    "listMonitorTasks": rpc_list_monitor_tasks,
    "createTask": rpc_create_task,
    "setTaskStatus": rpc_set_task_status,
    "listProblemLogs": rpc_list_problem_logs,
    "clearProblemLogs": rpc_clear_problem_logs,
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
    "reconcileHistory": rpc_reconcile_history,
    "getSettings": rpc_get_settings,
    "saveSettings": rpc_save_settings,
    "testEmail": rpc_test_email,
    "shutdown": rpc_shutdown,
    "startEngine": rpc_start_engine,
    "stopEngine": rpc_stop_engine,
}

# P4-B：RPC 命名空间化（C4）。METHODS 为平台旧名（前端当前调用点，不变式）；
# _NS_METHODS 提供「域.动作」风格入口（tasks.list / grab.start / settings.save…），
# 为后续 api.py 按域拆分（rpc/server/）落好契约。别名关系记录在 _NS_TO_OLD。
_NS_TO_OLD = {
    "tasks.list": "listMonitorTasks",
    "tasks.create": "createTask",
    "tasks.set_status": "setTaskStatus",
    "tasks.delete": "deleteTask",
    "tasks.pause_all": "pauseAll",
    "tasks.resume_all": "resumeAll",
    "problems.list": "listProblemLogs",
    "problems.clear": "clearProblemLogs",
    "grab.save": "saveGrabTask",
    "grab.delete": "deleteGrabTask",
    "grab.rename": "renameGrabTask",
    "grab.start": "startGrab",
    "grab.stop": "stopGrab",
    "grab.result": "grabResult",
    "grab.ticker": "grabTicker",
    "tickets.query": "queryTickets",
    "seats.priority_feedback": "seatPriorityFeedback",
    "stations.search": "searchStation",
    "presets.list": "listPresets",
    "presets.save": "savePreset",
    "presets.delete": "deletePreset",
    "history.push_query": "pushQueryHistory",
    "history.list": "listHistory",
    "history.reconcile": "reconcileHistory",
    "network.ip_loc": "ipLocate",
    "update.check": "checkUpdate",
    "passengers.list": "listPassengers",
    "passengers.save": "savePassengers",
    "passengers.detail": "passengerDetail",
    "passengers.sync_12306": "sync12306Passengers",
    "session.check": "checkSession",
    "session.relogin": "relogin",
    "session.account": "getAccount",
    "settings.get": "getSettings",
    "settings.save": "saveSettings",
    "settings.test_email": "testEmail",
    "engines.start": "startEngine",
    "engines.stop": "stopEngine",
    "system.shutdown": "shutdown",
}

_NS_METHODS = {ns: METHODS[old] for ns, old in _NS_TO_OLD.items()}

# 慢方法（网络/浏览器/大文件）：移出 stdin 分发线程，否则 1s/2s 的前端轮询
# 会排在 3×15s 的会话检查后面（2026-10-19 审查）。threaded=True 时才异步。
_SLOW_METHODS = frozenset({
    "queryTickets", "checkSession", "relogin", "sync12306Passengers",
    "reconcileHistory", "testEmail", "ipLocate", "getAccount",
})

_RPC_SLOTS = threading.BoundedSemaphore(6)

def _rpc_handle(id_, method, fn, params):
    """执行一个 RPC 方法并回包（分发线程与慢方法线程共用）。"""
    try:
        result = fn(params)
        reply(id_, result=result)
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        # 界面「问题日志」页的数据源：RPC 方法异常也记一条台账（含堆栈）
        logutil.append_problem("ERROR", "api.%s" % method,
                               "%s: %s" % (type(e).__name__, e),
                               traceback.format_exc())
        # A-3b：message 保持旧文案逐字不变；evidence 只放方法名与异常
        # 类名，绝不回显 params（可能含乘车人 PII）。
        reply(id_, error="%s: %s" % (type(e).__name__, e),
              code="handler_error",
              evidence={"method": method, "exc_type": type(e).__name__})

def rpc_dispatch(line, threaded=False):
    """处理一行 JSON-RPC 请求并回包。

    从 main() 的循环体抽出，让协议层可以脱离真管道被测试。
    threaded=True（仅 main 的真管道）时，慢方法交给短线程执行，避免堵住
    stdin 队列；并发慢请求超过 6 个则退化为同步（背压，不丢请求）。
    """
    line = line.strip()
    if not line:
        return
    try:
        msg = json.loads(line)
    except Exception as e:
        reply(None, error="JSON 解析失败：%s" % e, code="bad_request",
              evidence={"reason": "invalid_json"})
        return
    if not isinstance(msg, dict):
        # stdin 是信任边界：非对象载荷原来会 AttributeError 冒泡出主循环，
        # 直接带崩后端进程（前端仍会以为后端活着）。
        reply(None, error="请求必须是 JSON 对象", code="bad_request",
              evidence={"reason": "not_object"})
        return
    id_ = msg.get("id")
    method = msg.get("method") or ""
    params = msg.get("params") or {}
    # 旧名直连（前端现状）或命名空间入口（tasks.list 风格，P4-B 新增契约）
    fn = METHODS.get(method) or _NS_METHODS.get(method)
    if fn is None:
        reply(id_, error="未知方法：" + method, code="unknown_method",
              evidence={"method": method})
        return
    # 命名空间入口同样映射到慢方法集合（旧名集合判断两者皆可）
    is_slow = (method in _SLOW_METHODS
               or _NS_TO_OLD.get(method) in _SLOW_METHODS)
    if threaded and is_slow:
        if _RPC_SLOTS.acquire(blocking=False):
            def _run():
                try:
                    _rpc_handle(id_, method, fn, params)
                finally:
                    _RPC_SLOTS.release()
            t = threading.Thread(target=_run, daemon=True,
                                 name="rpc-" + method)
            try:
                t.start()
            except Exception:
                _RPC_SLOTS.release()   # 线程没起来必须归还名额
                raise
        else:
            _rpc_handle(id_, method, fn, params)
        return
    _rpc_handle(id_, method, fn, params)

def main():
    # P2：三个杂项守护线程统一登记进 Scheduler（daemon 类型，原函数语义零改动）
    global _SCHED
    _SCHED = Scheduler()
    _SCHED.add_daemon("log_pump", log_pump)                # 日志泵线程
    _SCHED.add_daemon("armed_scan", _armed_loop)           # 到点自动开抢调度
    _SCHED.add_daemon("session_watch", _session_watch_loop)  # 会话看门狗
    _SCHED.add_periodic("reconcile_guard", 300,
                        _reconcile_tick, immediate=False)  # 对账守护（P3）
    _SCHED.start()
    # 启动软件即启动监控引擎（config.json: auto_start_engine=false 可关）。
    # 必须接住异常：get_engine() 里会 load_station_map()（首次要联网）、读 config，
    # 任何一处抛出去就是「后端起不来 → main.js 立刻重启 → 无限崩溃循环」，
    # 而且此刻 ready 还没发，界面只会看到一片空白。
    try:
        _auto_start_engine()
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        push("log", {"line": "[错误] 监控引擎启动失败：%s: %s" % (type(e).__name__, e),
                     "tag": "err"})
    push("ready", {"pid": os.getpid()})
    for line in sys.stdin:
        rpc_dispatch(line, threaded=True)


if __name__ == "__main__":
    main()
