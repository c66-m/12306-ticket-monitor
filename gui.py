# -*- coding: utf-8 -*-
"""
12306 车票监控与自动购票系统 —— 桌面版（照片风格改造，Tkinter，无第三方依赖）

界面采用参考照片的 12306 手机风格：蓝色主色、白色卡片、大字标题、每项功能单独一行。

两种创建任务方式
    1. 新建任务（查后选）—— TaskWizard 四步向导：
       ① 查询页（出发/到达/日期/交换/历史路线 → 查询车票）
       ② 车次结果页（日期栏/筛选/排序/车次卡片 + 席别点选，数据来自 12306 官方接口）
       ③ 乘车人选择页（12306 账号乘车人 + 本地加密库，勾选/全选）
       ④ 确认页（行程卡/开关项/提示条 → 创建任务并启动监控）
    2. 直接监视（免查询）—— QuickMonitorDialog：
       直接填写 日期 + 车次 + 席别 + 乘车人 → 一键创建任务并开始监视

启动
    双击「启动桌面版.bat」，或：python gui.py

合规说明
    - 余票数据来自 12306 官方公开查询接口；下单走官网同款提交流程
    - 不绕过验证码、不执行支付（订单提交成功后停留在未支付状态）
"""

import calendar
import datetime
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import appcommon
import engine as engine_mod
import filelock
import logutil
import notify as notify_mod
import order as order_mod
import passengers as passengers_mod
import ticket
import launcher

CONFIG_PATH = os.path.join(HERE, "config.json")

# 席别勾选列表：唯一定义在 ticket.py（含动卧），此处只引用
SEAT_CHOICES = list(ticket.SEAT_CHOICES)

BLUE = "#4A90E2"
BLUE_DARK = "#3D7ED9"
BLUE_LIGHT = "#E8F1FC"
ORANGE = "#FA8C16"
ORANGE_LIGHT = "#FFF4E0"
BG = "#F5F7FA"
CARD = "#FFFFFF"
TEXT = "#1F2129"
GRAY = "#8A94A6"
BORDER = "#E5E9F0"
FONT = "Microsoft YaHei UI"

STATUS_COLOR = {
    "monitoring": "#1a7f37",
    "retrying": "#d97706",
    "success": "#0969da",
    "failed": "#cf222e",
    "paused": "#9a6700",
    "cancelled": "#6e7781",
}

# ----------------------------- 日志（文件 + 界面队列） -----------------------------

LOG_QUEUE = queue.Queue()


class QueueLogHandler(logging.Handler):
    def emit(self, record):
        try:
            LOG_QUEUE.put(self.format(record))
        except Exception:
            pass


def setup_logging():
    log = logging.getLogger("monitor")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    for h in list(log.handlers):
        log.removeHandler(h)
    log_dir = os.path.join(HERE, "logs")
    try:
        # 按天滚动：长跑跨天后日志自动切到新日期的文件
        fh = logutil.DayFileHandler(log_dir, "monitor")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except Exception:
        pass
    qh = QueueLogHandler()
    qh.setFormatter(fmt)
    log.addHandler(qh)
    log.propagate = False


LOG = logging.getLogger("monitor")


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _lock_timeout_abort():
    """file_lock 锁争用超时的统一出口：记日志、给用户明确提示，再上抛友好异常。

    调用方多为 Tk 回调：弹窗已告知用户"文件被占用，稍后重试"，Tk 打印
    traceback 但界面存活——不崩；用户稍后重试该操作即可。
    """
    LOG.warning("文件锁争用超时：另一进程正长时间持有，本次操作跳过")
    messagebox.showwarning("文件被占用", "文件被占用，稍后重试")
    raise TimeoutError("文件被占用，稍后重试")


def save_config(config):
    # 原子写 + 跨进程锁：与 launcher 的读-改-写互斥（config.json.lock）
    try:
        with filelock.file_lock(CONFIG_PATH + ".lock"):
            appcommon.atomic_write_json(CONFIG_PATH, config)
    except TimeoutError:
        _lock_timeout_abort()


def load_state():
    config = load_config()
    return engine_mod.load_state_file(
        os.path.join(HERE, config.get("state_file", "state.json")))


def save_state(state):
    """原子写入 state.json（与引擎线程的原子写入相互兼容，最后写入者生效）。"""
    config = load_config()
    path = os.path.join(HERE, config.get("state_file", "state.json"))
    # 跨进程锁：与 launcher.append_monitor_task 的 state 段互斥（state.json.lock）
    try:
        with filelock.file_lock(path + ".lock"):
            appcommon.write_state(path, state, tmp_kind="guisave")
    except TimeoutError:
        _lock_timeout_abort()


def update_config_locked(mutator):
    """config.json 读-改-写原子接口：整包在 file_lock 内，与 launcher/monitor 互斥。

    mutator(config) 就地修改读到的 dict；返回其返回值。
    替代「load_config() → 改 → save_config()」的锁外读模式（双端并发改任务丢数据）。
    """
    try:
        with filelock.file_lock(CONFIG_PATH + ".lock"):
            with open(CONFIG_PATH, encoding="utf-8") as f:
                config = json.load(f)
            result = mutator(config)
            appcommon.atomic_write_json(CONFIG_PATH, config)
            return result
    except TimeoutError:
        _lock_timeout_abort()


def update_state_locked(mutator):
    """state.json 读-改-写原子接口：整包在 file_lock 内，与引擎/launcher 互斥。

    mutator(state) 就地修改读到的 dict；返回其返回值。
    路径解析沿用 save_state 的口径（config 的 state_file，默认 state.json）。
    """
    config = load_config()
    path = os.path.join(HERE, config.get("state_file", "state.json"))
    try:
        with filelock.file_lock(path + ".lock"):
            state = engine_mod.load_state_file(path)
            result = mutator(state)
            appcommon.write_state(path, state, tmp_kind="guisave")
            return result
    except TimeoutError:
        _lock_timeout_abort()


def mark_task_created(app, task, start_now):
    """新建任务后立刻写状态：勾选了立即启动 = 监控中；否则 = 已暂停（未启动）。
    引擎运行时通过共享实例写入，引擎未运行时直接原子写文件。"""
    target = "monitoring" if start_now else "paused"
    msg = "立即启动" if start_now else "新建任务，未启动"
    if app is not None and app.engine_thread and app.engine_thread.is_alive():
        app.get_ops_engine().set_task_status(task, target, msg, force=True)
    else:
        name = task.get("name") or ""

        def _mark(state):
            entry = state.setdefault("tasks", {}).setdefault(name, {})
            entry["status"] = target
            entry.setdefault("fail_streak", 0)
            entry.setdefault("last_poll", 0)
            entry["message"] = msg

        update_state_locked(_mark)


EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def format_dates(task):
    """任务的监控日期展示：单日原样显示；多日全部列出或显示区间。"""
    ds = engine_mod.expand_dates(task) or []
    if not ds:
        return "-"
    if len(ds) == 1:
        return ds[0]
    if len(ds) <= 3:
        return " / ".join(ds)
    return "{0} ~ {1}（{2}天）".format(ds[0], ds[-1], len(ds))


def remove_task_from_config(config, task):
    """从配置中删除一个任务：有 uid 按 uid 删除；无 uid 的旧任务只删第一个
    同名任务——避免重名时误删全部。"""
    uid = task.get("uid")
    name = task.get("name")
    tasks = list(config.get("tasks") or [])
    if uid and any(t.get("uid") == uid for t in tasks):
        config["tasks"] = [t for t in tasks if t.get("uid") != uid]
    else:
        for idx, t in enumerate(tasks):
            if t.get("name") == name:
                tasks.pop(idx)
                break
        config["tasks"] = tasks
    return config


def delete_task_everywhere(app, task):
    """删除任务：config 移除 + state 条目同步清除（加锁）+ 通知运行中引擎。

    只删 config 会留下三处问题：state.json 条目成孤儿；引擎内存副本在下次
    _sync_config 的 mtime 检查前幽灵监控（≤一个轮询间隔）；同名重建继承陈旧
    状态/防重记录。

    引擎运行时经 live engine 清内存状态（立即停该任务，不等 mtime 检查；
    与 set_task_status 同一跨线程约定）；引擎未运行时直接加锁改文件。
    """
    name = task.get("name") or ""
    update_config_locked(lambda config: remove_task_from_config(config, task))
    if app is not None and getattr(app, "engine_thread", None) is not None \
            and app.engine_thread.is_alive():
        try:
            app.get_ops_engine().note_task_deleted(name)
            return
        except Exception as e:
            LOG.warning("通知引擎删除任务「%s」失败，改走文件路径：%s", name, e)

    def _clear(state):
        tasks = state.get("tasks")
        if isinstance(tasks, dict):
            tasks.pop(name, None)

    update_state_locked(_clear)


def normalize_email_settings(email):
    """校验并清洗邮件配置：
    - 发件邮箱必须是完整地址，否则抛 ValueError
    - 发件人地址为空或格式不对（如只填 @qq.com）时自动回退为发件邮箱
    - 收件人逐个校验格式，非法时抛 ValueError
    返回清洗后的配置字典。"""
    user = (email.get("username") or "").strip()
    if not EMAIL_RE.match(user):
        raise ValueError("发件邮箱格式不正确：「%s」" % user)
    from_addr = (email.get("from") or "").strip()
    if not EMAIL_RE.match(from_addr):
        from_addr = user
    email["username"] = user
    email["from"] = from_addr
    to_list = [x.strip() for x in (email.get("to") or []) if x.strip()]
    bad = [x for x in to_list if not EMAIL_RE.match(x)]
    if bad:
        raise ValueError("收件人格式不正确：%s" % "、".join(bad))
    if not to_list:
        raise ValueError("请至少填写一个收件人邮箱")
    email["to"] = to_list
    return email


_RESULT_QUEUE = queue.Queue()


def run_async(widget, fn, on_done):
    """在子线程执行 fn（网络等阻塞操作），完成后经队列回到 Tk 主线程调用 on_done(result, error)。
    Tk 不允许跨线程直接调用 after，因此统一由 start_async_poller 在主线程分发。
    widget 用于分发前检查存活：对话框销毁后不再回调，避免 TclError。"""
    def worker():
        try:
            res, fn_err = fn(), None
        except Exception as e:
            res, fn_err = None, e
        _RESULT_QUEUE.put((widget, on_done, res, fn_err))

    threading.Thread(target=worker, daemon=True).start()


def start_async_poller(root):
    """主线程轮询异步结果队列，执行各回调（应用启动时调用一次）。"""
    try:
        while True:
            widget, on_done, res, fn_err = _RESULT_QUEUE.get_nowait()
            try:
                # 对话框已销毁就不回调（winfo_exists=0），避免 invalid command name
                if widget is None or widget.winfo_exists():
                    on_done(res, fn_err)
            except Exception as e2:
                LOG.exception("界面回调异常: %s", e2)
    except queue.Empty:
        pass
    try:
        root.after(50, lambda: start_async_poller(root))
    except Exception:
        pass


def apply_style(root):
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    try:
        launcher.install_tick_indicator(style)   # clam 的选中是叉，换成对勾
    except Exception:
        LOG.exception("复选框对勾样式安装失败")
    style.configure(".", font=(FONT, 10))
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG)
    style.configure("White.TFrame", background=CARD)
    style.configure("White.TLabel", background=CARD)
    style.configure("Gray.TLabel", background=BG, foreground=GRAY)
    style.configure("Blue.TFrame", background=BLUE)
    style.configure("Blue.TLabel", background=BLUE, foreground="white")
    style.configure("Title.TLabel", background=BLUE, foreground="white",
                    font=(FONT, 13, "bold"))
    style.configure("Sub.TLabel", background=BLUE, foreground="#DCEAFB", font=(FONT, 9))
    style.configure("Treeview", font=(FONT, 10), rowheight=26, background=CARD,
                    fieldbackground=CARD)
    style.configure("Treeview.Heading", font=(FONT, 10, "bold"))
    style.configure("Blue.TButton", background=BLUE, foreground="white",
                    font=(FONT, 11, "bold"), padding=(16, 8), borderwidth=0)
    style.map("Blue.TButton",
              background=[("active", BLUE_DARK), ("disabled", "#A9C6EA")])
    style.configure("Light.TButton", background=CARD, foreground=BLUE,
                    font=(FONT, 10), padding=(10, 5), borderwidth=0)
    style.map("Light.TButton", background=[("active", BLUE_LIGHT)])
    style.configure("White.TButton", background=CARD, foreground=TEXT,
                    font=(FONT, 10), padding=(10, 5))
    style.map("White.TButton", background=[("active", "#F0F2F5")])


def station_key(name):
    common = ("北京", "上海", "广州", "深圳", "杭州", "南京", "武汉",
              "成都", "重庆", "西安", "郑州", "长沙")
    for i, c in enumerate(common):
        if name.startswith(c):
            return (0, i, name)
    return (1, 0, name)


# ----------------------------- 车站选择 -----------------------------

class StationPicker(tk.Toplevel):
    def __init__(self, master, on_pick):
        super().__init__(master)
        self.title("选择车站")
        self.geometry("360x480")
        self.on_pick = on_pick
        try:
            self.name2code, self.code2name = ticket.load_station_map()
        except Exception as e:
            messagebox.showerror("错误", "车站代码表加载失败：%s" % e, parent=self)
            self.destroy()
            return
        self.all_names = sorted(self.name2code.keys(), key=station_key)
        ttk.Label(self, text="输入站名关键字（如：北京 / 虹桥 / 广州南）：").pack(
            anchor="w", padx=10, pady=(10, 2))
        self.entry = ttk.Entry(self)
        self.entry.pack(fill="x", padx=10)
        self.entry.bind("<KeyRelease>", self.on_type)
        ttk.Label(self, text="匹配结果（双击选择）：").pack(anchor="w", padx=10, pady=(8, 2))
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self.listbox = tk.Listbox(frame, font=(FONT, 11))
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.listbox.bind("<Double-Button-1>", self.choose)
        ttk.Button(self, text="选定", command=self.choose).pack(pady=(0, 10))
        self.on_type()
        self.entry.focus_set()

    def on_type(self, _evt=None):
        kw = self.entry.get().strip()
        cands = [n for n in self.all_names if kw in n][:100]
        self.listbox.delete(0, "end")
        for n in cands:
            self.listbox.insert("end", n)

    def choose(self, _evt=None):
        sel = self.listbox.curselection()
        if sel:
            self.on_pick(self.listbox.get(sel[0]))
            self.destroy()


class StationField(tk.Frame):
    """一行：标签 + 车站名输入框 + [选择] 按钮。"""

    def __init__(self, master, label, big=False):
        super().__init__(master, bg=CARD)
        tk.Label(self, text=label, bg=CARD, fg=GRAY, font=(FONT, 10),
                 width=6, anchor="w").pack(side="left")
        self.entry = tk.Entry(self, width=12, font=(FONT, 16 if big else 11, "bold"
                                                    if big else "normal"),
                              fg=TEXT, relief="flat", bg=CARD,
                              highlightthickness=1,
                              highlightbackground=BORDER,
                              highlightcolor=BLUE, justify="center")
        self.entry.pack(side="left", padx=(0, 4), ipady=4)
        tk.Button(self, text="选择", command=self.pick, font=(FONT, 9),
                  bg=BLUE, fg="white", activebackground=BLUE_DARK,
                  activeforeground="white", relief="flat", padx=10,
                  cursor="hand2").pack(side="left")

    def pick(self):
        def on_pick(name):
            self.entry.delete(0, "end")
            self.entry.insert(0, name)
        StationPicker(self, on_pick)

    def get(self):
        return self.entry.get().strip()


# ----------------------------- 日历表格式日期选择器 -----------------------------

class CalendarPicker(tk.Toplevel):
    """点击日期输入框弹出日历表，点选日期即回填。
    support_range=True 时支持两次点选（起点~终点）组成日期范围，如 2026-10-07~2026-10-09。"""

    WEEK_HEAD = ("一", "二", "三", "四", "五", "六", "日")

    def __init__(self, master, support_range=False, on_pick=None, max_span_days=None):
        super().__init__(master)
        self.title("选择日期")
        self.resizable(False, False)
        self.configure(bg=CARD)
        self.support_range = support_range
        self.on_pick = on_pick
        self.max_span_days = max_span_days  # 区间起点~终点最多相差多少天（None=不限制）
        self.start = None
        self.end = None
        today = datetime.date.today()
        self.year, self.month = today.year, today.month

        head = tk.Frame(self, bg=CARD)
        head.pack(fill="x", padx=8, pady=8)
        tk.Button(head, text="‹", command=self._shift(-1), bg=CARD, fg=BLUE,
                  font=(FONT, 12, "bold"), relief="flat", cursor="hand2",
                  width=2).pack(side="left")
        self.title_lbl = tk.Label(head, text="", bg=CARD, fg=TEXT,
                                  font=(FONT, 12, "bold"))
        self.title_lbl.pack(side="left", expand=True)
        tk.Button(head, text="›", command=self._shift(1), bg=CARD, fg=BLUE,
                  font=(FONT, 12, "bold"), relief="flat", cursor="hand2",
                  width=2).pack(side="left")
        tk.Button(head, text="今天", command=self._goto_today, bg=BLUE_LIGHT, fg=BLUE,
                  relief="flat", font=(FONT, 9), cursor="hand2").pack(side="left", padx=(8, 0))

        self.grid = tk.Frame(self, bg=CARD)
        self.grid.pack(padx=8)
        for i, w in enumerate(self.WEEK_HEAD):
            tk.Label(self.grid, text=w, bg=CARD, fg=GRAY, font=(FONT, 9),
                     width=3).grid(row=0, column=i, padx=1, pady=2)
        self.day_btns = {}
        self.status = tk.Label(self, text="", bg=CARD, fg=BLUE, font=(FONT, 9))
        self.status.pack(pady=(2, 4))

        btns = tk.Frame(self, bg=CARD)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btns, text="确定", style="Blue.TButton",
                   command=self._confirm).pack(side="right", padx=4)
        ttk.Button(btns, text="取消", style="White.TButton",
                   command=self.destroy).pack(side="right")

        self._render()

    def _shift(self, delta):
        def go():
            m = self.month + delta
            y = self.year
            if m < 1:
                y, m = y - 1, 12
            if m > 12:
                y, m = y + 1, 1
            self.year, self.month = y, m
            self._render()
        return go

    def _goto_today(self):
        t = datetime.date.today()
        self.year, self.month = t.year, t.month
        self._render()

    def _render(self):
        self.title_lbl.config(text="%d 年 %d 月" % (self.year, self.month))
        for b in self.day_btns.values():
            b.destroy()
        self.day_btns = {}
        today = datetime.date.today()
        first_weekday = datetime.date(self.year, self.month, 1).weekday()  # 0=周一
        days_in_month = calendar.monthrange(self.year, self.month)[1]
        row, col = 1, first_weekday
        for day in range(1, days_in_month + 1):
            d = datetime.date(self.year, self.month, day)
            btn = tk.Button(self.grid, text=str(day), width=3, relief="flat",
                            font=(FONT, 9), cursor="hand2",
                            command=lambda dd=d: self._pick(dd))
            btn.grid(row=row, column=col, padx=1, pady=1)
            self.day_btns[day] = btn
            col += 1
            if col > 6:
                col = 0
                row += 1
        self._paint_selection()
        self._update_status()

    def _pick(self, d):
        if self.support_range:
            if self.start is None or (self.start and self.end):
                self.start, self.end = d, None
            elif d < self.start:
                self.start, self.end = d, None
            elif self.max_span_days and (d - self.start).days > self.max_span_days:
                # 超出允许跨度：以新点击日期为起点重新选择
                self.start, self.end = d, None
                self._paint_selection()
                self.status.config(text="跨越不能超过 %d 天，已重新以 %s 为起点"
                                   % (self.max_span_days, d.isoformat()))
                return
            else:
                self.end = d
        else:
            self.start, self.end = d, None
        self._paint_selection()
        self._update_status()

    def _paint_selection(self):
        today = datetime.date.today()
        for day, btn in self.day_btns.items():
            d = datetime.date(self.year, self.month, day)
            if self.end and self.start and self.start <= d <= self.end:
                btn.config(bg=BLUE, fg="white")
            elif d == self.start:
                btn.config(bg=BLUE, fg="white")
            elif d == today:
                btn.config(bg=BLUE_LIGHT, fg=BLUE)
            else:
                btn.config(bg=CARD, fg=(GRAY if d.weekday() >= 5 else TEXT))

    def _update_status(self):
        if self.start is None:
            self.status.config(text="请点击选择日期%s" % (
                "（再点一次结束日期组成范围）" if self.support_range else ""))
        elif self.end:
            self.status.config(text="已选：%s ~ %s" % (
                self.start.isoformat(), self.end.isoformat()))
        else:
            self.status.config(text="已选：%s%s" % (
                self.start.isoformat(),
                "（可再点结束日期）" if self.support_range else ""))

    def _confirm(self):
        if self.start is None:
            messagebox.showwarning("提示", "请先选择日期", parent=self)
            return
        value = self.start.isoformat()
        if self.end:
            value = "%s~%s" % (self.start.isoformat(), self.end.isoformat())
        if self.on_pick:
            self.on_pick(value)
        self.destroy()


def attach_calendar(entry, support_range=False, parent=None, max_span_days=None):
    """点击日期输入框弹出日历表选择日期（范围支持时两次点选=起点~终点）。"""
    entry.config(cursor="hand2")

    def open_calendar(_evt=None):
        CalendarPicker(parent or entry.winfo_toplevel(), support_range=support_range,
                       max_span_days=max_span_days,
                       on_pick=lambda v: entry.delete(0, "end") or entry.insert(0, v))

    entry.bind("<Button-1>", open_calendar)


# ----------------------------- 可滚动区域 -----------------------------

class ScrollFrame(ttk.Frame):
    def __init__(self, master, height=None):
        super().__init__(master)
        self.canvas = tk.Canvas(self, bg=CARD, highlightthickness=0, height=height)
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.inner = ttk.Frame(self.canvas, style="White.TFrame")
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        # 滚轮绑到所属 Toplevel（add 叠加，不占全局 bind_all 槽位），由指针位置
        # 决定滚哪个滚动区：多窗口/多滚动区并存时互不抢占、也不会把别的窗口滚跑
        self.winfo_toplevel().bind("<MouseWheel>", self._wheel, add="+")

    def _wheel(self, e):
        try:
            w = self.winfo_containing(e.x_root, e.y_root)
            while w is not None and w is not self.canvas:
                w = w.master
            if w is None:
                return  # 指针不在本滚动区上：不接管，别的滚动区/控件自己处理
            if self.canvas.winfo_exists():
                self.canvas.yview_scroll(int(-e.delta / 120), "units")
        except tk.TclError:
            pass  # 窗口/控件销毁竞态
        except Exception:
            return

    def clear(self):
        for w in self.inner.winfo_children():
            w.destroy()
        self.refresh_region()

    def refresh_region(self):
        """强制刷新滚动区域：清空/重渲染后调用，防止内容更新后 scrollregion
        未同步导致下方卡片被裁切看不到。"""
        self.canvas.configure(scrollregion=(0, 0, 0, 0))
        self.inner.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))


# ----------------------------- 新建任务向导（四步） -----------------------------

class TaskWizard(tk.Toplevel):
    def __init__(self, app, on_created=None):
        super().__init__(app.root)
        self.app = app
        self.on_created = on_created
        self.title("新建监控任务")
        self.geometry("520x780")
        self.minsize(480, 700)
        self.configure(bg=CARD)
        try:
            self.name2code, self.code2name = ticket.load_station_map()
        except Exception as e:
            messagebox.showerror("错误", "车站代码表加载失败：%s" % e, parent=self)
            self.destroy()
            return

        self.sel = {}            # (train_code, seat) -> {"info": ..., "count": n}
        self.seat_buttons = {}
        self.seat_counts = {}   # (date, train_code, seat) -> 该日该席别余票数
        self.query_date = ""
        self.monitor_dates = []  # 要监控的全部日期（单日或区间展开）
        self.dates_rows = {}     # 日期 -> 解析后的车次列表
        self.active_date = None  # 第二步当前查看的日期
        self.passengers = []     # [{"name","type","id_masked","source","is_default"}]
        self.pass_vars = {}
        self.sel_passengers = set()
        self._pass_loaded = False
        self.step_no = 0
        self.auto_var = tk.BooleanVar(value=True)
        self.stop_var = tk.BooleanVar(value=True)
        self.notify_var = tk.BooleanVar(value=True)
        self.prio_var = tk.IntVar(value=5)
        self.purpose_var = tk.StringVar(value="ADULT")

        # 顶部蓝色导航条
        header = tk.Frame(self, bg=BLUE)
        header.pack(fill="x")
        tk.Label(header, text="新建监控任务", bg=BLUE, fg="white",
                 font=(FONT, 14, "bold"), pady=10).pack(side="left", padx=16)
        self.step_label = tk.Label(header, text="1/4 查询车票", bg=BLUE, fg="#DCEAFB",
                                   font=(FONT, 10))
        self.step_label.pack(side="right", padx=16)

        # 内容区
        self.body = tk.Frame(self, bg=CARD)
        self.body.pack(fill="both", expand=True)
        self.frames = []
        self.frames.append(self._build_step1())
        self.frames.append(self._build_step2())
        self.frames.append(self._build_step3())
        self.frames.append(self._build_step4())

        # 底部操作条
        self.footer = tk.Frame(self, bg=BG)
        self.footer.pack(fill="x", side="bottom")
        self.prev_btn = ttk.Button(self.footer, text="← 上一步", style="White.TButton",
                                   command=self.prev_step)
        self.prev_btn.pack(side="left", padx=12, pady=8)
        self.next_btn = None  # 每步各自的下一步按钮

        self.show_step(0)

    # ---------------- 通用 ----------------

    def show_step(self, n):
        for f in self.frames:
            f.pack_forget()
        self.frames[n].pack(fill="both", expand=True)
        self.step_no = n
        self.step_label.config(text="{0}/4 {1}".format(
            n + 1, ["查询车票", "选择车次", "选择乘车人", "确认创建"][n]))
        self.prev_btn.config(state=("normal" if n > 0 else "disabled"))
        if n == 2 and not self._pass_loaded:
            self._load_passengers()
        if n == 3:
            self._refresh_confirm()

    def prev_step(self):
        if self.step_no > 0:
            self.show_step(self.step_no - 1)

    def quick_date(self, offset):
        d = datetime.date.today() + datetime.timedelta(days=offset)
        self.date_var.set(d.isoformat())

    # ---------------- 第一步：查询页 ----------------

    def _build_step1(self):
        f = tk.Frame(self.body, bg=CARD)
        tk.Label(f, text="查询车票", bg=CARD, fg=TEXT,
                 font=(FONT, 18, "bold")).pack(pady=(20, 4))
        tk.Label(f, text="先查询车次，再选择要监视的车次和席别",
                 bg=CARD, fg=GRAY, font=(FONT, 10)).pack(pady=(0, 16))

        row = tk.Frame(f, bg=CARD)
        row.pack(fill="x", padx=28)
        tk.Label(row, text="出发站", bg=CARD, fg=GRAY, font=(FONT, 10),
                 width=6, anchor="w").pack(side="left")
        self.from_field = launcher.StationEntry(row, width=14)
        self.from_field.pack(side="left", padx=(4, 6))
        tk.Button(row, text="⇄", command=self.swap_stations, bg=BLUE, fg="white",
                  activebackground=BLUE_DARK, activeforeground="white",
                  font=(FONT, 12, "bold"), relief="flat", width=3,
                  cursor="hand2").pack(side="left", padx=10, ipady=6)
        tk.Label(row, text="到达站", bg=CARD, fg=GRAY, font=(FONT, 10),
                 width=6, anchor="w").pack(side="left")
        self.to_field = launcher.StationEntry(row, width=14)
        self.to_field.pack(side="left")

        date_row = tk.Frame(f, bg=CARD)
        date_row.pack(fill="x", padx=28, pady=(18, 6))
        tk.Label(date_row, text="乘车日期", bg=CARD, fg=GRAY,
                 font=(FONT, 10)).pack(side="left")
        tk.Label(date_row, text="从", bg=CARD, fg=GRAY,
                 font=(FONT, 10)).pack(side="left", padx=(4, 2))
        self.date_var = tk.StringVar()
        self.date_entry = tk.Entry(date_row, textvariable=self.date_var, width=11,
                                   justify="center", font=(FONT, 12, "bold"),
                                   relief="flat", highlightthickness=1,
                                   highlightbackground=BORDER, highlightcolor=BLUE)
        self.date_entry.pack(side="left", ipady=3)
        attach_calendar(self.date_entry)
        tk.Label(date_row, text="到", bg=CARD, fg=GRAY,
                 font=(FONT, 10)).pack(side="left", padx=6)
        self.date_end_var = tk.StringVar()
        self.date_end_entry = tk.Entry(date_row, textvariable=self.date_end_var, width=11,
                                       justify="center", font=(FONT, 12),
                                       relief="flat", highlightthickness=1,
                                       highlightbackground=BORDER, highlightcolor=BLUE)
        self.date_end_entry.pack(side="left", ipady=3)
        attach_calendar(self.date_end_entry)
        quick_row = tk.Frame(f, bg=CARD)
        quick_row.pack(fill="x", padx=28, pady=(0, 2))
        for text, off in (("今天", 0), ("明天", 1), ("后天", 2)):
            tk.Button(quick_row, text=text, command=lambda o=off: self.quick_date(o),
                      bg=BLUE_LIGHT, fg=BLUE, activebackground="#D6E8FA",
                      relief="flat", font=(FONT, 9), padx=8,
                      cursor="hand2").pack(side="left", padx=2)
        tk.Label(f, text="「从」必填；「到」留空 = 只监视那一天，填了 = 监视从到连续区间（最多相差 5 天）",
                 bg=CARD, fg=GRAY, font=(FONT, 9)).pack(anchor="w", padx=28)

        opt_row = tk.Frame(f, bg=CARD)
        opt_row.pack(fill="x", padx=28, pady=(4, 2))
        tk.Checkbutton(opt_row, text="余票命中后自动下单（不支付）", variable=self.auto_var,
                       bg=CARD, activebackground=CARD, fg=TEXT,
                       font=(FONT, 10)).pack(side="left")

        type_row = tk.Frame(f, bg=CARD)
        type_row.pack(fill="x", padx=28, pady=4)
        tk.Label(type_row, text="票种：", bg=CARD, fg=GRAY, font=(FONT, 10)).pack(side="left")
        tk.Radiobutton(type_row, text="成人票", value="ADULT", variable=self.purpose_var,
                       bg=CARD, activebackground=CARD, font=(FONT, 10),
                       cursor="hand2").pack(side="left", padx=(0, 10))
        tk.Radiobutton(type_row, text="学生票", value="0X00", variable=self.purpose_var,
                       bg=CARD, activebackground=CARD, font=(FONT, 10),
                       cursor="hand2").pack(side="left")

        tk.Button(f, text="查 询 车 票", command=self.do_query, bg=BLUE, fg="white",
                  activebackground=BLUE_DARK, activeforeground="white",
                  font=(FONT, 14, "bold"), relief="flat", pady=12,
                  cursor="hand2").pack(fill="x", padx=28, pady=(14, 4))
        self.query_hint = tk.Label(f, text="", bg=CARD, fg=GRAY, font=(FONT, 9))
        self.query_hint.pack()

        # 历史路线
        tk.Label(f, text="最近用过的路线：", bg=CARD, fg=GRAY,
                 font=(FONT, 9)).pack(anchor="w", padx=28, pady=(18, 4))
        self.hist_frame = tk.Frame(f, bg=CARD)
        self.hist_frame.pack(fill="x", padx=28)
        self._render_history()
        return f

    def _render_history(self):
        for w in self.hist_frame.winfo_children():
            w.destroy()
        pairs, seen = [], set()
        try:
            for t in (load_config().get("tasks") or []):
                key = (t.get("from"), t.get("to"))
                if key[0] and key[1] and key not in seen:
                    seen.add(key)
                    pairs.append(key)
        except Exception:
            pass
        if not pairs:
            tk.Label(self.hist_frame, text="（暂无）", bg=CARD, fg=GRAY).pack(anchor="w")
            return
        for fr, to in pairs[-6:]:
            tk.Button(self.hist_frame, text="{0} ⇄ {1}".format(fr, to),
                      command=lambda a=fr, b=to: self.fill_stations(a, b),
                      bg=CARD, fg=BLUE, activebackground=BLUE_LIGHT,
                      activeforeground=BLUE, relief="flat", font=(FONT, 9),
                      highlightthickness=1, highlightbackground=BORDER,
                      padx=10, pady=4, cursor="hand2").pack(side="left", padx=3, pady=2)

    def fill_stations(self, fr, to):
        self.from_field.set(fr)
        self.to_field.set(to)

    def swap_stations(self):
        a = self.from_field.get()
        b = self.to_field.get()
        self.from_field.set(b)
        self.to_field.set(a)

    def do_query(self):
        from_name, to_name = self.from_field.get(), self.to_field.get()
        if from_name not in self.name2code or to_name not in self.name2code:
            messagebox.showwarning("提示", "请先通过「选择」确定有效的出发站/到达站", parent=self)
            return
        if from_name == to_name:
            messagebox.showwarning("提示", "出发站与到达站不能相同", parent=self)
            return
        # 解析乘车日期：只填「从」= 单日；「到」也填 = 连续区间（appcommon 单点口径）
        try:
            dates, _date_range = appcommon.parse_date_range(
                self.date_var.get().strip(), self.date_end_var.get().strip())
        except ValueError as e:
            messagebox.showwarning("提示", str(e), parent=self)
            return
        self.monitor_dates = list(dates)   # appcommon 返回的已是 ISO 字符串
        self.query_date = self.monitor_dates[0]
        self.query_hint.config(text="正在查询 %d 天车次  %s → %s ..."
                               % (len(self.monitor_dates), from_name, to_name))

        # Tk 变量只能在主线程读：先取值再进子线程（worker 里调 get() 会跨线程访问 Tcl）
        purpose = self.purpose_var.get()

        def do_query():
            out = {}
            for d in self.monitor_dates:
                rows = ticket.query_tickets(self.name2code[from_name],
                                            self.name2code[to_name], d, purpose)
                out[d] = rows
            return self.code2name, out, from_name, to_name

        def on_done(res, err):
            self.query_hint.config(text="")
            if err:
                self.query_hint.config(text="查询失败：%s" % err, fg="#cf222e")
                return
            code2name, dates_rows, from_name, to_name = res
            self.from_name, self.to_name = from_name, to_name
            self.dates_rows = {d: [ticket.parse_row(r, code2name) for r in rows]
                               for d, rows in dates_rows.items()}
            self.sel = {}
            self.seat_buttons = {}
            self.seat_counts = {}
            self.active_date = self.monitor_dates[0] if self.monitor_dates else None
            self.render_date_tabs()
            self.render_cards()
            self.show_step(1)

        run_async(self, do_query, on_done)

    # ---------------- 第二步：车次结果页 ----------------

    def _build_step2(self):
        f = tk.Frame(self.body, bg=CARD)
        tk.Label(f, text="选择车次与席别", bg=CARD, fg=TEXT,
                 font=(FONT, 15, "bold")).pack(pady=(12, 2))
        tk.Label(f, text="点上方日期切换查看该日车次；点击席别加入监视（无票的席别也可先选上，等放票）",
                 bg=CARD, fg=GRAY, font=(FONT, 9)).pack()
        self.date_tab_bar = tk.Frame(f, bg=CARD)
        self.date_tab_bar.pack(fill="x", padx=14, pady=(6, 0))
        bar2 = tk.Frame(f, bg=CARD)
        bar2.pack(fill="x", padx=14, pady=6)
        self.filter_gt = tk.BooleanVar(value=False)
        self.filter_kt = tk.BooleanVar(value=False)
        self.filter_ticket = tk.BooleanVar(value=False)
        tk.Checkbutton(bar2, text="只看高铁/动车", variable=self.filter_gt,
                       bg=CARD, activebackground=CARD, font=(FONT, 9),
                       command=self.render_cards).pack(side="left")
        tk.Checkbutton(bar2, text="只看普速", variable=self.filter_kt,
                       bg=CARD, activebackground=CARD, font=(FONT, 9),
                       command=self.render_cards).pack(side="left", padx=6)
        tk.Checkbutton(bar2, text="只看有票", variable=self.filter_ticket,
                       bg=CARD, activebackground=CARD, font=(FONT, 9),
                       command=self.render_cards).pack(side="left", padx=6)
        self.sort_mode = tk.StringVar(value="time")
        self.sort_dur_btn = tk.Button(bar2, text="历时最短", command=lambda: self.set_sort("dur"),
                                      bg=CARD, fg=GRAY, activebackground=BLUE_LIGHT,
                                      relief="flat", font=(FONT, 9), cursor="hand2")
        self.sort_dur_btn.pack(side="right", padx=4)
        self.sort_time_btn = tk.Button(bar2, text="发时最早", command=lambda: self.set_sort("time"),
                                       bg=CARD, fg=BLUE, activebackground=BLUE_LIGHT,
                                       relief="flat", font=(FONT, 9), cursor="hand2")
        self.sort_time_btn.pack(side="right")
        self._paint_sort()

        self.cards_area = ScrollFrame(f, height=330)
        self.cards_area.pack(fill="both", expand=True, padx=14)

        bottom = tk.Frame(f, bg=CARD)
        bottom.pack(fill="x", padx=14, pady=6)
        self.sel_label = tk.Label(bottom, text="已选 0 个席别", bg=CARD, fg=GRAY,
                                  font=(FONT, 10))
        self.sel_label.pack(side="left")
        self.next_btn2 = tk.Button(bottom, text="下一步：选择乘车人 →", bg=BLUE,
                                   fg="white", activebackground=BLUE_DARK,
                                   activeforeground="white", relief="flat",
                                   font=(FONT, 11, "bold"), pady=8, padx=18,
                                   state="disabled", cursor="hand2",
                                   command=lambda: self.show_step(2))
        self.next_btn2.pack(side="right")
        return f

    def set_sort(self, mode):
        self.sort_mode.set(mode)
        self._paint_sort()
        self.render_cards()

    def _paint_sort(self):
        for btn, m in ((self.sort_time_btn, "time"), (self.sort_dur_btn, "dur")):
            active = (self.sort_mode.get() == m)
            btn.config(fg=(BLUE if active else GRAY),
                       font=(FONT, 9, "bold" if active else "normal"))

    def _duration_min(self, dur):
        try:
            hh, mm = ((dur or "00:00").split(":"))[:2]
            return int(hh) * 60 + int(mm)
        except Exception:
            return 10 ** 9

    def _filtered_rows(self, rows):
        if self.filter_gt.get():
            rows = [r for r in rows if r["train_code"][:1] in ("G", "D", "C")]
        if self.filter_kt.get():
            rows = [r for r in rows if r["train_code"][:1] not in ("G", "D", "C")]
        if self.filter_ticket.get():
            rows = [r for r in rows if r["available_seats"]]
        if self.sort_mode.get() == "dur":
            rows = sorted(rows, key=lambda r: self._duration_min(r["duration"]))
        else:
            rows = sorted(rows, key=lambda r: r["start_time"] or "99:99")
        return rows

    @staticmethod
    def _date_title(d):
        """日期分组标题：2026-10-06 星期二"""
        wd = "一二三四五六日"[datetime.date.fromisoformat(d).weekday()]
        return "%s 星期%s" % (d, wd)

    def render_date_tabs(self):
        """顶部日期按钮行：点哪个日期，下面显示哪个日期的车次。"""
        for w in self.date_tab_bar.winfo_children():
            w.destroy()
        for d in self.monitor_dates:
            wd = "一二三四五六日"[datetime.date.fromisoformat(d).weekday()]
            active = (d == self.active_date)
            tk.Button(self.date_tab_bar, text="%s\n周%s" % (d[5:], wd),
                      command=lambda dd=d: self.select_date(dd),
                      bg=(BLUE if active else CARD),
                      fg=("white" if active else TEXT),
                      activebackground=BLUE_LIGHT, activeforeground=BLUE,
                      relief="flat", font=(FONT, 9, "bold" if active else "normal"),
                      padx=10, pady=3, cursor="hand2").pack(side="left", padx=2, pady=2)

    def select_date(self, d):
        if d == self.active_date:
            return
        self.active_date = d
        self.render_date_tabs()
        self.render_cards()

    def render_cards(self):
        self.cards_area.clear()
        # 清空后的旧按钮引用会指向已销毁控件（切换日期时调用 config 会抛错），必须重建
        self.seat_buttons = {}
        self.seat_counts = {}
        rows = self._filtered_rows(
            self.dates_rows.get(self.active_date) or [])
        if self.active_date:
            tk.Label(self.cards_area.inner, text="{0} · {1} 个车次".format(
                self._date_title(self.active_date), len(rows)),
                bg=CARD, fg=GRAY, font=(FONT, 9)).pack(anchor="w")
        if not rows:
            tk.Label(self.cards_area.inner, text="没有符合条件的车次",
                     bg=CARD, fg=GRAY, font=(FONT, 12)).pack(pady=40)
        for info in rows:
            self._build_card(self.active_date, info)
        LOG.info("[向导] 第2步 渲染日期 %s：%d 个车次", self.active_date, len(rows))
        self.cards_area.refresh_region()
        self._update_sel_label()

    def _build_card(self, d, info):
        code = info["train_code"]
        card = tk.Frame(self.cards_area.inner, bg=CARD, highlightthickness=1,
                        highlightbackground=BORDER)
        card.pack(fill="x", pady=4)
        # 顶部：时间 车次 时间 历时
        top = tk.Frame(card, bg=CARD)
        top.pack(fill="x", padx=10, pady=(8, 2))
        tk.Label(top, text=info["start_time"] or "--:--", bg=CARD, fg=TEXT,
                 font=(FONT, 15, "bold")).pack(side="left")
        kind = "高铁/动车" if code[:1] in ("G", "D", "C") else "普速"
        mid_txt = "{0}  {1}".format(code, kind)
        tk.Label(top, text="—— " + mid_txt + " ——", bg=CARD, fg=GRAY,
                 font=(FONT, 9)).pack(side="left", padx=8)
        tk.Label(top, text=info["arrive_time"] or "--:--", bg=CARD, fg=TEXT,
                 font=(FONT, 15, "bold")).pack(side="left")
        tk.Label(top, text="历时 {0}".format(info["duration"] or "-"), bg=CARD,
                 fg=GRAY, font=(FONT, 9)).pack(side="right")
        # 站点行
        tk.Label(card, text="{0} → {1}".format(info["from_name"], info["to_name"]),
                 bg=CARD, fg=GRAY, font=(FONT, 9)).pack(anchor="w", padx=10)
        # 席别按钮（有票=蓝色可点，无票=灰色仍可点选加入监视）
        seat_row = tk.Frame(card, bg=CARD)
        seat_row.pack(fill="x", padx=10, pady=(4, 8))
        for idx, seat in enumerate(SEAT_CHOICES):
            count = info["available_seats"].get(seat, "")
            btn = tk.Button(seat_row, text="{0}\n{1}".format(seat, count or "无"),
                            command=lambda c=code, s=seat, i=info, dd=d: self.toggle_seat(dd, c, s, i),
                            font=(FONT, 8), relief="flat", bd=0, width=9,
                            pady=4, cursor="hand2")
            btn.grid(row=idx // 6, column=idx % 6)
            self.seat_buttons[(d, code, seat)] = btn
            self.seat_counts[(d, code, seat)] = count
            self._paint_seat(d, code, seat, count)

    def _paint_seat(self, d, code, seat, count=None):
        # 每个日期的席别按钮独立高亮（不同日期可勾选不同席别）
        btn = self.seat_buttons.get((d, code, seat))
        if btn is None or not btn.winfo_exists():
            return
        if count is None:
            count = self.seat_counts.get((d, code, seat), "")
        if (d, code, seat) in self.sel:
            btn.config(bg=BLUE, fg="white", activebackground=BLUE_DARK,
                       activeforeground="white")
        elif count:
            btn.config(bg=BLUE_LIGHT, fg=BLUE, activebackground="#D6E8FA",
                       activeforeground=BLUE)
        else:
            btn.config(bg=CARD, fg="#C0C7D1", activebackground="#F0F2F5",
                       activeforeground=GRAY)

    def toggle_seat(self, d, code, seat, info):
        key = (d, code, seat)
        if key in self.sel:
            del self.sel[key]
        else:
            avail = (info or {}).get("available_seats") or {}
            self.sel[key] = {"info": info, "count": avail.get(seat, "")}
        self._paint_seat(d, code, seat)
        self._update_sel_label()

    def _update_sel_label(self):
        trains = sorted({c for (_d, c, _s) in self.sel})
        seats = sorted({s for (_d, _c, s) in self.sel})
        self.sel_label.config(text="已选 {0} 趟车 · {1} 种席别  {2}{3}".format(
            len(trains), len(seats), "/".join(trains), "　" + "/".join(seats)))
        if self.sel:
            self.next_btn2.config(state="normal")
        else:
            self.next_btn2.config(state="disabled")

    # ---------------- 第三步：乘车人选择 ----------------

    def _build_step3(self):
        f = tk.Frame(self.body, bg=CARD)
        tk.Label(f, text="选择乘车人", bg=CARD, fg=TEXT,
                 font=(FONT, 15, "bold")).pack(pady=(12, 2))
        tk.Label(f, text="优先取下单账号内已保存的乘车人（本地加密库作为补充）",
                 bg=CARD, fg=GRAY, font=(FONT, 9)).pack()

        bar = tk.Frame(f, bg=CARD)
        bar.pack(fill="x", padx=14, pady=4)
        tk.Button(bar, text="全选", command=self.pass_all, bg=BLUE_LIGHT, fg=BLUE,
                  activebackground="#D6E8FA", relief="flat", font=(FONT, 9),
                  cursor="hand2").pack(side="left", padx=2)
        tk.Button(bar, text="全不选", command=self.pass_none, bg="#F0F2F5", fg=TEXT,
                  activebackground="#E3E6EB", relief="flat", font=(FONT, 9),
                  cursor="hand2").pack(side="left", padx=2)
        self.pass_count_label = tk.Label(bar, text="已选 0 人", bg=CARD, fg=BLUE,
                                         font=(FONT, 10, "bold"))
        self.pass_count_label.pack(side="right")
        self.pass_hint = tk.Label(f, text="加载中...", bg=CARD, fg=GRAY, font=(FONT, 9))
        self.pass_hint.pack(anchor="w", padx=14)

        self.pass_area = ScrollFrame(f, height=380)
        self.pass_area.pack(fill="both", expand=True, padx=14, pady=6)

        bottom = tk.Frame(f, bg=CARD)
        bottom.pack(fill="x", padx=14, pady=8)
        tk.Label(bottom, text="不选则自动使用默认乘车人 / 账号常用乘车人",
                 bg=CARD, fg=GRAY, font=(FONT, 9)).pack(side="left")
        tk.Button(bottom, text="下一步：确认创建 →", bg=BLUE, fg="white",
                  activebackground=BLUE_DARK, activeforeground="white",
                  relief="flat", font=(FONT, 11, "bold"), pady=8, padx=18,
                  cursor="hand2", command=lambda: self.show_step(3)).pack(side="right")
        return f

    def _load_passengers(self):
        def do_load():
            account = []
            try:
                if (load_config().get("order_mode") or "http") == "browser":
                    sess = order_mod.session_from_browser_state()
                else:
                    sess = order_mod.load_session(
                        load_config().get("session_cookies_file"))
                ok, _who = order_mod.check_login(sess)
                if ok:
                    account = order_mod.get_passengers(sess)
            except Exception:
                pass
            local = passengers_mod.load_passengers()
            return account, local

        def on_done(res, err):
            account, local = res or ([], [])
            merged = {}
            for p in account:
                if p.get("name"):
                    merged[p["name"]] = {
                        "name": p["name"],
                        "type": p.get("type_name") or ("成人" if p.get("is_adult", True) else "非成人"),
                        "id_masked": _mask_id(p.get("id_no")),
                        "source": "账号",
                        "is_default": False,
                    }
            for p in local:
                name = p.get("name")
                if not name or name in merged:
                    continue
                merged[name] = {
                    "name": name,
                    "type": "成人" if p.get("is_adult", True) else "非成人",
                    "id_masked": _mask_id(p.get("id_no")),
                    "source": "本地",
                    "is_default": bool(p.get("is_default")),
                }
            self.passengers = list(merged.values())
            self._pass_loaded = True
            self._render_passengers()
            if not self.passengers:
                self.pass_hint.config(
                    text="未取到乘车人：请先在主窗口「乘车人管理」添加，或「登录会话」登录 12306",
                    fg=ORANGE)

        run_async(self, do_load, on_done)

    def _render_passengers(self):
        self.pass_area.clear()
        self.pass_vars = {}
        for p in self.passengers:
            var = tk.BooleanVar(value=bool(p.get("is_default")))
            self.pass_vars[p["name"]] = (var, p)
            row = tk.Frame(self.pass_area.inner, bg=CARD, highlightthickness=1,
                           highlightbackground=BORDER)
            row.pack(fill="x", pady=2)
            tk.Checkbutton(row, variable=var,
                           command=lambda n=p["name"]: self._on_pass_toggle(n),
                           bg=CARD, activebackground=CARD, cursor="hand2").pack(side="left", padx=4)
            name_lbl = tk.Label(row, text=p["name"], bg=CARD, fg=TEXT,
                                font=(FONT, 12, "bold"))
            name_lbl.pack(side="left", padx=(2, 8))
            tk.Label(row, text="【{0}】".format(p["type"]), bg=CARD, fg=GRAY,
                     font=(FONT, 9)).pack(side="left")
            tk.Label(row, text=p["id_masked"], bg=CARD, fg=GRAY,
                     font=(FONT, 10)).pack(side="left", padx=8)
            tk.Label(row, text=p["source"], bg=("#F0F7FF" if p["source"] == "账号" else "#FFF4E0"),
                     fg=(BLUE if p["source"] == "账号" else "#B26A00"),
                     font=(FONT, 8)).pack(side="right", padx=8, ipadx=6)
        self._on_pass_toggle(None, refresh_only=True)

    def _on_pass_toggle(self, name, refresh_only=False):
        self.sel_passengers = {n for n, (v, _p) in self.pass_vars.items() if v.get()}
        self.pass_count_label.config(text="已选 {0} 人".format(len(self.sel_passengers)))

    def pass_all(self):
        for name, (var, _p) in self.pass_vars.items():
            var.set(True)
        self._on_pass_toggle(None, refresh_only=True)

    def pass_none(self):
        for name, (var, _p) in self.pass_vars.items():
            var.set(False)
        self._on_pass_toggle(None, refresh_only=True)

    # ---------------- 第四步：确认页 ----------------

    def _build_step4(self):
        f = tk.Frame(self.body, bg=CARD)
        tk.Label(f, text="确认创建监控任务", bg=CARD, fg=TEXT,
                 font=(FONT, 15, "bold")).pack(pady=(12, 6))

        tip = tk.Frame(f, bg=ORANGE_LIGHT, highlightthickness=1,
                       highlightbackground="#F5D9A8")
        tip.pack(fill="x", padx=14, pady=(0, 8))
        tk.Label(tip, text="创建后将进入监控队列：每 30~60 秒查询一次余票，命中即自动提交订单（不执行支付）。",
                 bg=ORANGE_LIGHT, fg="#B26A00", font=(FONT, 9), justify="left",
                 wraplength=440).pack(anchor="w", padx=10, pady=8)

        self.summary_card = tk.Frame(f, bg=CARD, highlightthickness=1,
                                     highlightbackground=BORDER)
        self.summary_card.pack(fill="x", padx=14, pady=4)
        self.summary_label = tk.Label(self.summary_card, bg=CARD, fg=TEXT,
                                      font=(FONT, 10), justify="left", anchor="w")
        self.summary_label.pack(fill="x", padx=12, pady=10)

        opt_frame = tk.Frame(f, bg=CARD)
        opt_frame.pack(fill="x", padx=14, pady=6)
        self._switch(opt_frame, "自动下单", "余票命中后自动提交订单（不支付）", self.auto_var)
        self._switch(opt_frame, "成功后停止", "一次下单成功后自动停止本任务（防止重复购买）", self.stop_var)
        self._switch(opt_frame, "邮件通知", "下单成功后发送邮件通知详情", self.notify_var)

        prio_row = tk.Frame(f, bg=CARD)
        prio_row.pack(fill="x", padx=14, pady=4)
        tk.Label(prio_row, text="任务优先级（1~10，越大越优先）：", bg=CARD,
                 font=(FONT, 10)).pack(anchor="w")
        prio_pick = tk.Frame(f, bg=CARD)
        prio_pick.pack(fill="x", padx=14, pady=(2, 4))
        for v in range(1, 11):
            tk.Radiobutton(prio_pick, text=str(v), value=v, variable=self.prio_var,
                           bg=CARD, activebackground=CARD, font=(FONT, 9),
                           indicatoron=False, selectcolor=BLUE,
                           cursor="hand2").pack(side="left", padx=1)

        ttk.Button(f, text="创建任务并启动监控", style="Blue.TButton",
                   command=lambda: self.create_task(start_now=True)).pack(
            fill="x", padx=14, pady=(12, 4))
        ttk.Button(f, text="仅创建任务（稍后手动启动）", style="White.TButton",
                   command=lambda: self.create_task(start_now=False)).pack(
            fill="x", padx=14, pady=2)
        return f

    @staticmethod
    def _switch(parent, title, desc, var):
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=title, bg=CARD, fg=TEXT, width=10, anchor="w",
                 font=(FONT, 10)).pack(side="left")
        tk.Label(row, text=desc, bg=CARD, fg=GRAY, font=(FONT, 9)).pack(side="left")
        # 标准勾选框：选中即打勾 ✓
        tk.Checkbutton(row, variable=var, bg=CARD, activebackground=CARD,
                       cursor="hand2").pack(side="right")

    def _refresh_confirm(self):
        date_text = "、".join(self.monitor_dates) if self.monitor_dates else ""
        trains = sorted({c for (_d, c, _s) in self.sel})
        pnames = sorted(self.sel_passengers)
        # 席别按日列出（不同日期可不同）
        seat_lines = []
        for d in self.monitor_dates:
            sd = sorted({s for (dd, _c, s) in self.sel if dd == d})
            seat_lines.append("%s：%s" % (d, "、".join(sd) or "（未选）"))
        seat_text = "\n".join(seat_lines) or "（未选）"
        self.summary_label.config(text=(
            "区间：{0} → {1}\n"
            "日期：{2}\n"
            "车次：{3}\n"
            "席别（按日）：\n{4}\n"
            "乘车人：{5}"
        ).format(self.from_name, self.to_name, date_text,
                 "、".join(trains) or "（未选）",
                 seat_text,
                 "、".join(pnames) if pnames else "默认/账号常用乘车人"))

    def create_task(self, start_now):
        trains = sorted({c for (_d, c, _s) in self.sel})
        seats = sorted({s for (_d, _c, s) in self.sel})
        if not trains or not seats:
            messagebox.showwarning("提示", "请先选择要监视的车次和席别", parent=self)
            return
        names = sorted(self.sel_passengers)
        if self.auto_var.get() and not names:
            # 自动下单需要乘车人；提示但允许继续（将使用默认/账号常用乘车人）
            messagebox.showinfo("提示",
                                "未勾选乘车人：下单时将自动使用默认乘车人 / 账号常用乘车人。",
                                parent=self)
        # 按日独立的席别选择：{日期: [席别...]}
        seats_by_date = {}
        for d in self.monitor_dates:
            sd = sorted({s for (dd, _c, s) in self.sel if dd == d})
            seats_by_date[d] = sd
        name = "{0}-{1} {2} {3}".format(
            self.from_name, self.to_name, "/".join(trains), "/".join(seats))
        task = {
            "name": name,
            "uid": uuid.uuid4().hex,
            "from": self.from_name,
            "to": self.to_name,
            "dates": list(self.monitor_dates),
            "date_range": [],
            "trains": trains,
            "seat_types": seats,
            "seats_by_date": seats_by_date,
            "auto_order": bool(self.auto_var.get()),
            "stop_after_order": bool(self.stop_var.get()),
            "passenger_names": names,
            "priority": int(self.prio_var.get()),
            "purpose_code": self.purpose_var.get(),
            "notify_channels": ["email"] if self.notify_var.get() else [],
        }
        update_config_locked(lambda config: config.setdefault("tasks", []).append(task))
        mark_task_created(self.app, task, start_now)
        if self.on_created:
            self.on_created()
        self.destroy()
        if start_now:
            self.app.start_engine()
            messagebox.showinfo("完成",
                                "任务「{0}」已创建，监控已启动。\n命中余票将自动提交订单并邮件通知。".format(name),
                                parent=self.app.root)
        else:
            messagebox.showinfo("完成",
                                "任务「{0}」已创建。\n在主窗口点击「▶ 启动监控」即可开始。".format(name),
                                parent=self.app.root)


# ----------------------------- 直接监视（免查询） -----------------------------

class QuickMonitorDialog(tk.Toplevel):
    """第二种建任务方式：直接填日期+车次+席别+乘车人，一键开始监视。"""

    def __init__(self, app, on_created=None):
        super().__init__(app.root)
        self.app = app
        self.on_created = on_created
        self.title("直接监视（免查询）")
        self.geometry("460x640")
        self.configure(bg=CARD)
        try:
            self.name2code, self.code2name = ticket.load_station_map()
        except Exception as e:
            messagebox.showerror("错误", "车站代码表加载失败：%s" % e, parent=self)
            self.destroy()
            return

        header = tk.Frame(self, bg=BLUE)
        header.pack(fill="x")
        tk.Label(header, text="直接监视", bg=BLUE, fg="white",
                 font=(FONT, 14, "bold"), pady=10).pack(side="left", padx=16)
        tk.Label(header, text="免查询 · 直接填车次", bg=BLUE, fg="#DCEAFB",
                 font=(FONT, 9)).pack(side="right", padx=16)

        body = tk.Frame(self, bg=CARD)
        body.pack(fill="both", expand=True, padx=18)

        self.from_field = StationField(body, "出发站", big=True)
        self.from_field.pack(fill="x", pady=6)
        self.to_field = StationField(body, "到达站", big=True)
        self.to_field.pack(fill="x", pady=6)

        row = tk.Frame(body, bg=CARD)
        row.pack(fill="x", pady=6)
        tk.Label(row, text="乘车日期", bg=CARD, fg=GRAY, width=8,
                 anchor="w").pack(side="left")
        self.date_var = tk.StringVar()
        self.date_entry = tk.Entry(row, textvariable=self.date_var, width=30,
                                   font=(FONT, 10))
        self.date_entry.pack(side="left")
        attach_calendar(self.date_entry, support_range=True,
                       max_span_days=appcommon.MAX_DATE_SPAN_DAYS)
        tk.Label(body, text="单日：2026-10-06　范围：2026-10-06~2026-10-08",
                 bg=CARD, fg=GRAY, font=(FONT, 8)).pack(anchor="w", pady=(0, 4))

        row = tk.Frame(body, bg=CARD)
        row.pack(fill="x", pady=6)
        tk.Label(row, text="车次", bg=CARD, fg=GRAY, width=8,
                 anchor="w").pack(side="left")
        self.trains_var = tk.StringVar()
        tk.Entry(row, textvariable=self.trains_var, width=30,
                 font=(FONT, 10)).pack(side="left")
        tk.Label(body, text="多个车次用逗号分隔，如：G547,K225（留空=监控全部车次）",
                 bg=CARD, fg=GRAY, font=(FONT, 8)).pack(anchor="w", pady=(0, 4))

        row = tk.Frame(body, bg=CARD)
        row.pack(fill="x", pady=6)
        tk.Label(row, text="票种", bg=CARD, fg=GRAY, width=8, anchor="w").pack(side="left")
        self.purpose_var = tk.StringVar(value="ADULT")
        tk.Radiobutton(row, text="成人票", value="ADULT", variable=self.purpose_var,
                       bg=CARD, activebackground=CARD, font=(FONT, 10),
                       cursor="hand2").pack(side="left", padx=(0, 10))
        tk.Radiobutton(row, text="学生票", value="0X00", variable=self.purpose_var,
                       bg=CARD, activebackground=CARD, font=(FONT, 10),
                       cursor="hand2").pack(side="left")

        tk.Label(body, text="监控席别（可多选 Ctrl/Shift）：", bg=CARD, fg=GRAY,
                 font=(FONT, 9)).pack(anchor="w")
        frame = tk.Frame(body, bg=CARD)
        frame.pack(fill="x")
        self.seat_list = tk.Listbox(frame, height=5, selectmode="multiple",
                                    font=(FONT, 10), relief="flat",
                                    highlightthickness=1, highlightbackground=BORDER)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.seat_list.yview)
        self.seat_list.configure(yscrollcommand=sb.set)
        self.seat_list.pack(side="left", fill="x", expand=True)
        sb.pack(side="right", fill="y")
        for s in SEAT_CHOICES:
            self.seat_list.insert("end", s)

        tk.Label(body, text="乘车人（不选=默认/账号常用）：", bg=CARD, fg=GRAY,
                 font=(FONT, 9)).pack(anchor="w", pady=(8, 0))
        self.passenger_items = []
        self.psg_list = tk.Listbox(body, height=3, selectmode="multiple", font=(FONT, 10),
                                   relief="flat", highlightthickness=1,
                                   highlightbackground=BORDER)
        self.psg_list.pack(fill="x")
        for p in passengers_mod.load_passengers():
            label = "{0}{1}".format(p.get("name"), "（默认）" if p.get("is_default") else "")
            self.psg_list.insert("end", label)
            self.passenger_items.append(p.get("name"))

        row = tk.Frame(body, bg=CARD)
        row.pack(fill="x", pady=8)
        tk.Label(row, text="优先级", bg=CARD, fg=GRAY, width=8,
                 anchor="w").pack(side="left")
        self.prio_var = tk.IntVar(value=5)
        for v in range(1, 11):
            tk.Radiobutton(row, text=str(v), value=v, variable=self.prio_var,
                           bg=CARD, activebackground=CARD, font=(FONT, 8),
                           indicatoron=False, selectcolor=BLUE,
                           cursor="hand2").pack(side="left", padx=1)

        self.auto_var = tk.BooleanVar(value=True)
        tk.Checkbutton(body, text="余票命中后自动下单（不支付）", variable=self.auto_var,
                       bg=CARD, activebackground=CARD, font=(FONT, 10)).pack(anchor="w")
        self.stop_var = tk.BooleanVar(value=True)
        tk.Checkbutton(body, text="一次下单成功后自动停止本任务", variable=self.stop_var,
                       bg=CARD, activebackground=CARD, font=(FONT, 10)).pack(anchor="w")

        ttk.Button(self, text="创建任务并立即开始监视", style="Blue.TButton",
                   command=lambda: self.create(start_now=True)).pack(
            fill="x", padx=18, pady=(6, 2))
        ttk.Button(self, text="仅创建任务", style="White.TButton",
                   command=lambda: self.create(start_now=False)).pack(
            fill="x", padx=18, pady=(0, 10))

    def _parse_dates(self):
        return appcommon.parse_date_range(self.date_var.get().strip())

    def create(self, start_now):
        from_name, to_name = self.from_field.get(), self.to_field.get()
        if from_name not in self.name2code or to_name not in self.name2code:
            messagebox.showwarning("提示", "请先通过「选择」确定有效的出发站/到达站", parent=self)
            return
        if from_name == to_name:
            messagebox.showwarning("提示", "出发站与到达站不能相同", parent=self)
            return
        try:
            dates, date_range = self._parse_dates()
        except ValueError as e:
            messagebox.showwarning("提示", "日期格式错误：%s" % e, parent=self)
            return
        seats = [self.seat_list.get(i) for i in self.seat_list.curselection()]
        if not seats:
            messagebox.showwarning("提示", "请至少选择一个席别", parent=self)
            return
        trains = [t.strip() for t in
                  self.trains_var.get().replace("，", ",").split(",") if t.strip()]
        passengers = [self.passenger_items[i] for i in self.psg_list.curselection()]
        name = "{0}-{1} {2} {3}".format(
            from_name, to_name, "/".join(trains) if trains else "全部车次",
            "/".join(seats))
        task = {
            "name": name,
            "uid": uuid.uuid4().hex,
            "from": from_name,
            "to": to_name,
            "dates": dates,
            "date_range": date_range,
            "trains": trains,
            "seat_types": seats,
            "auto_order": bool(self.auto_var.get()),
            "stop_after_order": bool(self.stop_var.get()),
            "passenger_names": passengers,
            "priority": int(self.prio_var.get()),
            "purpose_code": self.purpose_var.get(),
            "notify_channels": ["email"],
        }
        update_config_locked(lambda config: config.setdefault("tasks", []).append(task))
        mark_task_created(self.app, task, start_now)
        if self.on_created:
            self.on_created()
        self.destroy()
        if start_now:
            self.app.start_engine()
            messagebox.showinfo("完成", "任务「%s」已创建，监控已启动。" % name,
                                parent=self.app.root)
        else:
            messagebox.showinfo("完成", "任务「%s」已创建，可稍后启动监控。" % name,
                                parent=self.app.root)


# ----------------------------- 乘车人管理 -----------------------------

class PassengerDialog(tk.Toplevel):
    def __init__(self, master, on_changed=None):
        super().__init__(master)
        self.title("乘车人管理（加密存储）")
        self.geometry("640x500")
        self.on_changed = on_changed
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=12, pady=8)
        ttk.Label(body, text="乘车人列表（证件号/手机号加密保存，显示时脱敏）：").pack(anchor="w")
        frame = ttk.Frame(body)
        frame.pack(fill="both", expand=True, pady=4)
        self.listbox = tk.Listbox(frame, font=(FONT, 10))
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.refresh()

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=12, pady=10)
        ttk.Button(btns, text="添加", command=lambda: self.edit(None)).pack(side="left", padx=4)
        ttk.Button(btns, text="编辑", command=lambda: self.edit(self.selected())).pack(side="left", padx=4)
        ttk.Button(btns, text="删除", command=self.remove).pack(side="left", padx=4)
        ttk.Button(btns, text="设为默认", command=self.set_default).pack(side="left", padx=4)
        ttk.Button(btns, text="关闭", command=self.destroy).pack(side="right")

    def refresh(self):
        self.passengers = passengers_mod.load_passengers()
        self.listbox.delete(0, "end")
        for p in self.passengers:
            self.listbox.insert("end", "{0}{1}  证件:{2} {3}".format(
                p.get("name"), "【默认】" if p.get("is_default") else "",
                passengers_mod.ID_TYPE_NAMES.get(p.get("id_type_code"), "?"),
                _mask_id(p.get("id_no"))))

    def selected(self):
        sel = self.listbox.curselection()
        return sel[0] if sel else None

    def edit(self, index):
        p = self.passengers[index] if index is not None else None
        dlg = tk.Toplevel(self)
        dlg.title("添加乘车人" if p is None else "编辑乘车人")
        dlg.geometry("440x320")
        body = ttk.Frame(dlg)
        body.pack(fill="both", expand=True, padx=12, pady=10)

        def row(label, widget):
            r = ttk.Frame(body)
            r.pack(fill="x", pady=3)
            ttk.Label(r, text=label, width=10).pack(side="left")
            widget.pack(side="left", fill="x", expand=True)
            return r

        name_var = tk.StringVar(value=(p or {}).get("name", ""))
        row("姓名", ttk.Entry(body, textvariable=name_var))
        type_var = tk.StringVar(value=(p or {}).get("id_type_code", "1"))
        combo = ttk.Combobox(body, textvariable=type_var, state="readonly", width=26)
        combo["values"] = ["%s - %s" % (k, v) for k, v in passengers_mod.ID_TYPE_NAMES.items()]
        combo.set("1 - 二代身份证")
        row("证件类型", combo)
        id_var = tk.StringVar(value=(p or {}).get("id_no", ""))
        id_entry = ttk.Entry(body, textvariable=id_var, show="*")
        id_row = row("证件号码", id_entry)
        # 小眼睛：证件号默认掩码，点一下切明文、再点还原
        id_visible = tk.BooleanVar(value=False)

        def _toggle_id():
            id_entry.configure(show="" if id_visible.get() else "*")
            eye_btn.config(text="🙈" if id_visible.get() else "👁")

        eye_btn = ttk.Button(id_row, text="👁", width=3, command=_toggle_id)
        eye_btn.pack(side="left", padx=(6, 0))
        mobile_var = tk.StringVar(value=(p or {}).get("mobile", ""))
        row("手机号", ttk.Entry(body, textvariable=mobile_var))
        default_var = tk.BooleanVar(value=bool((p or {}).get("is_default")))
        ttk.Checkbutton(body, text="设为默认乘车人（自动下单优先使用）",
                        variable=default_var).pack(anchor="w", pady=3)
        adult_var = tk.BooleanVar(value=bool((p or {}).get("is_adult", True)))
        ttk.Checkbutton(body, text="成人（学生/儿童请取消勾选）",
                        variable=adult_var).pack(anchor="w", pady=3)

        def save():
            name = name_var.get().strip()
            if not name:
                messagebox.showwarning("提示", "姓名不能为空", parent=dlg)
                return
            code = type_var.get().split(" ")[0].strip()
            data = {"name": name, "id_type_code": code,
                    "id_no": id_var.get().strip(), "mobile": mobile_var.get().strip(),
                    "is_default": bool(default_var.get()), "is_adult": bool(adult_var.get())}
            if index is None:
                self.passengers.append(data)
            else:
                self.passengers[index].update(data)
            passengers_mod.save_passengers(self.passengers)
            self.refresh()
            if self.on_changed:
                self.on_changed()
            dlg.destroy()

        btns = ttk.Frame(dlg)
        btns.pack(fill="x", padx=12, pady=10)
        ttk.Button(btns, text="保存", command=save).pack(side="right", padx=6)
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="right")

    def remove(self):
        idx = self.selected()
        if idx is None:
            messagebox.showwarning("提示", "请先选择乘车人", parent=self)
            return
        if messagebox.askyesno("确认", "确定删除乘车人「%s」？" % self.passengers[idx].get("name"),
                               parent=self):
            del self.passengers[idx]
            passengers_mod.save_passengers(self.passengers)
            self.refresh()

    def set_default(self):
        idx = self.selected()
        if idx is None:
            messagebox.showwarning("提示", "请先选择乘车人", parent=self)
            return
        for p in self.passengers:
            p["is_default"] = False
        self.passengers[idx]["is_default"] = True
        passengers_mod.save_passengers(self.passengers)
        self.refresh()
        messagebox.showinfo("完成", "已将「%s」设为默认乘车人" % self.passengers[idx].get("name"),
                            parent=self)


def _mask_id(id_no):
    if not id_no or len(id_no) < 8:
        return id_no or ""
    return id_no[:3] + "*" * (len(id_no) - 7) + id_no[-4:]


# ----------------------------- 购票历史 -----------------------------

HISTORY_RESULT_LABEL = {"success": "下单成功", "dup": "防重跳过",
                        "failed": "下单失败", "hit_no_order": "命中未下单",
                        "ambiguous": "结果待人工核对"}


def read_history_records(limit=200):
    """读购票历史：最近 limit 条、最新在前。文件缺失/损坏返回空列表。"""
    path = os.path.join(HERE, load_config().get("history_file", "order_history.json"))
    try:
        with open(path, encoding="utf-8") as f:
            history = json.load(f)
    except Exception:
        return []
    return list(reversed(history[-limit:]))


class HistoryDialog(tk.Toplevel):
    RESULT_LABEL = HISTORY_RESULT_LABEL

    def __init__(self, master):
        super().__init__(master)
        self.title("购票历史与通知记录")
        self.geometry("960x520")
        cols = ("time", "task", "train", "date", "route", "seat",
                "passengers", "order_no", "result")
        heads = ("时间", "任务", "车次", "日期", "区间", "席别",
                 "乘车人", "订单号", "结果")
        widths = (130, 150, 60, 90, 120, 70, 110, 110, 70)
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=10, pady=8)
        self.tree = ttk.Treeview(frame, columns=cols, show="headings")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="center")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        ttk.Button(self, text="关闭", command=self.destroy).pack(pady=8)
        self.load()

    def load(self):
        for r in read_history_records():
            self.tree.insert("", "end", values=(
                r.get("time", ""), r.get("task", ""), r.get("train", ""),
                r.get("date", ""), "%s-%s" % (r.get("from", ""), r.get("to", "")),
                r.get("seat", ""), "、".join(r.get("passengers") or []),
                r.get("order_no", ""),
                self.RESULT_LABEL.get(r.get("result"), r.get("result", ""))))


# ----------------------------- 通知设置 -----------------------------

class NotifyFormMixin:
    """邮件通知表单公共实现（NotifyDialog 与 NotifyPanel 共用，勿两处各改一份）。

    子类先把 self.email 指到 config 里的 email 字典，再调 _build_notify_fields；
    collect/save/test 三个方法两边完全一致，统一放这里。"""

    def _build_notify_fields(self, body, hint_wraplength):
        def row(label, var, show=None, width=34):
            r = ttk.Frame(body)
            r.pack(fill="x", pady=4)
            ttk.Label(r, text=label, width=12).pack(side="left")
            ttk.Entry(r, textvariable=var, width=width, show=show).pack(
                side="left", fill="x", expand=True)
            return r

        self.enabled_var = tk.BooleanVar(value=bool(self.email.get("enabled", True)))
        ttk.Checkbutton(body, text="启用邮件通知", variable=self.enabled_var).pack(anchor="w", pady=2)
        self.host_var = tk.StringVar(value=self.email.get("smtp_host", "smtp.qq.com"))
        row("SMTP 服务器", self.host_var)
        self.port_var = tk.StringVar(value=str(self.email.get("smtp_port", 465)))
        row("端口", self.port_var, width=10)
        self.user_var = tk.StringVar(value=self.email.get("username", ""))
        row("发件邮箱", self.user_var)
        self.pwd_var = tk.StringVar(value=notify_mod.secret_of(self.email.get("password", "")))
        row("邮箱授权码", self.pwd_var, show="*")
        self.from_var = tk.StringVar(value=self.email.get("from", ""))
        row("发件人地址", self.from_var)
        self.to_var = tk.StringVar(value=",".join(self.email.get("to") or []))
        row("收件人", self.to_var)
        ttk.Label(body, text="收件人多个用逗号分隔；发件人地址留空则默认同发件邮箱；QQ/163 邮箱需在邮箱设置中开启 SMTP 服务并生成授权码",
                  foreground=GRAY, wraplength=hint_wraplength).pack(anchor="w", pady=4)

    def collect(self):
        self.email.update({
            "enabled": bool(self.enabled_var.get()),
            "smtp_host": self.host_var.get().strip(),
            "smtp_port": int(self.port_var.get().strip() or 465),
            "username": self.user_var.get().strip(),
            "password": notify_mod.protect_secret(self.pwd_var.get().strip()),
            "from": self.from_var.get().strip(),
            "to": [x.strip() for x in self.to_var.get().replace("，", ",").split(",")
                   if x.strip()],
        })
        return normalize_email_settings(self.email)

    def save(self):
        try:
            email = self.collect()
        except ValueError as e:
            messagebox.showwarning("无法保存", str(e), parent=self)
            return
        def _save_email(c):
            c.setdefault("notify", {})["email"] = email

        update_config_locked(_save_email)
        messagebox.showinfo("完成", "通知设置已保存", parent=self)

    def test(self):
        try:
            email = self.collect()
        except ValueError as e:
            messagebox.showwarning("无法发送", str(e), parent=self)
            return

        def do_test():
            return notify_mod.send_email(email, "测试邮件：12306 监控系统",
                                         "这是一封测试邮件，收到说明邮件通知可用。")

        def on_done(res, err):
            if err:
                messagebox.showerror("失败", "测试发送异常：%s" % err, parent=self)
            else:
                ok, msg = res
                (messagebox.showinfo if ok else messagebox.showerror)("测试结果", msg, parent=self)

        run_async(self, do_test, on_done)


class NotifyDialog(NotifyFormMixin, tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("邮件通知设置（SMTP）")
        self.geometry("500x420")
        cfg = load_config()
        self.email = cfg.setdefault("notify", {}).setdefault("email", {})
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=12, pady=10)
        self._build_notify_fields(body, hint_wraplength=460)
        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=12, pady=10)
        ttk.Button(btns, text="保存", command=self.save).pack(side="right", padx=6)
        ttk.Button(btns, text="发送测试邮件", command=self.test).pack(side="right", padx=6)


# ----------------------------- 登录会话 -----------------------------

def _relogin_ok_via_script(script):
    """运行 capture_session.py 做重登：仅当退出码为 0 返回 True。

    旧代码 subprocess.run(...) 后 ok = True 硬编码——退出码非 0（登录失败）
    也被当成成功，侧边栏误置"已登录"。超时（300s）同样视为失败返回 False，
    不挂死、不抛到界面。
    """
    try:
        proc = subprocess.run([sys.executable, script], cwd=HERE, timeout=300)
    except subprocess.TimeoutExpired:
        LOG.error("重新登录失败：capture_session.py 运行超时（300s）")
        return False
    ok = proc.returncode == 0
    if not ok:
        LOG.error("重新登录失败：capture_session.py 退出码 %s", proc.returncode)
    return ok


class SessionDialog(tk.Toplevel):
    def __init__(self, master, app=None):
        super().__init__(master)
        self.app = app          # MonitorApp：把登录态同步给侧边栏/小窗
        self.title("登录会话")
        self.geometry("520x440")
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=12, pady=10)
        self.status_label = ttk.Label(body, text="正在检查会话状态...")
        self.status_label.pack(anchor="w", pady=2)

        ttk.Label(body, text="账号已保存的乘车人（下单时可选用）：").pack(anchor="w", pady=(8, 2))
        frame = ttk.Frame(body)
        frame.pack(fill="both", expand=True)
        self.p_list = tk.Listbox(frame, font=(FONT, 10))
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.p_list.yview)
        self.p_list.configure(yscrollcommand=sb.set)
        self.p_list.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=12, pady=10)
        ttk.Button(btns, text="重新登录（弹出浏览器）", command=self.relogin).pack(side="left", padx=4)
        ttk.Button(btns, text="刷新", command=self.check).pack(side="left", padx=4)
        ttk.Button(btns, text="关闭", command=self.destroy).pack(side="right")
        self.check()

    def check(self):
        def do_check():
            if (load_config().get("order_mode") or "http") == "browser":
                # 浏览器模式：会话以 .browser_profile 为准
                import browser_order
                if browser_order.busy():
                    # 登录窗口/下单还开着，抢锁只会超时。别把"没轮到"说成"已失效"。
                    return None, "浏览器忙（登录或下单进行中），稍后再刷新", []
                ok, who = browser_order.check_session(timeout=6)
                passengers = []
                if ok:
                    # 乘车人列表用浏览器 Cookie 组会话取（浏览器模式的登录态
                    # 在 .browser_profile，session_cookies.json 是过期的旧载体）
                    try:
                        sess = order_mod.session_from_browser_state()
                        passengers = order_mod.get_passengers(sess)
                    except Exception:
                        passengers = []
                return ok, who, passengers

            sess = order_mod.load_session(load_config().get("session_cookies_file"))
            ok, who = order_mod.check_login(sess)
            passengers = order_mod.get_passengers(sess) if ok else []
            return ok, who, passengers

        def on_done(res, err):
            if err:
                self.status_label.config(text="会话检查失败：%s" % err)
                return
            ok, who, passengers = res
            if ok is None:
                self.status_label.config(text=str(who))
                return
            self.status_label.config(
                text="会话有效（%s）" % who if ok else "会话失效：%s（请重新登录）" % who)
            # 同步侧边栏/小窗：免得弹窗说已登录、左上角还挂着未登录
            if self.app is not None:
                self.app._set_account(
                    ("已登录 %s" % who) if ok else "未登录", bool(ok))
            self.p_list.delete(0, "end")
            for p in passengers:
                self.p_list.insert("end", "%s（%s）" % (
                    p["name"], p.get("type_name") or ("成人" if p.get("is_adult", True) else "非成人")))

        self.status_label.config(text="正在检查会话状态...")
        run_async(self, do_check, on_done)

    def relogin(self):
        script = os.path.join(HERE, "capture_session.py")
        self.status_label.config(text="浏览器已打开，请在弹出的窗口中完成登录（最多 5 分钟）...")
        self.p_list.delete(0, "end")
        self.p_list.insert("end", "登录完成后会自动刷新，稍候即可")

        def worker():
            ok = False
            try:
                if (load_config().get("order_mode") or "http") == "browser":
                    # 浏览器模式：登录态要落进 .browser_profile，capture_session.py
                    # 只写 session_cookies.json，两条路不通用。
                    import browser_order
                    ok = bool(browser_order.login())
                else:
                    # capture_session.py 若挂住不能永久卡住重登线程；
                    # 按退出码判定成败：失败不再被当成功
                    ok = _relogin_ok_via_script(script)
            except Exception as e:
                LOG.error("重新登录失败: %s", e)
            # 跨线程调 after 违反 Tk 规则，走与 run_async 相同的队列
            _RESULT_QUEUE.put((self, lambda _res, _err: self._after_relogin(ok), None, None))

        threading.Thread(target=worker, daemon=True).start()

    def _after_relogin(self, ok):
        """登录流程一结束就先把界面切成已登录，不等下一次会话体检。

        浏览器模式下引擎体检是 20 分钟一次，只靠它的话，用户明明刚在浏览器里
        登录成功，侧边栏还要再挂十几分钟「未登录」。"""
        if ok and self.app is not None:
            self.app._set_account("已登录", True)
            self.app._acct_time = time.time()
        self.check()


# ----------------------------- 余票速查 -----------------------------

class QuickCheckDialog(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("余票速查（免登录）")
        self.geometry("880x520")
        try:
            self.name2code, self.code2name = ticket.load_station_map()
        except Exception as e:
            messagebox.showerror("错误", "车站代码表加载失败：%s" % e, parent=self)
            self.destroy()
            return
        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=8)
        self.from_field = StationField(top, "出发站")
        self.from_field.pack(side="left", padx=4)
        self.to_field = StationField(top, "到达站")
        self.to_field.pack(side="left", padx=4)
        ttk.Label(top, text="日期").pack(side="left", padx=(8, 2))
        self.date_var = tk.StringVar(value=datetime.date.today().isoformat())
        self.date_entry = ttk.Entry(top, textvariable=self.date_var, width=12)
        self.date_entry.pack(side="left")
        attach_calendar(self.date_entry)
        ttk.Button(top, text="查询", command=self.query).pack(side="left", padx=8)

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=12, pady=4)
        cols = ("train", "route", "time", "duration", "seats")
        heads = ("车次", "区间", "时间", "历时", "余票")
        widths = (80, 160, 130, 70, 340)
        self.tree = ttk.Treeview(frame, columns=cols, show="headings")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c == "seats" else "center")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.status = ttk.Label(self, text="")
        self.status.pack(anchor="w", padx=12, pady=(0, 8))

    def query(self):
        from_name, to_name = self.from_field.get(), self.to_field.get()
        date = self.date_var.get().strip()
        if from_name not in self.name2code or to_name not in self.name2code:
            messagebox.showwarning("提示", "请选择有效的车站", parent=self)
            return
        if from_name == to_name:
            messagebox.showwarning("提示", "出发站与到达站不能相同", parent=self)
            return
        self.status.config(text="查询中...")
        self.tree.delete(*self.tree.get_children())

        def do_query():
            rows = ticket.query_tickets(self.name2code[from_name],
                                        self.name2code[to_name], date)
            out = []
            for row in rows:
                info = ticket.parse_row(row, self.code2name)
                seats = " ".join("{0}({1})".format(k, v)
                                 for k, v in info["available_seats"].items()) \
                    or "暂无可视余票"
                out.append((info["train_code"],
                            "%s-%s" % (info["from_name"], info["to_name"]),
                            "%s-%s" % (info["start_time"], info["arrive_time"]),
                            info["duration"], seats))
            return out

        def on_done(res, err):
            if err:
                self.status.config(text="")
                messagebox.showerror("查询失败", str(err), parent=self)
                return
            for row in res:
                self.tree.insert("", "end", values=row)
            self.status.config(text="共 %d 趟" % len(res))

        run_async(self, do_query, on_done)


# ----------------------------- 启动监控（选择任务） -----------------------------

class StartMonitorDialog(tk.Toplevel):
    """启动监控时选择要运行哪些任务；已启动的任务标记「已启动」并锁定勾选。"""

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("启动监控（选择任务）")
        self.geometry("560x500")
        self.configure(bg=CARD)

        header = tk.Frame(self, bg=BLUE)
        header.pack(fill="x")
        tk.Label(header, text="启动监控", bg=BLUE, fg="white",
                 font=(FONT, 14, "bold"), pady=10).pack(side="left", padx=16)
        tk.Label(header, text="勾选要监控的任务", bg=BLUE, fg="#DCEAFB",
                 font=(FONT, 9)).pack(side="right", padx=16)

        self.running = bool(app.engine_thread and app.engine_thread.is_alive())
        tip_text = ("引擎正在运行：已启动的任务自动锁定；新勾选的任务会自动纳入，取消勾选转为「已暂停」。"
                    if self.running else "勾选要启动的任务，点击「启动所选任务」开始监控。")
        tk.Label(self, text=tip_text, bg="#FFF4E0", fg="#B26A00",
                 font=(FONT, 9), justify="left", wraplength=500).pack(
            fill="x", padx=12, pady=8)

        self.task_rows = tk.Frame(self, bg=CARD)
        self.task_rows.pack(fill="both", expand=True, padx=14)

        config = load_config()
        state = load_state().get("tasks", {})
        self.vars = {}
        tasks = config.get("tasks") or []
        if not tasks:
            tk.Label(self.task_rows, text="暂无任务，请先创建任务。", bg=CARD, fg=GRAY,
                     font=(FONT, 11)).pack(pady=30)
        for idx, t in enumerate(tasks):
            name = t.get("name") or ""
            st = state.get(name, {}).get("status", "paused")
            active = st in engine_mod.ACTIVE_STATUSES
            started = self.running and active
            var = tk.BooleanVar(value=active)
            self.vars[idx] = var

            row = tk.Frame(self.task_rows, bg=CARD, highlightthickness=1,
                           highlightbackground=BORDER)
            row.pack(fill="x", pady=3)
            cb = tk.Checkbutton(row, variable=var, bg=CARD, activebackground=CARD,
                                cursor="hand2",
                                state=("disabled" if started else "normal"))
            cb.pack(side="left", padx=6)
            info = tk.Frame(row, bg=CARD)
            info.pack(side="left", fill="x", expand=True)
            tk.Label(info, text=name, bg=CARD, fg=TEXT,
                     font=(FONT, 11, "bold"), anchor="w").pack(fill="x")
            sub = "{0} → {1}   {2}   {3}  优先级 {4}".format(
                t.get("from", ""), t.get("to", ""),
                format_dates(t),
                "/".join(t.get("trains") or []) or "全部车次",
                t.get("priority", 5))
            tk.Label(info, text=sub, bg=CARD, fg=GRAY, font=(FONT, 9),
                     anchor="w").pack(fill="x")
            if started:
                tk.Label(row, text="已启动", bg=BLUE_LIGHT, fg=BLUE,
                         font=(FONT, 10, "bold"), padx=8).pack(side="right", padx=10)
            else:
                color = STATUS_COLOR.get(st, TEXT)
                tk.Label(row, text=engine_mod.STATUS_LABELS.get(st, st),
                         bg=CARD, fg=color, font=(FONT, 10)).pack(side="right", padx=10)

        btns = tk.Frame(self, bg=BG)
        btns.pack(fill="x", side="bottom")
        ttk.Button(btns, text="启动所选任务", style="Blue.TButton",
                   command=self.apply_start).pack(side="right", padx=12, pady=8)
        ttk.Button(btns, text="关闭", style="White.TButton",
                   command=self.destroy).pack(side="right")

    def apply_start(self):
        if not any(v.get() for v in self.vars.values()):
            messagebox.showwarning("未选择任务",
                                   "请至少勾选一个要启动的任务。\n全部取消勾选相当于不启动任何监控。",
                                   parent=self)
            return
        config = load_config()
        eng = self.app.get_ops_engine()
        for idx, t in enumerate(config.get("tasks") or []):
            var = self.vars.get(idx)
            if var is None:
                continue
            st = eng.task_status(t)
            if var.get():
                if st not in engine_mod.ACTIVE_STATUSES:
                    eng.set_task_status(t, "monitoring", "由启动对话框启用", force=True)
            else:
                if st in engine_mod.ACTIVE_STATUSES:
                    eng.set_task_status(t, "paused", "在启动对话框中取消勾选")
        self.app.refresh_tasks()
        self.app.start_engine()
        self.app.refresh_tasks()
        self.destroy()


# ----------------------------- 侧边栏 -----------------------------

SIDEBAR_WIDTH = 190
SIDEBAR_COLLAPSED = 58


class SidebarPanel(tk.Frame):
    """左侧导航栏：账号状态区 + 四项导航（管理信息含子项"乘车人信息"），支持展开/收起。"""

    def __init__(self, master, app):
        super().__init__(master, bg=CARD, width=SIDEBAR_WIDTH)
        self.pack_propagate(False)
        self.app = app
        self.expanded = True
        self.submenu_open = True
        self.active_key = "tasks"
        self.items = {}

        # 顶部账号状态（点击可切换账号）
        self.acc = tk.Frame(self, bg=CARD, cursor="hand2")
        self.acc.pack(fill="x", pady=(10, 2))
        self.avatar = tk.Label(self.acc, text="客", bg=BLUE, fg="white",
                               font=(FONT, 11, "bold"), width=3)
        self.avatar.pack(side="left", padx=(10, 6))
        self.account_text = tk.Label(self.acc, text="未登录", bg=CARD, fg=GRAY,
                                     font=(FONT, 10, "bold"), anchor="w")
        self.account_text.pack(side="left", fill="x", expand=True)
        self.account_arrow = tk.Label(self.acc, text="›", bg=CARD, fg=GRAY,
                                      font=(FONT, 12))
        self.account_arrow.pack(side="left", padx=(0, 10))
        for w in (self.acc, self.avatar, self.account_text, self.account_arrow):
            w.bind("<Button-1>", lambda e: self.app.open_session())

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x", padx=8)
        self.nav = tk.Frame(self, bg=CARD)
        self.nav.pack(fill="both", expand=True, pady=4)

        # 导航项（固定顺序）
        self._add_item("tasks", "▤", "任务状态", lambda: self.app.show_page("tasks"))
        self._add_item("grab", "⚡", "抢票任务", lambda: self.app.show_page("grab"))
        self._add_item("history", "◷", "购票历史", lambda: self.app.show_page("history"))
        self._add_item("notify", "✉", "通知设置", lambda: self.app.show_page("notify"))

        self._apply_visuals()

    def _add_item(self, key, icon, text, cmd, sub=False):
        lbl = tk.Label(self.nav, bg=CARD, fg=TEXT, font=(FONT, 10),
                       anchor="w", cursor="hand2", padx=12, pady=9)
        lbl.bind("<Button-1>", lambda e, c=cmd: c())
        self.items[key] = (lbl, icon, text, sub)

    def set_active(self, key):
        self.active_key = key
        self._apply_visuals()

    def set_expanded(self, expanded):
        if self.expanded == expanded:
            return
        self.expanded = expanded
        self.config(width=SIDEBAR_WIDTH if expanded else SIDEBAR_COLLAPSED)
        if expanded:
            self.account_text.pack(side="left", fill="x", expand=True)
            self.account_arrow.pack(side="left", padx=(0, 10))
            self.avatar.pack(side="left", padx=(10, 6))
        else:
            self.account_text.pack_forget()
            self.account_arrow.pack_forget()
        self._apply_visuals()

    def set_account(self, text, logged_in):
        if self.expanded:
            self.account_text.config(text=text, fg=(TEXT if logged_in else GRAY))
        self.avatar.config(text="✓" if logged_in else "客",
                           bg=("#1a7f37" if logged_in else BLUE))

    def _apply_visuals(self):
        # 先全部卸载，再按固定顺序重装（保证隐藏/展开后顺序不变）
        for _key, (lbl, _i, _t, _s) in self.items.items():
            lbl.pack_forget()
        for key, (lbl, icon, text, sub) in self.items.items():
            if sub and (not self.expanded or not self.submenu_open):
                continue
            if self.expanded:
                prefix = "    " if sub else ""
                lbl.config(text="{0}  {1}{2}".format(icon, prefix, text),
                           anchor="w", padx=12)
            else:
                lbl.config(text=icon, anchor="w", padx=18)
            if key == self.active_key:
                lbl.config(bg=BLUE, fg="white", font=(FONT, 10, "bold"))
            else:
                lbl.config(bg=CARD, fg=TEXT, font=(FONT, 10))
            lbl.pack(fill="x", pady=1)


# ----------------------------- 购票历史页 -----------------------------

class HistoryPanel(ttk.Frame):
    RESULT_LABEL = {"success": "下单成功", "dup": "防重跳过",
                    "failed": "下单失败", "hit_no_order": "命中未下单"}

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        head = ttk.Frame(self)
        head.pack(fill="x", pady=(4, 2))
        ttk.Label(head, text="购票历史与通知记录：", font=(FONT, 11, "bold")).pack(side="left")
        ttk.Button(head, text="刷新", style="White.TButton", command=self.load).pack(side="right")

        cols = ("time", "task", "train", "date", "route", "seat",
                "passengers", "order_no", "result")
        heads = ("时间", "任务", "车次", "日期", "区间", "席别",
                 "乘车人", "订单号", "结果")
        widths = (130, 150, 60, 90, 110, 70, 110, 110, 70)
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=cols, show="headings")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c in ("time", "task", "route",
                                                             "passengers") else "center")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.load()

    def load(self):
        self.tree.delete(*self.tree.get_children())
        for r in read_history_records():
            self.tree.insert("", "end", values=(
                r.get("time", ""), r.get("task", ""), r.get("train", ""),
                r.get("date", ""), "%s-%s" % (r.get("from", ""), r.get("to", "")),
                r.get("seat", ""), "、".join(r.get("passengers") or []),
                r.get("order_no", ""),
                self.RESULT_LABEL.get(r.get("result"), r.get("result", ""))))


# ----------------------------- 通知设置页 -----------------------------

class NotifyPanel(NotifyFormMixin, ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        cfg = load_config()
        self.email = cfg.setdefault("notify", {}).setdefault("email", {})
        head = ttk.Frame(self)
        head.pack(fill="x", pady=(4, 2))
        ttk.Label(head, text="邮件通知设置（SMTP 授权码）：", font=(FONT, 11, "bold")).pack(side="left")

        body = ttk.Frame(self, padding=8)
        body.pack(fill="both", expand=True)
        self._build_notify_fields(body, hint_wraplength=640)

        btns = ttk.Frame(self)
        btns.pack(fill="x", pady=8)
        ttk.Button(btns, text="保存", style="Blue.TButton", command=self.save).pack(side="right", padx=6)
        ttk.Button(btns, text="发送测试邮件", style="White.TButton", command=self.test).pack(side="right", padx=6)


# ----------------------------- 任务状态页 -----------------------------

class TaskPage(ttk.Frame):
    """任务状态页：显示所有已创建任务；「已暂停」状态的选中任务可再编辑。"""

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self._task_by_iid = {}

        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(4, 6))
        ttk.Button(toolbar, text="新建任务（查后选）", style="Blue.TButton",
                   command=app.open_wizard).pack(side="left", padx=3)
        ttk.Button(toolbar, text="直接监视（免查询）", style="Light.TButton",
                   command=app.open_quick_monitor).pack(side="left", padx=3)
        app.start_btn = ttk.Button(toolbar, text="▶ 启动监控", style="Blue.TButton",
                                   command=app.open_start_dialog)
        app.start_btn.pack(side="left", padx=3)
        app.stop_btn = ttk.Button(toolbar, text="■ 停止监控", style="White.TButton",
                                  command=app.stop_engine, state="disabled")
        app.stop_btn.pack(side="left", padx=3)
        self.edit_btn = ttk.Button(toolbar, text="编辑任务", style="White.TButton",
                                   command=self.edit_selected, state="disabled")
        self.edit_btn.pack(side="left", padx=3)
        ttk.Button(toolbar, text="余票速查", style="White.TButton",
                   command=app.open_quick_check).pack(side="right", padx=3)

        ttk.Label(self, text="说明：右键任务可进行 编辑 / 暂停 / 恢复 / 取消 / 重置 / 删除 操作；"
                             "任务处于「已暂停」状态时可再编辑。",
                  foreground="#B26A00", font=(FONT, 9)).pack(anchor="w", pady=(0, 4))

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True)
        cols = ("started", "idx", "name", "route", "dates", "trains", "seats",
                "prio", "status", "msg")
        heads = ("启动状态", "#", "任务名称", "区间", "日期", "车次", "席别",
                 "优先级", "状态", "备注")
        widths = (76, 34, 150, 90, 140, 90, 110, 60, 80, 180)
        self.tree = ttk.Treeview(frame, columns=cols, show="headings")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c in ("name", "msg") else "center")
        for st, color in STATUS_COLOR.items():
            self.tree.tag_configure(st, foreground=color)
        self.tree.tag_configure("started", foreground="#1a7f37")
        self.tree.tag_configure("stopped", foreground="#8A94A6")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Button-3>", self._popup_menu)

    def refresh(self):
        try:
            tasks = load_config().get("tasks") or []
            state = load_state().get("tasks", {})
        except Exception:
            return
        engine_running = bool(self.app.engine_thread
                              and self.app.engine_thread.is_alive())
        rows = []
        for i, t in enumerate(tasks, 1):
            st = state.get(t.get("name"), {}).get("status", "paused")
            msg = state.get(t.get("name"), {}).get("message", "")
            # 启动状态：引擎运行中且任务处于 监控中/等待重试 = 已启动
            started = engine_running and st in engine_mod.ACTIVE_STATUSES
            rows.append((
                "● 已启动" if started else "○ 未启动",
                i, t.get("name", ""), "%s-%s" % (t.get("from", ""), t.get("to", "")),
                format_dates(t),
                "/".join(t.get("trains") or []) or "全部",
                "/".join(t.get("seat_types") or []),
                t.get("priority", 5),
                engine_mod.STATUS_LABELS.get(st, st), msg,
                "started" if started else "stopped", st))
        # 内容没变就不重建：避免每 2 秒清空用户选中的行、重置滚动位置
        sig = repr(rows)
        if sig == getattr(self, "_last_rows_sig", None):
            return
        self._last_rows_sig = sig
        sel_names = set()
        for i in self.tree.selection():
            t = self._task_by_iid.get(i, (None, None))[0]
            if t:
                sel_names.add(t.get("name"))
        yview = self.tree.yview()
        self.tree.delete(*self.tree.get_children())
        self._task_by_iid = {}
        for i, t in enumerate(tasks, 1):
            row = rows[i - 1]
            iid = self.tree.insert("", "end", values=row[:10],
                                   tags=(row[10], row[11]))
            self._task_by_iid[iid] = (t, row[11])
        # 恢复刷新前的选中任务与滚动位置
        for iid, (t, _st) in self._task_by_iid.items():
            if t.get("name") in sel_names:
                self.tree.selection_set(iid)
                break
        if yview:
            self.tree.yview_moveto(yview[0])
        self._on_select()

    def _on_select(self, _evt=None):
        sel = self.tree.selection()
        if not sel:
            self.edit_btn.config(state="disabled")
            return
        _task, st = self._task_by_iid.get(sel[0], (None, None))
        # 任务在暂停状态可再编辑
        if st == "paused":
            self.edit_btn.config(state="normal")
        else:
            self.edit_btn.config(state="disabled")

    def edit_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先在列表中选择一个任务", parent=self)
            return
        task, _st = self._task_by_iid.get(sel[0], (None, None))
        if task is None:
            return
        # 实时从 state.json 读取最新状态，避免使用过期缓存导致误判
        fresh = load_state().get("tasks", {}).get(task.get("name"), {}).get(
            "status", "paused")
        if fresh != "paused":
            messagebox.showwarning("提示",
                                   "只有「已暂停」的任务可以编辑。\n请先右键该任务并选择「暂停监控」。",
                                   parent=self)
            return
        TaskEditDialog(self.app, task, on_saved=self.app.refresh_tasks)

    # ----------------------------- 右键菜单 -----------------------------

    def _popup_menu(self, evt):
        iid = self.tree.identify_row(evt.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        self._on_select()
        task, st = self._task_by_iid.get(iid, (None, None))
        if task is None:
            return
        menu = tk.Menu(self.tree, tearoff=0)
        if st == "paused":
            menu.add_command(label="编辑任务", command=self.edit_selected)
        else:
            menu.add_command(label="编辑任务（需先暂停）", state="disabled")
        menu.add_separator()
        menu.add_command(label="暂停监控",
                         command=lambda: self._op(task, "paused"))
        menu.add_command(label="恢复监控",
                         command=lambda: self._op(task, "monitoring"))
        menu.add_command(label="取消任务",
                         command=lambda: self._op(task, "cancelled"))
        menu.add_command(label="重置并清除防重记录",
                         command=lambda: self._op(task, "reset"))
        menu.add_command(label="删除任务",
                         command=lambda: self._op(task, "delete"))
        menu.tk_popup(evt.x_root, evt.y_root)

    def _op(self, task, action):
        """右键菜单的操作执行器。"""
        name = task.get("name") or ""
        eng = self.app.get_ops_engine()
        info = None
        try:
            if action == "paused":
                eng.set_task_status(task, "paused", "右键菜单暂停")
                info = "任务「%s」已暂停。" % name
            elif action == "monitoring":
                eng.set_task_status(task, "monitoring", "右键菜单恢复", force=True)
                info = "任务「%s」已恢复监控。" % name
            elif action == "cancelled":
                if messagebox.askyesno("确认", "确定取消任务「%s」？取消后不再监控。" % name, parent=self):
                    eng.set_task_status(task, "cancelled", "右键菜单取消")
            elif action == "reset":
                if messagebox.askyesno("确认", "确定重置「%s」并清除其防重下单记录？" % name, parent=self):
                    eng.empty_task_dedup(task)
                    eng.set_task_status(task, "monitoring", "右键菜单重置", force=True)
            elif action == "delete":
                if messagebox.askyesno("确认", "确定从配置中删除任务「%s」？" % name, parent=self):
                    delete_task_everywhere(self.app, task)
        finally:
            self.app.refresh_tasks()
        # 先刷新列表再弹窗，减小阻塞期间被旧状态覆盖的窗口
        if info:
            messagebox.showinfo("完成", info, parent=self)


# ----------------------------- 编辑任务（暂停态） -----------------------------

class TaskEditDialog(tk.Toplevel):
    """编辑已暂停的任务：日期 / 车次 / 席别 / 乘车人 / 优先级等，保存后覆盖原任务。"""

    def __init__(self, app, task, on_saved=None):
        super().__init__(app.root)
        self.app = app
        self.task = task
        self.on_saved = on_saved
        self.title("编辑任务")
        self.geometry("480x660")
        self.configure(bg=CARD)
        try:
            self.name2code, self.code2name = ticket.load_station_map()
        except Exception as e:
            messagebox.showerror("错误", "车站代码表加载失败：%s" % e, parent=self)
            self.destroy()
            return

        header = tk.Frame(self, bg=BLUE)
        header.pack(fill="x")
        tk.Label(header, text="编辑任务", bg=BLUE, fg="white",
                 font=(FONT, 14, "bold"), pady=10).pack(side="left", padx=16)
        tk.Label(header, text=task.get("name", ""), bg=BLUE, fg="#DCEAFB",
                 font=(FONT, 9)).pack(side="right", padx=16)

        body = tk.Frame(self, bg=CARD)
        body.pack(fill="both", expand=True, padx=18)

        self.from_field = StationField(body, "出发站", big=True)
        self.from_field.pack(fill="x", pady=6)
        self.from_field.entry.insert(0, task.get("from", ""))
        self.to_field = StationField(body, "到达站", big=True)
        self.to_field.pack(fill="x", pady=6)
        self.to_field.entry.insert(0, task.get("to", ""))

        dates = engine_mod.expand_dates(task)
        date_text = dates[0] if len(dates) == 1 else ("%s~%s" % (dates[0], dates[-1])) if dates else ""
        row = tk.Frame(body, bg=CARD)
        row.pack(fill="x", pady=6)
        tk.Label(row, text="乘车日期", bg=CARD, fg=GRAY, width=8,
                 anchor="w").pack(side="left")
        self.date_var = tk.StringVar(value=date_text)
        self.date_entry = tk.Entry(row, textvariable=self.date_var, width=30,
                                   font=(FONT, 10))
        self.date_entry.pack(side="left")
        attach_calendar(self.date_entry, support_range=True,
                       max_span_days=appcommon.MAX_DATE_SPAN_DAYS)
        tk.Label(body, text="单日：2026-10-07　范围：2026-10-07~2026-10-09",
                 bg=CARD, fg=GRAY, font=(FONT, 8)).pack(anchor="w", pady=(0, 4))

        row = tk.Frame(body, bg=CARD)
        row.pack(fill="x", pady=6)
        tk.Label(row, text="车次", bg=CARD, fg=GRAY, width=8,
                 anchor="w").pack(side="left")
        self.trains_var = tk.StringVar(value=",".join(task.get("trains") or []))
        tk.Entry(row, textvariable=self.trains_var, width=30, font=(FONT, 10)).pack(side="left")
        tk.Label(body, text="多个车次用逗号分隔；留空=监控全部车次",
                 bg=CARD, fg=GRAY, font=(FONT, 8)).pack(anchor="w", pady=(0, 4))

        row = tk.Frame(body, bg=CARD)
        row.pack(fill="x", pady=6)
        tk.Label(row, text="票种", bg=CARD, fg=GRAY, width=8, anchor="w").pack(side="left")
        self.purpose_var = tk.StringVar(value=task.get("purpose_code") or "ADULT")
        tk.Radiobutton(row, text="成人票", value="ADULT", variable=self.purpose_var,
                       bg=CARD, activebackground=CARD, font=(FONT, 10),
                       cursor="hand2").pack(side="left", padx=(0, 10))
        tk.Radiobutton(row, text="学生票", value="0X00", variable=self.purpose_var,
                       bg=CARD, activebackground=CARD, font=(FONT, 10),
                       cursor="hand2").pack(side="left")

        tk.Label(body, text="监控席别（Ctrl/Shift 多选）：", bg=CARD, fg=GRAY,
                 font=(FONT, 9)).pack(anchor="w")
        self.seat_list = tk.Listbox(body, height=5, selectmode="multiple",
                                    font=(FONT, 10), relief="flat",
                                    highlightthickness=1, highlightbackground=BORDER)
        self.seat_list.pack(fill="x")
        for s in SEAT_CHOICES:
            self.seat_list.insert("end", s)
        for s in (task.get("seat_types") or []):
            if s in SEAT_CHOICES:
                self.seat_list.selection_set(SEAT_CHOICES.index(s))

        tk.Label(body, text="乘车人（不选=默认/账号常用）：", bg=CARD, fg=GRAY,
                 font=(FONT, 9)).pack(anchor="w", pady=(8, 0))
        self.psg_items = []
        self.psg_list = tk.Listbox(body, height=3, selectmode="multiple", font=(FONT, 10),
                                   relief="flat", highlightthickness=1,
                                   highlightbackground=BORDER)
        self.psg_list.pack(fill="x")
        for p in passengers_mod.load_passengers():
            label = "{0}{1}".format(p.get("name"), "（默认）" if p.get("is_default") else "")
            self.psg_list.insert("end", label)
            self.psg_items.append(p.get("name"))
        for n in (task.get("passenger_names") or []):
            if n in self.psg_items:
                self.psg_list.selection_set(self.psg_items.index(n))

        row = tk.Frame(body, bg=CARD)
        row.pack(fill="x", pady=8)
        tk.Label(row, text="优先级", bg=CARD, fg=GRAY, width=8,
                 anchor="w").pack(side="left")
        self.prio_var = tk.IntVar(value=int(task.get("priority") or 5))
        for v in range(1, 11):
            tk.Radiobutton(row, text=str(v), value=v, variable=self.prio_var,
                           bg=CARD, activebackground=CARD, font=(FONT, 8),
                           indicatoron=False, selectcolor=BLUE,
                           cursor="hand2").pack(side="left", padx=1)

        self.auto_var = tk.BooleanVar(value=bool(task.get("auto_order", True)))
        tk.Checkbutton(body, text="余票命中后自动下单（不支付）", variable=self.auto_var,
                       bg=CARD, activebackground=CARD, font=(FONT, 10)).pack(anchor="w")
        self.stop_var = tk.BooleanVar(value=bool(task.get("stop_after_order", True)))
        tk.Checkbutton(body, text="一次下单成功后自动停止本任务", variable=self.stop_var,
                       bg=CARD, activebackground=CARD, font=(FONT, 10)).pack(anchor="w")

        btns = tk.Frame(self, bg=BG)
        btns.pack(fill="x", side="bottom")
        ttk.Button(btns, text="保存修改", style="Blue.TButton",
                   command=self.save).pack(side="right", padx=12, pady=8)
        ttk.Button(btns, text="取消", style="White.TButton",
                   command=self.destroy).pack(side="left", padx=12, pady=8)

    def save(self):
        from_name, to_name = self.from_field.get(), self.to_field.get()
        if from_name not in self.name2code or to_name not in self.name2code:
            messagebox.showwarning("提示", "请先通过「选择」确定有效的出发站/到达站", parent=self)
            return
        if from_name == to_name:
            messagebox.showwarning("提示", "出发站与到达站不能相同", parent=self)
            return
        raw = self.date_var.get().strip()
        dates, date_range = [], []
        try:
            dates, date_range = appcommon.parse_date_range(raw)
        except ValueError as e:
            messagebox.showwarning("提示", "日期格式错误：%s" % e, parent=self)
            return
        seats = [self.seat_list.get(i) for i in self.seat_list.curselection()]
        if not seats:
            messagebox.showwarning("提示", "请至少选择一个席别", parent=self)
            return
        trains = [t.strip() for t in
                  self.trains_var.get().replace("，", ",").split(",") if t.strip()]
        passengers = [self.psg_items[i] for i in self.psg_list.curselection()]

        new_task = {
            "name": self.task.get("name"),
            "uid": self.task.get("uid"),
            "from": from_name,
            "to": to_name,
            "dates": dates,
            "date_range": date_range,
            "trains": trains,
            "seat_types": seats,
            "auto_order": bool(self.auto_var.get()),
            "stop_after_order": bool(self.stop_var.get()),
            "passenger_names": passengers,
            "priority": int(self.prio_var.get()),
            "purpose_code": self.purpose_var.get(),
            "notify_channels": self.task.get("notify_channels") or ["email"],
            "seats_by_date": {},  # 编辑为全局席别模式（按日席别需在向导重建）
        }
        def _replace_task(config):
            uid = self.task.get("uid")
            for i, t in enumerate(config.get("tasks") or []):
                if (uid and t.get("uid") == uid) or \
                        (not uid and t.get("name") == self.task.get("name")):
                    config["tasks"][i] = new_task
                    return
            config.setdefault("tasks", []).append(new_task)

        update_config_locked(_replace_task)
        if self.on_saved:
            self.on_saved()
        self.destroy()
        messagebox.showinfo("完成", "任务「%s」已更新。\n在主界面启动监控后按新参数运行。" % new_task["name"],
                            parent=self.app.root)


# ----------------------------- 主窗口 -----------------------------

# ----------------------------- 小窗模式 -----------------------------

# 小窗只展示"抢票相关"的事件：带这些标签的日志才进去。
# 引擎启动横幅、每轮 [运行] 播报、[查询重试]、[配置] 同步之类全部过滤。
MINI_LOG_TAGS = ("[有票]", "[抢到]", "[错误]", "[冷却]", "[防重]",
                 "[跳过]", "[会话]", "[状态]")

MINI_STAT_COLOR = STATUS_COLOR  # 与任务页状态色同一份（改色只改 STATUS_COLOR）

MINI_LOG_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2}),\d+ \[\w+\] (.*)$")


class MiniWindow(tk.Toplevel):
    """极简小窗：登录状态 + 正在进行的任务 + 抢票日志。

    纯展示，不承载任何操作入口。关闭时隐藏而不销毁，再次打开日志是连续的。
    """

    MAX_LINES = 400
    TRIM_LINES = 60

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.primed = False
        self.pinned = False
        self._wrap = 0
        self._wrap_labels = []
        self._last_sig = None

        self.title("12306 抢票小窗")
        self.geometry("460x420")
        self.minsize(340, 260)
        self.configure(bg=BG)
        # 点 × 关闭小窗：隐藏小窗并恢复主窗口（回到打开小窗前的界面）
        self.protocol("WM_DELETE_WINDOW", self.close_mini)

        head = tk.Frame(self, bg=BLUE)
        head.pack(fill="x")
        tk.Label(head, text="抢票小窗", bg=BLUE, fg="white",
                 font=(FONT, 11, "bold")).pack(side="left", padx=12, pady=8)

        self.pin_btn = tk.Button(head, text="置顶", command=self.toggle_pin,
                                 bg=BLUE, fg="white", activebackground=BLUE_DARK,
                                 activeforeground="white", relief="flat", bd=0,
                                 font=(FONT, 9), cursor="hand2")
        self.pin_btn.pack(side="right", padx=(0, 12))

        self.acct_label = tk.Label(head, text="检查中...", bg=BLUE, fg="white",
                                   font=(FONT, 10))
        self.acct_label.pack(side="right", padx=(0, 6))
        self.acct_dot = tk.Label(head, text="●", bg=BLUE, fg="#DCEAFB",
                                 font=(FONT, 9))
        self.acct_dot.pack(side="right")

        # 任务区：只有状态标识 + 备注
        self.task_box = tk.Frame(self, bg=BG)
        self.task_box.pack(fill="x", pady=(6, 4))

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x", padx=10)

        log_wrap = tk.Frame(self, bg=BG)
        log_wrap.pack(fill="both", expand=True, padx=10, pady=(4, 8))
        self.log_text = scrolledtext.ScrolledText(
            log_wrap, state="disabled", wrap="word", font=("Consolas", 9),
            bg=CARD, relief="flat", highlightthickness=0)
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_configure("hit", foreground="#d97706")
        self.log_text.tag_configure("bought", foreground="#0969da")
        self.log_text.tag_configure("err", foreground="#cf222e")
        self.log_text.tag_configure("misc", foreground=GRAY)

        self.bind("<Configure>", self._on_resize)

    # ---- 登录状态 ----

    def set_account(self, text, logged_in):
        self.acct_label.config(text=text or "未登录")
        self.acct_dot.config(fg=("#5AE08A" if logged_in else "#DCEAFB"))

    # ---- 任务区 ----

    def _collect(self):
        """活跃任务在后，已成功的任务置顶——抢到票是最终结果，不能从小窗里错过。"""
        try:
            tasks = load_config().get("tasks") or []
            state = load_state().get("tasks") or {}
        except Exception:
            return []
        live, done = [], []
        for t in tasks:
            name = t.get("name", "")
            rec = state.get(name) or {}
            st = rec.get("status", "paused")
            if st == "success":
                done.append((name, st, rec.get("message", "")))
            elif st in engine_mod.ACTIVE_STATUSES:
                live.append((name, st, rec.get("message", "")))
        return done + live

    def refresh_tasks(self):
        rows = self._collect()
        sig = repr(rows)
        if sig == self._last_sig:
            return
        self._last_sig = sig

        for w in self.task_box.winfo_children():
            w.destroy()
        self._wrap_labels = []

        if not rows:
            tk.Label(self.task_box, text="暂无正在进行的任务", bg=BG, fg=GRAY,
                     font=(FONT, 10)).pack(anchor="w", padx=14, pady=8)
            return

        for name, st, msg in rows:
            color = MINI_STAT_COLOR.get(st, GRAY)
            card = tk.Frame(self.task_box, bg=CARD)
            card.pack(fill="x", padx=10, pady=3)
            tk.Frame(card, bg=color, width=3).pack(side="left", fill="y")
            inner = tk.Frame(card, bg=CARD)
            inner.pack(side="left", fill="x", expand=True, padx=9, pady=7)

            top = tk.Frame(inner, bg=CARD)
            top.pack(fill="x")
            tk.Label(top, text="●", bg=CARD, fg=color,
                     font=(FONT, 9)).pack(side="left")
            tk.Label(top, text=engine_mod.STATUS_LABELS.get(st, st), bg=CARD,
                     fg=color, font=(FONT, 10, "bold")).pack(side="left", padx=(4, 0))

            self._wrap_labels.append(
                self._mklabel(inner, name, TEXT, (FONT, 10), pady=(3, 0)))
            if msg:
                self._wrap_labels.append(
                    self._mklabel(inner, msg, GRAY, (FONT, 9)))

        self._apply_wrap(self._wrap or max(180, self.winfo_width() - 80))

    def _mklabel(self, parent, text, fg, font, pady=0):
        lbl = tk.Label(parent, text=text, bg=CARD, fg=fg, font=font,
                       anchor="w", justify="left")
        lbl.pack(fill="x", pady=pady)
        return lbl

    def _apply_wrap(self, w):
        self._wrap = w
        for lbl in self._wrap_labels:
            try:
                lbl.config(wraplength=w)
            except Exception:
                pass

    def _on_resize(self, evt):
        if evt.widget is not self:
            return
        w = max(180, evt.width - 80)
        if abs(w - self._wrap) < 12:
            return
        self._apply_wrap(w)

    # ---- 日志 ----

    def feed_log(self, record):
        m = MINI_LOG_RE.match(record or "")
        if not m:
            return
        tstamp, msg = m.group(1), m.group(2)
        if not any(tag in msg for tag in MINI_LOG_TAGS):
            return

        if "[抢到]" in msg:
            tag = "bought"
        elif "[有票]" in msg:
            tag = "hit"
        elif "[错误]" in msg:
            tag = "err"
        else:
            tag = "misc"

        self.log_text.config(state="normal")
        self.log_text.insert("end", "%s  %s\n" % (tstamp, msg), tag)
        self.log_text.see("end")
        try:
            line_no = int(self.log_text.index("end-1c").split(".")[0])
            if line_no > self.MAX_LINES:
                self.log_text.delete("1.0", "%d.0" % (self.TRIM_LINES + 1))
        except Exception:
            pass
        self.log_text.config(state="disabled")

    def clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    # ---- 窗口 ----

    def toggle_pin(self):
        self.pinned = not self.pinned
        try:
            self.attributes("-topmost", self.pinned)
        except Exception:
            pass
        self.pin_btn.config(text="已置顶" if self.pinned else "置顶",
                            bg=(BLUE_DARK if self.pinned else BLUE))

    def hide(self):
        """程序化隐藏小窗（不恢复主窗口）。"""
        self.withdraw()

    def close_mini(self):
        """点击小窗 × 关闭：隐藏小窗，并让主窗口回到打开小窗前的界面状态。"""
        self.withdraw()
        try:
            self.app.restore_main()
        except Exception:
            pass


class MonitorApp:
    def __init__(self, root):
        self.root = root
        root.title("12306 车票监控与自动购票系统（桌面版）")
        root.geometry("1200x800")
        root.minsize(940, 660)
        apply_style(root)
        try:
            root.tk.call("tk", "scaling", 1.15)
        except Exception:
            pass

        self.stop_event = None
        self.engine_thread = None
        self.live_engine = None
        self._acct_time = 0.0
        self.mini = None

        # 蓝色顶部栏：侧边栏开关 + 标题 + 引擎状态
        header = tk.Frame(root, bg=BLUE)
        header.pack(fill="x")
        self.toggle_btn = tk.Button(header, text="☰", command=self.toggle_sidebar,
                                    bg=BLUE, fg="white", activebackground=BLUE_DARK,
                                    activeforeground="white", relief="flat",
                                    font=(FONT, 13, "bold"), bd=0, cursor="hand2",
                                    width=3)
        self.toggle_btn.pack(side="left", padx=(10, 4), pady=6)
        tk.Label(header, text="12306 车票监控与自动购票系统", bg=BLUE, fg="white",
                 font=(FONT, 14, "bold"), pady=12).pack(side="left", padx=4)
        self.engine_label = tk.Label(header, text="引擎：未运行", bg=BLUE, fg="#DCEAFB",
                                     font=(FONT, 10))
        self.engine_label.pack(side="right", padx=16)
        tk.Button(header, text="小窗模式", command=self.open_mini,
                  bg=BLUE_DARK, fg="white", activebackground=BLUE,
                  activeforeground="white", relief="flat", bd=0,
                  font=(FONT, 10), cursor="hand2", padx=10).pack(
                      side="right", padx=(0, 12), pady=8)

        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True)

        # 左侧边栏
        self.sidebar = SidebarPanel(body, self)
        self.sidebar.pack(side="left", fill="y")

        # 右侧：内容页 + 底部日志
        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True)
        self.content = tk.Frame(right, bg=BG)
        self.content.pack(fill="both", expand=True, padx=10, pady=(8, 2))

        self.pages = {}
        self.pages["tasks"] = TaskPage(self.content, self)
        self.pages["history"] = HistoryPanel(self.content, self)
        self.pages["notify"] = NotifyPanel(self.content, self)
        # 抢票任务：多任务管理器（每个任务独立窗口、独立配置，可并行抢票）
        self.pages["grab"] = launcher.TaskManagerPanel(self.content)
        self.pages["grab"].configure(bg=BG)
        self.task_page = self.pages["tasks"]

        log_head = tk.Frame(right, bg=BG)
        log_head.pack(fill="x", padx=10, pady=(6, 2))
        tk.Label(log_head, text="运行日志：", bg=BG, fg=GRAY,
                 font=(FONT, 10)).pack(side="left")
        tk.Button(log_head, text="清空日志", command=self.clear_log, bg=BG, fg=BLUE,
                  activebackground=BG, activeforeground=BLUE, relief="flat",
                  font=(FONT, 9), cursor="hand2").pack(side="right")
        log_frame = tk.Frame(right, bg=BG)
        log_frame.pack(fill="both", expand=False, padx=10, pady=(0, 8))
        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=9, state="disabled", wrap="word",
            font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_configure("ERROR", foreground="#cf222e")
        self.log_text.tag_configure("WARNING", foreground="#9a6700")

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Configure>", self._on_resize)
        self.root.bind("<Control-m>", lambda _e: self.open_mini())
        start_async_poller(self.root)
        self.show_page("tasks")
        self.refresh_account()
        self.poll_log()
        self.root.after(2000, self.tick)

    # ----------------------------- 侧边栏与页面 -----------------------------

    def show_page(self, key):
        if key not in self.pages:
            return
        for page in self.pages.values():
            page.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        mapping = {"tasks": "tasks", "history": "history", "notify": "notify",
                   "grab": "grab"}
        self.sidebar.set_active(mapping[key])

    def toggle_sidebar(self):
        self._auto_collapsed = False
        self.sidebar.set_expanded(not self.sidebar.expanded)

    def _on_resize(self, evt):
        # 响应式：窗口过窄时自动收起侧边栏，变宽后自动展开
        if evt.widget is not self.root:
            return
        if evt.width < 980 and self.sidebar.expanded \
                and not getattr(self, "_auto_collapsed", False):
            self._auto_collapsed = True
            self.sidebar.set_expanded(False)
        elif evt.width >= 1000 and not self.sidebar.expanded \
                and getattr(self, "_auto_collapsed", False):
            self._auto_collapsed = False
            self.sidebar.set_expanded(True)

    def _set_account(self, text, logged_in):
        """登录态的唯一出口：侧边栏与小窗一起更新，两边不会再显示打架。"""
        self.sidebar.set_account(text, logged_in)
        if self.mini is not None and self.mini.winfo_exists():
            self.mini.set_account(text, logged_in)

    def refresh_account(self):
        """异步校验登录会话，更新侧边栏账号状态（已登录时显示手机尾号后四位）。

        浏览器模式下不再在这里自查：真实会话在 .browser_profile 里，而这里
        只会去验已弃用的 session_cookies.json，结果显示的是假的"已登录"。
        该模式的真实状态由引擎的会话体检日志驱动，见 poll_log。"""
        self._acct_time = time.time()

        if (load_config().get("order_mode") or "http") == "browser":
            # 浏览器模式：真实会话在 .browser_profile，必须用它校验；
            # 查 session_cookies.json 只会得到一个跟现实无关的结果。
            def do_check_browser():
                import browser_order
                # 登录窗口/下单正在跑的时候抢锁只会超时，这一轮直接跳过。
                if browser_order.busy():
                    return None
                ok, who = browser_order.check_session(timeout=6)
                return ok, who or ""

            def on_done_browser(res, err):
                # 浏览器忙（res=None）或校验本身失败（err）：都保持现有显示。
                # 之前这两种情况一律落到"未登录"，于是刚在浏览器里登录成功，
                # 侧边栏却是"未登录"——典型的误报。
                if res is None:
                    if err is not None:
                        LOG.debug("会话校验未完成：%s", err)
                    return
                ok, who = res
                if ok:
                    self._set_account(
                        "已登录 {0}".format(who) if who else "已登录", True)
                else:
                    self._set_account("未登录", False)

            run_async(self.root, do_check_browser, on_done_browser)
            return

        def do_check():
            try:
                sess = order_mod.load_session(load_config().get("session_cookies_file"))
                return order_mod.verify_session(sess)
            except Exception:
                return False, "", False

        def on_done(res, err):
            ok, who, permanent = res or (False, "", True)
            if ok:
                digits = re.findall(r"\d", who or "")
                tail = "".join(digits)[-4:]
                self._set_account("已登录 {0}".format(tail) if tail else "已登录", True)
            elif permanent:
                self._set_account("未登录", False)
            else:
                # 临时网络故障：不显示"未登录"，避免误导
                self._set_account("登录校验失败（网络）", False)

        run_async(self.root, do_check, on_done)

    # ----------------------------- 引擎控制 -----------------------------

    def get_ops_engine(self):
        """状态操作引擎：引擎在运行时直接复用其实例（共享内存状态），
        避免新实例的旧状态把 GUI 的修改覆盖回 state.json。"""
        if self.live_engine is not None and self.engine_thread \
                and self.engine_thread.is_alive():
            return self.live_engine
        return engine_mod.MonitorEngine(setup_logging=False)

    def start_engine(self):
        if self.engine_thread and self.engine_thread.is_alive():
            return
        try:
            self.live_engine = engine_mod.MonitorEngine(setup_logging=False)
        except Exception as e:
            messagebox.showerror("启动失败", "引擎初始化失败：%s" % e, parent=self.root)
            return
        self.stop_event = threading.Event()
        self.engine_thread = threading.Thread(
            target=self.live_engine.run, args=(self.stop_event,), daemon=True)
        self.engine_thread.start()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.engine_label.config(text="引擎：运行中")
        self.refresh_tasks()

    def stop_engine(self):
        if self.stop_event is not None:
            self.stop_event.set()
        self.engine_label.config(text="引擎：正在停止...")
        self.stop_btn.config(state="disabled")

    def tick(self):
        if self.engine_thread and not self.engine_thread.is_alive():
            self.engine_thread = None
            self.stop_event = None
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")
            self.engine_label.config(text="引擎：已停止")
        elif self.engine_thread and self.engine_thread.is_alive():
            try:
                tasks = load_config().get("tasks") or []
                state = load_state().get("tasks", {})
                n = sum(1 for t in tasks
                        if state.get(t.get("name"), {}).get("status")
                        in engine_mod.ACTIVE_STATUSES)
                self.engine_label.config(text="引擎：运行中（%d 个任务）" % n)
            except Exception:
                pass
        # 浏览器模式的校验要开一次浏览器，间隔放宽到 3 分钟
        # （登录窗口开着时这一轮会被 busy() 跳过，不会去撞锁）
        try:
            acct_iv = 180 if (load_config().get("order_mode") or "http") == "browser" else 60
            if time.time() - self._acct_time > acct_iv:
                self.refresh_account()
            self.refresh_tasks()
        except Exception:
            LOG.exception("tick 刷新失败（不影响下一轮）")
        finally:
            # after 必须无条件重排：否则任何一次刷新异常都会永久杀死 2 秒轮询链
            try:
                self.root.after(2000, self.tick)
            except tk.TclError:
                pass

    # ----------------------------- 任务列表 -----------------------------

    def refresh_tasks(self):
        if hasattr(self, "task_page"):
            self.task_page.refresh()
        if self.mini is not None and self.mini.winfo_exists():
            self.mini.refresh_tasks()

    # ----------------------------- 日志 -----------------------------

    def poll_log(self):
        # 单条日志处理异常（如控件被销毁的 TclError）不能逃出循环，
        # 否则 after 轮询链断掉，主日志/小窗/会话失效弹窗全部静默停摆
        while True:
            try:
                record = LOG_QUEUE.get_nowait()
            except queue.Empty:
                break
            try:
                # 会话永久失效：立即弹窗提醒，避免用户一直蒙在鼓里
                if "[错误] 登录会话已失效" in record:
                    self._set_account("登录已失效", False)
                    self._warn_session_dead()
                elif "[会话] 登录状态正常" in record:
                    self._session_dead_warned = False
                    m = re.search(r"登录状态正常[:：]\s*(\S+)", record)
                    self._set_account(
                        "已登录 {0}".format(m.group(1)) if m else "已登录", True)
                if self.mini is not None and self.mini.winfo_exists():
                    self.mini.feed_log(record)
                self.log_text.config(state="normal")
                tag = None
                if "[ERROR]" in record or "[FATAL]" in record:
                    tag = "ERROR"
                elif "[WARNING]" in record:
                    tag = "WARNING"
                self.log_text.insert("end", record + "\n", tag)
                if float(self.log_text.index("end-1c")) > 3000.0:
                    self.log_text.delete("1.0", "100.0")
                self.log_text.see("end")
                self.log_text.config(state="disabled")
            except Exception:
                pass
        try:
            self.root.after(300, self.poll_log)
        except tk.TclError:
            pass

    def _warn_session_dead(self):
        """会话永久失效时弹窗（只弹一次，重新登录成功后自动复位）。
        点「是」直接拉起浏览器重登；登录完成后引擎会把因会话失效而失败的任务自动恢复。"""
        if getattr(self, "_session_dead_warned", False):
            return
        self._session_dead_warned = True
        if messagebox.askyesno(
                "登录会话已失效",
                "12306 登录已失效，自动下单已停止。\n\n"
                "点「是」立即打开浏览器重新登录（登录成功后任务自动恢复）；\n"
                "点「否」稍后自行处理。",
                parent=self.root):
            if (load_config().get("order_mode") or "http") == "browser":
                # 必须在本进程里开线程，不能 Popen 新进程：_BROWSER_LOCK 是进程内
                # 的，另一个进程照样会去抢同一个 .browser_profile，浏览器启动即退
                # （exitCode=21），而且会和 GUI 自己的体检撞车。
                def _relogin():
                    ok = False
                    try:
                        import browser_order
                        ok = bool(browser_order.login())
                    except Exception as e:
                        LOG.error("重新登录失败: %s", e)
                    _RESULT_QUEUE.put((self.root,
                                       lambda _r, _e: self._after_warn_relogin(ok),
                                       None, None))

                threading.Thread(target=_relogin, daemon=True).start()
            else:
                subprocess.Popen(
                    [sys.executable, os.path.join(HERE, "capture_session.py")], cwd=HERE)

    def _after_warn_relogin(self, ok):
        """弹窗触发的重登结束后立刻恢复界面登录态并复查。"""
        if ok:
            self._session_dead_warned = False
            self._set_account("已登录", True)
            self._acct_time = time.time()
            self.refresh_account()

    def clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")
        if self.mini is not None and self.mini.winfo_exists():
            self.mini.clear_log()

    # ----------------------------- 对话框 -----------------------------

    def open_wizard(self):
        TaskWizard(self, on_created=self.refresh_tasks)

    def open_start_dialog(self):
        StartMonitorDialog(self)

    def open_quick_monitor(self):
        QuickMonitorDialog(self, on_created=self.refresh_tasks)

    def open_history(self):
        self.show_page("history")

    def open_notify(self):
        self.show_page("notify")

    def open_session(self):
        SessionDialog(self.root, app=self)

    def open_quick_check(self):
        QuickCheckDialog(self.root)

    # ----------------------------- 小窗模式 -----------------------------

    def open_mini(self):
        """打开极简小窗；已打开则前置。首次打开会回灌主窗口里已有的日志。
        小窗模式下隐藏主窗口，界面只显示小窗内容；点 × 关闭后自动恢复主窗口。"""
        if self.mini is None or not self.mini.winfo_exists():
            try:
                self.mini = MiniWindow(self)
            except Exception as e:
                messagebox.showerror("小窗模式", "小窗创建失败：%s" % e,
                                     parent=self.root)
                return
        try:
            self.mini.deiconify()
            self.mini.lift()
        except Exception:
            return
        self.mini.refresh_tasks()
        if not self.mini.primed:
            self.mini.primed = True
            try:
                text = self.log_text.get("1.0", "end")
            except Exception:
                text = ""
            for line in text.splitlines():
                self.mini.feed_log(line)
        # 只显示小窗：主窗口隐藏（widget 不销毁，页面/侧边栏状态原样保留）
        try:
            self.root.withdraw()
        except Exception:
            pass

    def restore_main(self):
        """小窗关闭后恢复主窗口，回到打开小窗前的界面状态。
        页面、侧边栏、窗口尺寸等从未改动，仅重新显示并前置。"""
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    def on_close(self):
        if self.engine_thread and self.engine_thread.is_alive():
            if not messagebox.askyesno("确认", "监控引擎正在运行，确定停止并退出？", parent=self.root):
                return
            self.stop_event.set()
            self.engine_thread.join(timeout=5)
        self.root.destroy()


def _ensure_stdio():
    """pythonw 下没有控制台，sys.stdout / sys.stderr 是 None。

    Playwright 启动浏览器时会往标准输出写日志并调用 flush()，
    在 None 上调用会抛 AttributeError，浏览器进程随即退出（exitCode=21）。
    这里把它们接到日志上：既不再崩，也能在运行日志里看到浏览器说了什么。
    """
    class _Writer(object):
        def __init__(self, level):
            self._level = level
            self._buf = ""

        def write(self, s):
            if not s:
                return 0
            # \r 结尾的进度条输出没有换行，会让缓冲无限增长：统一按换行处理
            self._buf += s.replace("\r\n", "\n").replace("\r", "\n")
            if len(self._buf) > 65536:  # 兜底：超长无换行输出强制落日志
                line, self._buf = self._buf, ""
                LOG.log(self._level, "[stdio] %s", line[:60000])
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    LOG.log(self._level, "[stdio] %s", line)
            return len(s)

        def flush(self):
            if self._buf.strip():
                LOG.log(self._level, "[stdio] %s", self._buf)
            self._buf = ""

        def isatty(self):
            return False

    if sys.stdout is None:
        sys.stdout = _Writer(logging.INFO)
    if sys.stderr is None:
        sys.stderr = _Writer(logging.WARNING)


def main():
    setup_logging()
    _ensure_stdio()
    if not os.path.exists(CONFIG_PATH):
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("错误", "未找到 config.json（%s）" % CONFIG_PATH)
        root.destroy()
        return
    root = tk.Tk()
    MonitorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()