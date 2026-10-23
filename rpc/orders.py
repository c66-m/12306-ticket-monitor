# -*- coding: utf-8 -*-
"""rpc.orders —— RPC 域 history.*（list/reconcile/push_query）与对账守护辅助。

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

from rpc.common import (HERE, _warn, load_config, push, read_history_records)

import grabber
import appcommon
import filelock
import order as order_mod

def _reconcile_update(key, rec):
    """对账写台账（显式传入 ReconcileRunner，走 appcommon 落 JSON+镜像）。"""
    try:
        appcommon.upsert_order(appcommon.data_path("orders.json"), key, rec)
    except Exception as e:
        _warn("对账写台账", e)

def _classify_session():
    """按 config 的 order_mode 选官方会话载体，返回 requests.Session。

    本函数补的是 _reconcile_tick 原本想调的会话分类逻辑——与
    engine.MonitorEngine._classify_session 同款口径（按模式选会话载体）；
    rpc 侧没有引擎实例，故在本模块实现等价的模块级版本。与 engine 版的
    差异：engine 在 browser 模式返回 None、由 classify 内部回退读
    .browser_state.json；而 SnapshotProvider 必须拿到真实 Session（构造
    时校验 .post），故这里 browser 模式直接 session_from_browser_state()。
    会话文件缺失等异常向上抛——_reconcile_tick 捕获后按「本轮跳过」审计，
    符合其「会话可用才跑」的设计（此前未定义此名，每轮必 NameError 被吞，
    对账守护从未真正运行）。
    """
    cfg = load_config()
    if (cfg.get("order_mode") or "http") == "browser":
        return order_mod.session_from_browser_state()
    return order_mod.load_session(
        appcommon.data_path(
            cfg.get("session_cookies_file", "session_cookies.json")))

def _reconcile_tick():
    """后台对账守护（P3）：每 5 分钟扫台账未决记录，用官方快照改判。

    会话可用才跑；会话异常/查询失败只做审计不中断。首跑延迟 300s
    （immediate=False），启动瞬间不立即改写台账。
    """
    try:
        from adapters.railway.snapshot import SnapshotProvider
        from app.reconcile_service import ReconcileRunner
        prov = SnapshotProvider(_classify_session())
        runner = ReconcileRunner(prov, updater=_reconcile_update)
        rep = runner.reconcile_once()
        push("log", {"line": "[对账] 台账对账完成 %s" % rep.summary(),
                     "tag": "info"})
    except Exception as e:
        push("log", {"line": "[对账] 本轮跳过：%s：%s"
                     % (type(e).__name__, e), "tag": "warn"})

def rpc_push_query_history(p):
    """p = {from, to, date}：记录一次查票历史（去重、最多 10 条，与 gui 同口径）。"""
    item = {"from": (p.get("from") or "").strip(),
            "to": (p.get("to") or "").strip(),
            "date": (p.get("date") or "").strip()}
    if not (item["from"] and item["to"]):
        return {"ok": False, "error": "缺少 from/to"}
    cfg = grabber.load_launcher_config()
    hist = [h for h in (cfg.get("query_history") or [])
            if not (isinstance(h, dict)
                    and h.get("from") == item["from"] and h.get("to") == item["to"]
                    and h.get("date") == item["date"])]
    hist.insert(0, item)
    cfg["query_history"] = hist[:10]
    appcommon.atomic_write_json(grabber.LAUNCHER_CFG_PATH, cfg)
    return {"ok": True, "query_history": cfg["query_history"]}

# ---------------- 历史 ----------------

def rpc_list_history(p):
    try:
        limit = int(p.get("limit") or 200)
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(limit, 1000))   # 负数会让 history[-(-5):] 返回最旧一段
    return {"records": read_history_records(limit)}

def rpc_reconcile_history(p):
    """把历史里「本地没确认到」的记录与 12306 官方订单对一次账。

    只回答一个问题：这些记录背后平台到底有没有订单。
    不下单、不支付、不取消、不改 state.json / 防重记录、不动引擎轮询。
    """
    # 与 rpc_list_history 同款兜底：limit 被传成 "abc"/列表等非法值时
    # 回落 50，不让 ValueError/TypeError 逃出 handler 变 handler_error
    try:
        limit = int(p.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    limit = min(200, max(1, limit))
    path = appcommon.history_path_of(load_config(), HERE)

    todo = ("failed", "ambiguous", "browser_dead")
    targets = [r for r in appcommon.read_history_records(path, limit=limit)
               if isinstance(r, dict)
               and r.get("result") in todo
               and not r.get("reconciled_at")
               and (r.get("date") or "").strip()]
    if not targets:
        return {"ok": True, "checked": 0, "changed": 0,
                "message": "没有需要核对的记录"}

    try:
        sess = order_mod.session_from_browser_state()
    except Exception as e:
        return {"ok": False,
                "message": "读不到登录会话（%s）。请先在界面点「重新登录」后再核对。" % e}

    # 按乘车日期分组：一个日期只打一次官方接口（12306 限流）
    by_date = {}
    for r in targets:
        by_date.setdefault(r["date"], []).append(r)

    # ponytail: 用 (time, date, train) 当定位键。同一秒同车次重复下单的概率
    # 可以忽略；真出现了也是同一份结论，不会写错值。
    outcome = {}
    errors = []
    for date, group in by_date.items():
        try:
            orders = order_mod.check_existing_orders(sess, date)
        except Exception as e:
            errors.append("%s：%s" % (date, e))
            continue
        for r in group:
            outcome[(r.get("time"), date, r.get("train"))] = \
                order_mod._classify_orders(date, r.get("train") or "",
                                           r.get("passengers") or [], orders)

    if not outcome:
        return {"ok": False, "checked": 0, "changed": 0, "errors": errors,
                "message": "官方订单查询全部失败，未做任何改动"}

    changed = 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with filelock.file_lock(path + ".lock", timeout=10):
            # 锁内重读：引擎随时往这个文件 append，用锁外读到的快照写回会吃掉新记录
            with open(path, encoding="utf-8-sig") as f:
                history = json.load(f)
            if not isinstance(history, list):
                return {"ok": False, "message": "order_history.json 结构异常，已放弃写回"}
            for rec in history:
                if not isinstance(rec, dict):
                    continue
                rid = (rec.get("time"), rec.get("date"), rec.get("train"))
                if rid not in outcome or rec.get("reconciled_at"):
                    continue
                cls, ono, raw = outcome[rid]
                rec["reconciled_cls"] = cls
                if raw:
                    rec["reconciled_raw"] = raw
                rec["reconciled_at"] = now
                if cls in ("unpaid", "paid"):
                    # 关键改判：平台有单，本地记的「失败」是假的
                    rec["reconciled_from"] = rec.get("result") or ""
                    rec["result"] = ("confirmed_unpaid" if cls == "unpaid"
                                     else "confirmed_paid")
                    if ono:
                        rec["order_no"] = ono
                    changed += 1
            appcommon.atomic_write_json(path, history)
    except TimeoutError:
        return {"ok": False, "message": "历史文件正被其它进程写入，请稍后再试"}
    except Exception as e:
        return {"ok": False, "message": "写回历史失败：%s" % e}

    msg = "核对 %d 条，更新 %d 条" % (len(targets), changed)
    if errors:
        msg += "；%d 个日期查询失败" % len(errors)
    push("log", {"line": "[对账] " + msg, "tag": "info"})
    return {"ok": True, "checked": len(targets), "changed": changed,
            "errors": errors, "message": msg}
