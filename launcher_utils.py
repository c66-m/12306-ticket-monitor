# -*- coding: utf-8 -*-
"""launcher.py 的纯工具子模块：不引用任何被测试 mock 的符号。

被 mock 的符号（log, HERE, ttk, messagebox, Grabber, 等）保留在 launcher.py 中，
本模块只包含不被 mock 的全局变量、工具函数和 TrainCard 类。
"""
import re
import sys
import uuid
from datetime import datetime, timedelta

import requests

import tkinter as tk

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


# ----------------------------- 复选框图标 -----------------------------

def _make_check_icon(checked, size=14):
    """内存生成复选框指示器图标（不依赖外部图片文件）：空框 / 框内对勾。"""
    img = tk.PhotoImage(width=size, height=size)
    border, fill = "#8A94A6", "#FFFFFF"
    img.put(fill, to=(0, 0, size, size))
    img.put(border, to=(0, 0, size, 1))
    img.put(border, to=(0, size - 1, size, size))
    img.put(border, to=(0, 1, 1, size - 1))
    img.put(border, to=(size - 1, 1, size, size - 1))
    if checked:
        tick = "#1A73E8"
        pts = [(3, 6), (4, 7), (5, 8), (6, 9), (7, 8), (8, 7), (9, 6), (10, 4)]
        for x, y in pts:
            img.put(tick, to=(x, y, x + 2, y + 2))
    return img


def _patch_tick_layout(items):
    """把 TCheckbutton 布局里的 indicator element 换成自定义的对勾。"""
    out = []
    for name, opts in items:
        opts = dict(opts)
        if name == "Checkbutton.indicator":
            name = "Tick.indicator"
        if "children" in opts:
            opts["children"] = _patch_tick_layout(opts["children"])
        out.append((name, opts))
    return out


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


def _mask_order_no(no):
    """订单号脱敏（日志用；与 engine._mask_order_no 同口径）：保留前后各 4 位，
    中间打码；过短则全打码。"""
    s = str(no or "")
    if len(s) <= 8:
        return "****"
    return s[:4] + "****" + s[-4:]


def _mask_one_name(n):
    """单个姓名打码（Task 88c：与 _mask_names 同口径的单源）：保留首字其余打码。"""
    n = (n or "").strip()
    if not n:
        return ""
    return n[0] + "*" * (len(n) - 1) if len(n) > 1 else "*"


def _mask_names(names):
    """乘车人姓名打码（日志用；与 engine._mask_names 同口径）：保留首字其余打码；
    ['张三','李四'] -> '张*、李*'。"""
    out = []
    for n in names or []:
        m = _mask_one_name(n)
        if m:
            out.append(m)
    return "、".join(out)


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
    形状非法记警告后忽略；非字符串条目跳过（席别为中文名，不做 strip/upper）。"""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        warn("[提醒] seat_types 为字符串，已按单个席别处理：%s" % raw)
        return [raw]
    if not isinstance(raw, (list, tuple)):
        warn("[提醒] seat_types 形状非法，已忽略：%r" % (raw,))
        return []
    return [s for s in raw if isinstance(s, str)]


def _display_seat_types(raw):
    """展示用席别归一化（Task 103b）：与 _display_trains 同口径（字符串按单个
    席别、绝不逐字符拆），但不打日志——展示路径可能高频刷新，畸形值的警告由
    数据消费路径（engine/launcher 运行链）负责；这里只解决展示。"""
    return _normalize_seat_types(raw, lambda *a: None)


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
    """百度 IP 定位（公开接口，免 key）。返回可读字符串或错误提示。"""
    try:
        r = requests.get("https://qifu-api.baidubce.com/ip/local/geo/v1/district",
                         timeout=timeout)
        d = r.json()
        if d.get("code") == "Success":
            g = d.get("data") or {}
            parts = [g.get("country", ""), g.get("prov", ""), g.get("city", "")]
            parts = [p for p in parts if p]
            isp = g.get("isp", "")
            return ("%s（%s）" % (" ".join(parts), isp)) if isp else " ".join(parts)
        return "定位失败：%s" % (d.get("message") or d)
    except Exception as e:
        return "定位失败：%s" % e


# ----------------------------- 监控任务组装 -----------------------------

def build_monitor_task(from_name, to_name, dates, date_range, trains, seats,
                       passengers, purpose_code, auto_order, stop_after_order,
                       priority, pax_purpose=None, seat_priority=None):
    """按监控系统的任务结构组装任务字典（与 gui.py QuickMonitorDialog 一致）。"""
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


# ----------------------------- 车次卡片 -----------------------------

class TrainCard(tk.Frame):
    """单趟车卡片：时间 — 车次 类型 — 时间 / 历时 / 区间 / 有票席别摘要，整卡可点选。"""

    def __init__(self, master, info, on_click, selected=False):
        super().__init__(master, bd=0, bg="#ffffff",
                         highlightthickness=1, highlightbackground="#d0d7de", cursor="hand2")
        self.info = info
        self.on_click = on_click
        self.key = "%s@%s" % (info.get("_date") or info.get("query_date") or "",
                              info.get("train_code") or "?")
        self.selected = False
        body = tk.Frame(self, bg="#ffffff")
        body.pack(fill="x", padx=8, pady=3)

        small = (FONT[0], 9)
        head = tk.Frame(body, bg="#ffffff")
        head.pack(fill="x")
        big = (FONT[0], 11, "bold")
        tk.Label(head, text=info.get("start_time") or "-", font=big, bg="#ffffff").pack(side="left")
        tk.Label(head, text="——", fg="#8c959f", bg="#ffffff").pack(side="left", padx=5)
        tk.Label(head, text=info.get("train_code") or "?", font=(FONT[0], 10, "bold"),
                 fg="#0969da", bg="#ffffff").pack(side="left")
        tk.Label(head, text=train_kind(info.get("train_code")), fg="#57606a",
                 bg="#ffffff", font=small).pack(side="left", padx=(4, 0))
        tk.Label(head, text="——", fg="#8c959f", bg="#ffffff").pack(side="left", padx=5)
        tk.Label(head, text=info.get("arrive_time") or "-", font=big, bg="#ffffff").pack(side="left")
        tk.Label(head, text="历时 " + (info.get("duration") or "-"), fg="#57606a",
                 bg="#ffffff", font=small).pack(side="right")

        sub = tk.Frame(body, bg="#ffffff")
        sub.pack(fill="x")
        tk.Label(sub, text="%s → %s" % (info.get("from_name") or "?",
                                        info.get("to_name") or "?"),
                 fg="#57606a", bg="#ffffff", font=small).pack(side="left")
        if info.get("_date"):
            tk.Label(sub, text="  " + fmt_date_tag(info["_date"]), fg="#8250df",
                     bg="#ffffff", font=small).pack(side="left")
        seats = info.get("available_seats") or {}
        names = list(SEAT_OPTIONS) + [k for k in seats if k not in SEAT_ORDER]
        avail = [(n, seats.get(n)) for n in names if seats.get(n)]
        if avail:
            parts = ["%s %s" % (n, v) for n, v in avail[:5]]
            if len(avail) > 5:
                parts.append("…另 %d 席" % (len(avail) - 5))
            tk.Label(sub, text="    " + " · ".join(parts), fg="#1a7f37",
                     bg="#ffffff", font=(FONT[0], 9, "bold")).pack(side="left")
        else:
            tk.Label(sub, text="    %d 种席别均无票" % len(names), fg="#8c959f",
                     bg="#ffffff", font=small).pack(side="left")

        self._bind_click_all(self)
        self.set_selected(selected)

    def _bind_click_all(self, w):
        w.bind("<Button-1>", self._clicked)
        for c in w.winfo_children():
            self._bind_click_all(c)

    def _clicked(self, _e=None):
        self.on_click(self)
        return "break"

    def _all_children(self, w):
        out = []
        for c in w.winfo_children():
            out.append(c)
            out.extend(self._all_children(c))
        return out

    def set_selected(self, on):
        self.selected = bool(on)
        bg = "#eff6ff" if self.selected else "#ffffff"
        self.configure(highlightbackground="#0969da" if self.selected else "#d0d7de",
                       highlightthickness=2 if self.selected else 1)
        for w in [self] + self._all_children(self):
            try:
                w.configure(bg=bg)
            except tk.TclError:
                pass