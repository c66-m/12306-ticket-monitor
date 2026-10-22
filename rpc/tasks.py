# -*- coding: utf-8 -*-
"""rpc.tasks —— RPC 域 tasks.* + engines.*：监控任务的增删改查、状态切换与引擎启停入口。

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

from rpc.common import (_task_defaults, _warn, delete_task_everywhere, engine_running, get_engine, load_config, load_state, push, start_engine, stop_engine, update_state_locked)

import grabber
import ticket
import engine as engine_mod
import appcommon

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
            # uid 供前端精确删除/替换：同名任务（改日期后名字不变）只能靠它区分
            "uid": t.get("uid") or "",
            "from": t.get("from") or "",
            "to": t.get("to") or "",
            "fromCode": _name2code.get(t.get("from") or "", ""),
            "toCode": _name2code.get(t.get("to") or "", ""),
            "dates": t.get("dates") or [],
            "trains": t.get("trains") or [],
            "seat_types": t.get("seat_types") or [],
            "seat_priority": t.get("seat_priority") or "",
            # 落盘字段名是 purpose_code / passenger_names（见 grabber_utils.build_monitor_task）；
            # 早先这里读 t["purpose"] / t["passengers"] 恒为空 → 编辑弹窗回填不出票种和乘车人，
            # 用户保存一次就把乘车人清空了
            "purpose": t.get("purpose_code") or "ADULT",
            "passengers": t.get("passenger_names") or [],
            "priority": t.get("priority") or "",
            "desired_status": t.get("status") or "paused",
            "status": rec.get("status") or t.get("status") or "paused",
            "message": rec.get("message") or "",
            "status_label": engine_mod.STATUS_LABELS.get(rec.get("status") or t.get("status") or "paused", "?"),
            # 缺省与引擎同口径（engine._run_task 取 task.get("auto_order", True)）：
            # 老任务可能存着 false，之前界面完全不显示这一项，用户会以为「有票就该下单」
            "auto_order": bool(t.get("auto_order", True)),
        })
    # engineRunning=线程活着；engine_alive=引擎在真监控（线程活着且存在
    # monitoring/retrying 任务）。引擎空跑时任何任务都不是 ACTIVE，engine_alive=False，
    # 前端据此把「是否监控」徽章关掉。
    active = any(t["status"] in ("monitoring", "retrying") for t in tasks_out)
    return {"tasks": tasks_out, "engineRunning": engine_running(),
            "engine_alive": engine_running() and active}

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
        a, b = str(raw[0]), str(raw[1])
        try:
            # 列表形态同样必须是 ISO 日期：非 ISO 落盘后会被引擎静默丢弃
            datetime.date.fromisoformat(a)
            datetime.date.fromisoformat(b)
        except (TypeError, ValueError):
            return dates, []
        if a > b:
            # 开始晚于结束：ISO 串可直接比较。不交换的话引擎展开不出任何日期，
            # 任务会被判「监控日期已全部过期」直接失败（2026-10-18 实测）。
            a, b = b, a
            push("log", {"line": "[提醒] 结束日期早于开始日期，已自动交换为 %s ~ %s"
                         % (a, b), "tag": "warn"})
        return dates, [a, b]
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
        v = p[key]
        if isinstance(v, str):   # bool("false") 是 True：字符串按字面值解释
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)
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
    raw_seats = p.get("seat_types")
    if isinstance(raw_seats, str):
        # 裸字符串不能被逐字符拆成「二/等/座」
        raw_seats = re.split(r"[,，\s]+", raw_seats)
    seats = [str(s).strip() for s in (raw_seats or []) if str(s).strip()]
    raw_pax = p.get("passengers")
    if isinstance(raw_pax, str):
        raw_pax = re.split(r"[,，\s]+", raw_pax)
    pax_names = [str(n).strip() for n in (raw_pax or []) if str(n).strip()]
    raw_purpose_map = p.get("pax_purpose")
    pax_purpose = raw_purpose_map if isinstance(raw_purpose_map, dict) else {}
    auto_order = _req_on(p, "auto_order")
    if auto_order and seats:
        # 与 gui 同规则：自动下单剔除不支持下单的席别
        bad = [s for s in seats if s not in ticket.SEAT_NAME_TO_CODE]
        if bad:
            seats = [s for s in seats if s in ticket.SEAT_NAME_TO_CODE]
            push("log", {"line": "[提醒] 席别 %s 暂不支持自动下单，已从该监控任务移除" % "、".join(bad), "tag": "warn"})
    dates, date_range = _split_date_input(p)
    if not dates and not date_range:
        # 空日期任务进了引擎只会得到「监控日期已全部过期」然后失败——
        # 在创建时就拦下，前端能直接看到原因（前端 dateSpan 对倒置区间返回 []）。
        return {"ok": False,
                "error": "监控日期为空：结束日期不能早于开始日期，请重新选择"}
    task = grabber.build_monitor_task(
        str(p.get("from") or ""), str(p.get("to") or ""),
        dates, date_range,
        trains, seats, pax_names,
        p.get("purpose") or "ADULT", auto_order,
        _req_on(p, "stop_after_order"), prio,
        pax_purpose=pax_purpose,
        seat_priority=pri_raw)
    task["notify_channels"] = ["email"] if _req_on(p, "notify") else []
    if pri_raw:
        try:
            parsed = ticket.seat_rules_parse(pri_raw)
            for warn in parsed["warnings"]:
                push("log", {"line": "[提醒] 首选席别：%s" % warn, "tag": "warn"})
        except Exception as e:
            _warn("首选席别解析", e)
    grabber.append_monitor_task(task)
    if _req_on(p, "start_now"):
        start_engine()
        get_engine().set_task_status(task, "monitoring", "新建任务立即启动", force=True)
    return {"ok": True, "name": task.get("name")}

_TASK_STATUSES = ("monitoring", "paused", "failed", "cancelled", "success")


def rpc_set_task_status(p):
    """p = {name, status, message?}"""
    name = p.get("name")
    status = p.get("status")
    if status not in _TASK_STATUSES:
        # 任意状态落盘会让界面显示 "?"，伪造 success 还会污染历史
        return {"ok": False, "error": "非法任务状态：%s" % status}
    cfg = load_config()
    task = next((t for t in cfg.get("tasks") or [] if t.get("name") == name), None)
    if not task:
        return {"ok": False, "error": "任务不存在：" + str(name)}
    if engine_running():
        get_engine().set_task_status(task, status, p.get("message") or "", force=True)
    else:
        # 引擎未运行：直接写 state（tasks 被手改成非 dict 时按空表重建）
        def _set(state):
            tasks = state.get("tasks")
            if not isinstance(tasks, dict):
                tasks = {}
                state["tasks"] = tasks
            rec = tasks.setdefault(name, {})
            rec["status"] = status
            rec["message"] = p.get("message") or ""
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
    uid, name = p.get("uid"), p.get("name")
    cfg = load_config()
    task = None
    if uid:
        # 优先按 uid：编辑产生的重复任务名字完全相同，按名字删只能删到第一条
        task = next((t for t in cfg.get("tasks") or [] if t.get("uid") == uid), None)
    if task is None and name:
        task = next((t for t in cfg.get("tasks") or [] if t.get("name") == name), None)
    if not task:
        return {"ok": False, "error": "任务不存在：" + str(uid or name)}
    delete_task_everywhere(None, task)  # app=None → 文件路径清 state
    return {"ok": True}

def rpc_start_engine(_p=None):
    return {"ok": start_engine()}

def rpc_stop_engine(_p=None):
    return {"ok": stop_engine()}
