# -*- coding: utf-8 -*-
"""rpc.grab —— RPC 域 grab.* + presets.* + tickets.* + seats.* + stations.*：抢票任务、
自动开抢、查票、席别反馈、车站搜索与常用行程预设。

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

from rpc.common import (_warn, push)

import grabber as launcher          # 2026-10-18 改名：launcher.py → grabber.py（别名保持调用点不变）
import grabber_utils as launcher_utils
import ticket
import appcommon

# 12306 预售期：2026-10-14 二分实测，今天+14 天（2026-10-28）仍能查到结果，
# 今天+15 天（2026-10-29）起四个端点全部返回 HTML 错误页（非 JSON）。窗口随官方
# 政策变动，故此天数只用于提前给出可读提示，真实成败仍由查询本身决定。
PRESALE_DAYS = 14

def rpc_save_grab_task(p):
    """p = 完整任务 dict（含 id/name/status）。"""
    tid = p.get("id")
    if not tid:
        return {"ok": False, "error": "缺少任务 id"}
    for k in ("poll_seconds", "warm_minutes", "remind_minutes"):
        if p.get(k) not in (None, ""):
            try:
                p[k] = float(p[k])
            except (TypeError, ValueError):
                return {"ok": False, "error": "参数 %s 必须是数字" % k}
    tasks = launcher.load_grab_tasks()
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
    if not tid:
        # 空 id 会命中「所有无 id 任务」，一次误调用清空老任务
        return {"ok": False, "error": "缺少任务 id"}
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
    from_code = str(p.get("fromCode") or "").strip().upper()
    to_code = str(p.get("toCode") or "").strip().upper()
    date = str(p.get("date") or "").strip()
    purpose = p.get("purpose") or "ADULT"
    if not (from_code and to_code and date):
        return {"error": "缺少 fromCode/toCode/date"}
    if not (re.fullmatch(r"[A-Z]{3}", from_code)
            and re.fullmatch(r"[A-Z]{3}", to_code)):
        return {"error": "车站代码格式不正确（应为 3 位大写字母）"}
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        return {"error": "日期格式必须是 YYYY-MM-DD"}
    if purpose not in ("ADULT", "0X00"):
        purpose = "ADULT"
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

def _armed_prepare_lead(t):
    """自动开抢的提前量（秒）：至少提前 3 分钟拉起 Grabber，留给登录+预热；
    warm_minutes 配得更大就按它。解析异常兜底 180 秒。"""
    try:
        return max(180, int(t.get("warm_minutes") or 10) * 60)
    except (TypeError, ValueError):
        return 180

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
                lead = _armed_prepare_lead(t)
                if (st - now).total_seconds() > lead:
                    # 提前 max(3min, warm_minutes) 启动：Grabber 起来后先登录+预热，
                    # 由 launcher 预热段等到开抢点再下单
                    continue
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
