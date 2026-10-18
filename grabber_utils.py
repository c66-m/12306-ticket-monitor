# -*- coding: utf-8 -*-
"""grabber.py 的纯工具子模块：不引用任何被测试 mock 的符号、不依赖 tkinter。

（2026-10-18 随 launcher.py → grabber.py 一同改名；调用方用
`import grabber_utils as launcher_utils` 别名，调用点不变。）

被 mock 的符号（log, HERE, Grabber, 等）保留在 grabber.py 中，
本模块只包含不被 mock 的全局变量与工具函数。

2026-10-13 界面重构：TrainCard（tk.Frame）与复选框图标工具已随旧 UI 删除，
本模块成为纯业务工具层（无任何 tkinter 依赖）。
"""
import json
import re
import sys
import uuid
from datetime import datetime, timedelta

import requests

import appcommon
import ticket

# ----------------------------- 席别表 -----------------------------

SEAT_OPTIONS = list(ticket.SEAT_CHOICES)
SEAT_ORDER = {v: i for i, v in enumerate(SEAT_OPTIONS)}
MONITOR_SEAT_CHOICES = SEAT_OPTIONS

# ----------------------------- 字体 -----------------------------

FONT = ("Microsoft YaHei UI", 10) if sys.platform == "win32" else ("Helvetica", 11)

# ----------------------------- 默认配置 -----------------------------

_DEFAULT_LC = {
    "version": 1,
    "passenger_names": [],
    "from": "长葛",
    "to": "确山",
    "trains": ["K225", "K1969", "K925"],
    "seat_types": ["硬座"],
    "seat_priority": "",
    "date": "",
    "date_to": "",
    "start_time": "",
    "remind_minutes": 10,
    "warm_minutes": 10,
    "poll_seconds": 3,
    "presets": [],
    "update_url": "",
    "station_history": ["长葛", "确山"],
    "purpose_code": "ADULT",
    "pax_purpose": {},
    "query_history": [],
}

# ----------------------------- 票种 -----------------------------

PURPOSE_LABELS = {"ADULT": "成人票", "0X00": "学生票"}
LABEL_TO_PURPOSE = {v: k for k, v in PURPOSE_LABELS.items()}


def purpose_of(lc, name=None):
    """按人取票种代码；name=None 时返回余票查询口径（全是学生才用 0X00）。

    12306 的 queryLeftTicket 用 ADULT / 0X00 切换余票口径；下单时每个乘车人的
    ticket_type 由本人票种决定（成人=1，学生=3），所以两者分开提供。
    """
    lc = lc or {}
    pm = lc.get("pax_purpose") or {}
    default = lc.get("purpose_code") or "ADULT"
    if name is not None:
        return pm.get(name) or default
    names = lc.get("passenger_names") or []
    codes = [pm.get(n) or default for n in names] or [default]
    return "0X00" if all(c == "0X00" for c in codes) else "ADULT"


def purpose_map_of(lc):
    """{姓名: 票种代码}，只含本次勾选的乘车人（下单按人写 ticket_type 用）。"""
    lc = lc or {}
    pm = lc.get("pax_purpose") or {}
    default = lc.get("purpose_code") or "ADULT"
    return {n: (pm.get(n) or default) for n in (lc.get("passenger_names") or [])}


# ----------------------------- 软失败判定 -----------------------------

def _is_soft_fail(msg):
    """瞬时繁忙/排队类软失败判定（Task 65i）。

    这类错误是 12306 侧的瞬时拥塞，不是"下单逻辑失败"：不计入 fail_streak
    （"同一目标连续失败 N 次自动停止"守卫本意只拦硬失败）。busy_n 继续驱动
    退避（3s→30s 封顶）；持续繁忙会一直重试直到用户停止——抢票本意如此。
    """
    return any(k in (msg or "") for k in ("系统繁忙", "网络异常", "排队"))


# ----------------------------- 抢票任务模板 -----------------------------

def new_grab_task(seq):
    """新建一个空任务模板（配置与主配置同构，各任务互不影响）。"""
    t = dict(_DEFAULT_LC)
    t["id"] = uuid.uuid4().hex[:8]
    t["name"] = "任务 %d" % seq
    t["status"] = "idle"
    t["date"] = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    return t


# ----------------------------- 掩码 -----------------------------

def mask_id(no):
    no = str(no or "").strip()
    if len(no) <= 8:
        return no
    return no[:4] + "*" * (len(no) - 8) + no[-4:]


def mask_mobile(m):
    m = str(m or "").strip()
    if len(m) == 11:
        return m[:3] + "****" + m[-4:]
    return m


# 脱敏口径单源在 appcommon（原实现迁走，语义不变）；别名保留，调用点与测试 pin 不变。
_mask_order_no = appcommon.mask_order_no
_mask_one_name = appcommon.mask_one_name
_mask_names = appcommon.mask_names


_ORDER_NO_RE = re.compile(r"(订单号\s?)([A-Za-z0-9]{4,})")


def _mask_pii_text(text):
    """日志文本 PII 脱敏（Task 84c）：把文本中「订单号 XXX」形式的订单号打码；
    无 PII 的文本原样返回。只用于日志路径，不碰用户界面与历史记录。"""
    return _ORDER_NO_RE.sub(
        lambda m: m.group(1) + _mask_order_no(m.group(2)), text or "")


# ----------------------------- 席别归一化 -----------------------------

def _normalize_seat_types(raw, warn):
    """seat_types 字段归一化（Task 103a；与 engine.normalize_seat_types 同口径）：
    裸字符串（如漏写方括号的 "二等座"）按单个席别处理并记警告，绝不逐字符拆
    （旧代码 list("二等座") 会拆成 ['二','等','座'] 写回污染配置 → 漏单类）；
    形状非法记警告后忽略；非字符串条目跳过（席别为中文名，不做 strip/upper）。

    机械层已收敛到 appcommon.normalize_seat_types；本函数只留「警告文案」这层
    策略，调用方与测试看到的签名 (raw, warn) 不变。
    """
    return appcommon.normalize_seat_types(
        raw, warn,
        "[提醒] seat_types 为字符串，已按单个席别处理：%s",
        "[提醒] seat_types 形状非法，已忽略：%r")


_display_seat_types = appcommon.display_seat_types


# ----------------------------- 时间工具 -----------------------------

def parse_dt(s):
    """解析开抢时间。支持 YYYY-MM-DD HH:MM[:SS] / MM-DD HH:MM / HH:MM[:SS]。"""
    s = (s or "").strip()
    if not s:
        return None
    now = datetime.now()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m-%d %H:%M:%S",
                "%m-%d %H:%M", "%H:%M:%S", "%H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            if fmt.startswith("%H"):
                dt = dt.replace(year=now.year, month=now.month, day=now.day)
                if dt < now - timedelta(hours=1):
                    dt += timedelta(days=1)
            elif fmt.startswith("%m-%d"):
                dt = dt.replace(year=now.year)
                if dt < now - timedelta(hours=1):
                    dt = dt.replace(year=now.year + 1)
            return dt
        except ValueError:
            continue
    return None


def fmt_countdown(sec):
    sec = int(sec)
    if sec <= 0:
        return "00:00:00"
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return "%d天 %02d:%02d:%02d" % (d, h, m, s)
    return "%02d:%02d:%02d" % (h, m, s)


# ---------------------- 车次类型 / 日期标签 ----------------------

def is_emu(train_code):
    """动车组（高铁 G / 动车 D / 城际 C）判定。"""
    return bool(train_code) and str(train_code)[0].upper() in ("G", "D", "C")


def train_kind(train_code):
    """车次号首字母 → 中文类型。"""
    c = str(train_code or "")[:1].upper()
    return {"G": "高铁", "D": "动车", "C": "城际", "Z": "直达", "T": "特快",
            "K": "快速", "Y": "旅游", "S": "市郊", "L": "临客"}.get(c, "普速")


def fmt_date_tag(date_str):
    """2026-10-06 → 10-06 周二（卡片日期角标用）。"""
    try:
        d = datetime.strptime(str(date_str), "%Y-%m-%d")
    except Exception:
        return str(date_str or "")
    return "%s 周%s" % (d.strftime("%m-%d"), "一二三四五六日"[d.weekday()])


# ----------------------------- 位置定位 -----------------------------

def ip_locate(timeout=6):
    """本机 IP 归属地。主用太平洋网络接口（GBK 编码），失败退回 ip-api。返回可读字符串或错误提示。

    2026-10-14：原百度企服接口（qifu-api.baidubce.com）已 404 下线，改为上述两源。
    """
    try:
        r = requests.get("https://whois.pconline.com.cn/ipJson.jsp",
                         params={"json": "true"}, timeout=timeout)
        d = json.loads(r.content.decode("gbk", "ignore").strip())
        addr = (d.get("addr") or "").strip()
        if addr:
            return addr
    except Exception:
        pass
    try:
        d = requests.get("http://ip-api.com/json/", params={"lang": "zh-CN"},
                         timeout=timeout).json()
        if d.get("status") == "success":
            parts = [d.get("country", ""), d.get("city", ""), d.get("isp", "")]
            parts = [p for p in parts if p]
            if parts:
                return " ".join(parts)
        return "定位失败：%s" % (d.get("message") or d)
    except Exception as e:
        return "定位失败：%s" % e


# ----------------------------- 监控任务组装 -----------------------------

def build_monitor_task(from_name, to_name, dates, date_range, trains, seats,
                       passengers, purpose_code, auto_order, stop_after_order,
                       priority, pax_purpose=None, seat_priority=None):
    """按监控系统的任务结构组装任务字典（与 gui.py 新建监控任务一致）。"""
    name = "%s-%s %s %s" % (
        from_name, to_name, "/".join(trains) if trains else "全部车次",
        "/".join(seats))
    return {
        "name": name,
        "uid": uuid.uuid4().hex,
        "from": from_name,
        "to": to_name,
        "dates": dates,
        "date_range": date_range,
        "trains": trains,
        "seat_types": seats,
        "seat_priority": (str(seat_priority).strip()
                          if isinstance(seat_priority, str) else list(seat_priority or [])),
        "auto_order": bool(auto_order),
        "stop_after_order": bool(stop_after_order),
        "passenger_names": passengers,
        "priority": int(priority),
        "purpose_code": purpose_code,
        "pax_purpose": dict(pax_purpose or {}),
        "notify_channels": ["email"],
    }


# ----------------------------- 全角→半角 -----------------------------

_FW_TRANS = {0x3000: 0x20}
_FW_TRANS.update({0xFF01 + i: 0x21 + i for i in range(0x5E)})


def _norm_query(text):
    """中文输入容错：全角→半角、剔除全部空白、转小写（长 葛→长葛,ｃｑ→cq）。"""
    t = (text or "").translate(_FW_TRANS)
    return "".join(t.split()).lower()