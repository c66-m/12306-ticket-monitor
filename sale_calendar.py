# -*- coding: utf-8 -*-
"""预售日期计算（纯函数，不碰网络/文件）。

12306 当前预售期按日历天计算：乘车日期 D 的车票在 D - PRESALE_DAYS 天开售，
开售时刻取出发站的官方起售时间。窗口随官方政策变动，调用方可通过
presale_days 覆盖；本模块只负责日期算术，不判断真实库存/起售策略。
"""
import datetime

PRESALE_DAYS = 14
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def parse_sale_time(raw):
    """把 12306 的 HHmm（如 "1245"）转成 datetime.time；非法返回 None。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if ":" in text:
        parts = text.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            return None
        hh, mm = int(parts[0]), int(parts[1])
    else:
        if not text.isdigit() or len(text) > 4:
            return None
        text = text.zfill(4)
        hh, mm = int(text[:2]), int(text[2:])
    if hh > 23 or mm > 59:
        return None
    return datetime.time(hh, mm)


def _as_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return datetime.datetime.strptime(str(value), "%Y-%m-%d").date()


def format_dt(value):
    return value.strftime("%Y-%m-%d %H:%M:%S")


def build_sale_rows(start_date, days, sale_time, now=None, presale_days=PRESALE_DAYS):
    """生成 start_date 起 days 个日历天内、尚未开售的乘车日期。

    sale_time 为出发站官方起售时刻（"HH:MM" 或 "HHmm"）。只保留
    sale_at > now 的日期；已开售的日期不返回。
    """
    now = now or datetime.datetime.now()
    start = _as_date(start_date)
    tm = parse_sale_time(sale_time)
    if tm is None:
        raise ValueError("出发站起售时间非法：%r" % (sale_time,))
    rows = []
    for offset in range(max(0, int(days))):
        travel = start + datetime.timedelta(days=offset)
        sale_date = travel - datetime.timedelta(days=int(presale_days))
        sale_at = datetime.datetime.combine(sale_date, tm)
        if sale_at <= now:
            continue
        rows.append({
            "travelDate": travel.strftime("%Y-%m-%d"),
            "weekday": _WEEKDAYS[travel.weekday()],
            "saleDate": sale_date.strftime("%Y-%m-%d"),
            "saleTime": tm.strftime("%H:%M"),
            "saleAt": format_dt(sale_at),
            "saleAtTs": sale_at.timestamp(),
            "minutesUntilSale": int((sale_at - now).total_seconds() // 60),
        })
    return rows
