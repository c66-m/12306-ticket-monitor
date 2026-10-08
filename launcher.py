# -*- coding: utf-8 -*-
"""
12306 桌面抢票启动器 —— 一键抢票 / 定时开抢 / 加密存储 / 更新检查

与后台监控（monitor.py / 桌面版）的区别
    监控 = 常驻轮询，有票才动手；启动器 = 面向「开抢时刻」，倒计时到点自动出手，
    平时也能手动点「立即抢票」。两者共用同一套浏览器下单链路（browser_order.py）。

功能
    1. 乘车人信息：复用 passengers.py 加密存储（Windows DPAPI / Fernet），界面打码显示
    2. 车次管理：行程预设（添加 / 编辑 / 删除），车次、席别、日期随预设保存
    3. 位置信息：出发 / 到达站自由选择，一键 IP 定位显示当前城市
    4. 开抢时间：精确到秒，倒计时显示，到点自动开抢，提前 N 分钟提醒（可选邮件）
    5. 一键抢票：主按钮直接开抢（打开真实浏览器自动完成 查询→预订→乘车人→席别→提交）
    6. 数据安全：证件号 / 手机号只存加密文件，界面只显示掩码
    7. 界面：单窗紧凑布局，日志按事件分级着色
    8. 兼容性：Windows / macOS / Linux；浏览器自动探测 Edge → Chrome → Chromium
    9. 日志：logs/launcher_YYYYMMDD.log + 界面日志区
   10. 自动更新：启动时向 update_url 拉版本清单比对，有新版弹提示（默认关闭）

用法
    python launcher.py        # 打开启动器
    pythonw launcher.py       # Windows 无控制台运行（桌面快捷方式用它）
"""

import calendar
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import appcommon
import browser_order
import filelock
import logutil
import notify
import order as order_mod
import passengers as passengers_mod
import ticket

__version__ = "1.0.0"
LAUNCHER_CFG_PATH = os.path.join(HERE, "launcher_config.json")
LOG_DIR = os.path.join(HERE, "logs")

# 席别勾选列表：唯一定义在 ticket.py（含动卧），此处只引用
SEAT_OPTIONS = list(ticket.SEAT_CHOICES)
SEAT_ORDER = {v: i for i, v in enumerate(SEAT_OPTIONS)}
# 监控系统任务的席别表：与 SEAT_OPTIONS 同一份（原先三处拷贝已收敛）
MONITOR_SEAT_CHOICES = SEAT_OPTIONS
LOG_COLOR = {
    "[有票]": "#d97706", "[抢到]": "#0969da", "[错误]": "#cf222e",
    "[会话]": "#6e7781", "[运行]": "#57606a", "[提醒]": "#9a6700", "[更新]": "#8250df",
    "[自动]": "#cf222e", "[启动]": "#0961da", "[停止]": "#6e7781", "[提示]": "#8250df",
}

LOGQ = queue.Queue()


def _is_soft_fail(msg):
    """瞬时繁忙/排队类软失败判定（Task 65i）。

    这类错误是 12306 侧的瞬时拥塞，不是"下单逻辑失败"：不计入 fail_streak
    （"同一目标连续失败 N 次自动停止"守卫本意只拦硬失败）。busy_n 继续驱动
    退避（3s→30s 封顶）；持续繁忙会一直重试直到用户停止——抢票本意如此。
    """
    return any(k in (msg or "") for k in ("系统繁忙", "网络异常", "排队"))


class _QHandler(logging.Handler):
    def emit(self, record):
        try:
            LOGQ.put(self.format(record))
        except Exception:
            pass


def _ensure_stdio():
    """pythonw 没有控制台，sys.stdout/stderr 是 None，Playwright 启动浏览器时
    调用 flush() 会抛 'NoneType' object has no attribute 'flush'，浏览器当场退出。"""
    if sys.stdout is None or sys.stderr is None:
        if not os.path.isdir(LOG_DIR):
            os.makedirs(LOG_DIR, exist_ok=True)
        f = open(os.path.join(LOG_DIR, "launcher_stdio.log"), "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = f
        if sys.stderr is None:
            sys.stderr = f


_ensure_stdio()

LOG = logging.getLogger("launcher")
LOG.setLevel(logging.INFO)
if not LOG.handlers:
    if not os.path.isdir(LOG_DIR):
        os.makedirs(LOG_DIR, exist_ok=True)
    _fh = logutil.DayFileHandler(LOG_DIR, "launcher")  # 按天滚动，长跑跨天不丢日志
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    LOG.addHandler(_fh)
    _qh = _QHandler()
    _qh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    LOG.addHandler(_qh)


def log(msg):
    LOG.info(msg)


# ----------------------------- 配置读写 -----------------------------

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

# 票种：界面按乘车人分别选择（成人票 / 学生票），下单按人写入 ticket_type
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


# ===== 复选框指示器：clam 主题选中时画的是「叉」(✗)，容易被当成取消，改成「对勾」(✓) =====
_CHECK_ICONS = {}   # 保持 PhotoImage 引用，防止被垃圾回收后图标消失


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


def install_tick_indicator(style=None):
    """让所有 ttk.Checkbutton 选中时显示对勾（clam 主题默认画叉）。

    gui.apply_style() 与 launcher 独立运行时各调用一次；换主题后需重新调用。
    """
    style = style or ttk.Style()
    try:
        if "Tick.indicator" not in style.element_names():
            off = _make_check_icon(False)
            on = _make_check_icon(True)
            _CHECK_ICONS["off"], _CHECK_ICONS["on"] = off, on
            style.element_create("Tick.indicator", "image", off, ("selected", on),
                                 border=1, sticky="")
        style.layout("TCheckbutton", _patch_tick_layout(style.layout("TCheckbutton")))
    except Exception as e:
        log("[警告] 复选框对勾样式安装失败：%s" % e)


def load_launcher_config():
    lc = dict(_DEFAULT_LC)
    if os.path.exists(LAUNCHER_CFG_PATH):
        try:
            with open(LAUNCHER_CFG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                for k, v in _DEFAULT_LC.items():
                    if k in saved:
                        lc[k] = saved[k]
                # 保留未知键（如 merge_trains_from_monitor 写入的 synced_trains）：
                # 否则用户删掉的同步车次会在重启后被再次自动加回
                for k, v in saved.items():
                    if k not in _DEFAULT_LC:
                        lc[k] = v
        except Exception as e:
            log("[错误] launcher_config.json 读取失败：%s，使用默认配置" % e)
    return lc


def save_launcher_config(lc):
    # 原子写：写一半被杀会损坏全部配置（同文件 append_monitor_task 已是此范式）
    appcommon.atomic_write_json(LAUNCHER_CFG_PATH, lc)


# ----------------------------- 抢票任务库（多任务管理） -----------------------------

GRAB_TASKS_PATH = os.path.join(HERE, "grab_tasks.json")


def load_grab_tasks():
    """读取全部抢票任务（每个任务 = 一份独立配置 + id/name/status）。"""
    if os.path.exists(GRAB_TASKS_PATH):
        try:
            with open(GRAB_TASKS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("tasks"), list):
                return data["tasks"]
        except Exception as e:
            log("[错误] grab_tasks.json 读取失败：%s" % e)
    return []


def save_grab_tasks(tasks):
    # 原子写：grab_tasks.json 是全部抢票任务的唯一存储，中途崩溃不能截断
    appcommon.atomic_write_json(GRAB_TASKS_PATH, {"tasks": tasks})


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


def _normalize_trains(raw, warn):
    """trains 字段归一化（Task 84a；与 engine.normalize_trains 同口径）：
    裸字符串（如漏写方括号的 "G101"）按单个车次处理并记警告，绝不逐字符拆
    （旧代码会拆成 ['G','1','0','1'] 静默漏单）；形状非法记警告后忽略；
    非字符串条目跳过。"""
    raw = raw or []
    if isinstance(raw, str):
        warn("[提醒] trains 为字符串，已按单个车次处理：%s" % raw)
        raw = [raw]
    elif not isinstance(raw, (list, tuple)):
        warn("[提醒] trains 形状非法，已忽略：%r" % (raw,))
        raw = []
    return [t.strip().upper() for t in raw
            if isinstance(t, str) and t.strip()]


def _display_trains(raw):
    """展示用车次归一化（Task 88b）：与 _normalize_trains 同口径（字符串按
    单个车次、绝不逐字符拆），但不打日志——展示路径可能高频刷新，畸形值的
    警告由数据消费路径（engine/launcher 运行链）负责；这里只解决展示。"""
    return _normalize_trains(raw, lambda *a: None)


def _save_passengers_or_warn(passengers, parent):
    """保存乘车人；磁盘盒子不可解密被拒写（return False）时弹 error 并返回 False。

    Task 84(d)：与 gui._save_passengers_or_warn 同口径。调用方在 False 时不得
    显示成功、不得刷新列表。抛出的异常仍由调用方 try/except 处理。"""
    if passengers_mod.save_passengers(passengers):
        return True
    messagebox.showerror("保存失败",
                         "乘车人数据保存失败：磁盘上的已有数据在本机不可解密，"
                         "已拒绝覆盖以保护原数据。\n"
                         "请在原机器解密后迁移，或使用 --force 放弃旧数据。",
                         parent=parent)
    return False


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
                # strptime 对 MM-DD 默认年份是 1900：先归位到今年；
                # 只填月日不跨年——12 月填「01-05」应理解为明年，而不是"已过"
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


# ----------------------------- 抢票引擎 -----------------------------

_SESSION_LOCK = threading.Lock()  # 多任务并发时，浏览器会话校验/登录串行化


class Grabber(threading.Thread):
    """后台抢票线程：校验会话 → 循环查询 → 命中即下单。

    result: 结束时写入 (ok, msg)。GUI 通过 LOGQ 收到过程日志。"""

    def __init__(self, lc, logq=None):
        super().__init__(daemon=True, name="grabber")
        self.lc = lc
        self.logq = logq or LOGQ
        self.stop_event = threading.Event()
        self.result = None

    def _log(self, msg):
        LOG.info(msg)  # 也落 logs/launcher_*.log：事后排查抢票过程全靠它
        self.logq.put(msg)

    def stop(self):
        self.stop_event.set()

    def run(self):
        log = self._log
        try:
            self._run()
        except Exception as e:
            self.result = (False, "抢票线程异常：%s: %s" % (type(e).__name__, e))
            log("[错误] 抢票线程异常：%s: %s" % (type(e).__name__, e))

    # ---- 内部 ----

    def _run(self):
        log = self._log
        lc = self.lc
        # 历史文件路径与监控系统同源(config.history_file),别写错文件
        try:
            _cfg = json.load(open(os.path.join(HERE, "config.json"),
                                  encoding="utf-8"))
            hist_path = os.path.join(HERE, _cfg.get("history_file",
                                                    "order_history.json"))
        except Exception:
            hist_path = os.path.join(HERE, "order_history.json")
        from_, to_ = (lc.get("from") or "").strip(), (lc.get("to") or "").strip()
        date = (lc.get("date") or "").strip()
        trains = _normalize_trains(lc.get("trains"), log)
        seats = [s for s in (lc.get("seat_types") or []) if s]
        pri_raw = lc.get("seat_priority") or ""
        names = [n for n in (lc.get("passenger_names") or []) if n]

        if not (from_ and to_ and date):
            self.result = (False, "请先填好出发站 / 到达站 / 日期")
            return
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            self.result = (False, "日期格式应为 YYYY-MM-DD")
            return
        if not seats:
            self.result = (False, "请至少勾选一种席别")
            return
        orderable = [s for s in seats if s in ticket.SEAT_NAME_TO_CODE]
        if len(orderable) != len(seats):
            log("[提醒] 席别 %s 暂不支持自动下单，已跳过" % "、".join(
                s for s in seats if s not in orderable))
        seats = orderable
        if not seats:
            self.result = (False, "勾选的席别都无法自动下单，请改选其他席别")
            return
        parsed_pri = ticket.seat_rules_parse(pri_raw)
        if parsed_pri["rules"] or parsed_pri["bare"]:
            summary = ["%s=%s" % (tr, "/".join(ss))
                       for tr, ss in parsed_pri["rules"].items()]
            if parsed_pri["bare"]:
                summary.append("其余车次：%s" % "/".join(parsed_pri["bare"]))
            log("[席别] 席别规则：%s（每趟车按各自候选抢，车次顺序优先）" % "；".join(summary))
        for warn in parsed_pri["warnings"]:
            log("[提醒] 首选席别：%s" % warn)
        if not names:
            log("[提醒] 未选择乘车人，将尝试使用账号默认乘车人（可能失败）")

        try:
            name2code, code2name = ticket.load_station_map()
        except Exception as e:
            # 离线首跑：车站表下载失败。下单 worker 后续每一步都要联网，
            # 空表继续只会误报"车站无法识别"，故直接以明确失败收尾
            #（Task 26 round 2；引擎/监控侧走空表降级继续）。
            log("[网络] 车站数据加载失败：%s" % e)
            self.result = (False, "车站数据加载失败（网络异常），请联网后重试")
            return
        fc, tc = name2code.get(from_), name2code.get(to_)
        if not fc or not tc:
            # 新开车站可能不在本地缓存里：后台强制刷新一次，下次可查到
            ticket.note_station_missing(from_ if not fc else to_)
            self.result = (False, "车站无法识别：%s → %s" % (from_, to_))
            return

        # 1) 会话（多任务并发时串行校验，避免两个线程同时操作同一个浏览器）
        log("[会话] 正在校验登录状态…（复用本机登录信息，全程无浏览器窗口）")
        with _SESSION_LOCK:
            ok, who = False, ""
            for attempt in range(60):
                if self.stop_event.is_set():
                    break
                if browser_order.busy():
                    # 多任务并行：另一任务正占着浏览器（预热/下单/登录）。
                    # 抢锁超时 ≠ 未登录——等对方放锁再试，别把自己的任务误杀。
                    log("[会话] 浏览器被其他任务占用，15 秒后再试（已等 %d 次）" % (attempt + 1))
                    if self.stop_event.wait(15):
                        break
                    continue
                try:
                    ok, who = browser_order.check_session()
                    break
                except Exception as e:
                    if browser_order.busy():
                        # 竞态：busy() 探完到拿锁之间被其他任务抢先——回到等待
                        continue
                    log("[错误] 会话校验失败：%s" % e)
                    ok, who = False, str(e)
                    break
            if not self.stop_event.is_set() and not ok:
                log("[会话] 当前未登录（%s），尝试拉起登录窗口，请在浏览器里完成登录…" % who)
                try:
                    browser_order.login(timeout_sec=240, stop_event=self.stop_event,
                                        lock_timeout=120)
                    if not self.stop_event.is_set():
                        ok, who = browser_order.check_session()
                except Exception as e:
                    log("[错误] 登录过程异常：%s" % e)
        if not ok:
            msg = "已手动停止" if self.stop_event.is_set() else "登录失败或超时：%s" % who
            self.result = (False, msg)
            log("[错误] %s" % self.result[1])
            return
        log("[会话] 已登录：%s" % who)

        # 2) 预热：开抢前 lead 分钟登录并停在车票列表页（到点只做 预订→提交）
        st = parse_dt(lc.get("start_time"))
        warm = None
        lead = 0
        try:
            lead = max(0, int(lc.get("warm_minutes") or 10))
        except Exception:
            pass
        if lead > 0 and st and datetime.now() < st - timedelta(minutes=lead):
            _wa = st - timedelta(minutes=lead)
            log("[预热] 距开抢较久，%s 开始登录预热" % _wa.strftime("%H:%M:%S"))
            if self.stop_event.wait((_wa - datetime.now()).total_seconds()):
                return
        if lead > 0 and st and datetime.now() < st:
            warm_info = {
                "train_code": trains[0] if trains else "?",
                "from_code": fc, "from_name": from_,
                "to_code": tc, "to_name": to_,
            }
            try:
                log("[预热] 开始预热（登录 + 打开车票列表页）…")
                warm = browser_order.warm_up(warm_info, date, headless=False,
                                             wait_code=(trains[0] if trains else None),
                                             stop_event=self.stop_event)
                log("[预热] 预热就绪，浏览器保持在线，等待 %s 到点立即下单" % st.strftime("%H:%M:%S"))
            except Exception as e:
                log("[预热] 预热失败（%s），到点改用普通下单流程" % e)
                warm = None
            if self.stop_event.wait((st - datetime.now()).total_seconds()):
                if warm is not None:
                    try:
                        warm.close()
                    except Exception:
                        pass
                return

        # 3) 抢票主循环
        poll = max(1.0, float(lc.get("poll_seconds") or 3))
        n = 0
        last_target = None   # (车次, 席别)：同一目标连续失败计数用
        fail_streak = 0
        busy_n = 0           # 系统繁忙连续次数，用于冷却退避
        bad = set()          # {(车次, 席别)}：网页端下单页不下发的组合，永久跳过
        ambiguous_retries = 0  # 提交后结果未知且官方查无订单的安全重试计数
        MAX_ORDER_FAILS = 5  # 同一目标连续失败这么多次就停，不再无限重复
        exc_key, exc_streak = None, 0  # 同一异常连续计数（Task 55a：防吞异常无限活锁）
        MAX_SAME_EXC = 3     # 同一异常连续这么多次 → 计入失败走正常停止逻辑

        def note_exc(e):
            """同一异常（类名+消息）连续计数；达到 MAX_SAME_EXC 返回 True（应停止）。"""
            nonlocal exc_key, exc_streak
            key = (type(e).__name__, str(e))
            if key == exc_key:
                exc_streak += 1
            else:
                exc_key, exc_streak = key, 1
            return exc_streak >= MAX_SAME_EXC

        try:
            while not self.stop_event.is_set():
                n += 1
                try:
                    rows = ticket.query_tickets(fc, tc, date, purpose=purpose_of(lc))
                except Exception as e:
                    log("[错误] 余票查询失败：%s（%s 秒后重试）" % (e, int(poll)))
                    if self.stop_event.wait(poll):
                        break
                    continue
    
                info, seat, seat_rank = None, None, None
                by_code = {}
                for row in rows:
                    p = ticket.parse_row(row, code2name, date)
                    by_code.setdefault(p.get("train_code"), p)
                for code in (trains or list(by_code)):   # 按点选顺序（留空=全部车次）
                    p = by_code.get(code)
                    if not p:
                        continue
                    avail = p.get("available_seats") or {}
                    # 每趟车各自的席别候选（唯一口径 ticket.seat_candidates_for）：
                    # 「车次=席别」专属规则 ∩ 勾选集，交集空=该车跳过；无规则的车
                    # 按全局偏好排序。候选永远与该车实际有票求交。
                    cand = ticket.seat_candidates_for(code, seats, pri_raw, avail)
                    for rank, s in enumerate(cand, 1):
                        if (code, s) not in bad:
                            info, seat, seat_rank = p, s, (rank, len(cand))
                            break
                    if info:
                        break

                if info:
                    log("[有票] %s %s %s %s→%s 余%s（%s发车）！开始下单…%s" % (
                        date, info["train_code"], seat,
                        info["from_name"], info["to_name"],
                        info["available_seats"].get(seat), info["start_time"],
                        "（候选 %d/%d）" % seat_rank if seat_rank else ""))
                    try:
                        attempt_ts = time.time()
                        sc = ticket.SEAT_NAME_TO_CODE[seat]
                        # 网页端下单页不下发「无座」：按同价席别改判（动车组→二等座，
                        # 普速→硬座，见 ticket.ORDER_SEAT_ALIAS / EMU_SEAT_ALIAS）
                        sc, _alias = ticket.order_seat_code(seat, sc, info.get("train_code"))
                        if _alias:
                            log("[席别] %s 网页端下单页不下发，按同价改判为 %s 下单" % (seat, _alias))
                        ok, msg, extra = browser_order.order_via_browser(
                            info, seat, sc, names, date, headless=False, verify_timeout=90,
                            purpose=purpose_of(lc), purpose_map=purpose_map_of(lc),
                            warm=warm, alias_name=_alias)
                    except RuntimeError as e:
                        if note_exc(e):
                            self.result = (False, "下单异常连续 %d 次（%s: %s），已自动停止"
                                           % (MAX_SAME_EXC, type(e).__name__, e))
                            log("[错误] %s" % self.result[1])
                            return
                        log("[错误] %s（5 秒后重试）" % e)
                        if self.stop_event.wait(5):
                            break
                        continue
                    except Exception as e:
                        if note_exc(e):
                            self.result = (False, "下单异常连续 %d 次（%s: %s），已自动停止"
                                           % (MAX_SAME_EXC, type(e).__name__, e))
                            log("[错误] %s" % self.result[1])
                            return
                        log("[错误] 下单异常：%s: %s（5 秒后重试）" % (type(e).__name__, e))
                        if self.stop_event.wait(5):
                            break
                        continue
                    exc_key, exc_streak = None, 0  # 本次下单尝试正常返回：异常 streak 断开
                    if ok:
                        self.result = (True, msg)
                        # Task 84c：日志路径 PII 脱敏（订单号打码）；self.result /
                        # 历史记录 / 通知里的原文不动，用户仍能在界面看到完整订单号
                        log("[抢到] %s" % _mask_pii_text(msg))
                        try:
                            order_no = (extra or {}).get("order_no") or ""
                            appcommon.append_history(
                                hist_path,
                                {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                                 "task": lc.get("name") or "启动器抢票",
                                 "result": "success", "train": info["train_code"],
                                 "date": date, "from": info["from_name"],
                                 "to": info["to_name"], "seat": seat,
                                 "passengers": names, "order_no": order_no,
                                 "message": msg, "notify": "已通知(启动器)"})
                        except Exception as e:
                            log("[提醒] 购票历史写入失败：%s" % e)
                        self._notify_success(info, seat, msg)
                        return
                    extra = extra or {}
                    if extra.get("reason") == "ambiguous":
                        # 提交确认后结果未知：先回读官方订单接口做时间戳归因。
                        # 订单已生成且下单时间对得上=本次成功；确认无订单=安全重试；
                        # 其余（查不到/更早旧单/其它行程挡路）=停下交人工，防重复下单。
                        cls, ono, raw, recent = order_mod.classify_with_time(
                            date, info["train_code"], names, not_before_ts=attempt_ts)
                        if cls == "unpaid" and recent:
                            order_no = ono or (recent.get("order_no") or "")
                            self.result = (True,
                                "订单已提交成功（未支付）：订单号 %s，下单时间 %s，请尽快去 12306 支付"
                                % (order_no, recent.get("order_time") or "未知"))
                            # Task 84c：日志里的订单号打码（与 Task 61 同口径）；
                            # self.result[1] 原文保留，用户界面仍显示完整订单号
                            log("[抢到] %s" % _mask_pii_text(self.result[1]))
                            try:
                                appcommon.append_history(
                                    hist_path,
                                    {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                                     "task": lc.get("name") or "启动器抢票",
                                     "result": "success", "train": info["train_code"],
                                     "date": date, "from": info["from_name"],
                                     "to": info["to_name"], "seat": seat,
                                     "passengers": names, "order_no": order_no,
                                     "message": self.result[1], "notify": "已通知(启动器)"})
                            except Exception as e:
                                log("[提醒] 购票历史写入失败：%s" % e)
                            self._notify_success(info, seat, self.result[1])
                            return
                        if cls in ("none", "cancelled"):
                            ambiguous_retries += 1
                            if ambiguous_retries >= 3:
                                self.result = (False,
                                    "连续多次提交后结果未知且官方查无订单，请人工核对后重新开抢")
                                log("[错误] %s" % self.result[1])
                                return
                            log("[提醒] 提交结果未知但官方确认无此订单（第 %d 次），继续重试" % ambiguous_retries)
                            if self.stop_event.wait(2):
                                break
                            continue
                        self.result = (False, "订单提交后结果未知——请先到 12306 查「未支付订单」："
                                              "有单就支付或取消，确认无单后再重新开抢")
                        log("[错误] %s（下单返回：%s；官方核验：%s）" % (self.result[1], msg, raw))
                        return
                    if extra.get("reason") == "dup":
                        if extra.get("dup_kind") == "行程冲突":
                            # 行程冲突 ≠ 本行程已有订单：别报"票已到手"误导去支付
                            self.result = (False, "12306 提示行程冲突——可能是其它行程的未支付订单挡路，"
                                                  "请到「未完成订单」查证处理后重新开抢")
                            log("[提醒] 下单返回：%s" % msg)
                            return
                        # 本行程已有订单：回读官方接口 + 下单时间归因，确认是不是本次提交的
                        cls, ono, raw, recent = order_mod.classify_with_time(
                            date, info["train_code"], names, not_before_ts=attempt_ts)
                        result_kind = "dup"
                        if cls == "paid":
                            self.result = (True, "该行程订单已支付，请查收")
                        elif cls == "unpaid" and recent:
                            result_kind = "success"
                            self.result = (True,
                                "本次已提交成功（未支付）：订单号 %s，下单时间 %s，请尽快去 12306 支付"
                                % (ono or (recent.get("order_no") or ""),
                                   recent.get("order_time") or "未知"))
                        elif cls == "unpaid":
                            self.result = (True,
                                "账号存在该行程更早的未支付订单（订单号 %s），非本次提交，请核对后尽快支付"
                                % (ono or "未知"))
                        else:
                            self.result = (True, "检测到该行程已有订单，请尽快去 12306 完成支付（官方核验：%s）" % raw)
                        log("[提示] 下单返回：%s" % msg)
                        try:
                            appcommon.append_history(
                                hist_path,
                                {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                                 "task": lc.get("name") or "启动器抢票",
                                 "result": result_kind, "train": info["train_code"],
                                 "date": date, "from": info["from_name"],
                                 "to": info["to_name"], "seat": seat,
                                 "passengers": names, "order_no": ono or "",
                                 "message": self.result[1],
                                 "notify": "已通知(启动器)" if result_kind == "success" else ""})
                        except Exception:
                            pass
                        self._notify_success(info, seat, self.result[1])
                        return
                    if extra.get("need_captcha"):
                        self.result = (False, "触发滑块验证：%s（脚本不自动过验证码，已停止）" % msg)
                        log("[错误] %s" % self.result[1])
                        return
                    if extra.get("alias_seat") and any(
                            k in msg for k in ("余票", "不足", "无票", "不下发", "改判")):
                        # 勾选无座、实际按同价硬座下单：硬座此刻没票/不可售属车次状态问题，
                        # 继续监控，不计入「同目标连续失败」
                        log("[提醒] %s（按 %s 监控中，继续等待）" % (msg, seat))
                        if self.stop_event.wait(2):
                            break
                        continue
                    if extra.get("reason") == "seat_unavailable" or "网页端不提供席别" in msg:
                        # 网页端下单页的可售席别由服务端下发（普速车常常没有「无座」）：
                        # 这个组合再点多少次都不会变，加入跳过名单，只在剩余席别里继续抢
                        bad.add((info["train_code"], seat))
                        log("[提醒] %s；已跳过 %s %s" % (msg, info["train_code"], seat))
                        remain = [(c, s) for c in (trains or list(by_code))
                                  for s in seats if (c, s) not in bad]
                        if not remain:
                            self.result = (False, "勾选的席别在 12306 网页端下单页都不可选，"
                                                  "已停止：%s" % msg)
                            log("[错误] %s" % self.result[1])
                            return
                        if self.stop_event.wait(2):
                            break
                        continue
                    if "页面上没有" in msg:
                        # 抢输竞速/席别已售罄：不是下单失败，继续监控即可
                        log("[提醒] %s（继续监控）" % msg)
                        if self.stop_event.wait(3):
                            break
                        continue
                    if _is_soft_fail(msg):
                        # Task 65i：瞬时繁忙/排队是软失败，不计入 fail_streak——
                        # 连续 5 次 busy（~45s）就触发"连续失败已自动停止"是 bug。
                        # busy_n 仍驱动退避（3s→30s 封顶）；持续繁忙一直重试，
                        # 直到用户停止（stop_event）。
                        busy_n += 1
                        wait_s = min(30, 3 * busy_n)
                        log("[错误] 下单遇系统繁忙：%s（%d 秒后重试，累计 %d 次）" % (msg, wait_s, busy_n))
                        if self.stop_event.wait(wait_s):
                            break
                        continue
                    target = (info["train_code"], seat)
                    if target == last_target:
                        fail_streak += 1
                    else:
                        last_target, fail_streak = target, 1
                    busy_n = 0  # 非繁忙类失败：退避计数复位，别一直卡在 30 秒档
                    log("[错误] 下单未成功：%s（同目标第 %d 次失败）" % (msg, fail_streak))
                    if fail_streak >= MAX_ORDER_FAILS:
                        self.result = (False, "同一车次席别连续 %d 次下单失败，已自动停止：%s" % (fail_streak, msg))
                        log("[错误] %s" % self.result[1])
                        return
                    if self.stop_event.wait(3):
                        break
                    continue
    
                busy_n = 0  # 本轮无下单繁忙：复位退避，恢复正常轮询节奏
                exc_key, exc_streak = None, 0  # 完整空轮询一轮：异常 streak 断开
                if n % 4 == 1 or n == 1:
                    log("[运行] 第 %d 轮无票（%s %s %s），%s 秒后再查" % (
                        n, date, "/".join(trains) or "全部车次", from_ + "→" + to_, int(poll)))
                if self.stop_event.wait(poll):
                    break
        finally:
            if warm is not None:
                try:
                    warm.close()
                except Exception:
                    pass

    def _notify_success(self, info, seat, msg):
        log = self._log
        try:
            with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8") as f:
                cfg = json.load(f)
            nc = (cfg.get("notify") or {}).get("email") or {}
            if not nc.get("enabled"):
                log("[提醒] 邮件通知未启用，跳过")
                return
            ok, m = notify.send_email(nc, "[抢票成功] %s %s %s" % (
                info["train_code"], info["query_date"], seat),
                "抢到票了！\n\n车次：%s\n日期：%s\n区间：%s → %s\n席别：%s\n\n%s\n\n请在 10 分钟内完成支付。" % (
                    info["train_code"], info["query_date"], info["from_name"],
                    info["to_name"], seat, msg))
            log("[提醒] 通知：%s" % m)
        except Exception as e:
            log("[错误] 发送通知失败：%s" % e)


# ----------------------------- 与监控系统联动 -----------------------------

def merge_trains_from_monitor(lc, saver=None):
    """把监控系统（config.json tasks）里的车次合并进启动器车次列表。

    规则：监控里新出现的车次自动加入启动器；被同步过的车次记录在
    synced_trains，用户在启动器里删掉后不会再次自动加回。返回是否合并。"""
    try:
        with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        log("[提醒] 读取监控系统 config.json 失败，本轮不合并车次：%s" % e)
        return False
    mon = []
    for t in (cfg.get("tasks") or []):
        # Task 84a：字符串 trains 按单车次归一化，绝不逐字符拆
        for tr in _normalize_trains(t.get("trains"), log):
            if tr and tr not in mon:
                mon.append(tr)
    cur = _normalize_trains(lc.get("trains"), log)
    old_synced = set(lc.get("synced_trains") or [])
    synced = set(old_synced)
    added = []
    for tr in mon:
        if tr not in synced and tr not in cur:
            cur.append(tr)
            added.append(tr)
        synced.add(tr)
    if added or synced != set(lc.get("synced_trains") or []):
        lc["trains"] = cur
        lc["synced_trains"] = sorted(synced)
        (saver or save_launcher_config)(lc)
        if added:
            log("[运行] 监控系统新增车次已同步：%s" % "、".join(added))
        return True
    return False


# ----------------------------- 更新检查 -----------------------------

def check_update(lc):
    """拉取 update_url 指向的版本清单。清单格式：
    {"version": "1.1.0", "notes": "...", "download": "https://..."}
    版本号三段数字逐段比较。返回 (has_new, new_version, notes, download)。"""
    url = (lc.get("update_url") or "").strip()
    if not url:
        return False, None, None, None
    try:
        r = requests.get(url, timeout=8)
        d = r.json()
        remote = str(d.get("version") or "")
        if not re.match(r"^\d+(\.\d+){1,2}$", remote):
            return False, None, None, None
        cur = tuple(int(x) for x in __version__.split("."))
        rmt = tuple(int(x) for x in remote.split("."))
        if rmt > cur:
            return True, remote, d.get("notes", ""), d.get("download", "")
    except Exception as e:
        log("[更新] 检查失败：%s" % e)
    return False, None, None, None

# ----------------------------- GUI -----------------------------

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


# 同进程对 config.json/state.json 的读-改-写串行锁：mtime 冲突检测在
# Windows 上有 ~15.6ms 系统时钟量化盲区，同进程并发必须靠锁兜住；
# 跨进程仍靠 mtime 比对（盲区可接受，见 RULES）。
_CFG_WRITE_LOCK = threading.Lock()


# Windows 占用退避重试的 os.replace（实现单点在 appcommon）
_atomic_replace = appcommon.replace_with_retry


def append_monitor_task(task, start_now=True):
    """把任务写入监控系统的 config.json 与 state.json，并返回任务名。
    监控引擎主循环每轮 _sync_config() 检测 mtime 变化后自动重建调度表，
    处于「监控中」状态的新任务会被自动捡起来，无需重启监控软件。"""
    with _CFG_WRITE_LOCK:  # 同进程多窗口并发串行化
        cfg_path = os.path.join(HERE, "config.json")
        # 临时名带线程标识：同进程多窗口并发追加时共用一个名字会互相踩
        # （Windows 下 os.replace 撞上别人打开的句柄直接 PermissionError）
        tmp = cfg_path + ".launcher%s" % threading.get_ident()
        # 读-改-写必须串行化：监控系统 GUI 是独立进程，也在改同一个 config.json。
        # 只比 mtime 是 TOCTOU——实测 3 个线程并发追加任务，最后只剩 1 条。
        # 锁文件用 filelock（进程被强杀时由系统释放）；mtime 比对留作第二道，
        # 能发现「锁外」的改动（手工编辑、其它工具）。
        try:
            with filelock.file_lock(cfg_path + ".lock"):
                for _ in range(3):
                    try:
                        before = os.path.getmtime(cfg_path)
                    except OSError:
                        before = None
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                    cfg.setdefault("tasks", []).append(task)
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(cfg, f, ensure_ascii=False, indent=2)
                    # Task 66: config.json 含 SMTP 授权码等密钥，落盘 0600
                    # （os.replace 继承 tmp 权限；Windows 下 chmod 仅影响只读位）
                    os.chmod(tmp, 0o600)
                    try:
                        after = os.path.getmtime(cfg_path)
                    except OSError:
                        after = None
                    if after == before:
                        _atomic_replace(tmp, cfg_path)
                        break
                    # 撞车：本轮作废，带着对方的新内容重来
                else:
                    _atomic_replace(tmp, cfg_path)  # 三次都撞车：以本方落盘收场（低概率，双方都是追加型写）
        except TimeoutError as e:
            # 锁争用超时：给用户明确提示，异常继续上抛给调用方的
            # messagebox.showerror（不崩）。任务未落盘，用户稍后重试。
            log("[错误] 文件被占用，稍后重试：%s" % cfg_path)
            raise TimeoutError("文件被占用，稍后重试") from e
        # state.json 条目（与 gui.mark_task_created 的无 app 分支保持一致）
        state_path = os.path.join(HERE, cfg.get("state_file", "state.json"))
        try:
            with filelock.file_lock(state_path + ".lock"):
                if os.path.exists(state_path):
                    try:
                        with open(state_path, "r", encoding="utf-8") as f:
                            state = json.load(f)
                    except Exception as e:
                        # state.json 损坏：挪档留证（带时间戳，反复损坏不互相覆盖），再按
                        # 只含本任务的新状态重建——保住「立即启动」语义，不让它静默降级成
                        # 未启动。其它任务的状态/防重记录在坏档里，引擎会按未启动重建。
                        bad = "%s.bad-%s" % (state_path, time.strftime("%Y%m%d-%H%M%S"))
                        try:
                            os.replace(state_path, bad)
                        except OSError:
                            # 挪不动（如杀毒软件占用）：保住坏档要紧，跳过状态写入，
                            # 任务将以「未启动」落库——这点必须让用户知道
                            log("[错误] 读取 state.json 失败且挪档失败（文件被占用？）：%s；"
                                "本次只写任务不写状态，任务「%s」将按未启动落库，"
                                "请人工处理坏档后再启动它" % (e, task["name"]))
                            return task["name"]
                        log("[错误] state.json 损坏（%s），已挪档为 %s 并按空状态重建。"
                            "其它任务的运行状态与防重记录都在坏档里——请尽快到 12306"
                            "「未支付订单」核对在途行程，避免重复下单" % (e, bad))
                        state = {}
                else:
                    state = {}  # 首次使用：还没有状态文件，从空状态开始是正常的
                entry = state.setdefault("tasks", {}).setdefault(task["name"], {})
                entry["status"] = "monitoring" if start_now else "paused"
                entry.setdefault("fail_streak", 0)
                entry.setdefault("last_poll", 0)
                entry["message"] = ("启动器创建，立即启动" if start_now
                                    else "启动器创建，未启动")
                appcommon.write_state(state_path, state, tmp_kind="launcher")
        except TimeoutError as e:
            # 锁争用超时：config 段已写、state 段未写。给用户明确提示，
            # 异常继续上抛给调用方的 messagebox.showerror（不崩）。
            # 引擎 _sync_config 下次会为该任务补建缺省状态条目。
            log("[错误] 文件被占用，稍后重试：%s" % state_path)
            raise TimeoutError("文件被占用，稍后重试") from e
        return task["name"]


import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

FONT = ("Microsoft YaHei UI", 10) if sys.platform == "win32" else ("Helvetica", 11)

ID_TYPES = {v: k for k, v in passengers_mod.ID_TYPE_NAMES.items()}


# ----------------------------- 车站搜索 -----------------------------

_STATION_INDEX = None


def get_station_index():
    """惰性加载车站全量索引（首次从 station_name.js 下载后缓存到 station_index.json）。"""
    global _STATION_INDEX
    if _STATION_INDEX is None:
        try:
            _STATION_INDEX = ticket.load_station_index()
            log("[车站] 车站索引就绪：%d 个车站" % len(_STATION_INDEX))
        except Exception as e:
            log("[提醒] 车站拼音索引加载失败（%s），本次只按站名匹配" % e)
            _STATION_INDEX = []
    return _STATION_INDEX


_FW_TRANS = {0x3000: 0x20}
_FW_TRANS.update({0xFF01 + i: 0x21 + i for i in range(0x5E)})   # ａｂｃ１２３→abc123


def _norm_query(text):
    """中文输入容错：全角→半角、剔除全部空白、转小写（长 葛→长葛,ｃｑ→cq）。"""
    t = (text or "").translate(_FW_TRANS)
    return "".join(t.split()).lower()


def search_stations(text, limit=12):
    """本地模糊搜索车站，按匹配度降序：精确站名 > 站名前缀 > 简拼/全拼/
    电报码前缀 > 站名包含。同档按站名长度短者优先（长葛排在长葛北前）。

    旧实现汉字查询只走"站名包含"档且按索引序截断，长葛这类普速小站常被
    挤出结果。返回 [{"name","code","py","spy"}, ...]，纯本地计算。"""
    q = _norm_query(text)
    if not q:
        return []
    ql = q
    capped = limit is not None
    name2code_rev = {}
    scored = []
    for idx, st in enumerate(get_station_index()):
        name, spy, py = st["name"], st["spy"], st["py"]
        code = st["code"].lower()
        if name not in name2code_rev:
            name2code_rev[name] = code
        name2code_rev[name] = code
        if q == name:
            score = 0                       # 精确站名：唯一首选项
        elif name.startswith(q):
            score = 1
        elif spy == ql or py == ql or code == ql:
            score = 2
        elif spy.startswith(ql):
            score = 3
        elif py.startswith(ql):
            score = 4
        elif code.startswith(ql):
            score = 5
        elif q in name:
            score = 6
        elif ql in py:
            score = 7
        else:
            continue
        # 同分内普速站优先于高铁/动车站，再按站名长度（用户口径：普通车站靠前）
        # name2code_rev 存的是小写 code，load_station_kinds() 的键是大写：查之前转大写
        kind = load_station_kinds().get(name2code_rev.get(name, "").upper(), "")
        kind_rank = 0 if "普速" in kind else (1 if kind else 2)
        scored.append((score, kind_rank, len(name), idx, st))
    scored.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
    out = [x[4] for x in scored]
    return out if limit is None else out[:limit]


# --------------------------- 车站类型（高铁/普速） ---------------------------
#
# 12306 没有公开的「车站类型」字段，但余票接口对**同城车站做聚合**：查许昌东
# 或查许昌，返回的都是许昌市全部车站的车次，而每行的 p6/p7 是这趟车真实的
# 上/下车站码。于是「本站 ⇄ 大枢纽」查一轮、按 p6/p7 统计车次字母（G=高铁、
# D/C=动车、其余=普速），就能反推每个站到底是高铁站还是普速站。
# 实测：许昌东=高铁 许昌=普速 长葛=普速 长葛北=高铁 驻马店西=高铁。
# 结果缓存进 station_kind.json，只探一次。

STATION_KIND_PATH = os.path.join(HERE, "station_kind.json")
_KIND_HUBS = ["BJP", "SHH", "GZQ", "ZZF"]      # 北京 / 上海 / 广州 / 郑州
_KIND_ORDER = {"高铁": 0, "动车": 1, "普速": 2}
_KIND_COLOR = {"高铁": "#0969da", "动车": "#1a7f37", "普速": "#6e7781"}
_station_kinds = {}
_kind_lock = threading.Lock()
_kind_pending = set()


def load_station_kinds():
    """惰性读车站类型缓存：{电报码: "高铁"/"动车"/"普速"/"高铁+普速"}。"""
    global _station_kinds
    with _kind_lock:
        if not _station_kinds:
            try:
                with open(STATION_KIND_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    _station_kinds = {k.upper(): v for k, v in data.items() if v}
            except Exception:
                _station_kinds = {}
        return _station_kinds


def save_station_kinds():
    with _kind_lock:
        data = dict(_station_kinds)
    if not data:
        return
    try:
        tmp = STATION_KIND_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, STATION_KIND_PATH)
    except Exception as e:
        log("[提醒] 车站类型缓存写入失败：%s" % e)


def station_kind(code):
    """车站类型文案（没探测过返回空串）。"""
    return load_station_kinds().get((code or "").upper(), "")


def _merge_kind(code, kind):
    """把一次观测合并进缓存：既有高铁又有普速时拼成 "高铁+普速"。

    kind 可能是多值串（`probe_city_kinds` 一次返回 "高铁+动车+普速"），必须拆开逐个
    合并：整串当成一个 part 会写出 "高铁+普速+高铁+动车+普速" 这种重复值
    （station_kind.json 里已经出现过，缓存文件被这么写脏过）。"""
    code = (code or "").upper()
    if len(code) != 3 or not kind:
        return False
    new_parts = {p for p in str(kind).split("+") if p}
    if not new_parts:
        return False
    load_station_kinds()  # 确保已加载（内部自带锁）
    with _kind_lock:  # 探测线程与主线程都会调这里：变更必须锁内做，
        old = _station_kinds.get(code)  # 否则与 save_station_kinds 的拷贝撞并发修改
        parts = {p for p in (old or "").split("+") if p}
        if new_parts <= parts:
            return False
        parts |= new_parts
        _station_kinds[code] = "+".join(sorted(parts, key=lambda k: _KIND_ORDER.get(k, 9)))
        return True


def learn_station_kinds(infos):
    """被动学习：从刚查到的车次里顺手记下车站类型（不联网，只累加）。"""
    changed = False
    for info in infos or []:
        head = (info.get("train_code") or "").strip().upper()[:1]
        if not head:
            continue
        if head == "G":
            kind = "高铁"
        elif head in "DC":
            kind = "动车"
        elif head in "KTLZYS" or head.isdigit():
            kind = "普速"
        else:
            continue
        for st in (info.get("from_code"), info.get("to_code")):
            if _merge_kind(st, kind):
                changed = True
    if changed:
        save_station_kinds()


def probe_city_kinds(code):
    """联网探测：返回 {站码: 类型}（同城车站一次查询顺带全算出来）。

    依次查「本站 → 大枢纽」，本站自身有结论就短路，最多 4 次请求。"""
    date = (datetime.now() + timedelta(days=8)).strftime("%Y-%m-%d")
    code = (code or "").upper()
    kinds = {}
    for hub in _KIND_HUBS:
        if hub == code:
            continue
        try:
            rows = ticket.query_tickets(code, hub, date)
        except Exception as e:
            log("[提醒] 车站类型探测失败（%s→%s）：%s" % (code, hub, e))
            continue
        for row in rows:
            f = row.split("|")
            if len(f) < 8:
                continue
            head = (f[3] or "").strip().upper()[:1]
            if not head:
                continue
            kind = "高铁" if head == "G" else ("动车" if head in "DC" else "普速")
            for st in (f[6], f[7]):
                if st and len(st) == 3:
                    kinds.setdefault(st, set()).add(kind)
        if kinds.get(code):
            break
    return {s: "+".join(sorted(v, key=lambda k: _KIND_ORDER.get(k, 9))) for s, v in kinds.items()}


def request_station_kinds(codes, on_done=None):
    """后台探测若干车站类型，结果写缓存；有新数据时回调 on_done()（子线程）。

    已在缓存、正在探测的站自动跳过；同城车站共用一次查询。"""
    known = load_station_kinds()
    todo = []
    for c in codes or []:
        c = (c or "").upper()
        if len(c) == 3 and not known.get(c) and c not in _kind_pending:
            todo.append(c)
    if not todo:
        return False
    with _kind_lock:
        _kind_pending.update(todo)

    def work():
        changed = False
        try:
            for code in todo:
                try:
                    got = probe_city_kinds(code)
                except Exception:
                    got = {}
                for c, k in got.items():
                    if _merge_kind(c, k):
                        changed = True
        finally:
            with _kind_lock:
                for c in todo:
                    _kind_pending.discard(c)
        if changed:
            save_station_kinds()
            if on_done is not None:
                try:
                    on_done()
                except Exception:
                    pass

    threading.Thread(target=work, daemon=True, name="station-kind").start()
    return True


class StationEntry(ttk.Frame):
    """车站输入框：输入即实时模糊搜索，下拉列表可选（支持键盘）。

    匹配规则：拼音简拼（cq → 重庆）、全拼（chongqing）、汉字（重庆）、电报码（CQW）。
    焦点为空时下拉显示历史车站，回车/双击/单击选定。"""

    def __init__(self, master, width=14, history=None, **kw):
        super().__init__(master, **kw)
        self.var = tk.StringVar()
        self.entry = ttk.Entry(self, textvariable=self.var, width=width)
        self.entry.pack(fill="x")
        self.history = [h for h in (history or []) if h]
        self._items = []
        self._lb = None
        self.entry.bind("<KeyRelease>", self._on_key)
        self.entry.bind("<Down>", self._focus_down)
        self.entry.bind("<Return>", self._enter)
        self.entry.bind("<Escape>", lambda e: self.hide())
        self.entry.bind("<FocusIn>", self._on_focus)
        self.entry.bind("<FocusOut>", lambda e: self.after(180, self.hide))

    def get(self):
        return self.var.get().strip()

    def set(self, value):
        self.var.set(value or "")

    def set_history(self, items):
        self.history = [h for h in (items or []) if h]

    # ---- 下拉 ----

    def _ensure_lb(self):
        """下拉框挂在顶层窗口上，避免被父容器裁剪。"""
        if self._lb is None or not self._lb.winfo_exists():
            top = self.winfo_toplevel()
            self._lb = tk.Listbox(top, height=7, activestyle="none", exportselection=False,
                                  font=(FONT[0], 10), relief="solid", borderwidth=1,
                                  highlightthickness=0)
            self._lb.bind("<ButtonRelease-1>", self._pick_click)
            self._lb.bind("<Double-Button-1>", self._pick)
            self._lb.bind("<Return>", self._pick)
            self._lb.bind("<Escape>", lambda e: (self.hide(), self.entry.focus_set()))
            self._lb.bind("<FocusOut>", lambda e: self.after(180, self.hide))
        return self._lb

    def show(self, items):
        lb = self._ensure_lb()
        lb.delete(0, "end")
        self._items = list(items)
        for i, it in enumerate(self._items):
            if isinstance(it, dict):
                kind = station_kind(it.get("code")) or "车站"
                text = "%s  %s · %s" % (it["name"], it["code"], kind)
                lb.insert("end", text)
                color = _KIND_COLOR.get((kind or "").split("+")[0])
                if color:
                    lb.itemconfig(i, foreground=color)
            else:
                lb.insert("end", str(it))
        if not self._items:
            self.hide()
            return
        h = self.entry.winfo_height() or 24
        lb.place(in_=self.entry, x=0, y=h, width=max(self.entry.winfo_width(), 210))
        lb.lift()

    def hide(self):
        if self._lb is not None and self._lb.winfo_exists():
            self._lb.place_forget()

    # ---- 事件 ----

    def _on_focus(self, _e=None):
        if not self.var.get().strip() and self.history:
            self.show(self.history[:8])

    def _on_key(self, event):
        if event.keysym in ("Down", "Up", "Return", "Escape", "Tab", "Shift_L", "Shift_R"):
            return
        # 防抖 150ms：连打时只搜最后一次（本地索引搜索本身 ~1ms,防抖只为省重绘）
        if getattr(self, "_search_job", None):
            try:
                self.after_cancel(self._search_job)
            except Exception:
                pass
        self._search_job = self.after(150, self._do_search)

    def _do_search(self):
        self._search_job = None
        if not self.winfo_exists():
            return
        text = self.var.get().strip()
        if not text:
            self.show(self.history[:8])
            return
        items = search_stations(text, limit=None)   # 全量命中站,下拉可滚
        if not items:
            items = [{"name": h} for h in self.history if text in h][:8]
        self.show(items)
        codes = [it.get("code") for it in items if isinstance(it, dict)]
        if codes:
            request_station_kinds(codes, self._on_kind_ready)

    def _on_kind_ready(self):
        """探测线程回调（子线程）——切回主线程刷新下拉。"""
        try:
            self.after(0, self._refresh_drop)
        except Exception:
            pass

    def _refresh_drop(self):
        """下拉仍开着时按当前输入重建列表，让刚探测到的类型标注立刻出现。"""
        try:
            if self._lb is None or not self._lb.winfo_exists() or not self._lb.winfo_ismapped():
                return
            text = self.var.get().strip()
            items = search_stations(text) if text else self.history[:8]
            if items:
                self.show(items)
        except Exception:
            pass

    def _focus_down(self, _e=None):
        if self._lb is not None and self._lb.winfo_ismapped() and self._lb.size():
            self._lb.focus_set()
            self._lb.selection_clear(0, "end")
            self._lb.selection_set(0)
            self._lb.activate(0)
        return "break"

    def _enter(self, _e=None):
        if self._lb is not None and self._lb.winfo_ismapped() and self._lb.size():
            sel = self._lb.curselection()
            self._apply(sel[0] if sel else 0)
        return "break"

    def _pick_click(self, _e=None):
        sel = self._lb.curselection()
        if sel:
            self._apply(sel[0])

    def _pick(self, _e=None):
        sel = self._lb.curselection()
        if sel:
            self._apply(sel[0])
        return "break"

    def _apply(self, idx):
        if 0 <= idx < len(self._items):
            it = self._items[idx]
            self.var.set(it["name"] if isinstance(it, dict) else str(it))
        self.hide()
        self.entry.focus_set()
        self.entry.icursor("end")


class CalendarDialog(tk.Toplevel):
    """简易月历，选中日期写回 var（YYYY-MM-DD）。"""

    def __init__(self, master, var):
        super().__init__(master)
        self.var = var
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", (var.get() or "").strip())
        try:
            self.cur = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else datetime.now()
        except ValueError:
            self.cur = datetime.now()
        self.title("选择乘车日期")
        self.resizable(False, False)
        self.transient(master.winfo_toplevel())
        self.body = ttk.Frame(self, padding=10)
        self.body.pack(fill="both", expand=True)
        self._render()
        self.grab_set()

    def _shift(self, delta):
        y, m = self.cur.year, self.cur.month + delta
        y += (m - 1) // 12
        m = (m - 1) % 12 + 1
        self.cur = self.cur.replace(year=y, month=m, day=1)
        self._render()

    def _sel_date(self):
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", (self.var.get() or "").strip())
        if not m:
            return None
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        except ValueError:
            return None

    def _render(self):
        for w in self.body.winfo_children():
            w.destroy()
        head = ttk.Frame(self.body)
        head.pack(fill="x", pady=(0, 8))
        ttk.Button(head, text="‹", width=3, command=lambda: self._shift(-1)).pack(side="left")
        ttk.Label(head, text="%d 年 %d 月" % (self.cur.year, self.cur.month),
                  font=(FONT[0], 11, "bold"), anchor="center").pack(side="left", expand=True)
        ttk.Button(head, text="›", width=3, command=lambda: self._shift(1)).pack(side="left")
        ttk.Button(head, text="今天", width=5, command=self._today).pack(side="left", padx=(8, 0))

        grid_w = ttk.Frame(self.body)
        grid_w.pack()
        for i, wd in enumerate("一二三四五六日"):
            ttk.Label(grid_w, text=wd, width=4, anchor="center",
                      foreground="#6e7781").grid(row=0, column=i, padx=1, pady=(0, 3))
        today = datetime.now().date()
        sel = self._sel_date()
        for r, week in enumerate(calendar.monthcalendar(self.cur.year, self.cur.month), start=1):
            for c, day in enumerate(week):
                if day == 0:
                    continue
                d = datetime(self.cur.year, self.cur.month, day).date()
                bg, fg = "#f6f8fa", "#1f2328"
                if d == today:
                    fg = "#0969da"
                if d == sel:
                    bg, fg = "#0969da", "#ffffff"
                tk.Button(grid_w, text=str(day), width=4, relief="flat", bg=bg, fg=fg,
                          activebackground="#dbeafe", cursor="hand2",
                          command=lambda dd=day: self._choose(dd)).grid(
                    row=r, column=c, padx=1, pady=1)

        tail = ttk.Frame(self.body)
        tail.pack(fill="x", pady=(10, 0))
        ttk.Button(tail, text="清除", command=self._clear).pack(side="left")
        ttk.Button(tail, text="关闭", command=self.destroy).pack(side="right")

    def _choose(self, day):
        self.var.set("%04d-%02d-%02d" % (self.cur.year, self.cur.month, day))
        self.destroy()

    def _today(self):
        self.var.set(datetime.now().strftime("%Y-%m-%d"))
        self.destroy()

    def _clear(self):
        self.var.set("")
        self.destroy()


class DateTimeDialog(tk.Toplevel):
    """表格选日期 + 时/分/秒，确定后写回 var（YYYY-MM-DD HH:MM:SS）。"""

    def __init__(self, master, var):
        super().__init__(master)
        self.var = var
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?$",
                     (var.get() or "").strip())
        now = datetime.now() + timedelta(minutes=5)
        try:
            self.sel = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                int(m.group(4) or 0), int(m.group(5) or 0),
                                int(m.group(6) or 0)) if m else now
        except ValueError:
            self.sel = now
        self.cur = self.sel.replace(day=1)
        self.h_var = tk.StringVar(value="%02d" % self.sel.hour)
        self.m_var = tk.StringVar(value="%02d" % self.sel.minute)
        self.s_var = tk.StringVar(value="%02d" % self.sel.second)
        self.title("选择开抢时间")
        self.resizable(False, False)
        self.transient(master.winfo_toplevel())
        self.body = ttk.Frame(self, padding=10)
        self.body.pack(fill="both", expand=True)
        self._render()
        self.grab_set()

    def _shift(self, delta):
        y, m = self.cur.year, self.cur.month + delta
        y += (m - 1) // 12
        m = (m - 1) % 12 + 1
        self.cur = self.cur.replace(year=y, month=m, day=1)
        self._render()

    def _render(self):
        for w in self.body.winfo_children():
            w.destroy()
        head = ttk.Frame(self.body)
        head.pack(fill="x", pady=(0, 8))
        ttk.Button(head, text="‹", width=3, command=lambda: self._shift(-1)).pack(side="left")
        ttk.Label(head, text="%d 年 %d 月" % (self.cur.year, self.cur.month),
                  font=(FONT[0], 11, "bold"), anchor="center").pack(side="left", expand=True)
        ttk.Button(head, text="›", width=3, command=lambda: self._shift(1)).pack(side="left")
        ttk.Button(head, text="今天", width=5, command=self._today).pack(side="left", padx=(8, 0))

        grid_w = ttk.Frame(self.body)
        grid_w.pack()
        for i, wd in enumerate("一二三四五六日"):
            ttk.Label(grid_w, text=wd, width=4, anchor="center",
                      foreground="#6e7781").grid(row=0, column=i, padx=1, pady=(0, 3))
        today = datetime.now().date()
        for r, week in enumerate(calendar.monthcalendar(self.cur.year, self.cur.month), start=1):
            for c, day in enumerate(week):
                if day == 0:
                    continue
                d = datetime(self.cur.year, self.cur.month, day).date()
                bg, fg = "#f6f8fa", "#1f2328"
                if d == today:
                    fg = "#0969da"
                if d == self.sel.date():
                    bg, fg = "#0969da", "#ffffff"
                tk.Button(grid_w, text=str(day), width=4, relief="flat", bg=bg, fg=fg,
                          activebackground="#dbeafe", cursor="hand2",
                          command=lambda dd=day: self._choose_day(dd)).grid(
                    row=r, column=c, padx=1, pady=1)

        tw = ttk.Frame(self.body)
        tw.pack(fill="x", pady=(8, 0))
        ttk.Label(tw, text="时间", foreground="#57606a").pack(side="left", padx=(0, 6))
        ttk.Spinbox(tw, from_=0, to=23, width=3, textvariable=self.h_var,
                    format="%02.0f", wrap=True).pack(side="left")
        ttk.Label(tw, text="时").pack(side="left", padx=(2, 10))
        ttk.Spinbox(tw, from_=0, to=59, width=3, textvariable=self.m_var,
                    format="%02.0f", wrap=True).pack(side="left")
        ttk.Label(tw, text="分").pack(side="left", padx=(2, 10))
        ttk.Spinbox(tw, from_=0, to=59, width=3, textvariable=self.s_var,
                    format="%02.0f", wrap=True).pack(side="left")
        ttk.Label(tw, text="秒").pack(side="left", padx=(2, 0))

        qw = ttk.Frame(self.body)
        qw.pack(fill="x", pady=(6, 0))
        ttk.Label(qw, text="常用", foreground="#57606a").pack(side="left", padx=(0, 6))
        for hh, mm in ((8, 0), (10, 0), (12, 0), (17, 0), (19, 0), (20, 0)):
            ttk.Button(qw, text="%02d:%02d" % (hh, mm), width=6,
                       command=lambda h=hh, mm2=mm: self._quick_time(h, mm2)).pack(
                side="left", padx=(0, 4))

        tail = ttk.Frame(self.body)
        tail.pack(fill="x", pady=(10, 0))
        ttk.Label(tail, text="格式 %s" % "YYYY-MM-DD HH:MM:SS",
                  foreground="#8c959f").pack(side="left")
        ttk.Button(tail, text="确定", command=self._ok).pack(side="right")
        ttk.Button(tail, text="清除", command=self._clear).pack(side="right", padx=(0, 6))

    def _choose_day(self, day):
        try:
            self.sel = self.sel.replace(year=self.cur.year, month=self.cur.month, day=day)
        except ValueError:
            self.sel = datetime(self.cur.year, self.cur.month, day)
        self._render()

    def _today(self):
        n = datetime.now()
        self.sel = n.replace(microsecond=0)
        self.h_var.set("%02d" % n.hour)
        self.m_var.set("%02d" % n.minute)
        self.s_var.set("%02d" % n.second)
        self.cur = self.sel.replace(day=1)
        self._render()

    def _quick_time(self, hh, mm):
        self.h_var.set("%02d" % hh)
        self.m_var.set("%02d" % mm)
        self.s_var.set("00")

    def _ok(self):
        def _num(v, hi):
            try:
                return max(0, min(hi, int(str(v.get()).strip())))
            except ValueError:
                return 0
        self.var.set("%04d-%02d-%02d %02d:%02d:%02d" % (
            self.sel.year, self.sel.month, self.sel.day,
            _num(self.h_var, 23), _num(self.m_var, 59), _num(self.s_var, 59)))
        self.destroy()

    def _clear(self):
        self.var.set("")
        self.destroy()


def _history_item_text(it):
    """历史查询记录条目的显示文本；非 dict 条目记 warning 返回 None（调用方跳过）。

    query_history 可能被手工改坏（如写成字符串数组），直接 it.get() 会
    AttributeError 炸掉整个对话框（Tasks 24/25/28 修过同类脏数据崩溃）。
    """
    if not isinstance(it, dict):
        LOG.warning("查询历史记录条目非 dict，已跳过：%r" % (it,))
        return None
    return "%s → %s    %s" % (it.get("from"), it.get("to"), it.get("date"))


class QueryHistoryDialog(tk.Toplevel):
    """历史查询记录：双击一条即填回行程。"""
    def __init__(self, master, items, on_pick):
        super().__init__(master)
        # Task 74g：构造时即过滤掉非 dict 条目（逐条记 warning），保证
        # self.items 与列表框行号 1:1 对齐；否则 _pick 会按错位索引取错条目。
        rows = []
        for it in items:
            text = _history_item_text(it)
            if text is None:
                continue
            rows.append((it, text))
        self.items = [it for it, _text in rows]
        self.on_pick = on_pick
        self.title("历史查询记录")
        self.geometry("380x320")
        self.transient(master.winfo_toplevel())
        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="双击一条记录填回行程：", foreground="#6e7781").pack(anchor="w")
        wrap = ttk.Frame(body)
        wrap.pack(fill="both", expand=True, pady=(6, 0))
        sb = ttk.Scrollbar(wrap, command=lambda *a: self.lb.yview(*a))
        self.lb = tk.Listbox(wrap, font=(FONT[0], 10), yscrollcommand=sb.set, activestyle="none")
        sb.pack(side="right", fill="y")
        self.lb.pack(side="left", fill="both", expand=True)
        for _it, text in rows:
            self.lb.insert("end", text)
        self.lb.bind("<Double-Button-1>", self._pick)
        self.lb.bind("<Return>", self._pick)
        if self.items:
            self.lb.selection_set(0)
        self.lb.focus_set()

    def _pick(self, _e=None):
        sel = self.lb.curselection()
        if sel:
            self.on_pick(self.items[sel[0]])
        self.destroy()


# ------------------------- 车次卡片列表 -------------------------

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
        # 席别只摘有票的，一行放下；全无票时给一句灰字
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


class TrainCardList(ttk.Frame):
    """可滚动的车次卡片列表，支持多选；选中的车次经 on_change 回调给主界面。"""

    def __init__(self, master, on_change, height=128):
        super().__init__(master)
        self.on_change = on_change
        self.selected = {}
        self.cards = []
        self.canvas = tk.Canvas(self, height=height, highlightthickness=0, bg="#ffffff")
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg="#ffffff")
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        # 滚轮绑到所属 Toplevel（add 叠加，不占全局 bind_all 槽位），由指针位置
        # 决定滚谁：多任务窗口并存时不再互抢全局绑定、也不会把别的窗口滚跑
        self.winfo_toplevel().bind("<MouseWheel>", self._wheel, add="+")

    def _wheel(self, event):
        try:
            w = self.winfo_containing(event.x_root, event.y_root)
            while w is not None and w is not self.canvas:
                w = w.master
            if w is None:
                return  # 指针不在本滚动区上：不接管
            if self.canvas.winfo_exists():
                self.canvas.yview_scroll(-1 * int(event.delta / 120), "units")
        except tk.TclError:
            pass  # 窗口/控件销毁竞态
        except Exception:
            return

    def render(self, infos, empty_text="（没有符合条件的车次）"):
        for w in self.inner.winfo_children():
            w.destroy()
        self.cards = []
        keep = set(self.selected)
        self.selected = {}
        if not infos:
            tk.Label(self.inner, text=empty_text, fg="#6e7781", bg="#ffffff").pack(
                anchor="w", pady=8, padx=4)
            self.on_change([])
            return
        for info in infos:
            card = TrainCard(self.inner, info, self._clicked)
            card.pack(fill="x", pady=(0, 4), padx=(0, 2))
            self.cards.append((card, info))
            if card.key in keep:
                card.set_selected(True)
                self.selected[card.key] = info
        self.canvas.yview_moveto(0)
        self.on_change(list(self.selected.values()))

    def _clicked(self, card):
        if card.key in self.selected:
            self.selected.pop(card.key, None)
            card.set_selected(False)
        else:
            self.selected[card.key] = card.info
            card.set_selected(True)
        self.on_change(list(self.selected.values()))

    def selected_codes(self):
        codes = []
        for info in self.selected.values():
            c = info.get("train_code")
            if c and c not in codes:
                codes.append(c)
        return codes


class LauncherApp(tk.Frame):
    """抢票主界面。master 为空 = 独立窗口（launcher.py 直接运行）；
    master 给定时 = 嵌入宿主界面（监控系统侧边栏「抢票中心」页）。"""

    def __init__(self, master=None, task=None, on_config_saved=None):
        self._standalone = master is None
        if self._standalone:
            master = tk.Tk()
            install_tick_indicator(ttk.Style(master))
        self._top = master if self._standalone else master.winfo_toplevel()
        super().__init__(master)
        self._mp = self._top          # 弹窗父窗口：独立=窗口自身，嵌入=宿主主窗口
        self.task_id = (task or {}).get("id")
        self.task_name = (task or {}).get("name") or ""
        # 任务模式：配置来自任务条目，保存时写回任务库（由 on_config_saved 完成）
        if task:
            self.lc = dict(_DEFAULT_LC)
            self.lc.update(task)
        else:
            self.lc = load_launcher_config()
        self._save_cfg = on_config_saved or save_launcher_config
        # 每个实例独立日志队列：任务窗口只显示自己的日志，多窗口互不串台
        self.logq = queue.Queue()
        self.grabber = None
        self.armed = False
        self.auto_fired = False
        self._auto_vfail_last_msg = None
        self.reminded = False
        self.pax_vars = {}
        self.pax_purpose_vars = {}
        self.seat_vars = {}
        self.update_info = None
        self._monitor_mtime = 0.0
        self._pax_mtime = 0.0
        self.dataq = queue.Queue()
        self._querying = False
        self._last_query_ts = 0.0
        self._train_rows = {}
        self._train_infos = []

        self._build()
        self._sync_from_lc()

        if self._standalone:
            self._top.title("12306 抢票启动器 v%s" % __version__)
            self._top.minsize(780, 620)
            self.pack(fill="both", expand=True)
            # 高度按内容实测，屏幕放不下时压缩到可视区内
            self.update_idletasks()
            need_h = self.winfo_reqheight()
            win_h = min(need_h, max(620, self.winfo_screenheight() - 70))
            win_x = max(0, (self.winfo_screenwidth() - 820) // 2)
            self._top.geometry("820x%d+%d+%d" % (win_h, win_x, 14))
            self._top.protocol("WM_DELETE_WINDOW", self._on_close)

        self.after(500, self._tick)
        self.after(200, self._drain)
        self.after(1200, self._check_update_async)
        self.after(2500, self._warm_station_kinds)

    def _warm_station_kinds(self):
        """启动后预热常用车站的高铁/普速标注（历史站 + 当前出发/到达）。

        放后台线程跑：车站代码表/索引缓存缺失时要联网下载（可达 20 秒），
        在主线程会把界面整个冻住。Tk 变量读取先在主线程完成再进线程。"""
        names = [self.from_ent.get(), self.to_ent.get()]
        names.extend(self.from_ent.history[:6])
        names.extend(self.to_ent.history[:6])

        def worker():
            try:
                n2c, _c2n = ticket.load_station_map()
                get_station_index()  # 一并预热车站拼音索引（首次要下载）
                codes = []
                for n in names:
                    c = n2c.get((n or "").strip())
                    if c and c not in codes:
                        codes.append(c)
                if codes and request_station_kinds(codes):
                    log("[车站] 正在后台识别常用车站类型（高铁 / 普速）")
            except Exception as e:
                log("[提醒] 常用车站类型预热失败：%s" % e)

        threading.Thread(target=worker, daemon=True, name="station-warm").start()

    # ---- 界面构建 ----

    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        # 整页滚动容器：窗口高度装不下时滚轮滚页、下方模块可达可点
        self.canvas = tk.Canvas(self, highlightthickness=0, bg="#f5f6f8")
        self._vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._vsb.set)
        self._vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg="#f5f6f8")
        self._body_win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(
            self._body_win, width=e.width))
        self.body.grid_columnconfigure(0, weight=1)
        self.winfo_toplevel().bind("<MouseWheel>", self._page_wheel, add="+")

        top = ttk.Frame(self.body, padding=(12, 8))
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text=self.task_name or "12306 抢票启动器", font=(FONT[0], 15, "bold")).pack(side="left")
        ttk.Label(top, text="v" + __version__, foreground="#6e7781").pack(side="left", padx=(8, 0))
        self.update_lbl = ttk.Label(top, text="", foreground="#8250df", cursor="hand2")
        self.update_lbl.pack(side="right")

        # 乘车人
        pf = ttk.LabelFrame(self.body, text=" 乘车人（勾选参与抢票） ", padding=8)
        pf.grid(row=3, column=0, sticky="ew", padx=12, pady=(3, 0))
        self.pax_box = ttk.Frame(pf)
        self.pax_box.pack(side="left", fill="x", expand=True)
        ttk.Button(pf, text="新增/编辑", command=self._edit_pax).pack(side="right")

        # 行程
        tf = ttk.LabelFrame(self.body, text=" 行程（车站支持拼音简拼 / 全拼 / 汉字 / 代码实时搜索） ", padding=8)
        tf.grid(row=1, column=0, sticky="ew", padx=12, pady=(2, 0))
        r0 = ttk.Frame(tf)
        r0.pack(fill="x")
        ttk.Label(r0, text="常用行程").pack(side="left")
        self.preset_cb = ttk.Combobox(r0, state="readonly", width=28)
        self.preset_cb.pack(side="left", padx=6, fill="x", expand=True)
        self.preset_cb.bind("<<ComboboxSelected>>", self._load_preset)
        ttk.Button(r0, text="存为预设", command=self._save_preset).pack(side="left", padx=4)
        ttk.Button(r0, text="删除", command=self._delete_preset).pack(side="left")

        r1 = ttk.Frame(tf)
        r1.pack(fill="x", pady=(8, 0))
        ttk.Label(r1, text="出发").pack(side="left")
        self.from_ent = StationEntry(r1, width=13, history=self.lc.get("station_history"))
        self.from_ent.pack(side="left", padx=(4, 2))
        ttk.Button(r1, text="⇄", width=3, command=self._swap_stations).pack(side="left", padx=2)
        ttk.Label(r1, text="到达").pack(side="left", padx=(6, 0))
        self.to_ent = StationEntry(r1, width=13, history=self.lc.get("station_history"))
        self.to_ent.pack(side="left", padx=(4, 6))
        ttk.Button(r1, text="定位", width=5, command=self._locate).pack(side="left")
        ttk.Label(r1, text="车次留空=全部；日期「到」留空=只查那一天（区间最多相差 5 天，共 6 天）",
                  foreground="#6e7781").pack(side="left", padx=(10, 0))

        r2 = ttk.Frame(tf)
        r2.pack(fill="x", pady=(8, 0))
        ttk.Label(r2, text="乘车日期").pack(side="left")
        ttk.Label(r2, text="从", foreground="#57606a").pack(side="left", padx=(6, 2))
        self.date_var = tk.StringVar()
        ttk.Entry(r2, textvariable=self.date_var, width=11).pack(side="left")
        ttk.Button(r2, text="📅", width=3,
                   command=lambda: self._open_calendar(self.date_var)).pack(side="left", padx=(2, 8))
        ttk.Label(r2, text="到", foreground="#57606a").pack(side="left")
        self.date_to_var = tk.StringVar()
        ttk.Entry(r2, textvariable=self.date_to_var, width=11).pack(side="left", padx=(4, 2))
        ttk.Button(r2, text="📅", width=3,
                   command=lambda: self._open_calendar(self.date_to_var)).pack(side="left")
        ttk.Label(r2, text="车次").pack(side="left", padx=(12, 0))
        self.trains_var = tk.StringVar()
        ttk.Entry(r2, textvariable=self.trains_var, width=16).pack(side="left", padx=(4, 0))

        # 车次列表（第 2 段：卡片式，点卡片即选车次）
        qf = ttk.LabelFrame(self.body, text=" 车次列表（查询实时余票，点卡片选中车次） ", padding=8)
        qf.grid(row=2, column=0, sticky="ew", padx=12, pady=(3, 0))
        q0 = ttk.Frame(qf)
        q0.pack(fill="x")
        self.query_btn = ttk.Button(q0, text="查询车次 (F5)", command=lambda: self.query_trains())
        self.query_btn.pack(side="left")
        self.query_state = ttk.Label(q0, text="", foreground="#6e7781")
        self.query_state.pack(side="left", padx=8)
        ttk.Button(q0, text="清空列表", command=self._clear_train_rows).pack(side="right")
        # 余票自动刷新：默认 2 秒/次、默认开启；控制界面按需求隐藏（变量仍驱动 _tick）
        self.refresh_sec_var = tk.IntVar(value=2)
        self.auto_refresh_var = tk.BooleanVar(value=True)

        q1 = ttk.Frame(qf)
        q1.pack(fill="x", pady=(6, 0))
        self.filter_emu_var = tk.BooleanVar(value=False)
        self.filter_putong_var = tk.BooleanVar(value=False)
        self.filter_avail_var = tk.BooleanVar(value=False)
        self.sort_var = tk.StringVar(value="dep")
        for _txt, _var in (("只看高铁动车", self.filter_emu_var),
                           ("只看普速", self.filter_putong_var),
                           ("只看有票", self.filter_avail_var)):
            ttk.Checkbutton(q1, text=_txt, variable=_var,
                            command=self._refresh_train_view).pack(side="left", padx=(0, 10))
        ttk.Radiobutton(q1, text="历时最短", value="dur", variable=self.sort_var,
                        command=self._refresh_train_view).pack(side="right")
        ttk.Radiobutton(q1, text="发时最早", value="dep", variable=self.sort_var,
                        command=self._refresh_train_view).pack(side="right", padx=(12, 0))
        self.sel_state = ttk.Label(q1, text="已选 0 趟车 · 0 种席别", foreground="#0969da")
        self.sel_state.pack(side="left", padx=(16, 0))

        self.cardlist = TrainCardList(qf, self._on_cards_changed)
        self.cardlist.pack(fill="x", pady=(6, 0))
        self.cardlist.render([])

        # 席别（第 4 段）。票种不在这里——已下移到「乘车人」区，按每个人分别选成人/学生
        self.sf = ttk.LabelFrame(
            self.body, text=" 席别（点车次卡片可按该车实际余票刷新） ", padding=8)
        sf = self.sf
        sf.grid(row=4, column=0, sticky="ew", padx=12, pady=(3, 0))
        # 该车次全部席别（含无票）：已从车次列表区迁入席别区（与勾选/首选同区）
        self.seat_detail = tk.Frame(sf, bg="#ffffff", highlightthickness=1,
                                    highlightbackground="#eaeef2")
        self.seat_detail.pack(fill="x", pady=(4, 0))
        self._render_seat_detail([])
        # 首选席别：填了就先抢它（可逗号分隔多个），首选都没票才按下面勾选顺序
        t0 = ttk.Frame(sf)
        t0.pack(fill="x")
        ttk.Label(t0, text="首选席别").pack(side="left")
        self.seat_pri_var = tk.StringVar()
        ttk.Entry(t0, textvariable=self.seat_pri_var, width=18).pack(side="left", padx=(4, 6))
        ttk.Label(t0, text="可填 硬座/无座/二等座/软卧/硬卧 等，逗号分隔，先填的先抢；"
                          "可指定车次：K225=硬座/无座, K1969=硬卧（只作用该趟车，∩勾选）；"
                          "无座=硬座，认不出按硬座",
                  foreground="#6e7781").pack(side="left")
        self.seat_pri_hint = ttk.Label(sf, foreground="#6e7781")
        self.seat_pri_hint.pack(fill="x", pady=(2, 4))
        self.seat_pri_var.trace_add("write", self._on_seat_pri_change)
        self._on_seat_pri_change()
        s0 = ttk.Frame(sf)
        s0.pack(fill="x")
        self.seat_box = ttk.Frame(s0)
        self.seat_box.pack(side="left", fill="x", expand=True)
        self._rebuild_seats()

        # 开抢时间
        ef = ttk.LabelFrame(self.body, text=" 开抢时间 ", padding=8)
        ef.grid(row=5, column=0, sticky="ew", padx=12, pady=(3, 0))
        r0e = ttk.Frame(ef)
        r0e.pack(fill="x")
        ttk.Label(r0e, text="到点自动开抢").pack(side="left")
        self.start_var = tk.StringVar()
        self.start_var.trace_add("write", self._on_start_change)
        ttk.Entry(r0e, textvariable=self.start_var, width=19).pack(side="left", padx=(6, 2))
        ttk.Button(r0e, text="📅", width=3,
                   command=self._open_datetime).pack(side="left", padx=(0, 6))
        ttk.Label(r0e, text="提前").pack(side="left")
        self.remind_var = tk.IntVar(value=10)
        ttk.Spinbox(r0e, from_=0, to=120, textvariable=self.remind_var, width=4).pack(side="left", padx=4)
        ttk.Label(r0e, text="分钟提醒（格式如 2026-10-09 17:00:00）").pack(side="left")
        self.countdown_lbl = ttk.Label(r0e, font=(FONT[0], 11, "bold"), foreground="#cf222e")
        self.countdown_lbl.pack(side="left", padx=(10, 0))
        r1e = ttk.Frame(ef)
        r1e.pack(fill="x", pady=(4, 0))
        ttk.Label(r1e, text="开抢前").pack(side="left")
        self.warm_var = tk.IntVar(value=10)
        ttk.Spinbox(r1e, from_=0, to=30, textvariable=self.warm_var, width=4).pack(side="left", padx=4)
        ttk.Label(r1e, text="分钟登录预热（到点立即填单下单，0=不预热）").pack(side="left")

        # 主按钮 + 状态
        bf = ttk.Frame(self.body)
        bf.grid(row=6, column=0, sticky="ew", padx=12, pady=(10, 0))
        self.go_btn = tk.Button(bf, text="立 即 抢 票", font=(FONT[0], 14, "bold"),
                                bg="#cf222e", fg="white", activebackground="#a40e26",
                                activeforeground="white", relief="flat", padx=30, pady=6,
                                cursor="hand2", command=self.toggle_grab)
        self.go_btn.pack(side="left", fill="x", expand=True)
        ttk.Button(bf, text="新建监控任务", command=self.open_new_monitor_task).pack(side="right", padx=(8, 0))
        ttk.Button(bf, text="测试会话", command=self._test_session).pack(side="right", padx=(8, 0))

        stf = ttk.Frame(self.body)
        stf.grid(row=7, column=0, sticky="ew", padx=12, pady=(8, 0))
        self.dot = tk.Canvas(stf, width=14, height=14, highlightthickness=0)
        self.dot.pack(side="left")
        self._dot_id = self.dot.create_oval(3, 3, 11, 11, fill="#6e7781", outline="")
        self.status_lbl = ttk.Label(stf, text="就绪")
        self.status_lbl.pack(side="left", padx=6)

        # 日志
        lg = ttk.LabelFrame(self.body, text=" 日志 ", padding=4)
        lg.grid(row=8, column=0, sticky="nsew", padx=12, pady=(8, 12))
        self.body.grid_rowconfigure(8, weight=1)
        txt = tk.Text(lg, height=3, wrap="word", state="disabled", font=(FONT[0], 9))
        sb = ttk.Scrollbar(lg, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)
        self.log_text = txt
        for tag, color in LOG_COLOR.items():
            txt.tag_configure(tag, foreground=color)

        # 快捷键：F5 查车次 / Ctrl+Enter 开抢或停止 / Ctrl+L 定位
        self.bind("<F5>", lambda e: self.query_trains())
        self.bind("<Control-Return>", lambda e: self.toggle_grab())
        self.bind("<Control-l>", lambda e: self._locate())

    # ---- 数据同步 ----

    def _sync_from_lc(self):
        lc = self.lc
        hist = lc.get("station_history") or []
        self.from_ent.set_history(hist)
        self.to_ent.set_history(hist)
        self.from_ent.set(lc.get("from") or "")
        self.to_ent.set(lc.get("to") or "")
        self.trains_var.set(",".join(_display_trains(lc.get("trains"))))
        self.date_var.set(lc.get("date") or (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"))
        self.date_to_var.set(lc.get("date_to") or "")
        # 票种不再全局设置：_refresh_pax() 里按每个乘车人各自的保存值建下拉
        for s, v in self.seat_vars.items():
            v.set(s in (lc.get("seat_types") or []))
        self.seat_pri_var.set(str(lc.get("seat_priority") or ""))
        self.start_var.set(lc.get("start_time") or "")
        self.remind_var.set(int(lc.get("remind_minutes") or 10))
        self.warm_var.set(max(0, min(30, int(lc.get("warm_minutes") or 10))))
        names = [p.get("name") or "" for p in (lc.get("presets") or [])]
        self.preset_cb.configure(values=names)
        if names:
            self.preset_cb.current(0)
        self._refresh_pax()
        if merge_trains_from_monitor(self.lc, self._save_cfg):
            self.trains_var.set(",".join(self.lc["trains"]))
        try:
            self._monitor_mtime = os.path.getmtime(os.path.join(HERE, "config.json"))
        except OSError:
            pass
        try:
            self._pax_mtime = os.path.getmtime(os.path.join(HERE, "passengers.json"))
        except OSError:
            pass
        st = parse_dt(self.lc.get("start_time") or "")
        self.armed = bool(st and st > datetime.now())
        self.auto_fired = False
        self._auto_vfail_last_msg = None
        self.reminded = False

    def _ui_to_lc(self, save=True):
        lc = self.lc
        lc["from"] = self.from_ent.get()
        lc["to"] = self.to_ent.get()
        lc["trains"] = [t.strip().upper() for t in re.split(r"[,，\s]+", self.trains_var.get()) if t.strip()]
        lc["seat_types"] = [s for s, v in self.seat_vars.items() if v.get()]
        lc["seat_priority"] = self.seat_pri_var.get().strip()
        lc["date"] = self.date_var.get().strip()
        lc["date_to"] = self.date_to_var.get().strip()
        lc["start_time"] = self.start_var.get().strip()
        # Spinbox 手输非数字时回退默认值（与 _tick 的 warm_var 兜底同口径），
        # 否则 int() 抛 ValueError/TclError，只有控制台 traceback
        try:
            lc["remind_minutes"] = max(0, min(120, int(self.remind_var.get() or 10)))
        except Exception:
            lc["remind_minutes"] = 10
        try:
            lc["warm_minutes"] = max(0, min(30, int(self.warm_var.get() or 10)))
        except Exception:
            lc["warm_minutes"] = 10
        lc["passenger_names"] = [n for n, v in self.pax_vars.items() if v.get()]
        if self.pax_purpose_vars:
            lc["pax_purpose"] = {
                n: (LABEL_TO_PURPOSE.get(v.get()) or "ADULT")
                for n, v in self.pax_purpose_vars.items()}
        # 兼容监控引擎 / 旧配置：全局口径 = 勾选乘车人的余票查询口径
        lc["purpose_code"] = purpose_of(lc)
        hist = list(lc.get("station_history") or [])
        for stn in (lc["from"], lc["to"]):
            if stn and stn not in hist:
                hist.insert(0, stn)
        lc["station_history"] = hist[:20]
        if save:
            self._save_cfg(lc)

    def _refresh_pax(self):
        cur_sel = {n for n, v in self.pax_vars.items() if v.get()}
        cur_pp = {n: v.get() for n, v in self.pax_purpose_vars.items()}
        for w in self.pax_box.winfo_children():
            w.destroy()
        self.pax_vars.clear()
        self.pax_purpose_vars.clear()
        try:
            plist = passengers_mod.load_passengers()
        except Exception as e:
            plist = []
            self._put_log("[错误] 乘车人读取失败：%s" % e)
        if not plist:
            ttk.Label(self.pax_box, text="（未添加乘车人，点右侧按钮添加）",
                      foreground="#6e7781").pack(side="left")
            return
        sel = cur_sel or self.lc.get("passenger_names") or passengers_mod.default_names() or [plist[0]["name"]]
        saved = self.lc.get("pax_purpose") or {}
        default_code = self.lc.get("purpose_code") or "ADULT"
        for p in plist:
            name = p["name"]
            row = ttk.Frame(self.pax_box)
            row.pack(side="left", padx=(0, 14))
            v = tk.BooleanVar(value=name in sel)
            self.pax_vars[name] = v
            label = "%s · %s" % (name, mask_id(p.get("id_no")))
            if p.get("mobile"):
                label += " · " + mask_mobile(p.get("mobile"))
            ttk.Checkbutton(row, text=label, variable=v).pack(side="left")
            # 票种按人：优先本次界面值 > 已保存值 > 全局兜底
            code = (LABEL_TO_PURPOSE.get(cur_pp.get(name) or "")
                    or saved.get(name) or default_code)
            pv = tk.StringVar(value=PURPOSE_LABELS.get(code) or "成人票")
            self.pax_purpose_vars[name] = pv
            cb = ttk.Combobox(row, textvariable=pv, width=6, state="readonly",
                              values=["成人票", "学生票"])
            cb.pack(side="left", padx=(4, 0))
            cb.bind("<<ComboboxSelected>>", self._on_pax_purpose_change)

    # ---- 车次查询与席别联动 ----

    def _page_wheel(self, e):
        """整页滚轮：指针在正文/页画布上时滚整页；车次卡片列表有自己的滚动。"""
        try:
            if not self.canvas.winfo_exists():
                return
            stops = [self.canvas, self.body]
            cl = getattr(self, "cardlist", None)
            if cl is not None:
                stops.append(cl.canvas)
            w = self.winfo_containing(e.x_root, e.y_root)
            while w is not None and not any(w is st for st in stops):
                w = w.master
            if w is None or w is getattr(self, "cardlist", None).canvas:
                return
            self.canvas.yview_scroll(int(-e.delta / 120), "units")
        except tk.TclError:
            pass
        except Exception:
            return

    def _on_seat_pri_change(self, *_a):
        """「首选席别」输入框的即时解析反馈（含 车次=席别 专属规则）。"""
        checked = [s for s, v in self.seat_vars.items() if v.get()]
        text, bad = ticket.seat_priority_feedback(self.seat_pri_var.get(), checked=checked)
        self.seat_pri_hint.configure(text=text,
                                     foreground="#cf222e" if bad else "#6e7781")

    def _rebuild_seats(self, available=None, allnames=None, houbu=False):
        """重建席别勾选区。

        available=None：显示全量可选席别（SEAT_OPTIONS）。
        allnames 非空（选中了车次）：按该车实际提供的全部席别显示 —— **含当前无票的**，
            抢票时可以把暂时无票的席别勾上，放票/退票回流后按勾选顺序抢。
        available={席别: 余票}：带余票数；无票的标「无」，该车可候补时标「候补」。
        勾选策略：优先保留原勾选；原勾选与新列表无交集时自动勾第一个可购席别。"""
        keep = {s for s, v in self.seat_vars.items() if v.get()}
        for w in self.seat_box.winfo_children():
            w.destroy()
        self.seat_vars = {}
        avail = available or {}
        if allnames:
            names = list(allnames)
            names += sorted((s for s in avail if s not in names),
                            key=lambda x: SEAT_ORDER.get(x, 99))
            self.sf.configure(text=" 席别（该车次共 %d 种席别，%d 种有票；无票的也能勾，"
                                   "放票/退票回流后按勾选顺序抢） "
                              % (len(names), len([s for s in names if avail.get(s)])))
        else:
            names = [s for s in SEAT_OPTIONS if available is None or s in avail]
            names += sorted((s for s in avail if s not in SEAT_OPTIONS),
                            key=lambda x: SEAT_ORDER.get(x, 99))
            if available is not None:
                self.sf.configure(text=" 席别（已按所选车次刷新：%d 种可购席别） " % len(names))
            else:
                self.sf.configure(text=" 席别（点车次卡片可按该车实际余票刷新，票价以官方下单页为准） ")
        if not names:
            ttk.Label(self.seat_box, text="（该车次暂无可购席别）",
                      foreground="#6e7781").grid(row=0, column=0, sticky="w")
            return
        default = set(self.lc.get("seat_types") or [])
        chosen = [s for s in names if s in keep] or [s for s in names if s in default] or [names[0]]
        for i, s in enumerate(names):
            v = tk.BooleanVar(value=s in chosen)
            self.seat_vars[s] = v
            val = avail.get(s)
            if val:
                label = "%s · %s" % (s, val)
            elif allnames or available is not None:
                label = "%s · %s" % (s, "候补" if houbu else "无")
            else:
                label = s
            if s not in ticket.SEAT_NAME_TO_CODE:
                label += "（不能自动下单）"
            ttk.Checkbutton(self.seat_box, text=label, variable=v).grid(
                row=i // 6, column=i % 6, sticky="w", padx=(0, 6), pady=1)
        if (allnames or available is not None) and not (keep & set(names)):
            self._put_log("[席别] 该车次%s与原勾选无交集，已自动勾选 %s"
                          % ("席别" if allnames else "可购席别", chosen[0]))

    def _sync_inputs(self):
        """把界面行程输入落到 lc 并做基本校验（查询车次用，不弹窗）。"""
        self._ui_to_lc()
        lc = self.lc
        if not lc.get("from") or not lc.get("to"):
            return False
        return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", lc.get("date") or ""))

    def query_trains(self, silent=False):
        """拉取实时余票填充车次列表（后台线程，界面不卡）。"""
        if self._querying:
            if not silent:
                self._put_log("[提醒] 上一次查询还在进行中…")
            return
        if not self._sync_inputs():
            if silent:
                self._put_log("[提醒] 行程信息不完整，本轮跳过余票自动刷新")
            else:
                messagebox.showwarning("参数不完整",
                                       "请填写出发站 / 到达站，日期格式 YYYY-MM-DD", parent=self._mp)
            return
        self._querying = True
        self._last_query_ts = time.time()
        self.query_btn.configure(state="disabled")
        self.query_state.configure(text="查询中…", foreground="#d97706")
        lc = dict(self.lc)
        _pm = lc.get("pax_purpose") or {}
        _ptxt = "、".join(
            # Task 88c：日志里的乘车人姓名打码（与 Task 61/84 同口径）；
            # 学生票后缀保留（非 PII）。
            "%s%s" % (_mask_one_name(n),
                      "（学生票）" if _pm.get(n) == "0X00" else "")
            for n in (lc.get("passenger_names") or [])) or "账号默认乘车人"
        self._put_log("[查询] %s → %s %s（乘车人：%s；余票口径 %s）…" % (
            lc.get("from"), lc.get("to"), lc.get("date"), _ptxt,
            PURPOSE_LABELS.get(purpose_of(lc)) or "成人票"))
        threading.Thread(target=self._query_work, args=(lc,), daemon=True,
                         name="train-query").start()

    @staticmethod
    def _resolve_dates(lc):
        """把「从 / 到」解析为待查日期列表（appcommon 单点口径：相差最多 5 天）。"""
        try:
            dates, date_range = appcommon.parse_date_range(
                (lc.get("date") or "").strip(), (lc.get("date_to") or "").strip())
        except ValueError as e:
            raise RuntimeError(str(e))
        if date_range:  # launcher 契约：区间展开成逐日列表
            d0 = datetime.strptime(date_range[0], "%Y-%m-%d").date()
            d1 = datetime.strptime(date_range[1], "%Y-%m-%d").date()
            dates = [(d0 + timedelta(days=i)).isoformat()
                     for i in range((d1 - d0).days + 1)]
        return dates

    def _query_work(self, lc):
        try:
            name2code, code2name = ticket.load_station_map()
            fc, tc = name2code.get(lc.get("from")), name2code.get(lc.get("to"))
            if not fc or not tc:
                # 新开车站可能不在本地缓存里：后台强制刷新一次，下次可查到
                ticket.note_station_missing(lc.get("from") if not fc else lc.get("to"))
                raise RuntimeError("车站无法识别：%s / %s" % (lc.get("from"), lc.get("to")))
            infos = []
            for dt in self._resolve_dates(lc):
                rows = ticket.query_tickets(fc, tc, dt, purpose=purpose_of(lc))
                for r in rows:
                    info = ticket.parse_row(r, code2name, dt)
                    info["_date"] = dt
                    infos.append(info)
            self.dataq.put(("trains", infos))
        except Exception as e:
            self.dataq.put(("trains_err", "%s: %s" % (type(e).__name__, e)))

    def _apply_train_rows(self, infos):
        self._querying = False
        self.query_btn.configure(state="normal")
        self._train_infos = list(infos)
        self._refresh_train_view(log=True)
        self._remember_query()
        learn_station_kinds(infos)

    def _refresh_train_view(self, log=False):
        """按筛选 / 排序条件重建车次卡片列表。"""
        infos = list(getattr(self, "_train_infos", []))
        only_emu = self.filter_emu_var.get()
        only_putong = self.filter_putong_var.get()
        if only_emu and not only_putong:
            infos = [i for i in infos if is_emu(i.get("train_code"))]
        elif only_putong and not only_emu:
            infos = [i for i in infos if not is_emu(i.get("train_code"))]
        if self.filter_avail_var.get():
            infos = [i for i in infos if i.get("available_seats")]
        if self.sort_var.get() == "dur":
            infos.sort(key=lambda x: ((x.get("_date") or ""), x.get("duration") or "99:99:99"))
        else:
            infos.sort(key=lambda x: ((x.get("_date") or ""), x.get("start_time") or "99:99"))
        total_all = len(getattr(self, "_train_infos", []))
        empty = "（没有符合筛选条件的车次）" if total_all else "（还没有车次，点「查询车次 (F5)」拉取实时余票）"
        self.cardlist.render(infos, empty)
        hit = [i for i in infos if i.get("available_seats")]
        if infos and len(infos) != total_all:
            state_txt = "%d/%d 趟 · 有票 %d 趟" % (len(infos), total_all, len(hit))
        else:
            state_txt = "%d 趟 · 有票 %d 趟" % (len(infos), len(hit))
        self.query_state.configure(text=state_txt,
                                   foreground="#1a7f37" if hit else "#6e7781")
        if log:
            if hit:
                self._put_log("[查询] %d 趟车里 %d 趟有票：%s" % (
                    len(infos), len(hit), " ".join(i["train_code"] for i in hit[:10])))
            else:
                self._put_log("[查询] 共 %d 趟，暂无可购余票" % len(infos))

    def _on_cards_changed(self, infos):
        """卡片选中变化：把选中车次写回车次框，并按并集余票刷新席别区。"""
        if not hasattr(self, "seat_box"):
            return          # _build 尚未建好席别区
        codes = self.cardlist.selected_codes()
        merged = {}
        allnames = []
        houbu = False
        for i in infos:
            for k, v in (i.get("available_seats") or {}).items():
                merged.setdefault(k, v)
            for n in (i.get("seats_all") or []):
                if n not in allnames:
                    allnames.append(n)
            if i.get("houbu"):
                houbu = True
        order = {n: idx for idx, n in enumerate(ticket.SEAT_SHOW_ORDER)}
        allnames.sort(key=lambda x: order.get(x, 99))
        self.trains_var.set(",".join(codes))
        # 选中车次后席别区按「该车提供的全部席别」刷新（含无票），抢票时也能勾
        self._rebuild_seats(merged if merged else None, allnames, houbu)
        self._render_seat_detail(infos)
        self.sel_state.configure(text="已选 %d 趟车 · %d 种席别（%d 种有票）"
                                 % (len(codes), len(allnames),
                                    len([s for s in allnames if merged.get(s)])))
        if codes:
            self._put_log("[选择] 已选 %d 趟车（%s）：全部席别 %d 种 —— %s" % (
                len(codes), ",".join(codes), len(allnames),
                " · ".join("%s %s" % (n, merged.get(n) or ("候补" if houbu else "无"))
                           for n in allnames)))

    # 车型 → 常见席别参考（没选车次时显示；选中车次后按该车实际席别码串显示）
    KIND_SEAT_HINT = ("G 高铁：商务座/特等座/一等座/二等座/无座    "
                      "D 动车：商务座/一等座/二等座/动卧（一等卧·二等卧）/无座    "
                      "C 城际：一等座/二等座（部分商务座）/无座    "
                      "Z·T·K 普速：软卧/硬卧/软座/硬座/无座")

    def _render_seat_detail(self, infos):
        """车次列表下方：列出选中车次的全部席别（含无票）。

        席别名来自该车 queryLeftTicket 的 p35(seat_types) 码串（ticket.seat_names_all），
        余票取 available_seats；没选车次时给车型参考表。"""
        if not hasattr(self, "seat_detail"):
            return
        bg = "#ffffff"
        for w in self.seat_detail.winfo_children():
            w.destroy()
        head = tk.Frame(self.seat_detail, bg=bg)
        head.pack(fill="x", padx=8, pady=(4, 0))
        line = tk.Frame(self.seat_detail, bg=bg)
        line.pack(fill="x", padx=8, pady=(1, 4))
        tk.Label(head, text="该车全部席别", fg="#57606a", bg=bg,
                 font=(FONT[0], 9, "bold")).pack(side="left")
        infos = [i for i in infos if i]
        if not infos:
            tk.Label(head, text="　　选中车次卡片后，这里列出这趟车提供的全部席别（无票也列）",
                     fg="#8c959f", bg=bg, font=(FONT[0], 9)).pack(side="left")
            tk.Label(line, text="车型参考　" + self.KIND_SEAT_HINT, fg="#8c959f",
                     bg=bg, font=(FONT[0], 9)).pack(side="left")
            return
        merged = {}
        names = []
        for i in infos:
            for n in (i.get("seats_all") or []):
                if n not in names:
                    names.append(n)
            for k, v in (i.get("available_seats") or {}).items():
                merged.setdefault(k, v)
                if k not in names:
                    names.append(k)
        order = {n: idx for idx, n in enumerate(ticket.SEAT_SHOW_ORDER)}
        names.sort(key=lambda x: order.get(x, 99))
        hit = [n for n in names if merged.get(n)]
        codes = [i.get("train_code") or "?" for i in infos]
        kinds = []
        for c in codes:
            k = train_kind(c)
            if k and k not in kinds:
                kinds.append(k)
        houbu = any(i.get("houbu") for i in infos)
        tk.Label(head, text="　%s（%s）　共 %d 种席别，有票 %d 种%s" % (
            "、".join(codes), "、".join(kinds), len(names), len(hit),
            "　售完可候补" if houbu else ""),
            fg="#57606a", bg=bg, font=(FONT[0], 9)).pack(side="left")
        # 一行最多排 8 个席别，多了换行：多选车次（如高铁+普速）时席别名会到
        # 十几个，单行排不下会被窗口右边裁掉
        chunks = [names[i:i + 8] for i in range(0, len(names), 8)]
        for ci, chunk in enumerate(chunks):
            row = line if ci == 0 else tk.Frame(self.seat_detail, bg=bg)
            if ci:
                row.pack(fill="x", padx=8, pady=(1, 4))
            for n in chunk:
                v = merged.get(n)
                if v:
                    txt, fg, ft = "%s %s" % (n, v), "#1a7f37", (FONT[0], 9, "bold")
                elif houbu:
                    txt, fg, ft = "%s 候补" % n, "#9a6700", (FONT[0], 9)
                else:
                    txt, fg, ft = "%s 无" % n, "#8c959f", (FONT[0], 9)
                tk.Label(row, text=txt, fg=fg, bg=bg, font=ft).pack(side="left", padx=(0, 12))

    def _apply_train_error(self, err):
        self._querying = False
        self.query_btn.configure(state="normal")
        self.query_state.configure(text="查询失败", foreground="#cf222e")
        self._put_log("[错误] 车次查询失败：%s" % err)

    def _on_pax_purpose_change(self, *_):
        """某个乘车人的票种改了：立刻落盘，并提示余票口径可能随之变化。"""
        self._ui_to_lc()
        lc = self.lc
        pm = lc.get("pax_purpose") or {}
        picked = [n for n, v in self.pax_vars.items() if v.get()]
        txt = "、".join("%s=%s" % (n, PURPOSE_LABELS.get(pm.get(n)) or "成人票")
                        for n in picked) or "未勾选乘车人"
        self._put_log("[票种] %s；余票口径 %s（建议重新查询车次）" % (
            txt, PURPOSE_LABELS.get(purpose_of(lc)) or "成人票"))

    def _clear_train_rows(self):
        self._train_infos = []
        self._train_rows = {}
        self.cardlist.render([])
        self.query_state.configure(text="", foreground="#6e7781")
        self.sel_state.configure(text="已选 0 趟车 · 0 种席别")
        self._rebuild_seats()
        self._render_seat_detail([])

    def _swap_stations(self):
        a, b = self.from_ent.get(), self.to_ent.get()
        self.from_ent.set(b)
        self.to_ent.set(a)
        if a or b:
            self._put_log("[行程] 已交换出发/到达：%s → %s" % (b or "?", a or "?"))

    def _open_datetime(self):
        DateTimeDialog(self, self.start_var)

    def _open_calendar(self, var=None):
        CalendarDialog(self, var if var is not None else self.date_var)

    def _remember_query(self):
        """记录本次查询到历史（去重、最多 10 条）。"""
        lc = self.lc
        item = {"from": lc.get("from") or "", "to": lc.get("to") or "",
                "date": lc.get("date") or ""}
        if not (item["from"] and item["to"]):
            return
        hist = [h for h in (lc.get("query_history") or [])
                if not (h.get("from") == item["from"] and h.get("to") == item["to"]
                        and h.get("date") == item["date"])]
        hist.insert(0, item)
        lc["query_history"] = hist[:10]
        self._save_cfg(lc)

    def _show_query_history(self):
        hist = self.lc.get("query_history") or []
        if not hist:
            messagebox.showinfo("历史查询记录", "还没有查询记录。", parent=self._mp)
            return
        QueryHistoryDialog(self, hist, self._apply_history)

    def _apply_history(self, item):
        self.from_ent.set(item.get("from") or "")
        self.to_ent.set(item.get("to") or "")
        self.date_var.set(item.get("date") or "")
        self.date_to_var.set("")
        self._put_log("[历史] 已填入 %s → %s %s" % (
            item.get("from"), item.get("to"), item.get("date")))

    def _auto_refresh_trains(self):
        """余票自动刷新（在 _tick 里按间隔触发）。"""
        if not self.auto_refresh_var.get() or self._querying or self.grabber:
            return
        try:
            gap = max(2, int(self.refresh_sec_var.get() or 2))
        except (tk.TclError, ValueError):
            gap = 2
        if time.time() - self._last_query_ts >= gap:
            self.query_trains(silent=True)

    # ---- 定时器 ----

    def _notify_nonmodal(self, title, msg):
        """非模态提醒窗：Toplevel，不 grab、不 wait_window，不阻塞 _tick 的 after 链。

        无人值守时自动开抢必须能继续；用户回来点"知道了"关掉即可。
        重复提醒不堆窗口：新窗出现前先关掉旧窗。
        """
        try:
            old = getattr(self, "_remind_win", None)
            if old is not None:
                try:
                    if old.winfo_exists():
                        old.destroy()
                except tk.TclError:
                    pass
        except Exception:
            pass
        try:
            top = tk.Toplevel(self._mp)
        except tk.TclError:
            return
        try:
            top.title(title)
            top.resizable(False, False)
            # 依附主窗口（任务栏不单独占位）+ 置顶，保证提醒可见
            top.transient(self._mp)
            top.attributes("-topmost", True)
        except tk.TclError:
            pass
        ttk.Label(top, text=msg, justify="left", wraplength=380).pack(padx=18, pady=(14, 8))
        ttk.Button(top, text="知道了", command=top.destroy).pack(pady=(0, 14))
        try:
            # 居中到主窗口
            top.update_idletasks()
            x = self._mp.winfo_x() + (self._mp.winfo_width() - top.winfo_reqwidth()) // 2
            y = self._mp.winfo_y() + (self._mp.winfo_height() - top.winfo_reqheight()) // 2
            top.geometry("+%d+%d" % (max(x, 0), max(y, 0)))
        except tk.TclError:
            pass
        # 注意：绝不能在这里 grab_set()/wait_window()——那会重新变成模态，冻住 _tick。
        self._remind_win = top

    def _tick(self):
        try:
            self._sync_monitor()
            self._sync_pax()
            self._auto_refresh_trains()
            now = datetime.now()
            g = self.grabber
            if g and not g.is_alive():
                self.grabber = None
                ok, msg = g.result if g.result else (False, "已停止")
                self.go_btn.configure(text="立 即 抢 票", bg="#cf222e", state="normal")
                if ok:
                    self.auto_fired = True
                    self._set_status("ok", "抢票成功！")
                    # 成功状态落盘：否则重启后任务库里显示"就绪"，已抢到的事被抹掉
                    self.lc["status"] = "ok"
                    try:
                        self._save_cfg(self.lc)
                    except Exception:
                        pass
                    self._top.bell()
                else:
                    self._set_status("idle", msg or "已停止")
            if not self.grabber:
                st = parse_dt(self.start_var.get().strip())
                if st:
                    diff = (st - now).total_seconds()
                    lead = 0
                    try:
                        lead = max(0, int(self.warm_var.get() or 10)) * 60
                    except Exception:
                        pass
                    if diff > lead:
                        _cd = "距开抢 %s" % fmt_countdown(diff)
                        _cd = ("⏰ " + _cd) if self.armed else ("⚠ 自动抢未启用 · " + _cd)
                        self.countdown_lbl.configure(text=_cd)
                        # remind/warm 手输非数字时回退（与 warm_var 兜底同口径），
                        # 否则 _tick 每 500ms 抛一次 TclError/ValueError 刷控制台
                        try:
                            remind = int(self.remind_var.get() or 0)
                        except Exception:
                            remind = 0
                        if self.armed and not self.reminded and remind > 0 and diff <= remind * 60:
                            self.reminded = True
                            self._put_log("[提醒] 距离开抢不到 %d 分钟！" % remind)
                            self._top.bell()
                            # 非模态提醒：模态弹窗会冻住 _tick 的 after 链，
                            # 无人值守时自动开抢永远到不了
                            self._notify_nonmodal("开抢提醒", "距离开抢不到 %d 分钟！\n开抢时间：%s" % (
                                remind, st.strftime("%Y-%m-%d %H:%M:%S")))
                    else:
                        if self.armed and not self.auto_fired and (now - st).total_seconds() <= max(90, lead):
                            self.countdown_lbl.configure(text="已到点，自动开抢！")
                            if lead > 0:
                                self._put_log("[自动] 进入预热窗口（开抢前 %d 分钟）：先登录并保持浏览器，到点立即下单" % int(lead / 60))
                            else:
                                self._put_log("[自动] 已到点，自动开始抢票（%s）" % st.strftime("%H:%M:%S"))
                            self._put_log("[提示] 抢票在后台运行，浏览器无窗口，请勿关闭本窗口")
                            self._top.bell()
                            self.start_grab(auto=True)
                        elif self.armed and not self.auto_fired:
                            self.armed = False
                            self.countdown_lbl.configure(text="开抢时间已过，请手动开抢")
                        elif diff > 0:
                            # 还没到点（只是进入了预热窗口）：照常显示倒计时，
                            # 别提前十分钟就喊"开抢时间已过"
                            self.countdown_lbl.configure(text="距开抢 %s" % fmt_countdown(diff))
                        else:
                            self.countdown_lbl.configure(text="开抢时间已过")
                else:
                    self.countdown_lbl.configure(text="未设置开抢时间")
        finally:
            try:
                self.after(500, self._tick)
            except tk.TclError:
                pass  # 窗口已销毁，停止循环

    # ---- 与监控系统实时联动 ----

    def _sync_monitor(self):
        """config.json 有变化时，把监控系统新加的车次合并进来。"""
        path = os.path.join(HERE, "config.json")
        try:
            mt = os.path.getmtime(path)
        except OSError:
            return
        if mt == self._monitor_mtime:
            return
        self._monitor_mtime = mt
        if merge_trains_from_monitor(self.lc, self._save_cfg):
            self.trains_var.set(",".join(self.lc["trains"]))

    def _sync_pax(self):
        """passengers.json 有变化（监控系统增删改乘车人）时刷新勾选区。"""
        path = os.path.join(HERE, "passengers.json")
        try:
            mt = os.path.getmtime(path)
        except OSError:
            return
        if mt == self._pax_mtime:
            return
        self._pax_mtime = mt
        self._refresh_pax()

    def _drain(self):
        try:
            while True:
                self._put_log(self.logq.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                kind, payload = self.dataq.get_nowait()
                if kind == "trains":
                    self._apply_train_rows(payload)
                elif kind == "trains_err":
                    self._apply_train_error(payload)
        except queue.Empty:
            pass
        try:
            self.after(200, self._drain)
        except tk.TclError:
            pass  # 窗口已销毁，停止循环

    def _put_log(self, line):
        txt = self.log_text
        txt.configure(state="normal")
        txt.insert("end", line + "\n")
        for tag in LOG_COLOR:
            if tag in line:
                txt.tag_add(tag, "end-2l", "end-1l")
                break
        # 行数超限裁掉头部：长跑数天不能让日志区无限膨胀
        if float(txt.index("end-1c")) > 2500.0:
            txt.delete("1.0", "1000.0")
        txt.configure(state="disabled")
        txt.see("end")

    def _set_status(self, color_key, text):
        colors = {"running": "#d97706", "ok": "#1a7f37", "idle": "#6e7781"}
        self.dot.itemconfigure(self._dot_id, fill=colors.get(color_key, "#6e7781"))
        self.status_lbl.configure(text=text)

    # ---- 抢票控制 ----

    def _validate(self, auto=False):
        lc = self.lc
        if not lc.get("from") or not lc.get("to"):
            return self._validate_fail("参数不完整", "请填写出发站和到达站", auto)
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", lc.get("date") or ""):
            return self._validate_fail("参数不完整", "日期格式应为 YYYY-MM-DD", auto)
        if not lc.get("seat_types"):
            return self._validate_fail("参数不完整", "请至少勾选一种席别", auto)
        if not lc.get("passenger_names"):
            if auto:
                # 无人值守无法确认"使用默认乘车人"，fail closed：跳过本次开抢
                return self._validate_fail("未选乘车人", "未勾选乘车人，自动开抢已跳过", auto)
            if not messagebox.askyesno("未选乘车人", "未勾选乘车人，将使用账号默认乘车人，继续？", parent=self._mp):
                return False
        return True

    def _validate_fail(self, title, msg, auto):
        """校验失败提示：手动弹模态框；自动只 bell+日志（无人值守弹模态会冻住主线程）。"""
        if auto:
            # Task 74d：同一种失败原因每个 armed 会话只 bell+日志一次
            # （否则 500ms 一次蜂鸣/日志刷屏）；失败原因变化时重新提示，
            # 保证用户修正第一个问题后，第二个失败原因仍然可见。校验通过后重置。
            if msg != getattr(self, "_auto_vfail_last_msg", None):
                self._top.bell()
                self._auto_vfail_last_msg = msg
                self._put_log("[自动开抢] 校验失败：%s（修正配置后将自动重试）" % msg)
            return False
        messagebox.showwarning(title, msg, parent=self._mp)
        return False

    def toggle_grab(self):
        if self.grabber and self.grabber.is_alive():
            self.stop_grab()
        else:
            self.start_grab()

    def start_grab(self, auto=False):
        if self.grabber and self.grabber.is_alive():
            return True  # 已在运行：视为已触发，避免 _tick 反复调用
        # 先同步界面到内存做校验，校验通过才落盘：失败时不写 grab_tasks.json，
        # 否则 _tick 每 500ms 重试一次就全量重写一次磁盘文件
        self._ui_to_lc(save=False)
        if not self._validate(auto=auto):
            return False
        self._save_cfg(self.lc)
        if auto:
            # Task 55b：_validate() 通过之后才置位；失败时保持 False，
            # 下个 _tick 会重试（之前提前置位会缴械整点自动开抢且无重试）。
            self.auto_fired = True
            self._auto_vfail_last_msg = None
        try:
            self.grabber = Grabber(dict(self.lc), logq=self.logq)
            self.grabber.start()
        except Exception as e:
            # 线程启动失败（极罕见）：回滚置位，允许下个 _tick 重试
            self.grabber = None
            if auto:
                self.auto_fired = False
            self._put_log("[启动] 抢票线程启动失败：%s" % e)
            return False
        self._set_status("running", "抢票中…（后台运行，无窗口）")
        self.go_btn.configure(text="停 止", bg="#57606a")
        self._put_log("[启动] 开始抢票：%s → %s %s，车次 %s" % (
            self.lc.get("from"), self.lc.get("to"), self.lc.get("date"),
            "、".join(_display_trains(self.lc.get("trains"))) or "全部"))
        return True

    def stop_grab(self):
        if self.grabber:
            self.grabber.stop()
            self._put_log("[停止] 已请求停止抢票")
            self._set_status("idle", "已请求停止，等待当前操作结束…")
            self.go_btn.configure(state="disabled")

    # ---- 会话 / 定位 / 更新 ----

    def open_new_monitor_task(self):
        """打开「新建监控任务」对话框（任务写入监控系统的任务表）。"""
        NewMonitorTaskDialog(self)

    def _test_session(self):
        self._put_log("[会话] 正在校验登录状态…")

        def worker():
            try:
                ok, who = browser_order.check_session()
                self.logq.put("[会话] 校验结果：%s（%s）" % ("已登录" if ok else "未登录", who))
            except Exception as e:
                # 异常也走本窗口的实例 logq（经 _drain 显示在本窗口日志区），
                # 不进管理器全局 LOGQ
                self.logq.put("[错误] 会话校验异常：%s" % e)
        threading.Thread(target=worker, daemon=True).start()

    def _locate(self):
        self._put_log("[运行] 正在定位当前 IP 位置…")

        def worker():
            self.logq.put("[提醒] 当前位置：%s" % ip_locate())
        threading.Thread(target=worker, daemon=True).start()

    def _check_update_async(self):
        def worker():
            try:
                res = check_update(self.lc)
            except Exception:
                return
            self.update_info = (res[1], res[2], res[3]) if res[0] else None
            try:
                self.after(0, self._show_update)
            except RuntimeError:
                pass  # 窗口已关闭，忽略
        threading.Thread(target=worker, daemon=True).start()

    def _show_update(self):
        if self.update_info:
            ver, notes, url = self.update_info
            self.update_lbl.configure(text="新版本 v%s 可用，点击查看" % ver)
            self.update_lbl.bind("<Button-1>", lambda e: messagebox.showinfo(
                "版本更新", "新版本：v%s\n\n%s\n\n下载地址：%s" % (ver, notes or "（无说明）", url or "（未提供）"), parent=self._mp))

    # ---- 预设 ----

    def _current_preset(self):
        sel = self.preset_cb.get().strip()
        for p in self.lc.get("presets") or []:
            if (p.get("name") or "") == sel:
                return p
        return None

    def _load_preset(self, _event=None):
        p = self._current_preset()
        if not p:
            return
        self.from_ent.set(p.get("from") or "")
        self.to_ent.set(p.get("to") or "")
        self.trains_var.set(",".join(_display_trains(p.get("trains"))))
        for s, v in self.seat_vars.items():
            v.set(s in (p.get("seat_types") or []))
        self.seat_pri_var.set(str(p.get("seat_priority") or ""))
        self.date_var.set(p.get("date") or "")
        self._put_log("[运行] 已载入常用行程「%s」" % p.get("name"))

    def _save_preset(self):
        self._ui_to_lc()
        name = simpledialog.askstring("存为预设", "给这个常用行程起个名字：", parent=self._mp,
                                      initialvalue=self.preset_cb.get())
        if not name:
            return
        entry = {
            "name": name.strip(),
            "from": self.lc.get("from"),
            "to": self.lc.get("to"),
            "trains": list(self.lc.get("trains") or []),
            "seat_types": list(self.lc.get("seat_types") or []),
            "seat_priority": self.lc.get("seat_priority") or "",
            "date": self.lc.get("date") or "",
        }
        presets = [p for p in (self.lc.get("presets") or []) if (p.get("name") or "") != entry["name"]]
        presets.append(entry)
        self.lc["presets"] = presets
        self._save_cfg(self.lc)
        self.preset_cb.configure(values=[p["name"] for p in presets])
        self.preset_cb.set(entry["name"])
        self._put_log("[运行] 常用行程「%s」已保存" % entry["name"])

    def _delete_preset(self):
        p = self._current_preset()
        if not p:
            messagebox.showinfo("删除预设", "请先在下拉框里选中要删除的行程", parent=self._mp)
            return
        if messagebox.askyesno("删除预设", "确定删除常用行程「%s」？" % p.get("name"), parent=self._mp):
            self.lc["presets"] = [x for x in (self.lc.get("presets") or []) if x is not p]
            self._save_cfg(self.lc)
            self.preset_cb.configure(values=[x.get("name") or "" for x in self.lc["presets"]])
            self.preset_cb.set("")
            self._put_log("[运行] 常用行程「%s」已删除" % p.get("name"))

    # ---- 乘车人编辑 ----

    def _edit_pax(self):
        dlg = PassengerDialog(self, on_saved=self._pax_saved_by_dialog)

    def _pax_saved_by_dialog(self):
        self._refresh_pax()
        try:
            self._pax_mtime = os.path.getmtime(os.path.join(HERE, "passengers.json"))
        except OSError:
            pass

    # ---- 收尾 ----

    def _on_start_change(self, *_):
        st = parse_dt(self.start_var.get())
        self.armed = bool(st and st > datetime.now())
        self.auto_fired = False
        self._auto_vfail_last_msg = None
        self.reminded = False

    def _on_close(self):
        if self.grabber:
            self.grabber.stop()
            # 等收尾（WarmSession 关浏览器、释放 profile 锁），避免留下孤儿浏览器
            try:
                self.grabber.join(timeout=15)
            except Exception:
                pass
        try:
            self._ui_to_lc()
        except Exception:
            pass
        if self._standalone:
            self._top.destroy()
        else:
            self.destroy()


class NewMonitorTaskDialog(tk.Toplevel):
    """新建监控任务（从监控系统搬来的「直接监视」模块）：
    填 出发/到达/日期/车次/席别/乘车人 → 写入监控系统的任务表。
    监控软件运行中每 2 秒刷新任务列表；引擎运行时新任务自动纳入调度。"""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("新建监控任务")
        self.geometry("500x780")
        self.resizable(False, False)
        self.seat_vars = {}
        self.pax_vars = {}
        try:
            self.name2code, self.code2name = ticket.load_station_map()
        except Exception as e:
            messagebox.showerror("错误", "车站代码表加载失败：%s" % e, parent=self)
            self.destroy()
            return
        self._build()
        self.transient(app.winfo_toplevel())
        self.grab_set()

    def _mp_seat_pri_change(self, *_a):
        """监控对话框里「首选席别」输入框的即时解析反馈（含 车次=席别 规则）。"""
        checked = [s for s, v in self.seat_vars.items() if v.get()]
        trains = [t.strip().upper() for t in
                  re.split(r"[,，\s]+", self.trains_var.get()) if t.strip()]
        text, bad = ticket.seat_priority_feedback(self.seat_pri_var.get(),
                                                  checked=checked, trains=trains)
        self.seat_pri_hint.configure(text=text,
                                     foreground="#cf222e" if bad else "#6e7781")

    def _build(self):
        # 行程
        tf = ttk.LabelFrame(self, text=" 行程 ", padding=8)
        tf.pack(fill="x", padx=10, pady=(10, 4))
        r0 = ttk.Frame(tf)
        r0.pack(fill="x")
        ttk.Label(r0, text="出发").pack(side="left")
        hist = self.app.lc.get("station_history") or []
        self.from_cb = ttk.Combobox(r0, values=hist, width=12)
        self.from_cb.pack(side="left", padx=4)
        self.from_cb.set(self.app.from_ent.get())
        ttk.Label(r0, text="到达").pack(side="left", padx=(12, 0))
        self.to_cb = ttk.Combobox(r0, values=hist, width=12)
        self.to_cb.pack(side="left", padx=4)
        self.to_cb.set(self.app.to_ent.get())
        ttk.Button(r0, text="从主界面带入", command=self._import_from_main).pack(side="right")

        r1 = ttk.Frame(tf)
        r1.pack(fill="x", pady=(8, 0))
        ttk.Label(r1, text="日期").pack(side="left")
        self.date_var = tk.StringVar(value=self.app.date_var.get())
        ttk.Entry(r1, textvariable=self.date_var, width=24).pack(side="left", padx=4)
        ttk.Label(r1, text="车次").pack(side="left", padx=(10, 0))
        self.trains_var = tk.StringVar(value=self.app.trains_var.get())
        ttk.Entry(r1, textvariable=self.trains_var).pack(side="left", padx=4, fill="x", expand=True)
        ttk.Label(tf, text="日期：单日 2026-10-06；范围 2026-10-06~2026-10-08（跨度≤5天）。车次逗号分隔，留空=全部车次",
                  foreground="#6e7781").pack(anchor="w", pady=(4, 0))

        # 票种
        pf = ttk.LabelFrame(self, text=" 票种 ", padding=8)
        pf.pack(fill="x", padx=10, pady=4)
        self.purpose_var = tk.StringVar(value="ADULT")
        ttk.Radiobutton(pf, text="成人票", value="ADULT",
                        variable=self.purpose_var).pack(side="left", padx=(0, 16))
        ttk.Radiobutton(pf, text="学生票", value="0X00",
                        variable=self.purpose_var).pack(side="left")

        # 席别
        sf = ttk.LabelFrame(self, text=" 监控席别（至少选一个） ", padding=8)
        sf.pack(fill="x", padx=10, pady=4)
        pro = ttk.Frame(sf)
        pro.grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(pro, text="首选席别").pack(side="left")
        self.seat_pri_var = tk.StringVar(value=str(self.app.lc.get("seat_priority") or ""))
        ttk.Entry(pro, textvariable=self.seat_pri_var, width=18).pack(side="left", padx=4)
        ttk.Label(pro, text="逗号分隔，先填的先抢；可指定车次：K1969=硬卧（只作用该趟车）；"
                          "无座=硬座，认不出按硬座",
                  foreground="#6e7781").pack(side="left")
        self.seat_pri_hint = ttk.Label(sf, foreground="#6e7781")
        self.seat_pri_hint.grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 4))
        self.seat_pri_var.trace_add("write", self._mp_seat_pri_change)
        for i, s in enumerate(MONITOR_SEAT_CHOICES):
            v = tk.BooleanVar(value=s in (self.app.lc.get("seat_types") or []))
            self.seat_vars[s] = v
            ttk.Checkbutton(sf, text=s, variable=v).grid(
                row=i // 3 + 2, column=i % 3, sticky="w", padx=(0, 8))
        self._mp_seat_pri_change()

        # 乘车人
        gf = ttk.LabelFrame(self, text=" 乘车人（不选=下单时用默认乘车人） ", padding=8)
        gf.pack(fill="x", padx=10, pady=4)
        sel = {n for n, v in self.app.pax_vars.items() if v.get()}
        try:
            plist = passengers_mod.load_passengers()
        except Exception:
            plist = []
        self.pax_frame = ttk.Frame(gf)
        self.pax_frame.pack(fill="x")
        if not plist:
            ttk.Label(self.pax_frame, text="（乘客库为空，可在启动器主界面添加）",
                      foreground="#6e7781").pack(side="left")
        for p in plist:
            name = p.get("name") or ""
            v = tk.BooleanVar(value=name in sel)
            self.pax_vars[name] = v
            ttk.Checkbutton(self.pax_frame, text=name, variable=v).pack(
                side="left", padx=(0, 12))

        # 选项
        of = ttk.LabelFrame(self, text=" 选项 ", padding=8)
        of.pack(fill="x", padx=10, pady=4)
        r2 = ttk.Frame(of)
        r2.pack(fill="x")
        ttk.Label(r2, text="优先级").pack(side="left")
        self.prio_var = tk.IntVar(value=5)
        ttk.Spinbox(r2, from_=1, to=10, textvariable=self.prio_var,
                    width=5).pack(side="left", padx=4)
        ttk.Label(r2, text="（1-10，数字越大越先抢）",
                  foreground="#6e7781").pack(side="left")
        self.auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(of, text="余票命中后自动下单（不支付）",
                        variable=self.auto_var).pack(anchor="w")
        self.stop_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(of, text="一次下单成功后自动停止本任务",
                        variable=self.stop_var).pack(anchor="w")

        # 按钮
        bf = ttk.Frame(self)
        bf.pack(fill="x", padx=10, pady=(8, 10))
        ttk.Button(bf, text="创建并开始监视",
                   command=lambda: self._create(True)).pack(
            side="left", fill="x", expand=True, padx=(0, 4))
        ttk.Button(bf, text="仅创建任务",
                   command=lambda: self._create(False)).pack(
            side="left", fill="x", expand=True, padx=(4, 0))

    def _import_from_main(self):
        """把启动器主界面的行程/车次/日期/席别带入对话框。"""
        self.from_cb.set(self.app.from_ent.get())
        self.to_cb.set(self.app.to_ent.get())
        self.trains_var.set(self.app.trains_var.get())
        self.date_var.set(self.app.date_var.get())
        for s, v in self.seat_vars.items():
            v.set(s in (self.app.lc.get("seat_types") or []))

    def _parse_dates(self):
        raw = self.date_var.get().strip()
        if "~" in raw:
            a, b = [x.strip() for x in raw.split("~", 1)]
            d0 = datetime.strptime(a, "%Y-%m-%d")
            d1 = datetime.strptime(b, "%Y-%m-%d")
            if d1 < d0:
                raise ValueError("结束日期不能早于开始日期")
            if (d1 - d0).days > 5:
                raise ValueError("日期跨度最多相差 5 天")
            return [], [d0.strftime("%Y-%m-%d"), d1.strftime("%Y-%m-%d")]
        return [datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")], []

    def _create(self, start_now):
        from_name, to_name = self.from_cb.get().strip(), self.to_cb.get().strip()
        if from_name not in self.name2code or to_name not in self.name2code:
            messagebox.showwarning("提示", "出发站/到达站无效，请选择下拉里的有效站名", parent=self)
            return
        if from_name == to_name:
            messagebox.showwarning("提示", "出发站与到达站不能相同", parent=self)
            return
        try:
            dates, date_range = self._parse_dates()
        except ValueError as e:
            messagebox.showwarning("提示", "日期格式错误：%s" % e, parent=self)
            return
        seats = [s for s, v in self.seat_vars.items() if v.get()]
        if not seats:
            messagebox.showwarning("提示", "请至少选择一个席别", parent=self)
            return
        if self.auto_var.get():
            # 席别区会按车次余票补出「一等卧/二等卧/高级动卧」这类不支持下单的席别名，
            # 开着自动下单又带上它们 → engine 每轮都以「未知席别」失败刷日志，任务永远抢不到。
            # 与抢票路径同一条规则（见 Grabber._run）：开自动下单时剔除并说明。
            bad = [s for s in seats if s not in ticket.SEAT_NAME_TO_CODE]
            if bad:
                seats = [s for s in seats if s in ticket.SEAT_NAME_TO_CODE]
                self.app._put_log("[提醒] 席别 %s 暂不支持自动下单，已从该监控任务移除" % "、".join(bad))
            if not seats:
                messagebox.showwarning(
                    "提示", "勾选的席别暂不支持自动下单。\n"
                    "可改选其他席别，或关掉「自动下单」只做有票提醒。", parent=self)
                return
        trains = [t.strip().upper() for t in
                  re.split(r"[,，\s]+", self.trains_var.get()) if t.strip()]
        passengers = [n for n, v in self.pax_vars.items() if v.get()]
        pri_raw = self.seat_pri_var.get().strip()
        task = build_monitor_task(from_name, to_name, dates, date_range, trains,
                                  seats, passengers, self.purpose_var.get(),
                                  self.auto_var.get(), self.stop_var.get(),
                                  self.prio_var.get(),
                                  pax_purpose=self.app.lc.get("pax_purpose") or {},
                                  seat_priority=pri_raw)
        if pri_raw:
            parsed = ticket.seat_rules_parse(pri_raw)
            summary = []
            for tr, ss in parsed["rules"].items():
                summary.append("%s=%s" % (tr, "/".join(ss)))
            if parsed["bare"]:
                summary.append("其余车次：%s" % "/".join(parsed["bare"]))
            for warn in parsed["warnings"]:
                self.app._put_log("[提醒] 首选席别：%s" % warn)
            self.app._put_log("[运行] 监控任务席别规则：%s" % "；".join(summary))
        try:
            name = append_monitor_task(task, start_now)
        except Exception as e:
            messagebox.showerror("创建失败", "写入监控系统配置失败：%s" % e, parent=self)
            return
        self.app._put_log("[运行] 已创建监控任务「%s」%s" % (
            name, "，监控中" if start_now else "，未启动"))
        self.destroy()
        if start_now:
            messagebox.showinfo(
                "完成",
                "任务「%s」已创建并开始监视。\n\n监控软件运行中会自动加载"
                "（约 2 秒），引擎运行时立即自动开抢；\n若监控软件未开启，"
                "下次启动后该任务已在监控中。" % name)
        else:
            messagebox.showinfo(
                "完成",
                "任务「%s」已创建。\n\n在监控软件里勾选「启动」后开始监视。" % name)


class PassengerDialog(tk.Toplevel):
    """乘车人新增/编辑。证件号/手机号经 passengers.py 加密落盘，界面只回显明文输入。"""

    def __init__(self, master, on_saved=None):
        super().__init__(master)
        self.master = master
        self.on_saved = on_saved
        self.title("乘车人信息")
        self.geometry("380x330")
        self.resizable(False, False)
        self.transient(master.winfo_toplevel())
        self.grab_set()
        try:
            self.plist = passengers_mod.load_passengers()
        except Exception as e:
            self.plist = []
            messagebox.showwarning("读取失败", "乘车人数据读取失败：%s" % e, parent=self)

        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)

        r0 = ttk.Frame(f)
        r0.pack(fill="x")
        ttk.Label(r0, text="选择").pack(side="left")
        self.pick = ttk.Combobox(r0, state="readonly", width=26,
                                 values=[p.get("name") or "" for p in self.plist])
        self.pick.pack(side="left", padx=6)
        self.pick.bind("<<ComboboxSelected>>", self._load)
        ttk.Button(r0, text="删除此人", command=self._delete).pack(side="left", padx=6)

        self.name_var = tk.StringVar()
        self.id_var = tk.StringVar()
        self.mob_var = tk.StringVar()
        self.type_var = tk.StringVar(value="二代身份证")
        self.adult_var = tk.BooleanVar(value=True)
        self.default_var = tk.BooleanVar(value=False)

        # 表单区单独一个容器：f 里已经 pack 了 r0，同一个父容器不能再混用 grid
        # （否则报 "cannot use geometry manager grid inside ... already has slaves
        #   managed by pack"，整个弹窗直接崩掉）。
        g = ttk.Frame(f)
        g.pack(fill="both", expand=True)
        self.id_entry = ttk.Entry(g, textvariable=self.id_var, width=26, show="*")
        grid = [
            ("姓名", ttk.Entry(g, textvariable=self.name_var, width=26)),
            ("证件类型", ttk.Combobox(g, textvariable=self.type_var, width=23,
                                      values=list(ID_TYPES.keys()), state="readonly")),
            ("证件号", self.id_entry),
            ("手机号", ttk.Entry(g, textvariable=self.mob_var, width=26)),
        ]
        for i, (lab, w) in enumerate(grid):
            ttk.Label(g, text=lab).grid(row=i + 1, column=0, sticky="e", pady=4)
            w.grid(row=i + 1, column=1, sticky="w", padx=6, pady=4)
        # 小眼睛：证件号默认掩码，点一下切明文、再点还原
        self._id_visible = False
        self.eye_btn = ttk.Button(g, text="👁", width=3, command=self._toggle_id_show)
        self.eye_btn.grid(row=3, column=2, sticky="w", padx=(0, 2))
        ttk.Checkbutton(g, text="成人票", variable=self.adult_var).grid(row=5, column=1, sticky="w", pady=2)
        ttk.Checkbutton(g, text="设为默认乘车人", variable=self.default_var).grid(row=6, column=1, sticky="w")

        bf = ttk.Frame(g)
        bf.grid(row=7, column=0, columnspan=2, pady=(10, 0))
        ttk.Button(bf, text="保 存", command=self._save).pack(side="left", padx=6)
        ttk.Button(bf, text="关 闭", command=self.destroy).pack(side="left", padx=6)

        if self.plist:
            self.pick.current(0)
            self._load()

    def _toggle_id_show(self):
        self._id_visible = not self._id_visible
        self.id_entry.configure(show="" if self._id_visible else "*")
        self.eye_btn.configure(text="🙈" if self._id_visible else "👁")

    def _load(self, _event=None):
        name = self.pick.get().strip()
        for p in self.plist:
            if (p.get("name") or "") == name:
                self.name_var.set(p.get("name") or "")
                self.id_var.set(p.get("id_no") or "")
                self.mob_var.set(p.get("mobile") or "")
                for lab, code in ID_TYPES.items():
                    if code == str(p.get("id_type_code")):
                        self.type_var.set(lab)
                self.adult_var.set(bool(p.get("is_adult", True)))
                self.default_var.set(bool(p.get("is_default")))
                return

    def _delete(self):
        name = self.pick.get().strip()
        if not name:
            return
        if not messagebox.askyesno("删除乘车人", "确定删除「%s」？" % name, parent=self):
            return
        self.plist = [p for p in self.plist if (p.get("name") or "") != name]
        try:
            # Task 84d：拒写（False）弹 error，不刷新列表、不显示成功
            if not _save_passengers_or_warn(self.plist, self):
                return
        except Exception as e:
            messagebox.showerror("保存失败", str(e), parent=self)
            return
        self.pick.configure(values=[p.get("name") or "" for p in self.plist])
        self.pick.set("")
        self._notify_saved()

    def _save(self):
        name = self.name_var.get().strip()
        id_no = self.id_var.get().strip()
        if not name or not id_no:
            messagebox.showwarning("信息不完整", "姓名和证件号不能为空", parent=self)
            return
        if self.default_var.get():
            for p in self.plist:
                p["is_default"] = False
        rec = {
            "name": name,
            "id_type_code": ID_TYPES.get(self.type_var.get(), "1"),
            "id_no": id_no,
            "mobile": self.mob_var.get().strip(),
            "is_default": bool(self.default_var.get()),
            "is_adult": bool(self.adult_var.get()),
        }
        replaced = False
        for i, p in enumerate(self.plist):
            if (p.get("name") or "") == name:
                self.plist[i] = rec
                replaced = True
                break
        if not replaced:
            self.plist.append(rec)
        try:
            # Task 84d：拒写（False）弹 error，不显示"已保存"
            if not _save_passengers_or_warn(self.plist, self):
                return
        except Exception as e:
            messagebox.showerror("保存失败", "加密保存失败：%s" % e, parent=self)
            return
        self.pick.configure(values=[p.get("name") or "" for p in self.plist])
        self.pick.set(name)
        self._notify_saved()
        messagebox.showinfo("已保存", "乘车人「%s」已加密保存" % name, parent=self)

    def _notify_saved(self):
        if self.on_saved:
            try:
                self.on_saved()
            except Exception:
                pass


def ensure_passengers():
    """首次运行：passengers.json 不存在时，把 config.json 任务里的乘车人名字
    迁入加密乘客库（证件号留空，待用户在「新增/编辑」里补全）。"""
    if os.path.exists(os.path.join(HERE, "passengers.json")):
        return
    names = []
    try:
        with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for t in (cfg.get("tasks") or []):
            for n in (t.get("passenger_names") or []):
                n = str(n).strip()
                if n and n not in names:
                    names.append(n)
    except Exception:
        pass
    if not names:
        return
    try:
        ok = passengers_mod.save_passengers([
            {"name": n, "id_type_code": "1", "id_no": "", "mobile": "",
             "is_default": i == 0, "is_adult": True}
            for i, n in enumerate(names)])
    except Exception as e:
        log("[错误] 乘车人导入失败：%s" % e)
        return
    # Task 84d：拒写（False）不得记"已导入"成功日志；Task 84c：姓名打码
    if not ok:
        log("[错误] 乘车人导入被拒：磁盘已有数据在本机不可解密，已保护原数据（本次未导入）")
    else:
        log("[提醒] 已从监控任务导入乘车人：%s（证件号请点「新增/编辑」补全）"
            % _mask_names(names))


class GrabTaskWindow(tk.Toplevel):
    """单个抢票任务的独立窗口：内嵌一套完整的 LauncherApp 抢票界面。

    各任务配置 / 日志 / 状态完全隔离，多窗口可同时并行抢票；
    关闭窗口 = 停止该任务抢票并保存配置。"""

    def __init__(self, manager, task):
        super().__init__(manager.winfo_toplevel())
        self.manager = manager
        self.task = task
        self.title("抢票任务 · %s" % (task.get("name") or "未命名"))
        self.app = LauncherApp(self, task=task, on_config_saved=self._save_task)
        self.app.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # 尺寸按内容实测，居中偏上放置，避免遮住任务列表
        self.update_idletasks()
        w, h = 860, min(760, self.winfo_screenheight() - 60)
        x = max(0, (self.winfo_screenwidth() - w) // 2)
        y = max(0, (self.winfo_screenheight() - h) // 3)
        self.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self.minsize(780, 620)
        self.lift()
        self.focus_set()

    def _save_task(self, lc):
        """保存回调：界面配置写回任务库，并刷新管理器列表。"""
        lc["id"] = self.task.get("id")
        lc["name"] = self.task.get("name") or lc.get("name") or "未命名"
        lc["status"] = lc.get("status") or "idle"
        self.task.update(lc)
        self.manager._upsert_task(lc)

    def _on_close(self):
        try:
            if self.app.grabber:
                self.app.stop_grab()
                # 等抢票线程收尾（关浏览器、写结果），最多 15 秒：
                # 直接销毁窗口会把中途的订单/浏览器晾在后台
                try:
                    self.app.grabber.join(timeout=15)
                except Exception:
                    pass
            # 抢到的任务保留"已抢到"状态，别在关窗时被抹成"就绪"
            if self.app.lc.get("status") != "ok":
                self.app.lc["status"] = "idle"
            self._save_task(self.app.lc)
        finally:
            self.manager._on_window_closed(self.task.get("id"))
            self.destroy()


class TaskManagerPanel(tk.Frame):
    """抢票多任务管理器：任务列表 + 每任务独立窗口（可并行抢票）+ 全局日志。

    master 为空 = 独立窗口（launcher.py 直接运行）；
    master 给定时 = 嵌入宿主界面（监控系统侧边栏「抢票任务」页）。"""

    STATUS_LABELS = {
        "idle": ("就绪", "#6e7781"),
        "running": ("抢票中", "#d97706"),
        "ok": ("已抢到", "#1a7f37"),
        "stopped": ("已停止", "#6e7781"),
    }

    def __init__(self, master=None):
        self._standalone = master is None
        if self._standalone:
            master = tk.Tk()
            install_tick_indicator(ttk.Style(master))
        self._top = master if self._standalone else master.winfo_toplevel()
        super().__init__(master)
        self._mp = self._top
        self.tasks = load_grab_tasks()
        self._windows = {}       # task_id -> GrabTaskWindow
        self._row_widgets = {}   # task_id -> (row_frame, status_lbl)
        self._build()
        self._refresh_list()
        if self._standalone:
            self._top.title("12306 抢票任务管理 v%s" % __version__)
            self._top.minsize(760, 520)
            self.pack(fill="both", expand=True)
            self.update_idletasks()
            win_h = min(max(560, self.winfo_reqheight()), self.winfo_screenheight() - 60)
            win_x = max(0, (self.winfo_screenwidth() - 820) // 2)
            self._top.geometry("820x%d+%d+%d" % (win_h, win_x, 40))
            self._top.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self._drain)
        self.after(800, self._tick)

    # ---- 界面 ----

    def _build(self):
        self.grid_columnconfigure(0, weight=1)

        top = ttk.Frame(self, padding=(12, 10))
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="抢票任务管理", font=(FONT[0], 15, "bold")).pack(side="left")
        ttk.Label(top, text="每个任务独立窗口、独立配置，可同时抢多个车次/日期",
                  foreground="#6e7781").pack(side="left", padx=(10, 0))
        ttk.Button(top, text="＋ 新建任务", command=self._new_task).pack(side="right")
        ttk.Button(top, text="测试会话", command=self._test_session).pack(side="right", padx=(0, 8))

        lf = ttk.LabelFrame(self, text=" 任务列表 ", padding=8)
        lf.grid(row=1, column=0, sticky="ew", padx=12, pady=(6, 0))
        self.list_box = ttk.Frame(lf)
        self.list_box.pack(fill="x")

        lg = ttk.LabelFrame(self, text=" 系统日志（全局：登录 / 下单细节 / 通用消息） ", padding=4)
        lg.grid(row=2, column=0, sticky="nsew", padx=12, pady=(8, 12))
        self.grid_rowconfigure(2, weight=1)
        txt = tk.Text(lg, height=3, wrap="word", state="disabled", font=(FONT[0], 9))
        sb = ttk.Scrollbar(lg, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)
        self.log_text = txt
        for tag, color in LOG_COLOR.items():
            txt.tag_configure(tag, foreground=color)

    # ---- 任务列表 ----

    def _refresh_list(self):
        for w in self.list_box.winfo_children():
            w.destroy()
        self._row_widgets.clear()
        if not self.tasks:
            ttk.Label(self.list_box, text="还没有任务，点右上角「＋ 新建任务」创建第一个抢票任务",
                      foreground="#6e7781").pack(pady=10)
            return
        for t in self.tasks:
            self._add_row(t)

    def _add_row(self, task):
        tid = task.get("id")
        row = ttk.Frame(self.list_box)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=task.get("name") or "未命名",
                  font=(FONT[0], 10, "bold"), width=12, anchor="w").pack(side="left")
        bits = ["%s → %s" % (task.get("from") or "?", task.get("to") or "?"),
                (task.get("date") or "?") + ("~%s" % task["date_to"] if task.get("date_to") else "")]
        if task.get("trains"):
            _dtr = _display_trains(task.get("trains"))
            if _dtr:
                bits.append("/".join(_dtr))
        ttk.Label(row, text=" · ".join(bits), foreground="#57606a").pack(
            side="left", padx=(6, 0), fill="x", expand=True)
        label, color = self.STATUS_LABELS.get(task.get("status") or "idle", ("就绪", "#6e7781"))
        status_lbl = ttk.Label(row, text=label, foreground=color, width=8, anchor="center")
        status_lbl.pack(side="left", padx=6)
        ttk.Button(row, text="打开", width=6,
                   command=lambda t=task: self._open_task(t.get("id"))).pack(side="left", padx=2)
        ttk.Button(row, text="改名", width=6,
                   command=lambda t=task: self._rename_task(t.get("id"))).pack(side="left", padx=2)
        ttk.Button(row, text="删除", width=6,
                   command=lambda t=task: self._delete_task(t.get("id"))).pack(side="left", padx=2)
        self._row_widgets[tid] = (row, status_lbl)

    # ---- 任务操作 ----

    def _find(self, tid):
        for t in self.tasks:
            if t.get("id") == tid:
                return t
        return None

    def _new_task(self):
        # 名字序号取现存最大号 +1：删除任务后新建不再出现重名（两个"任务 2"）
        seqs = []
        for t in self.tasks:
            m = re.match(r"任务\s*(\d+)$", (t.get("name") or "").strip())
            if m:
                seqs.append(int(m.group(1)))
        task = new_grab_task((max(seqs) if seqs else 0) + 1)
        self.tasks.append(task)
        save_grab_tasks(self.tasks)
        self._refresh_list()
        self._open_task(task["id"])
        self._put_log("[任务] 已创建「%s」" % task["name"])

    def _open_task(self, tid):
        w = self._windows.get(tid)
        if w and w.winfo_exists():
            w.deiconify()
            w.lift()
            w.focus_set()
            return
        task = self._find(tid)
        if task is None:
            return
        self._windows[tid] = GrabTaskWindow(self, task)

    def _rename_task(self, tid):
        task = self._find(tid)
        if task is None:
            return
        name = simpledialog.askstring("重命名任务", "新的任务名：", parent=self._mp,
                                      initialvalue=task.get("name") or "")
        if not name:
            return
        task["name"] = name.strip() or task.get("name")
        save_grab_tasks(self.tasks)
        w = self._windows.get(tid)
        if w and w.winfo_exists():
            w.title("抢票任务 · %s" % task["name"])
            w.app.task_name = task["name"]
        self._refresh_list()
        self._put_log("[任务] 已重命名为「%s」" % task["name"])

    def _delete_task(self, tid):
        task = self._find(tid)
        if task is None:
            return
        if not messagebox.askyesno("删除任务", "确定删除抢票任务「%s」？" % (task.get("name") or "未命名"),
                                   parent=self._mp):
            return
        w = self._windows.pop(tid, None)
        if w and w.winfo_exists():
            try:
                # 必须走窗口自己的 _on_close（停抢票线程 -> join -> 保存 -> 销毁）；
                # 直接 destroy 会把运行中的抢票线程晾在后台，可能为已删任务下单
                w._on_close()
            except Exception as e:
                self._put_log("[警告] 删除任务时关闭窗口异常：%s" % e)
        self.tasks = [t for t in self.tasks if t.get("id") != tid]
        save_grab_tasks(self.tasks)
        self._refresh_list()
        self._put_log("[任务] 已删除「%s」" % task.get("name"))

    def _upsert_task(self, lc):
        """任务窗口保存回调：按 id 写回任务库并刷新列表。"""
        tid = lc.get("id")
        for i, t in enumerate(self.tasks):
            if t.get("id") == tid:
                self.tasks[i] = lc
                break
        else:
            self.tasks.append(lc)
        save_grab_tasks(self.tasks)
        self._refresh_list()

    def _on_window_closed(self, tid):
        self._windows.pop(tid, None)
        for t in self.tasks:
            if t.get("id") == tid:
                # 抢到的任务保留"已抢到"状态（与 GrabTaskWindow._on_close 同口径），
                # 别在关窗时被抹成"就绪"
                if t.get("status") != "ok":
                    t["status"] = "idle"
        save_grab_tasks(self.tasks)
        self._refresh_list()
        self._put_log("[任务] 「%s」窗口已关闭（已停止抢票并保存配置）"
                      % (next((t.get("name") for t in self.tasks if t.get("id") == tid), "?")))

    # ---- 定时刷新 / 日志 ----

    def _tick(self):
        try:
            for tid, (row, status_lbl) in list(self._row_widgets.items()):
                w = self._windows.get(tid)
                if w and w.winfo_exists() and w.app.grabber and w.app.grabber.is_alive():
                    label, color = "抢票中", "#d97706"
                else:
                    t = self._find(tid)
                    label, color = self.STATUS_LABELS.get((t or {}).get("status") or "idle",
                                                          ("就绪", "#6e7781"))
                status_lbl.configure(text=label, foreground=color)
        finally:
            try:
                self.after(800, self._tick)
            except tk.TclError:
                pass

    def _drain(self):
        try:
            while True:
                self._put_log(LOGQ.get_nowait())
        except queue.Empty:
            pass
        try:
            self.after(200, self._drain)
        except tk.TclError:
            pass

    def _put_log(self, line):
        txt = self.log_text
        txt.configure(state="normal")
        txt.insert("end", line + "\n")
        for tag in LOG_COLOR:
            if tag in line:
                txt.tag_add(tag, "end-2l", "end-1l")
                break
        # 行数超限裁掉头部：长跑数天不能让日志区无限膨胀
        if float(txt.index("end-1c")) > 2500.0:
            txt.delete("1.0", "1000.0")
        txt.configure(state="disabled")
        txt.see("end")

    def _test_session(self):
        def worker():
            try:
                ok, who = browser_order.check_session()
                log("[会话] 校验结果：%s（%s）" % ("已登录" if ok else "未登录", who))
            except Exception as e:
                log("[错误] 会话校验异常：%s" % e)
        threading.Thread(target=worker, daemon=True).start()
        self._put_log("[会话] 正在校验登录状态…")

    def _on_close(self):
        # 必须走各窗口自己的 _on_close（停抢票线程 -> join -> 保存 -> 销毁）；
        # 直接 destroy 会绕过收尾，浏览器/下单中途被硬杀
        for w in list(self._windows.values()):
            try:
                if w.winfo_exists():
                    w._on_close()
            except Exception:
                pass
        self._top.destroy()


def main():
    ensure_passengers()
    panel = TaskManagerPanel()
    panel._top.mainloop()


if __name__ == "__main__":
    main()

