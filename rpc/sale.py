# -*- coding: utf-8 -*-
"""rpc.sale —— 预售日期查询与开售邮件提醒。

数据源：12306 官方「各站起售时间」缓存接口。开售日期由本地预售期
（rpc.grab.PRESALE_DAYS，当前 14 天）推算；提醒默认在开售前 30 分钟发送。
提醒队列落 data/sale_reminders.json，由 app.scheduler 周期任务驱动。
"""
import datetime
import json
import re
import time
import uuid

import filelock
import requests

import appcommon
import notify
import sale_calendar
from rpc.common import _warn, load_config, push
from rpc.grab import PRESALE_DAYS

SALE_TIME_URL = "https://www.12306.cn/index/otn/index12306/queryAllCacheSaleTime"
SALE_TIME_REFERER = "https://www.12306.cn/index/view/infos/sale_time.html"
SALE_TIME_TTL = 6 * 3600
REMIND_MINUTES = 30
MAX_REMIND_ATTEMPTS = 3

_SALE_TIMES_CACHE = {"at": 0.0, "stations": {}}


def _now():
    return datetime.datetime.now()


def _format_dt(value):
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _parse_dt(value):
    return datetime.datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")


def _parse_ymd(value):
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{8}", text):
        return None
    try:
        return datetime.datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


def _fetch_sale_times(force=False):
    """拉取并缓存 12306 各站起售时间；失败时回退旧缓存。"""
    now_ts = time.time()
    cached = _SALE_TIMES_CACHE.get("stations") or {}
    if not force and cached and now_ts - _SALE_TIMES_CACHE.get("at", 0.0) < SALE_TIME_TTL:
        return cached
    try:
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Safari/537.36"),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": SALE_TIME_REFERER,
            "Content-Length": "0",
        }
        resp = requests.post(SALE_TIME_URL, headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("status"):
            raise ValueError("接口 status 非 true")
        raw = data.get("data")
        if not isinstance(raw, list):
            raise ValueError("接口 data 不是列表")
        today = _now().date()
        stations = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            code = str(item.get("station_telecode") or "").strip().upper()
            name = str(item.get("station_name") or "").strip()
            tm = sale_calendar.parse_sale_time(item.get("sale_time"))
            if not re.fullmatch(r"[A-Z]{3}", code) or not name or tm is None:
                continue
            start = _parse_ymd(item.get("start_date"))
            stop = _parse_ymd(item.get("stop_date"))
            if start and today < start:
                continue
            if stop and today > stop:
                continue
            row = {"name": name, "saleTime": tm.strftime("%H:%M")}
            old = stations.get(code)
            # 同站多条（极少）：保留生效起始日更新的那条
            if old is None or str(item.get("start_date") or "") >= str(old.get("_start") or ""):
                row["_start"] = str(item.get("start_date") or "")
                stations[code] = row
        for row in stations.values():
            row.pop("_start", None)
        _SALE_TIMES_CACHE["stations"] = stations
        _SALE_TIMES_CACHE["at"] = now_ts
        return stations
    except Exception as e:
        _warn("拉取 12306 各站起售时间失败", e)
        if cached:
            return cached
        raise


def _email_cfg():
    cfg = load_config()
    notify_cfg = cfg.get("notify") if isinstance(cfg, dict) else None
    email = notify_cfg.get("email") if isinstance(notify_cfg, dict) else None
    return email if isinstance(email, dict) else {}


def _email_enabled():
    return bool(_email_cfg().get("enabled"))


def _reminders_path():
    return appcommon.data_path("sale_reminders.json")


def _load_reminders():
    path = _reminders_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        _warn("读取开售提醒文件失败", e)
        return []
    if isinstance(data, dict):
        data = data.get("reminders")
    if not isinstance(data, list):
        _warn("开售提醒数据形状非法已降级", type(data).__name__)
        return []
    return [dict(x) for x in data if isinstance(x, dict)]


def _save_reminders(reminders):
    appcommon.atomic_write_json(_reminders_path(),
                                {"version": 1, "reminders": reminders})


def _public_reminder(r):
    keys = ("id", "travelDate", "weekday", "fromCode", "toCode", "fromName",
            "toName", "saleDate", "saleTime", "saleAt", "triggerAt", "status",
            "attempts", "lastError", "createdAt", "sentAt")
    return {k: r.get(k) for k in keys if k in r}


def _route_key(r):
    return (r.get("fromCode"), r.get("toCode"), r.get("travelDate"))


def _station_or_error(stations, code, label):
    st = stations.get(code)
    if not st:
        return None, "未找到%s站（%s）的起售时间，请检查车站代码" % (label, code)
    return st, None


def _stored_tasks_path():
    return appcommon.data_path("stored_tasks.json")


def _load_stored_tasks():
    path = _stored_tasks_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        _warn("读取储存任务文件失败", e)
        return []
    if isinstance(data, dict):
        data = data.get("tasks")
    if not isinstance(data, list):
        _warn("储存任务数据形状非法已降级", type(data).__name__)
        return []
    return [dict(x) for x in data if isinstance(x, dict)]


def _save_stored_tasks(tasks):
    appcommon.atomic_write_json(_stored_tasks_path(),
                                {"version": 1, "tasks": tasks})


def _public_stored_task(t):
    keys = ("id", "travelDate", "weekday", "fromCode", "toCode", "fromName",
            "toName", "saleDate", "saleTime", "saleAt", "createdAt")
    return {k: t.get(k) for k in keys if k in t}


def _purge_expired_stored_tasks(tasks, now=None):
    """开抢后自动删除：只保留 saleAt 仍在未来的储存任务。"""
    now = now or _now()
    kept = []
    for t in tasks:
        try:
            sale_at = _parse_dt(t.get("saleAt"))
        except Exception:
            continue
        if sale_at > now:
            kept.append(t)
    return kept


def rpc_list_stored_tasks(p):
    p = p or {}
    try:
        with filelock.file_lock(_stored_tasks_path() + ".lock", timeout=10):
            tasks = _load_stored_tasks()
            kept = _purge_expired_stored_tasks(tasks)
            if len(kept) != len(tasks):
                _save_stored_tasks(kept)
        from_code = str(p.get("fromCode") or "").strip().upper()
        to_code = str(p.get("toCode") or "").strip().upper()
        if from_code:
            kept = [t for t in kept if t.get("fromCode") == from_code]
        if to_code:
            kept = [t for t in kept if t.get("toCode") == to_code]
        kept.sort(key=lambda t: str(t.get("saleAt") or ""))
        return {"tasks": [_public_stored_task(t) for t in kept]}
    except Exception as e:
        _warn("读取储存任务失败", e)
        return {"error": "读取储存任务失败：%s" % e}


def rpc_save_stored_task(p):
    """p = {fromCode, toCode, travelDate}：储存查询任务，不依赖邮件配置。"""
    p = p or {}
    try:
        from_code = str(p.get("fromCode") or "").strip().upper()
        to_code = str(p.get("toCode") or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", from_code or "") or \
                not re.fullmatch(r"[A-Z]{3}", to_code or ""):
            return {"ok": False, "error": "请先选择有效的出发站和到达站"}
        if from_code == to_code:
            return {"ok": False, "error": "出发站和到达站不能相同"}
        travel_text = str(p.get("travelDate") or "").strip()
        try:
            travel = datetime.datetime.strptime(travel_text, "%Y-%m-%d").date()
        except ValueError:
            return {"ok": False, "error": "乘车日期格式应为 YYYY-MM-DD"}
        stations = _fetch_sale_times()
        from_st, err = _station_or_error(stations, from_code, "出发")
        if err:
            return {"ok": False, "error": err}
        to_st, err = _station_or_error(stations, to_code, "到达")
        if err:
            return {"ok": False, "error": err}
        now = _now()
        rows = sale_calendar.build_sale_rows(
            travel, 1, from_st["saleTime"], now=now,
            presale_days=PRESALE_DAYS)
        if not rows:
            return {"ok": False, "error": "该日期车票已开售，无法储存任务"}
        row = rows[0]
        task = {
            "id": uuid.uuid4().hex[:12],
            "travelDate": row["travelDate"],
            "weekday": row["weekday"],
            "fromCode": from_code,
            "toCode": to_code,
            "fromName": from_st["name"],
            "toName": to_st["name"],
            "saleDate": row["saleDate"],
            "saleTime": row["saleTime"],
            "saleAt": row["saleAt"],
            "createdAt": _format_dt(now),
        }
        with filelock.file_lock(_stored_tasks_path() + ".lock", timeout=10):
            tasks = _purge_expired_stored_tasks(_load_stored_tasks(), now)
            for old in tasks:
                if _route_key(old) == _route_key(task):
                    return {"ok": True, "duplicate": True,
                            "task": _public_stored_task(old)}
            tasks.append(task)
            _save_stored_tasks(tasks)
        return {"ok": True, "task": _public_stored_task(task)}
    except Exception as e:
        _warn("保存储存任务失败", e)
        return {"ok": False, "error": "保存储存任务失败：%s" % e}


def rpc_delete_stored_task(p):
    rid = str((p or {}).get("id") or "").strip()
    if not rid:
        return {"ok": False, "error": "缺少储存任务 id"}
    try:
        with filelock.file_lock(_stored_tasks_path() + ".lock", timeout=10):
            tasks = _load_stored_tasks()
            kept = [t for t in tasks if t.get("id") != rid]
            if len(kept) == len(tasks):
                return {"ok": False, "error": "储存任务不存在"}
            _save_stored_tasks(kept)
        return {"ok": True}
    except Exception as e:
        _warn("删除储存任务失败", e)
        return {"ok": False, "error": "删除储存任务失败：%s" % e}


def rpc_query_sale_dates(p):
    """查询尚未开售的乘车日期；只读，不建提醒。"""
    try:
        from_code = str(p.get("fromCode") or "").strip().upper()
        to_code = str(p.get("toCode") or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", from_code or "") or \
                not re.fullmatch(r"[A-Z]{3}", to_code or ""):
            return {"error": "请先选择有效的出发站和到达站"}
        if from_code == to_code:
            return {"error": "出发站和到达站不能相同"}
        try:
            days = int(p.get("days") or 60)
        except (TypeError, ValueError):
            return {"error": "查询天数必须是数字"}
        days = max(1, min(days, 120))
        stations = _fetch_sale_times()
        from_st, err = _station_or_error(stations, from_code, "出发")
        if err:
            return {"error": err}
        to_st, err = _station_or_error(stations, to_code, "到达")
        if err:
            return {"error": err}
        now = _now()
        rows = sale_calendar.build_sale_rows(
            now.date(), days, from_st["saleTime"], now=now,
            presale_days=PRESALE_DAYS)
        reminders = _load_reminders()
        by_key = {}
        for r in reminders:
            if r.get("status") in ("pending", "sent", "failed"):
                by_key[_route_key(r)] = r.get("id")
        stored_tasks = _purge_expired_stored_tasks(_load_stored_tasks(), now)
        stored_by_key = {}
        for t in stored_tasks:
            stored_by_key[_route_key(t)] = t.get("id")
        for row in rows:
            row["reminderId"] = by_key.get((from_code, to_code, row["travelDate"]))
            row["storedTaskId"] = stored_by_key.get(
                (from_code, to_code, row["travelDate"]))
        return {
            "from": {"code": from_code, "name": from_st["name"],
                     "saleTime": from_st["saleTime"]},
            "to": {"code": to_code, "name": to_st["name"]},
            "presaleDays": PRESALE_DAYS,
            "days": days,
            "serverNow": _format_dt(now),
            "mailEnabled": _email_enabled(),
            "rows": rows,
            "storedTasks": [_public_stored_task(t) for t in stored_tasks
                            if t.get("fromCode") == from_code
                            and t.get("toCode") == to_code],
            "reminders": [_public_reminder(r) for r in reminders
                          if r.get("fromCode") == from_code
                          and r.get("toCode") == to_code],
        }
    except Exception as e:
        _warn("查询预售日期失败", e)
        return {"error": "查询预售日期失败：%s" % e}


def rpc_list_sale_reminders(p):
    p = p or {}
    try:
        reminders = _load_reminders()
        from_code = str(p.get("fromCode") or "").strip().upper()
        to_code = str(p.get("toCode") or "").strip().upper()
        if from_code:
            reminders = [r for r in reminders if r.get("fromCode") == from_code]
        if to_code:
            reminders = [r for r in reminders if r.get("toCode") == to_code]
        reminders.sort(key=lambda r: str(r.get("triggerAt") or ""))
        return {"reminders": [_public_reminder(r) for r in reminders],
                "mailEnabled": _email_enabled()}
    except Exception as e:
        _warn("读取开售提醒失败", e)
        return {"error": "读取开售提醒失败：%s" % e}


def rpc_create_sale_reminder(p):
    """p = {fromCode, toCode, travelDate}：按官方起售时间创建开售提醒。"""
    try:
        from_code = str(p.get("fromCode") or "").strip().upper()
        to_code = str(p.get("toCode") or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", from_code or "") or \
                not re.fullmatch(r"[A-Z]{3}", to_code or ""):
            return {"ok": False, "error": "请先选择有效的出发站和到达站"}
        if from_code == to_code:
            return {"ok": False, "error": "出发站和到达站不能相同"}
        if not _email_enabled():
            return {"ok": False, "error": "邮件通知未启用，请先在设置中配置并启用邮件通知"}
        travel_text = str(p.get("travelDate") or "").strip()
        try:
            travel = datetime.datetime.strptime(travel_text, "%Y-%m-%d").date()
        except ValueError:
            return {"ok": False, "error": "乘车日期格式应为 YYYY-MM-DD"}
        stations = _fetch_sale_times()
        from_st, err = _station_or_error(stations, from_code, "出发")
        if err:
            return {"ok": False, "error": err}
        to_st, err = _station_or_error(stations, to_code, "到达")
        if err:
            return {"ok": False, "error": err}
        now = _now()
        rows = sale_calendar.build_sale_rows(
            travel, 1, from_st["saleTime"], now=now,
            presale_days=PRESALE_DAYS)
        if not rows:
            return {"ok": False, "error": "该日期车票已开售，无法设置开售提醒"}
        row = rows[0]
        sale_at = _parse_dt(row["saleAt"])
        trigger_at = sale_at - datetime.timedelta(minutes=REMIND_MINUTES)
        reminder = {
            "id": uuid.uuid4().hex[:12],
            "travelDate": row["travelDate"],
            "weekday": row["weekday"],
            "fromCode": from_code,
            "toCode": to_code,
            "fromName": from_st["name"],
            "toName": to_st["name"],
            "saleDate": row["saleDate"],
            "saleTime": row["saleTime"],
            "saleAt": row["saleAt"],
            "triggerAt": _format_dt(trigger_at),
            "status": "pending",
            "attempts": 0,
            "createdAt": _format_dt(now),
        }
        with filelock.file_lock(_reminders_path() + ".lock", timeout=10):
            reminders = _load_reminders()
            for old in reminders:
                if _route_key(old) == _route_key(reminder):
                    return {"ok": True, "duplicate": True,
                            "reminder": _public_reminder(old)}
            reminders.append(reminder)
            _save_reminders(reminders)
        return {"ok": True, "reminder": _public_reminder(reminder)}
    except Exception as e:
        _warn("创建开售提醒失败", e)
        return {"ok": False, "error": "创建开售提醒失败：%s" % e}


def rpc_delete_sale_reminder(p):
    rid = str((p or {}).get("id") or "").strip()
    if not rid:
        return {"ok": False, "error": "缺少提醒 id"}
    try:
        with filelock.file_lock(_reminders_path() + ".lock", timeout=10):
            reminders = _load_reminders()
            kept = [r for r in reminders if r.get("id") != rid]
            if len(kept) == len(reminders):
                return {"ok": False, "error": "提醒不存在"}
            _save_reminders(kept)
        return {"ok": True}
    except Exception as e:
        _warn("删除开售提醒失败", e)
        return {"ok": False, "error": "删除开售提醒失败：%s" % e}


def _email_text(r):
    subject = "【12306】开售提醒：%s → %s %s 车票 %s 开售" % (
        r.get("fromName"), r.get("toName"), r.get("travelDate"), r.get("saleTime"))
    body = """你关注的行程即将开售：

出发：%s
到达：%s
乘车日期：%s（%s）
开售时间：%s
提醒时间：开售前 %d 分钟

请提前登录 12306，准备好乘车人信息。
—— 12306 车票监控
""" % (r.get("fromName"), r.get("toName"), r.get("travelDate"),
       r.get("weekday") or "", r.get("saleAt"), REMIND_MINUTES)
    return subject, body


def sale_reminder_tick(now=None):
    """调度器周期任务：发送到点的开售提醒。

    邮件发送在文件锁外进行，避免 SMTP 慢时阻塞前端创建/删除提醒；更新状态
    时重新加锁按 id 合并。scheduler 单线程，因此不会并发重入本函数。
    """
    now = now or _now()
    if not _email_enabled():
        return {"sent": 0, "failed": 0, "expired": 0, "skipped": "mail_disabled"}
    due = []
    expired = 0
    with filelock.file_lock(_reminders_path() + ".lock", timeout=10):
        reminders = _load_reminders()
        changed = False
        for r in reminders:
            status = r.get("status")
            if status not in ("pending", "failed"):
                continue
            try:
                sale_at = _parse_dt(r.get("saleAt"))
                trigger_at = _parse_dt(r.get("triggerAt"))
            except Exception:
                r["status"] = "failed"
                r["lastError"] = "提醒时间格式非法"
                changed = True
                continue
            if now >= sale_at:
                r["status"] = "expired"
                expired += 1
                changed = True
                continue
            if now < trigger_at:
                continue
            attempts = int(r.get("attempts") or 0)
            if attempts >= MAX_REMIND_ATTEMPTS:
                r["status"] = "failed"
                changed = True
                continue
            due.append(dict(r))
        if changed:
            _save_reminders(reminders)
    if not due:
        return {"sent": 0, "failed": 0, "expired": expired}

    results = []
    email = _email_cfg()
    for r in due:
        subject, body = _email_text(r)
        try:
            ok, msg = notify.send_email(dict(email), subject, body)
        except Exception as e:
            ok, msg = False, "邮件发送异常：%s" % e
        results.append((r, bool(ok), msg or ""))

    sent = failed = 0
    with filelock.file_lock(_reminders_path() + ".lock", timeout=10):
        reminders = _load_reminders()
        by_id = {r.get("id"): r for r in reminders}
        for snapshot, ok, msg in results:
            r = by_id.get(snapshot.get("id"))
            if not r:
                continue
            r["attempts"] = int(r.get("attempts") or 0) + 1
            r["lastAttemptAt"] = _format_dt(now)
            if ok:
                r["status"] = "sent"
                r["sentAt"] = _format_dt(now)
                r.pop("lastError", None)
                sent += 1
            else:
                r["lastError"] = msg
                if r["attempts"] >= MAX_REMIND_ATTEMPTS:
                    r["status"] = "failed"
                failed += 1
        _save_reminders(reminders)
    for snapshot, ok, msg in results:
        if ok:
            push("log", {"line": "[提示] 已发送开售提醒邮件：%s → %s %s"
                         % (snapshot.get("fromName"), snapshot.get("toName"),
                            snapshot.get("travelDate")), "tag": "info"})
        else:
            push("log", {"line": "[警告] 开售提醒邮件发送失败：%s → %s %s：%s"
                         % (snapshot.get("fromName"), snapshot.get("toName"),
                            snapshot.get("travelDate"), msg), "tag": "warn"})
    return {"sent": sent, "failed": failed, "expired": expired}
