# -*- coding: utf-8 -*-
"""12306 车票监控与自动购票系统 —— 极简单窗口版（Tkinter，无第三方 UI 依赖）。

【legacy / 已停用】2026-10-14 起界面为 Electron 版（ui/ + api.py + electron/），本文件不再是任何入口，
新功能一律改 ui/app.js。保留原因：tests/test_all.py 有 120+ 处引用（GrabTaskForm/GrabTab/NewTaskDialog/
PassengerDialog/QueryHistoryDialog 及其 patch 目标），且 TestConfigKeys._PRODUCT_FILES 把它列为产品文件；
移动或删除会连带推翻该测试基线。仅作功能参照与回归测试对象维护，不再新增界面能力。

2026-10-13 界面重构（推翻旧玻璃拟态 UI 与「监控/抢票」双窗口架构）：
    - 单窗口 + 页签：监控任务 / 抢票任务 / 查票 / 乘车人 / 历史 / 设置 + 底部全局日志
    - 全部原生 ttk 控件：无图片、无动画、无自定义绘制、无主题引擎
    - launcher.py 只保留抢票业务（Grabber 等），全部界面收敛到本文件

功能（全保留）：
    监控任务    新建（单表单）/ 编辑（暂停态）/ 启动 / 暂停 / 恢复 / 取消 / 重置 / 删除
    抢票任务    任务库（新建 / 改名 / 删除）+ 每任务编辑（行程 / 日期 / 车次 / 席别 /
                首选席别规则 / 乘车人 / 开抢时间 / 提醒 / 预热 / 预设 / 历史记录 / 定位）
    查票        免登录实时余票查询，可一键用查询条件创建监控任务
    乘车人      加密库增删改 / 设为默认
    历史        购票记录（order_history.json）
    设置        邮件通知（SMTP）/ 登录会话（检查 / 重新登录）/ 引擎与更新

启动
    双击「启动桌面版.bat」，或：python gui.py

合规说明
    - 余票数据来自 12306 官方公开查询接口；下单走官网同款提交流程
    - 不绕过验证码、不执行支付（订单提交成功后停留在未支付状态）
"""

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
from tkinter import messagebox, simpledialog, ttk

import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

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

# 状态色（语义色，无主题引擎；涨红跌绿不涉及，仅成功/警示/失败三档）
STATUS_COLOR = {
    "monitoring": "#0969da",
    "retrying": "#d97706",
    "paused": "#6e7781",
    "cancelled": "#6e7781",
    "success": "#1a7f37",
    "failed": "#cf222e",
}

# 全局日志按事件标签着色（沿用旧 launcher 的事件分级；纯 Text tag，无自定义绘制）
LOG_COLOR = {
    "[有票]": "#d97706", "[抢到]": "#0969da", "[错误]": "#cf222e",
    "[会话]": "#6e7781", "[运行]": "#57606a", "[提醒]": "#9a6700", "[更新]": "#8250df",
    "[自动]": "#cf222e", "[启动]": "#0961da", "[停止]": "#6e7781", "[提示]": "#8250df",
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


_CONFIG_CORRUPT_WARNED = False


def load_config():
    """读 config.json。文件损坏（JSON 解析失败、非 UTF-8 编码）或解析成功但
    顶层不是 dict 时记一次 error 并返回空配置，GUI 按缺省/空配置启动
    （Task 75a；Task 29 只覆盖了 monitor 侧缺文件；Task 94a 补 UnicodeDecodeError
    与非 dict 口径——旧代码只抓 JSONDecodeError，GBK 等非 UTF-8 文件的
    UnicodeDecodeError 逃出崩启动，[] 等非 dict 则在下游 .get() 处崩）。"""
    global _CONFIG_CORRUPT_WARNED
    try:
        with open(CONFIG_PATH, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        if not _CONFIG_CORRUPT_WARNED:
            _CONFIG_CORRUPT_WARNED = True
            LOG.error("config.json JSON 解析失败或非 UTF-8 编码，将以空配置启动：%s",
                      CONFIG_PATH)
        return {}
    if not isinstance(data, dict):
        if not _CONFIG_CORRUPT_WARNED:
            _CONFIG_CORRUPT_WARNED = True
            LOG.error("config.json 顶层不是 dict 对象（%s），将以空配置启动：%s",
                      type(data).__name__, CONFIG_PATH)
        return {}
    return data


def _lock_timeout_abort():
    """file_lock 锁争用超时的统一出口：记日志、给用户明确提示，再上抛友好异常。

    与 monitor._lock_timeout_abort 同名不同语义（那边是 CLI print，不弹窗），
    勿合并——RULES.md:47「各写方的防护语义是参数不是噪音」。

    调用方多为 Tk 回调：弹窗已告知用户"文件被占用，稍后重试"，Tk 打印
    traceback 但界面存活——不崩；用户稍后重试该操作即可。
    """
    LOG.warning("文件锁争用超时：另一进程正长时间持有，本次操作跳过")
    messagebox.showwarning("文件被占用", "文件被占用，稍后重试")
    raise TimeoutError("文件被占用，稍后重试")


def save_config(config):
    # 原子写 + 跨进程锁：与 launcher 的读-改-写互斥（config.json.lock）
    # Task 66: config.json 含 SMTP 授权码等密钥，落盘 0600
    try:
        with filelock.file_lock(CONFIG_PATH + ".lock"):
            appcommon.atomic_write_json(CONFIG_PATH, config, mode=0o600)
    except TimeoutError:
        _lock_timeout_abort()


def load_state():
    config = load_config()
    return engine_mod.load_state_file(
        os.path.join(HERE, config.get("state_file", "state.json")))


def _state_tasks_dict():
    """读 state.json 的 tasks 节：顶层非 dict 或 tasks 节非 dict 时记警告并返回 {}。

    Task 101f：state.json 为 [] 等非对象形状时，旧代码 load_state().get("tasks")
    直接抛 AttributeError，导致"启动监控"/"编辑任务"对话框打不开。"""
    try:
        state = load_state()
    except Exception as e:
        LOG.warning("state.json 读取失败（%s），已按空状态处理", e)
        return {}
    if not isinstance(state, dict):
        LOG.warning("state.json 顶层不是对象（%s），已按空状态处理",
                    type(state).__name__)
        return {}
    tasks = state.get("tasks", {})
    if not isinstance(tasks, dict):
        LOG.warning("state.json 的 tasks 节不是对象，已按空处理")
        return {}
    return tasks


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

    Task 84b：config.json 损坏（JSON 解析失败）时友好中止——弹 error 提示并抛
    友好异常（不 traceback、不覆盖旧文件）；与 Task 75 读侧口径一致。
    """
    try:
        with filelock.file_lock(CONFIG_PATH + ".lock"):
            try:
                with open(CONFIG_PATH, encoding="utf-8-sig") as f:
                    config = json.load(f)
            except (json.JSONDecodeError, UnicodeDecodeError):
                LOG.warning("config.json 已损坏，拒绝覆盖写入：%s", CONFIG_PATH)
                messagebox.showerror(
                    "配置损坏",
                    "config.json 已损坏，本次保存已取消（旧文件未被覆盖）。\n"
                    "请手动修复该文件，或备份后删除让程序重建。",
                    parent=None)
                raise RuntimeError(
                    "config.json 已损坏：本次保存已取消，旧文件未被覆盖")
            if not isinstance(config, dict):
                # Task 103d：合法 JSON 但非 dict（如手改的 []）——写侧同样友好
                # 拒绝（与解析失败同口径）：记 warning + 弹 error，不抛裸 TypeError；
                # 拒绝点在 mutator 之前，无任何写入，旧文件不丢。
                LOG.warning("config.json 内容非 dict（%s），拒绝覆盖写入：%s",
                            type(config).__name__, CONFIG_PATH)
                messagebox.showerror(
                    "配置损坏",
                    "config.json 内容不是对象，本次保存已取消（旧文件未被覆盖）。\n"
                    "请手动修复该文件，或备份后删除让程序重建。",
                    parent=None)
                raise RuntimeError(
                    "config.json 内容非 dict：本次保存已取消，旧文件未被覆盖")
            result = mutator(config)
            # Task 66: config.json 含密钥，落盘 0600
            appcommon.atomic_write_json(CONFIG_PATH, config, mode=0o600)
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
            if not isinstance(state, dict):
                # Task 75e：合法非 dict JSON（如 []）直接透传会导致 mutator 的
                # state.setdefault 抛 AttributeError；记 warning 后按 {} 处理
                # （与 Task 62c 的 appcommon 读路径口径一致）。
                LOG.warning("state.json 内容非 dict（%s），已按空状态处理：%s",
                            type(state).__name__, path)
                state = {}
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


def _date_display_text(dates):
    """Task 75d：任务编辑器的日期展示文本。返回 (text, non_contiguous)。

    连续多日沿用「首日~末日」区间写法；非连续多日如实列出（、分隔），
    回写时见 _resolve_saved_dates（绝不静默扩展为连续区间）。
    """
    ds = sorted(set(dates or []))
    if not ds:
        return "", False
    if len(ds) == 1:
        return ds[0], False
    try:
        dd = [datetime.date.fromisoformat(d) for d in ds]
    except (ValueError, TypeError):
        # 非法条目：原样列出，交由回写决策处理（不静默扩展）
        return "、".join(ds), True
    contig = all((dd[i + 1] - dd[i]).days == 1 for i in range(len(dd) - 1))
    if contig:
        return "%s~%s" % (ds[0], ds[-1]), False
    return "、".join(ds), True


def _resolve_saved_dates(orig_text, non_contig, orig_dates, orig_range, raw_text):
    """Task 75d：任务编辑器的日期回写决策。

    非连续展示且用户未改动输入 → 原样返回原 dates/date_range，
    绝不静默扩展为连续区间；否则走 parse_date_range
    （非法抛 ValueError，调用方弹提示）。
    """
    if non_contig and (raw_text or "").strip() == (orig_text or ""):
        return list(orig_dates or []), list(orig_range or [])
    return appcommon.parse_date_range(raw_text)


# 展示归一化单源在 appcommon（原两处内联实现迁走，语义不变）；别名保留，
# gui 内调用点与测试 pin 不变。
_display_trains = appcommon.display_trains
_display_seat_types = appcommon.display_seat_types


def _build_edited_task(task, *, from_name, to_name, dates, date_range, trains,
                       seat_types, passengers, auto_order, stop_after_order,
                       priority, purpose_code):
    """TaskEditDialog.save 的纯函数部分：组装更新后的任务 dict（便于单测）。

    Task 75b：seats_by_date 原样保留——编辑器不支持按日席别编辑，
    静默抹除会绕过 engine 的按日席别（:608），导致在用户排除的日期
    下单不想要的席别。未来编辑器支持按日编辑时，改为传入编辑后的值。
    """
    return {
        "name": task.get("name"),
        "uid": task.get("uid"),
        "from": from_name,
        "to": to_name,
        "dates": dates,
        "date_range": date_range,
        "trains": trains,
        "seat_types": seat_types,
        "auto_order": auto_order,
        "stop_after_order": stop_after_order,
        "passenger_names": passengers,
        "priority": priority,
        "purpose_code": purpose_code,
        "notify_channels": task.get("notify_channels") or ["email"],
        "seats_by_date": task.get("seats_by_date") or {},
    }


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


def station_key(name):
    common = ("北京", "上海", "广州", "深圳", "杭州", "南京", "武汉",
              "成都", "重庆", "西安", "郑州", "长沙")
    for i, c in enumerate(common):
        if name.startswith(c):
            return (0, i, name)
    return (1, 0, name)


# ----------------------------- 车站输入与选择 -----------------------------

class StationEntry(ttk.Frame):
    """车站输入框：一个普通 Entry + 「选」按钮（弹出车站搜索框）。

    保留旧 StationsEntry 的 get()/set()/history 接口（抢票表单历史预热用）。"""

    def __init__(self, master, width=14, history=None, **kw):
        super().__init__(master, **kw)
        self.var = tk.StringVar()
        self.entry = ttk.Entry(self, textvariable=self.var, width=width)
        self.entry.pack(side="left")
        ttk.Button(self, text="选", width=3, command=self.pick).pack(
            side="left", padx=(2, 0))
        self.history = [h for h in (history or []) if h]

    def get(self):
        return self.var.get().strip()

    def set(self, value):
        self.var.set(value or "")

    def set_history(self, items):
        self.history = [h for h in (items or []) if h]

    def pick(self):
        def on_pick(name):
            self.set(name)
        StationPickerDialog(self, on_pick, history=self.history)


class StationPickerDialog(tk.Toplevel):
    """车站搜索：输入站名/拼音简拼/全拼/电报码，双击选择。"""

    def __init__(self, master, on_pick, history=None):
        super().__init__(master)
        self.on_pick = on_pick
        self.title("选择车站")
        self.geometry("360x460")
        self.transient(master.winfo_toplevel())
        try:
            self.name2code, self.code2name = ticket.load_station_map()
        except Exception as e:
            messagebox.showerror("错误", "车站代码表加载失败：%s" % e, parent=self)
            self.destroy()
            return
        self.all_names = sorted(self.name2code.keys(), key=station_key)
        self.history = [h for h in (history or []) if h]

        ttk.Label(self, text="输入站名 / 拼音简拼 / 全拼 / 电报码：").pack(
            anchor="w", padx=10, pady=(10, 2))
        self.entry = ttk.Entry(self)
        self.entry.pack(fill="x", padx=10)
        self.entry.bind("<KeyRelease>", self.on_type)
        ttk.Label(self, text="匹配结果（双击选择）：").pack(anchor="w", padx=10, pady=(8, 2))
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self.listbox = tk.Listbox(frame)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.listbox.bind("<Double-Button-1>", self.choose)
        self.listbox.bind("<Return>", self.choose)
        ttk.Button(self, text="选定", command=self.choose).pack(pady=(0, 10))
        self.on_type()
        self.entry.focus_set()
        self.grab_set()

    def on_type(self, _evt=None):
        kw = self.entry.get().strip()
        self.listbox.delete(0, "end")
        if not kw:
            for n in self.history[:8]:
                self.listbox.insert("end", n)
            return
        try:
            items = launcher.search_stations(kw, limit=100)
        except Exception:
            items = []
        names = [it["name"] for it in items if isinstance(it, dict)]
        if not names:
            names = [n for n in self.all_names if kw in n][:100]
        for n in names:
            self.listbox.insert("end", n)

    def choose(self, _evt=None):
        sel = self.listbox.curselection()
        if sel:
            self.on_pick(self.listbox.get(sel[0]))
            self.destroy()


# ----------------------------- 监控任务页 -----------------------------

class TaskPage(ttk.Frame):
    """任务状态页：显示所有已创建任务；「已暂停」状态的选中任务可再编辑。"""

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self._task_by_iid = {}
        # Task 75c：日期展示文本缓存。refresh 每 2 秒跑一次，非法日期每次
        # 调 expand_dates 都会打一条 warning（约 30 条/分/任务）；同一任务
        # 同一日期配置只展开一次，配置变更时键变化自动重算。
        self._dates_text_cache = {}

        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(4, 6))
        ttk.Button(toolbar, text="＋ 新建任务",
                   command=app.open_wizard).pack(side="left", padx=3)
        ttk.Button(toolbar, text="启动监控…",
                   command=app.open_start_dialog).pack(side="left", padx=3)
        self.edit_btn = ttk.Button(toolbar, text="编辑任务",
                                   command=self.edit_selected, state="disabled")
        self.edit_btn.pack(side="left", padx=3)
        ttk.Button(toolbar, text="暂停", width=6,
                   command=lambda: self._op_selected("paused")).pack(side="left", padx=3)
        ttk.Button(toolbar, text="恢复", width=6,
                   command=lambda: self._op_selected("monitoring")).pack(side="left", padx=3)
        ttk.Button(toolbar, text="取消任务", width=8,
                   command=lambda: self._op_selected("cancelled")).pack(side="left", padx=3)
        ttk.Button(toolbar, text="重置防重", width=8,
                   command=lambda: self._op_selected("reset")).pack(side="left", padx=3)
        ttk.Button(toolbar, text="删除任务", width=8,
                   command=lambda: self._op_selected("delete")).pack(side="left", padx=3)
        ttk.Button(toolbar, text="余票速查",
                   command=app.open_quick_check).pack(side="right", padx=3)

        ttk.Label(self, text="说明：右键任务可进行 编辑 / 暂停 / 恢复 / 取消 / 重置 / 删除 操作；"
                             "任务处于「已暂停」状态时可再编辑。",
                  foreground="#9a6700").pack(anchor="w", pady=(0, 6))

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
        self.tree.tag_configure("stopped", foreground="#6e7781")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Button-3>", self._popup_menu)

    def _cached_format_dates(self, task):
        """Task 75c：带缓存的日期展示。键含任务名与日期配置原文，
        配置不变时直接复用文本，不再调 expand_dates（不重复打 warning）。"""
        key = (task.get("name"), repr(task.get("dates")),
               repr(task.get("date_range")))
        try:
            return self._dates_text_cache[key]
        except KeyError:
            text = format_dates(task)
            self._dates_text_cache[key] = text
            return text

    def refresh(self):
        try:
            tasks = load_config().get("tasks") or []
            state = load_state().get("tasks", {})
        except Exception:
            return
        engine_running = bool(self.app.engine_thread
                              and self.app.engine_thread.is_alive())
        rows = []
        seen_date_keys = set()
        for i, t in enumerate(tasks, 1):
            st = state.get(t.get("name"), {}).get("status", "paused")
            msg = state.get(t.get("name"), {}).get("message", "")
            # 启动状态：引擎运行中且任务处于 监控中/等待重试 = 已启动
            started = engine_running and st in engine_mod.ACTIVE_STATUSES
            date_key = (t.get("name"), repr(t.get("dates")),
                        repr(t.get("date_range")))
            seen_date_keys.add(date_key)
            rows.append((
                "● 已启动" if started else "○ 未启动",
                i, t.get("name", ""), "%s-%s" % (t.get("from", ""), t.get("to", "")),
                self._cached_format_dates(t),
                "/".join(_display_trains(t.get("trains"))) or "全部",
                "/".join(_display_trains(t.get("seat_types"))),
                t.get("priority", 5),
                engine_mod.STATUS_LABELS.get(st, st), msg,
                "started" if started else "stopped", st))
        # 剪掉已删除/已改配置任务的缓存键，防止无界增长
        for k in list(self._dates_text_cache):
            if k not in seen_date_keys:
                del self._dates_text_cache[k]
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

    def _selected_task(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先在列表中选择一个任务", parent=self)
            return None
        task, _st = self._task_by_iid.get(sel[0], (None, None))
        return task

    def edit_selected(self):
        task = self._selected_task()
        if task is None:
            return
        # 实时从 state.json 读取最新状态，避免使用过期缓存导致误判
        fresh = _state_tasks_dict().get(task.get("name"), {}).get(
            "status", "paused")
        if fresh != "paused":
            messagebox.showwarning("提示",
                                   "只有「已暂停」的任务可以编辑。\n请先点「暂停」再编辑。",
                                   parent=self)
            return
        TaskEditDialog(self.app, task, on_saved=self.app.refresh_tasks)

    def _op_selected(self, action):
        task = self._selected_task()
        if task is None:
            return
        self._op(task, action)

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
        """右键菜单/工具条的操作执行器。"""
        name = task.get("name") or ""
        eng = self.app.get_ops_engine()
        info = None
        try:
            if action == "paused":
                eng.set_task_status(task, "paused", "手动暂停")
                info = "任务「%s」已暂停。" % name
            elif action == "monitoring":
                eng.set_task_status(task, "monitoring", "手动恢复", force=True)
                info = "任务「%s」已恢复监控。" % name
            elif action == "cancelled":
                if messagebox.askyesno("确认", "确定取消任务「%s」？取消后不再监控。" % name, parent=self):
                    eng.set_task_status(task, "cancelled", "手动取消")
            elif action == "reset":
                if messagebox.askyesno("确认", "确定重置「%s」并清除其防重下单记录？" % name, parent=self):
                    eng.empty_task_dedup(task)
                    eng.set_task_status(task, "monitoring", "手动重置", force=True)
            elif action == "delete":
                if messagebox.askyesno("确认", "确定从配置中删除任务「%s」？" % name, parent=self):
                    delete_task_everywhere(self.app, task)
        finally:
            self.app.refresh_tasks()
        # 先刷新列表再弹窗，减小阻塞期间被旧状态覆盖的窗口
        if info:
            messagebox.showinfo("完成", info, parent=self)


# ----------------------------- 新建监控任务（单表单） -----------------------------

class NewTaskDialog(tk.Toplevel):
    """新建监控任务：单个平铺表单（替代旧四步向导与「直接监视」两套入口）。

    填 出发/到达/日期/车次/席别/首选席别/乘车人/选项 → 写入监控系统任务表。
    「查后选」流程走查票页：查询 → 用查询条件创建任务（本对话框会预填）。"""

    def __init__(self, app, on_created=None, prefill=None):
        super().__init__(app.root)
        self.app = app
        self.on_created = on_created
        self.title("新建监控任务")
        self.geometry("560x760")
        self.minsize(520, 640)
        self.seat_vars = {}
        self.pax_vars = {}
        try:
            self.name2code, self.code2name = ticket.load_station_map()
        except Exception as e:
            messagebox.showerror("错误", "车站代码表加载失败：%s" % e, parent=self)
            self.destroy()
            return
        self._build()
        prefill = prefill or {}
        if prefill.get("from"):
            self.from_ent.set(prefill.get("from"))
        if prefill.get("to"):
            self.to_ent.set(prefill.get("to"))
        if prefill.get("date"):
            self.date_var.set(prefill.get("date"))
        if prefill.get("trains"):
            self.trains_var.set(",".join(_display_trains(prefill.get("trains"))))
        self._update_seat_pri_hint()
        self.transient(app.root)
        self.grab_set()

    def _build(self):
        # 行程
        tf = ttk.LabelFrame(self, text=" 行程 ", padding=8)
        tf.pack(fill="x", padx=10, pady=(10, 4))
        r0 = ttk.Frame(tf)
        r0.pack(fill="x")
        ttk.Label(r0, text="出发").pack(side="left")
        hist = []
        try:
            hist = launcher.load_launcher_config().get("station_history") or []
        except Exception:
            hist = []
        self.from_ent = StationEntry(r0, width=12, history=hist)
        self.from_ent.pack(side="left", padx=4)
        ttk.Button(r0, text="⇄", width=3, command=self._swap).pack(side="left", padx=2)
        ttk.Label(r0, text="到达").pack(side="left", padx=(8, 0))
        self.to_ent = StationEntry(r0, width=12, history=hist)
        self.to_ent.pack(side="left", padx=4)

        r1 = ttk.Frame(tf)
        r1.pack(fill="x", pady=(8, 0))
        ttk.Label(r1, text="乘车日期").pack(side="left")
        self.date_var = tk.StringVar()
        ttk.Entry(r1, textvariable=self.date_var, width=13).pack(side="left", padx=4)
        ttk.Label(r1, text="到").pack(side="left", padx=(6, 0))
        self.date_to_var = tk.StringVar()
        ttk.Entry(r1, textvariable=self.date_to_var, width=13).pack(side="left", padx=4)
        ttk.Label(r1, text="车次").pack(side="left", padx=(10, 0))
        self.trains_var = tk.StringVar()
        ttk.Entry(r1, textvariable=self.trains_var).pack(side="left", padx=4, fill="x",
                                                         expand=True)
        ttk.Label(tf, text="日期：单日 2026-10-06；范围填「到」即可（跨度≤5天，共 6 天）。"
                           "车次逗号分隔，留空=监控全部车次",
                  foreground="#57606a").pack(anchor="w", pady=(4, 0))

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
        pro.grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(pro, text="首选席别").pack(side="left")
        self.seat_pri_var = tk.StringVar(value="")
        ttk.Entry(pro, textvariable=self.seat_pri_var, width=18).pack(side="left", padx=4)
        ttk.Label(pro, text="逗号分隔，先填的先抢；可指定车次：K1969=硬卧（只作用该趟车）；"
                            "无座=硬座，认不出按硬座",
                  foreground="#57606a").pack(side="left")
        self.seat_pri_hint = ttk.Label(sf, foreground="#57606a")
        self.seat_pri_hint.grid(row=1, column=0, columnspan=4, sticky="w", pady=(2, 4))
        self.seat_pri_var.trace_add("write", lambda *_a: self._update_seat_pri_hint())
        for i, s in enumerate(SEAT_CHOICES):
            v = tk.BooleanVar(value=(s == "硬座"))
            self.seat_vars[s] = v
            ttk.Checkbutton(sf, text=s, variable=v,
                            command=self._update_seat_pri_hint).grid(
                row=i // 4 + 2, column=i % 4, sticky="w", padx=(0, 8))

        # 乘车人
        gf = ttk.LabelFrame(self, text=" 乘车人（不选=下单时用默认乘车人） ", padding=8)
        gf.pack(fill="x", padx=10, pady=4)
        try:
            plist = passengers_mod.load_passengers()
        except Exception:
            plist = []
        self.pax_frame = ttk.Frame(gf)
        self.pax_frame.pack(fill="x")
        if not plist:
            ttk.Label(self.pax_frame, text="（乘车人库为空，可在「乘车人」页添加）",
                      foreground="#57606a").pack(side="left")
        for p in plist:
            name = p.get("name") or ""
            if not name:
                continue
            v = tk.BooleanVar(value=bool(p.get("is_default")))
            self.pax_vars[name] = v
            label = name + ("（默认）" if p.get("is_default") else "")
            ttk.Checkbutton(self.pax_frame, text=label, variable=v).pack(
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
                  foreground="#57606a").pack(side="left")
        self.auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(of, text="余票命中后自动下单（不支付）",
                        variable=self.auto_var).pack(anchor="w")
        self.stop_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(of, text="一次下单成功后自动停止本任务",
                        variable=self.stop_var).pack(anchor="w")
        self.notify_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(of, text="下单成功后发送邮件通知",
                        variable=self.notify_var).pack(anchor="w")

        # 按钮
        bf = ttk.Frame(self)
        bf.pack(fill="x", padx=10, pady=(8, 10))
        ttk.Button(bf, text="创建并开始监控",
                   command=lambda: self.create(start_now=True)).pack(
            side="left", fill="x", expand=True, padx=(0, 4))
        ttk.Button(bf, text="仅创建任务",
                   command=lambda: self.create(start_now=False)).pack(
            side="left", fill="x", expand=True, padx=(4, 0))
        ttk.Button(bf, text="取消", command=self.destroy).pack(side="left", padx=(8, 0))

    def _swap(self):
        a, b = self.from_ent.get(), self.to_ent.get()
        self.from_ent.set(b)
        self.to_ent.set(a)

    def _update_seat_pri_hint(self):
        """「首选席别」输入框的即时解析反馈（含 车次=席别 专属规则）。"""
        checked = [s for s, v in self.seat_vars.items() if v.get()]
        trains = [t.strip().upper() for t in
                  re.split(r"[,，\s]+", self.trains_var.get()) if t.strip()]
        text, bad = ticket.seat_priority_feedback(self.seat_pri_var.get(),
                                                  checked=checked, trains=trains)
        self.seat_pri_hint.configure(text=text,
                                     foreground="#cf222e" if bad else "#57606a")

    def _parse_dates(self):
        return appcommon.parse_date_range(self.date_var.get().strip(),
                                          self.date_to_var.get().strip())

    def create(self, start_now):
        from_name, to_name = self.from_ent.get(), self.to_ent.get()
        if from_name not in self.name2code or to_name not in self.name2code:
            messagebox.showwarning("提示", "出发站/到达站无效，请通过「选」按钮选择有效站名",
                                   parent=self)
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
            # 开着自动下单时，剔除不支持下单的席别（与抢票路径同一条规则）。
            bad = [s for s in seats if s not in ticket.SEAT_NAME_TO_CODE]
            if bad:
                seats = [s for s in seats if s in ticket.SEAT_NAME_TO_CODE]
                self.app.log_line("[提醒] 席别 %s 暂不支持自动下单，已从该监控任务移除"
                                  % "、".join(bad))
            if not seats:
                messagebox.showwarning(
                    "提示", "勾选的席别暂不支持自动下单。\n"
                    "可改选其他席别，或关掉「自动下单」只做有票提醒。", parent=self)
                return
        trains = [t.strip().upper() for t in
                  re.split(r"[,，\s]+", self.trains_var.get()) if t.strip()]
        passengers = [n for n, v in self.pax_vars.items() if v.get()]
        pri_raw = self.seat_pri_var.get().strip()
        # Task 96b：Spinbox 手输非数字时 tk.IntVar.get() 抛 TclError →
        # 友好提示并用安全默认值 5。
        try:
            prio = self.prio_var.get()
        except (tk.TclError, ValueError, TypeError):
            messagebox.showwarning("优先级无效", "优先级请输入 1-10 的数字，已使用默认值 5。",
                                   parent=self)
            prio = 5
        task = launcher.build_monitor_task(
            from_name, to_name, dates, date_range, trains, seats, passengers,
            self.purpose_var.get(), self.auto_var.get(), self.stop_var.get(),
            prio, pax_purpose={}, seat_priority=pri_raw)
        task["notify_channels"] = ["email"] if self.notify_var.get() else []
        if pri_raw:
            parsed = ticket.seat_rules_parse(pri_raw)
            summary = []
            for tr, ss in parsed["rules"].items():
                summary.append("%s=%s" % (tr, "/".join(ss)))
            if parsed["bare"]:
                summary.append("其余车次：%s" % "/".join(parsed["bare"]))
            for warn in parsed["warnings"]:
                self.app.log_line("[提醒] 首选席别：%s" % warn)
            if summary:
                self.app.log_line("[运行] 监控任务席别规则：%s" % "；".join(summary))
        try:
            name = launcher.append_monitor_task(task, start_now)
        except Exception as e:
            messagebox.showerror("创建失败", "写入监控系统配置失败：%s" % e, parent=self)
            return
        self.app.log_line("[运行] 已创建监控任务「%s」%s" % (
            name, "，监控中" if start_now else "，未启动"))
        if self.on_created:
            self.on_created()
        self.destroy()
        if start_now:
            self.app.start_engine()
            messagebox.showinfo("完成",
                                "任务「%s」已创建并开始监视。\n命中余票将自动提交订单。"
                                % name, parent=self.app.root)
        else:
            messagebox.showinfo("完成",
                                "任务「%s」已创建。\n在「监控任务」页点「启动监控…」即可开始。"
                                % name, parent=self.app.root)


# ----------------------------- 编辑任务（暂停态） -----------------------------

class TaskEditDialog(tk.Toplevel):
    """编辑已暂停的任务：日期 / 车次 / 席别 / 乘车人 / 优先级等，保存后覆盖原任务。"""

    def __init__(self, app, task, on_saved=None):
        super().__init__(app.root)
        self.app = app
        self.task = task
        self.on_saved = on_saved
        self.title("编辑任务")
        self.geometry("520x700")
        try:
            self.name2code, self.code2name = ticket.load_station_map()
        except Exception as e:
            messagebox.showerror("错误", "车站代码表加载失败：%s" % e, parent=self)
            self.destroy()
            return

        ttk.Label(self, text="编辑任务：%s" % (task.get("name", "")),
                  font=("", 11, "bold")).pack(anchor="w", padx=14, pady=(10, 4))

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=14)

        r0 = ttk.Frame(body)
        r0.pack(fill="x", pady=4)
        ttk.Label(r0, text="出发").pack(side="left")
        self.from_field = StationEntry(r0, width=12)
        self.from_field.pack(side="left", padx=4)
        self.from_field.set(task.get("from", ""))
        ttk.Label(r0, text="到达").pack(side="left", padx=(8, 0))
        self.to_field = StationEntry(r0, width=12)
        self.to_field.pack(side="left", padx=4)
        self.to_field.set(task.get("to", ""))

        dates = engine_mod.expand_dates(task)
        # Task 75d：记下原始日期配置与展示文本；非连续多日如实列出，
        # 保存时未改动则原样回写（不静默扩展为连续区间）。
        self._orig_dates = list(task.get("dates")) \
            if isinstance(task.get("dates"), list) else []
        self._orig_date_range = list(task.get("date_range")) \
            if isinstance(task.get("date_range"), list) else []
        self._date_text_orig, self._date_non_contig = _date_display_text(dates)
        date_text = self._date_text_orig
        r1 = ttk.Frame(body)
        r1.pack(fill="x", pady=4)
        ttk.Label(r1, text="乘车日期").pack(side="left")
        self.date_var = tk.StringVar(value=date_text)
        ttk.Entry(r1, textvariable=self.date_var, width=26).pack(side="left", padx=4)
        ttk.Label(r1, text="车次").pack(side="left", padx=(10, 0))
        self.trains_var = tk.StringVar(
            value=",".join(_display_trains(task.get("trains"))))
        ttk.Entry(r1, textvariable=self.trains_var).pack(side="left", padx=4,
                                                         fill="x", expand=True)
        ttk.Label(body, text="单日：2026-10-06　范围：2026-10-06~2026-10-09；"
                             "车次逗号分隔，留空=监控全部车次",
                  foreground="#57606a").pack(anchor="w", pady=(0, 4))

        r2 = ttk.Frame(body)
        r2.pack(fill="x", pady=4)
        ttk.Label(r2, text="票种").pack(side="left")
        self.purpose_var = tk.StringVar(value=task.get("purpose_code") or "ADULT")
        ttk.Radiobutton(r2, text="成人票", value="ADULT",
                        variable=self.purpose_var).pack(side="left", padx=(8, 16))
        ttk.Radiobutton(r2, text="学生票", value="0X00",
                        variable=self.purpose_var).pack(side="left")

        ttk.Label(body, text="监控席别（Ctrl/Shift 多选）：").pack(anchor="w", pady=(6, 0))
        self.seat_list = tk.Listbox(body, height=6, selectmode="multiple")
        self.seat_list.pack(fill="x")
        for s in SEAT_CHOICES:
            self.seat_list.insert("end", s)
        # Task 103c：展示用归一化——裸字符串按单个席别。
        for s in _display_seat_types(task.get("seat_types")):
            if s in SEAT_CHOICES:
                self.seat_list.selection_set(SEAT_CHOICES.index(s))

        ttk.Label(body, text="乘车人（不选=默认/账号常用）：").pack(anchor="w", pady=(8, 0))
        self.psg_items = []
        self.psg_list = tk.Listbox(body, height=4, selectmode="multiple")
        self.psg_list.pack(fill="x")
        for p in passengers_mod.load_passengers():
            label = "{0}{1}".format(p.get("name"), "（默认）" if p.get("is_default") else "")
            self.psg_list.insert("end", label)
            self.psg_items.append(p.get("name"))
        # Task 106：展示用归一化——裸字符串按单个姓名处理。
        for n in _display_seat_types(task.get("passenger_names")):
            if n in self.psg_items:
                self.psg_list.selection_set(self.psg_items.index(n))

        r3 = ttk.Frame(body)
        r3.pack(fill="x", pady=8)
        ttk.Label(r3, text="优先级").pack(side="left")
        self.prio_var = tk.IntVar(value=int(task.get("priority") or 5))
        ttk.Spinbox(r3, from_=1, to=10, textvariable=self.prio_var,
                    width=5).pack(side="left", padx=4)
        ttk.Label(r3, text="（1-10，越大越先抢）", foreground="#57606a").pack(side="left")

        self.auto_var = tk.BooleanVar(value=bool(task.get("auto_order", True)))
        ttk.Checkbutton(body, text="余票命中后自动下单（不支付）",
                        variable=self.auto_var).pack(anchor="w")
        self.stop_var = tk.BooleanVar(value=bool(task.get("stop_after_order", True)))
        ttk.Checkbutton(body, text="一次下单成功后自动停止本任务",
                        variable=self.stop_var).pack(anchor="w")

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=14, pady=10)
        ttk.Button(btns, text="保存修改", command=self.save).pack(side="right", padx=6)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right")

    def save(self):
        from_name, to_name = self.from_field.get(), self.to_field.get()
        if from_name not in self.name2code or to_name not in self.name2code:
            messagebox.showwarning("提示", "出发站/到达站无效，请通过「选」按钮选择有效站名",
                                   parent=self)
            return
        if from_name == to_name:
            messagebox.showwarning("提示", "出发站与到达站不能相同", parent=self)
            return
        raw = self.date_var.get().strip()
        try:
            dates, date_range = _resolve_saved_dates(
                self._date_text_orig, self._date_non_contig,
                self._orig_dates, self._orig_date_range, raw)
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
        try:
            prio = self.prio_var.get()
        except (tk.TclError, ValueError, TypeError):
            prio = 5

        new_task = _build_edited_task(
            self.task, from_name=from_name, to_name=to_name,
            dates=dates, date_range=date_range, trains=trains,
            seat_types=seats, passengers=passengers,
            auto_order=bool(self.auto_var.get()),
            stop_after_order=bool(self.stop_var.get()),
            priority=int(prio),
            purpose_code=self.purpose_var.get())

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
        messagebox.showinfo("完成", "任务「%s」已更新。\n在主界面启动监控后按新参数运行。"
                            % new_task["name"], parent=self.app.root)


# ----------------------------- 启动监控（选择任务） -----------------------------

class StartMonitorDialog(tk.Toplevel):
    """启动监控时选择要运行哪些任务；已启动的任务标记「已启动」并锁定勾选。"""

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("启动监控（选择任务）")
        self.geometry("600x520")

        self.running = bool(app.engine_thread and app.engine_thread.is_alive())
        tip_text = ("引擎正在运行：已启动的任务自动锁定；新勾选的任务会自动纳入，"
                    "取消勾选转为「已暂停」。"
                    if self.running else "勾选要启动的任务，点击「启动所选任务」开始监控。")
        ttk.Label(self, text=tip_text, foreground="#9a6700",
                  wraplength=560, justify="left").pack(fill="x", padx=12, pady=8)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=14)

        self.vars = {}
        config = load_config()
        state = _state_tasks_dict()
        tasks = config.get("tasks") or []
        if not tasks:
            ttk.Label(body, text="暂无任务，请先创建任务。").pack(pady=30)
        for idx, t in enumerate(tasks):
            name = t.get("name") or ""
            st = state.get(name, {}).get("status", "paused")
            active = st in engine_mod.ACTIVE_STATUSES
            started = self.running and active
            var = tk.BooleanVar(value=active)
            self.vars[idx] = var

            row = ttk.Frame(body)
            row.pack(fill="x", pady=3)
            cb = ttk.Checkbutton(row, variable=var,
                                 state=("disabled" if started else "normal"))
            cb.pack(side="left", padx=6)
            info = ttk.Frame(row)
            info.pack(side="left", fill="x", expand=True)
            ttk.Label(info, text=name, font=("", 10, "bold")).pack(anchor="w")
            sub = "{0} → {1}   {2}   {3}   优先级 {4}".format(
                t.get("from", ""), t.get("to", ""),
                format_dates(t),
                "/".join(_display_trains(t.get("trains"))) or "全部车次",
                t.get("priority", 5))
            ttk.Label(info, text=sub, foreground="#57606a").pack(anchor="w")
            if started:
                ttk.Label(row, text="已启动", foreground="#0969da").pack(side="right", padx=10)
            else:
                ttk.Label(row, text=engine_mod.STATUS_LABELS.get(st, st),
                          foreground=STATUS_COLOR.get(st, "")).pack(side="right", padx=10)

        btns = ttk.Frame(self)
        btns.pack(fill="x", side="bottom")
        ttk.Button(btns, text="启动所选任务",
                   command=self.apply_start).pack(side="right", padx=12, pady=8)
        ttk.Button(btns, text="关闭",
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


# ----------------------------- 历史查询记录 -----------------------------

class QueryHistoryDialog(tk.Toplevel):
    """历史查询记录：双击一条即填回行程。"""
    def __init__(self, master, items, on_pick):
        super().__init__(master)
        # Task 74g：构造时即过滤掉非 dict 条目（逐条记 warning），保证
        # self.items 与列表框行号 1:1 对齐；否则 _pick 会按错位索引取错条目。
        rows = []
        for it in items:
            text = launcher._history_item_text(it)
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
        self.lb = tk.Listbox(wrap, yscrollcommand=sb.set, activestyle="none")
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


# ----------------------------- 查票页（免登录） -----------------------------

class QuickCheckTab(ttk.Frame):
    """余票速查：免登录查询实时余票；可用查询条件一键创建监控任务。"""

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        try:
            self.name2code, self.code2name = ticket.load_station_map()
        except Exception as e:
            messagebox.showerror("错误", "车站代码表加载失败：%s" % e, parent=self)
            self.name2code, self.code2name = {}, {}

        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=8)
        ttk.Label(top, text="出发").pack(side="left")
        self.from_ent = StationEntry(top, width=13)
        self.from_ent.pack(side="left", padx=4)
        ttk.Button(top, text="⇄", width=3, command=self._swap).pack(side="left", padx=2)
        ttk.Label(top, text="到达").pack(side="left", padx=(6, 0))
        self.to_ent = StationEntry(top, width=13)
        self.to_ent.pack(side="left", padx=4)
        ttk.Label(top, text="日期").pack(side="left", padx=(8, 2))
        self.date_var = tk.StringVar(value=datetime.date.today().isoformat())
        ttk.Entry(top, textvariable=self.date_var, width=12).pack(side="left")
        ttk.Button(top, text="查询", command=self.query).pack(side="left", padx=8)
        ttk.Button(top, text="用此条件创建监控任务",
                   command=self._create_task).pack(side="left")

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=10, pady=4)
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
        self.status.pack(anchor="w", padx=10, pady=(0, 8))

    def _swap(self):
        a, b = self.from_ent.get(), self.to_ent.get()
        self.from_ent.set(b)
        self.to_ent.set(a)

    def _create_task(self):
        prefill = {"from": self.from_ent.get(), "to": self.to_ent.get(),
                   "date": self.date_var.get().strip()}
        self.app.open_wizard(prefill=prefill)

    def query(self):
        from_name, to_name = self.from_ent.get(), self.to_ent.get()
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


# ----------------------------- 乘车人 -----------------------------

def _save_passengers_or_warn(passengers, parent, expect_stamp=passengers_mod._STAMP_UNSET):
    """保存乘车人；磁盘盒子不可解密被拒写时弹 error 提示并返回 False。

    Task 80(b)：save_passengers 的拒写（return False）原来三处裸调无人处理，
    GUI 会显示"完成"/刷新列表，但磁盘未写，重启后修改丢失。此处失败弹
    error（与 save_passengers 内的 LOG.error 口径一致），调用方不再显示
    "完成"、不再刷新列表。

    Task 89(c)：expect_stamp 传入时做乐观并发检查（与 Task 88d/monitor 同口径）；
    编辑期间被外部修改导致放弃保存时弹 warning（不覆盖对方修改），返回 False。
    不传 expect_stamp 时保持 Task 80 行为（直接写）。
    """
    if passengers_mod.save_passengers(passengers, expect_stamp=expect_stamp):
        return True
    if expect_stamp is not passengers_mod._STAMP_UNSET \
            and passengers_mod.passengers_stamp() != expect_stamp:
        # 乐观并发放弃：save_passengers 已在锁内检出指纹变化并打印警告；
        # GUI 侧再给一次明确提示（不静默），调用方负责重载最新数据。
        messagebox.showwarning("保存已放弃",
                               "乘车人数据在您编辑期间被其他程序修改，本次保存已放弃，\n"
                               "未覆盖对方的修改。请基于最新数据重新操作。",
                               parent=parent)
        return False
    messagebox.showerror("保存失败",
                         "乘车人数据保存失败：磁盘上的已有数据在本机不可解密，"
                         "已拒绝覆盖以保护原数据。\n"
                         "请在原机器解密后迁移，或使用 --force 放弃旧数据。",
                         parent=parent)
    return False


class PassengerPanel(ttk.Frame):
    """乘车人管理页：列表 + 添加 / 编辑 / 删除 / 设为默认（加密存储，展示打码）。"""

    def __init__(self, master):
        super().__init__(master)
        ttk.Label(self, text="乘车人列表（证件号/手机号加密保存，显示时脱敏）：").pack(
            anchor="w", padx=12, pady=(10, 2))
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=12, pady=4)
        self.listbox = tk.Listbox(frame)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=12, pady=10)
        ttk.Button(btns, text="添加", command=lambda: self.edit(None)).pack(side="left", padx=4)
        ttk.Button(btns, text="编辑", command=lambda: self.edit(self.selected())).pack(side="left", padx=4)
        ttk.Button(btns, text="删除", command=self.remove).pack(side="left", padx=4)
        ttk.Button(btns, text="设为默认", command=self.set_default).pack(side="left", padx=4)
        ttk.Label(btns, text="默认乘车人在抢票/监控未勾选时优先使用",
                  foreground="#57606a").pack(side="left", padx=10)
        self.refresh()

    def refresh(self):
        self.passengers = passengers_mod.load_passengers()
        # Task 89c：记下 load 时的字节指纹，供保存时做乐观并发检查
        #（本页面打开期间若被外部修改，保存时警告并放弃，不静默覆写）。
        self._pax_stamp = passengers_mod.passengers_stamp()
        self.listbox.delete(0, "end")
        for p in self.passengers:
            self.listbox.insert("end", "{0}{1}  证件:{2} {3}".format(
                p.get("name"), "【默认】" if p.get("is_default") else "",
                passengers_mod.ID_TYPE_NAMES.get(p.get("id_type_code"), "?"),
                _mask_id(p.get("id_no"))))

    def selected(self):
        sel = self.listbox.curselection()
        return sel[0] if sel else None

    def _save_passengers_or_refresh(self, parent):
        """保存 self.passengers（Task 89c：Task 88d 同口径的乐观并发检查）。

        成功 → (True, "ok")。指纹变化（本页面打开期间被外部修改）→
        重载最新数据+警告，返回 (False, "stale")，调用方放弃本次保存。
        不可解密盒子/锁超时 → error，返回 (False, "refused")（不重载，不丢内存数据）。
        """
        if _save_passengers_or_warn(self.passengers, parent, self._pax_stamp):
            return True, "ok"
        if self._pax_stamp is not passengers_mod._STAMP_UNSET \
                and passengers_mod.passengers_stamp() != self._pax_stamp:
            # 外部写入导致放弃：重载最新数据+新指纹。
            # （expect_stamp 为哨兵值时本就没有乐观检查，不存在"被外部修改"。）
            self.refresh()
            return False, "stale"
        return False, "refused"

    def edit(self, index):
        p = self.passengers[index] if index is not None else None
        dlg = tk.Toplevel(self)
        dlg.title("添加乘车人" if p is None else "编辑乘车人")
        dlg.geometry("460x340")
        dlg.transient(self.winfo_toplevel())
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
        if type_var.get() not in [c.split(" ")[0] for c in combo["values"]]:
            combo.set("1 - 二代身份证")
        else:
            for c in combo["values"]:
                if c.split(" ")[0] == type_var.get():
                    combo.set(c)
                    break
        row("证件类型", combo)
        id_var = tk.StringVar(value=(p or {}).get("id_no", ""))
        id_entry = ttk.Entry(body, textvariable=id_var, show="*")
        id_row = row("证件号码", id_entry)
        id_visible = tk.BooleanVar(value=False)

        def _toggle_id():
            id_entry.configure(show="" if id_visible.get() else "*")

        ttk.Button(id_row, text="显示", width=5, command=lambda: (
            id_visible.set(not id_visible.get()), _toggle_id())).pack(side="left", padx=(6, 0))
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
            ok, reason = self._save_passengers_or_refresh(dlg)
            if not ok:
                if reason == "stale":
                    # 数据已重载为最新：关闭编辑框，用户基于最新数据重做
                    dlg.destroy()
                return
            self.refresh()
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
            ok, _ = self._save_passengers_or_refresh(self)
            if not ok:
                return
            self.refresh()

    def set_default(self):
        idx = self.selected()
        if idx is None:
            messagebox.showwarning("提示", "请先选择乘车人", parent=self)
            return
        for p in self.passengers:
            p["is_default"] = False
        self.passengers[idx]["is_default"] = True
        ok, _ = self._save_passengers_or_refresh(self)
        if not ok:
            return
        self.refresh()
        messagebox.showinfo("完成", "已将「%s」设为默认乘车人" % self.passengers[idx].get("name"),
                            parent=self)


# 口径单源在 appcommon.mask_id_partial（与 launcher_utils.mask_id 是两种口径，
# 勿混用）；别名保留，调用点与测试 pin 不变。
_mask_id = appcommon.mask_id_partial


# ----------------------------- 购票历史 -----------------------------

HISTORY_RESULT_LABEL = {"success": "下单成功", "dup": "防重跳过",
                        "failed": "下单失败", "hit_no_order": "命中未下单",
                        "ambiguous": "结果待人工核对"}


def read_history_records(limit=200):
    """读购票历史：最近 limit 条、最新在前。文件缺失/损坏返回空列表；
    顶层非 list 时记 warning 后按空历史展示（Task 94b——旧代码切片在 try 外，
    非 list 经 eager 构造的 HistoryPanel 崩 GUI 启动）。"""
    path = os.path.join(HERE, load_config().get("history_file", "order_history.json"))
    try:
        with open(path, encoding="utf-8-sig") as f:
            history = json.load(f)
        if not isinstance(history, list):
            LOG.warning("order_history.json 顶层不是列表（%s），按空历史展示：%s",
                        type(history).__name__, path)
            return []
        return list(reversed(history[-limit:]))
    except Exception:
        return []


class HistoryTab(ttk.Frame):
    """购票历史与通知记录（含「记录已通知」的空通知结果）。"""

    RESULT_LABEL = HISTORY_RESULT_LABEL

    def __init__(self, master, app=None):
        super().__init__(master)
        self.app = app
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(top, text="购票历史（最新在前）").pack(side="left")
        ttk.Button(top, text="刷新", command=self.refresh).pack(side="right")

        cols = ("time", "task", "train", "date", "route", "seat",
                "passengers", "order_no", "result")
        heads = ("时间", "任务", "车次", "日期", "区间", "席别",
                 "乘车人", "订单号", "结果")
        widths = (130, 150, 60, 90, 120, 70, 110, 110, 70)
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 8))
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
        self.refresh()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for r in read_history_records():
            self.tree.insert("", "end", values=(
                r.get("time", ""), r.get("task", ""), r.get("train", ""),
                r.get("date", ""), "%s-%s" % (r.get("from", ""), r.get("to", "")),
                r.get("seat", ""), "、".join(r.get("passengers") or []),
                r.get("order_no", ""),
                self.RESULT_LABEL.get(r.get("result"), r.get("result", ""))))


# ----------------------------- 设置页（通知 / 会话） -----------------------------

class SettingsTab(ttk.Frame):
    """设置：邮件通知（SMTP）与登录会话（检查 / 重新登录 / 账号乘车人）。"""

    def __init__(self, master, app=None):
        super().__init__(master)
        self.app = app

        # ---------------- 邮件通知 ----------------
        nf = ttk.LabelFrame(self, text=" 邮件通知（SMTP） ", padding=8)
        nf.pack(fill="x", padx=10, pady=(10, 4))
        cfg = load_config()
        self.email = cfg.setdefault("notify", {}).setdefault("email", {})
        self._build_notify_fields(nf)

        # ---------------- 登录会话 ----------------
        sf = ttk.LabelFrame(self, text=" 登录会话 ", padding=8)
        sf.pack(fill="both", expand=True, padx=10, pady=4)
        self.status_label = ttk.Label(sf, text="会话状态未检查")
        self.status_label.pack(anchor="w", pady=2)
        ttk.Label(sf, text="账号已保存的乘车人（下单时可选用）：").pack(anchor="w", pady=(8, 2))
        frame = ttk.Frame(sf)
        frame.pack(fill="both", expand=True)
        self.p_list = tk.Listbox(frame, height=6)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.p_list.yview)
        self.p_list.configure(yscrollcommand=sb.set)
        self.p_list.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        btns = ttk.Frame(sf)
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="检查会话", command=self.check).pack(side="left", padx=4)
        ttk.Button(btns, text="重新登录（弹出浏览器）", command=self.relogin).pack(side="left", padx=4)

    # ---- 邮件通知表单（原 NotifyFormMixin 单点实现，勿两处各改一份） ----

    def _build_notify_fields(self, body):
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
        try:
            _pw = notify_mod.secret_of(self.email.get("password", ""))
        except notify_mod.SecretDecryptError:
            # Task 60b：损坏的密文不再吞成空串；显示留空并提示用户重输
            LOG.warning("[配置] 邮箱授权码解密失败，已清空显示（请重新输入授权码）")
            _pw = ""
        self.pwd_var = tk.StringVar(value=_pw)
        row("邮箱授权码", self.pwd_var, show="*")
        self.from_var = tk.StringVar(value=self.email.get("from", ""))
        row("发件人地址", self.from_var)
        self.to_var = tk.StringVar(value=",".join(self.email.get("to") or []))
        row("收件人", self.to_var)
        ttk.Label(body, text="收件人多个用逗号分隔；发件人地址留空则默认同发件邮箱；"
                             "QQ/163 邮箱需在邮箱设置中开启 SMTP 服务并生成授权码",
                  foreground="#57606a", wraplength=560).pack(anchor="w", pady=4)
        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text="保存通知设置", command=self.save).pack(side="left", padx=4)
        ttk.Button(btns, text="发送测试邮件", command=self.test).pack(side="left", padx=4)

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

    # ---- 登录会话 ----

    def check(self):
        self.status_label.config(text="正在检查会话状态...")

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
            if self.app is not None:
                self.app._set_account(
                    ("已登录 %s" % who) if ok else "未登录", bool(ok))
            self.p_list.delete(0, "end")
            for p in passengers:
                self.p_list.insert("end", "%s（%s）" % (
                    p["name"], p.get("type_name") or ("成人" if p.get("is_adult", True) else "非成人")))

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
        """登录流程一结束就先把界面切成已登录，不等下一次会话体检。"""
        if ok and self.app is not None:
            self.app._set_account("已登录", True)
            self.app._acct_time = time.time()
        self.check()


def _relogin_ok_via_script(script):
    """运行 capture_session.py 做重登：仅当退出码为 0 返回 True。

    旧代码 subprocess.run(...) 后 ok = True 硬编码——退出码非 0（登录失败）
    也被当成成功，侧边栏误置"已登录"。超时同样视为失败返回 False，
    不挂死、不抛到界面。
    Task 99a：超时必须覆盖脚本实际总耗时（MAX_WAIT_SEC 等待 + Edge 启动
    开销 + 最终验证），否则用户在等待末尾登录成功会被误判"运行超时"。
    """
    try:
        import capture_session as _cs
        timeout = _cs.GUI_SUBPROCESS_TIMEOUT
    except Exception:
        timeout = 420  # 与 GUI_SUBPROCESS_TIMEOUT(=MAX_WAIT_SEC+120) 同值兜底
    try:
        proc = subprocess.run([sys.executable, script], cwd=HERE, timeout=timeout)
    except subprocess.TimeoutExpired:
        LOG.error("重新登录失败：capture_session.py 运行超时（%ss）", timeout)
        return False
    ok = proc.returncode == 0
    if not ok:
        LOG.error("重新登录失败：capture_session.py 退出码 %s", proc.returncode)
    return ok


# ----------------------------- 抢票任务页 -----------------------------

GRAB_STATUS_LABELS = {
    "idle": "就绪",
    "running": "抢票中",
    "ok": "已抢到",
    "stopped": "已停止",
}


class GrabTab(ttk.Frame):
    """抢票任务管理：任务列表 + 每任务编辑表单（选中即切换），可多任务并行抢票。

    替代旧「任务管理器 + 每任务独立窗口」两层结构：每个任务一份 GrabTaskForm
    （懒创建、常驻），表单自己跑 _tick（倒计时 / 提醒 / 到点自动开抢），
    是否显示都不影响运行。"""

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.tasks = launcher.load_grab_tasks()
        self.forms = {}         # tid -> GrabTaskForm
        self._tv_iid = {}       # tree iid -> task
        self._refreshing = False
        self._deleting_tids = set()  # 删除中的任务：收尾期间不再重建表单
        self._build()
        self._refresh_list()
        self._select_first()
        self.after(800, self._tick)

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Button(top, text="＋ 新建任务", command=self._new_task).pack(side="left", padx=2)
        ttk.Button(top, text="改名", command=lambda: self._rename_task(self._selected_tid())).pack(
            side="left", padx=2)
        ttk.Button(top, text="删除", command=lambda: self._delete_task(self._selected_tid())).pack(
            side="left", padx=2)
        ttk.Button(top, text="测试会话", command=self._test_session).pack(side="left", padx=2)
        ttk.Label(top, text="每个任务独立配置、可同时抢多个车次/日期；选中任务后在下方面板编辑。",
                  foreground="#57606a").pack(side="left", padx=10)

        frame = ttk.Frame(self)
        frame.pack(fill="x", padx=8)
        cols = ("name", "route", "date", "trains", "status")
        heads = ("任务名称", "区间", "日期", "车次", "状态")
        widths = (140, 170, 170, 180, 80)
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", height=6)
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="center")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="x", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_select())

        # 表单宿主（简单滚动容器：窗口高度不够时可滚）
        host = ttk.Frame(self)
        host.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self._canvas = tk.Canvas(host, highlightthickness=0)
        hsb = ttk.Scrollbar(host, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=hsb.set)
        hsb.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        self.form_host = ttk.Frame(self._canvas)
        self._win = self._canvas.create_window((0, 0), window=self.form_host, anchor="nw")
        self.form_host.bind("<Configure>", lambda e: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfigure(
            self._win, width=e.width))
        self.winfo_toplevel().bind("<MouseWheel>", self._on_wheel, add="+")

    def _on_wheel(self, e):
        """指针在本页表单区时滚表单；车次表/任务列表有自己的滚动，不抢。"""
        try:
            node = self.winfo_containing(e.x_root, e.y_root)
        except Exception:
            return
        while node is not None:
            if isinstance(node, ttk.Treeview):
                return
            if node is self._canvas or node is self.form_host:
                try:
                    self._canvas.yview_scroll(int(-e.delta / 120), "units")
                except Exception:
                    pass
                return
            node = getattr(node, "master", None)

    # ---- 任务列表 ----

    def _find(self, tid):
        for t in self.tasks:
            if t.get("id") == tid:
                return t
        return None

    def _selected_tid(self):
        for i in self.tree.selection():
            t = self._tv_iid.get(i)
            if t is not None:
                return t.get("id")
        return None

    def _refresh_list(self):
        # 重入保护：表单构造/关闭过程中会经 _upsert_task 回调到这里；
        # 嵌套重入直接返回，由最外层刷新统一收口（避免表单重复创建）。
        if self._refreshing:
            return
        self._refreshing = True
        try:
            keep = self._selected_tid()
            self.tree.delete(*self.tree.get_children())
            self._tv_iid = {}
            self._ensure_forms()
            self._prune_forms()
            for t in self.tasks:
                try:
                    self._add_row(t)
                except Exception as e:
                    LOG.warning("抢票任务条目异常，已跳过展示：%r（%s）", t, e)
            if keep is not None and any(t.get("id") == keep for t in self.tasks):
                self._select_tid(keep)
            else:
                self._select_first()
        finally:
            self._refreshing = False

    def _add_row(self, task):
        # Task 96c/104：grab_tasks.json 脏条目（非 dict）→ task.get 致列表崩；
        # 入口已 sanitize，这里再防一手：记 warning 后跳过展示，不崩。
        if not isinstance(task, dict):
            LOG.warning("抢票任务条目非 dict，已跳过展示：%r" % (task,))
            return
        trains = _display_trains(task.get("trains"))
        iid = self.tree.insert("", "end", values=(
            task.get("name") or "未命名",
            "%s → %s" % (task.get("from") or "?", task.get("to") or "?"),
            (task.get("date") or "?") + ("~%s" % task["date_to"] if task.get("date_to") else ""),
            "/".join(trains) or "全部",
            GRAB_STATUS_LABELS.get(task.get("status") or "idle", ("就绪"))))
        self._tv_iid[iid] = task

    def _ensure_forms(self):
        for t in self.tasks:
            tid = t.get("id")
            if tid is None or tid in self.forms or tid in self._deleting_tids:
                continue
            try:
                self.forms[tid] = GrabTaskForm(self.form_host, self, t)
            except Exception as e:
                LOG.exception("抢票任务表单创建失败（%s）：%s", tid, e)

    def _prune_forms(self):
        alive = {t.get("id") for t in self.tasks if isinstance(t, dict)}
        for tid in list(self.forms):
            if tid not in alive:
                f = self.forms.pop(tid)
                try:
                    f.close(stop_thread=True)
                except Exception:
                    pass
                try:
                    f.destroy()
                except Exception:
                    pass

    def _select_first(self):
        children = self.tree.get_children()
        if children:
            self.tree.selection_set(children[0])
            self._on_select()

    def _select_tid(self, tid):
        for iid, t in self._tv_iid.items():
            if t.get("id") == tid:
                self.tree.selection_set(iid)
                self._on_select()
                return
        self._on_select()

    def _on_select(self):
        tid = self._selected_tid()
        for t2, f in self.forms.items():
            try:
                if t2 == tid:
                    f.pack(fill="both", expand=True)
                else:
                    f.pack_forget()
            except Exception:
                pass
        self._current_tid = tid

    # ---- 任务操作 ----

    def _new_task(self):
        # 名字序号取现存最大号 +1：删除任务后新建不再出现重名（两个"任务 2"）
        seqs = []
        for t in self.tasks:
            m = re.match(r"任务\s*(\d+)$", (t.get("name") or "").strip())
            if m:
                seqs.append(int(m.group(1)))
        task = launcher.new_grab_task((max(seqs) if seqs else 0) + 1)
        self.tasks.append(task)
        launcher.save_grab_tasks(self.tasks)
        self._refresh_list()
        self._select_tid(task["id"])
        self._put_log("[任务] 已创建「%s」" % task["name"])

    def _rename_task(self, tid):
        task = self._find(tid)
        if task is None:
            messagebox.showinfo("重命名任务", "请先在列表里选中要改名的任务",
                                parent=self.winfo_toplevel())
            return
        name = simpledialog.askstring("重命名任务", "新的任务名：",
                                      parent=self.winfo_toplevel(),
                                      initialvalue=task.get("name") or "")
        if not name:
            return
        task["name"] = name.strip() or task.get("name")
        launcher.save_grab_tasks(self.tasks)
        form = self.forms.get(tid)
        if form is not None:
            form.task_name = task["name"]
        self._refresh_list()
        self._put_log("[任务] 已重命名为「%s」" % task["name"])

    def _delete_task(self, tid):
        task = self._find(tid)
        if task is None:
            messagebox.showinfo("删除任务", "请先在列表里选中要删除的任务",
                                parent=self.winfo_toplevel())
            return
        if not messagebox.askyesno("删除任务", "确定删除抢票任务「%s」？" % (task.get("name") or "未命名"),
                                   parent=self.winfo_toplevel()):
            return
        form = self.forms.pop(tid, None)
        if form is not None:
            try:
                # 必须走表单自己的 close（停抢票线程 -> join -> 同步界面编辑）；
                # 直接 destroy 会把运行中的抢票线程晾在后台，可能为已删任务下单
                self._deleting_tids.add(tid)  # 收尾期间禁止重建这张表单
                form.close(stop_thread=True)
            except Exception as e:
                self._put_log("[警告] 删除任务时关闭表单异常：%s" % e)
            finally:
                self._deleting_tids.discard(tid)
            try:
                form.destroy()
            except Exception:
                pass
        self.tasks = [t for t in self.tasks if t.get("id") != tid]
        launcher.save_grab_tasks(self.tasks)
        self._refresh_list()
        self._select_first()
        self._put_log("[任务] 已删除「%s」" % task.get("name"))

    def _upsert_task(self, lc):
        """任务表单保存回调：按 id 写回任务库并刷新列表。"""
        tid = lc.get("id")
        for i, t in enumerate(self.tasks):
            if t.get("id") == tid:
                self.tasks[i] = lc
                break
        else:
            self.tasks.append(lc)
        launcher.save_grab_tasks(self.tasks)
        self._refresh_list()

    def _on_window_closed(self, tid):
        """关任务前的状态修正（保留「已抢到」，其余归「就绪」）——与旧实现同口径。"""
        for t in self.tasks:
            if t.get("id") == tid:
                if t.get("status") != "ok":
                    t["status"] = "idle"
        launcher.save_grab_tasks(self.tasks)
        self._refresh_list()
        self._put_log("[任务] 「%s」已停止并保存配置"
                      % next((t.get("name") for t in self.tasks if t.get("id") == tid), "?"))

    # ---- 定时刷新 / 日志 / 会话 ----

    def _tick(self):
        try:
            for iid, task in list(self._tv_iid.items()):
                try:
                    if not self.tree.exists(iid):
                        continue
                except Exception:
                    continue
                form = self.forms.get(task.get("id"))
                if form is not None and form.grabber is not None \
                        and form.grabber.is_alive():
                    label = "抢票中"
                else:
                    label = GRAB_STATUS_LABELS.get(task.get("status") or "idle", "就绪")
                self.tree.set(iid, "status", label)
        finally:
            try:
                self.after(800, self._tick)
            except tk.TclError:
                pass

    def _put_log(self, line):
        self.app.log_line(line)

    def _test_session(self):
        self._put_log("[会话] 正在校验登录状态…")

        def worker():
            # 子线程不能直接碰 Tk：结果走 launcher 日志队列，由主线程轮询显示
            try:
                ok, who = launcher.browser_order.check_session()
                launcher.log("[会话] 校验结果：%s（%s）" % ("已登录" if ok else "未登录", who))
            except Exception as e:
                launcher.log("[错误] 会话校验异常：%s" % e)
        threading.Thread(target=worker, daemon=True).start()

    def close_all(self):
        """程序退出前收尾：逐任务停抢票线程并保存配置。"""
        for tid, form in list(self.forms.items()):
            try:
                form.close(stop_thread=True)
            except Exception as e:
                LOG.warning("抢票任务收尾失败（%s）：%s", tid, e)
        try:
            launcher.save_grab_tasks(self.tasks)
        except Exception:
            pass


class GrabTaskForm(ttk.Frame):
    """单个抢票任务的编辑表单（原 LauncherApp 行为等价物，嵌入式朴素控件）。

    每个任务一份实例，常驻（不显示也照跑 _tick：倒计时/提醒/到点自动开抢）。
    日志统一走全局日志区（launcher 日志队列），不再有任务级日志窗。"""

    def __init__(self, master, tab, task):
        super().__init__(master)
        self.app = tab.app
        self.tab = tab
        self._top = self.app.root        # 蜂鸣/弹窗宿主（与旧接口同名，便于测试）
        self._mp = self.app.root
        self.task = task
        self.tid = task.get("id")
        self.task_name = task.get("name") or ""
        self.lc = dict(launcher._DEFAULT_LC)
        self.lc.update(task)
        self._save_cfg = self._save_lc
        self.grabber = None
        self.armed = False
        self.auto_fired = False
        self.reminded = False
        self._auto_vfail_last_msg = None
        self.pax_vars = {}
        self.pax_purpose_vars = {}
        self.seat_vars = {}
        self.update_info = None
        self._monitor_mtime = 0.0
        self._pax_mtime = 0.0
        self.dataq = queue.Queue()
        self._querying = False
        self._last_query_ts = 0.0
        self._train_infos = []
        self._sel_keys = set()
        self._tv_iids = {}
        self._syncing_sel = False

        self._build()
        self._sync_from_lc()
        self.after(500, self._tick)
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
                launcher.get_station_index()  # 一并预热车站拼音索引（首次要下载）
                codes = []
                for n in names:
                    c = n2c.get((n or "").strip())
                    if c and c not in codes:
                        codes.append(c)
                if codes and launcher.request_station_kinds(codes):
                    launcher.log("[车站] 正在后台识别常用车站类型（高铁 / 普速）")
            except Exception as e:
                launcher.log("[提醒] 常用车站类型预热失败：%s" % e)

        threading.Thread(target=worker, daemon=True, name="station-warm").start()

    # ---- 界面构建：行程/车次/席别/乘车人/开抢 ----

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x")
        self.title_lbl = ttk.Label(top, text="任务：%s" % self.task_name,
                                   font=("", 11, "bold"))
        self.title_lbl.pack(side="left", padx=4)
        self.update_lbl = ttk.Label(top, text="", foreground="#8250df", cursor="hand2")
        self.update_lbl.pack(side="right", padx=6)

        # 乘车人
        pf = ttk.LabelFrame(self, text=" 乘车人（勾选参与抢票；票种按人选择） ", padding=8)
        pf.pack(fill="x", pady=(4, 0))
        self.pax_box = ttk.Frame(pf)
        self.pax_box.pack(side="left", fill="x", expand=True)
        ttk.Button(pf, text="新增/编辑", command=self._edit_pax).pack(side="right")

        # 行程
        tf = ttk.LabelFrame(self, text=" 行程（车站支持拼音简拼 / 全拼 / 汉字 / 代码搜索） ", padding=8)
        tf.pack(fill="x", pady=(4, 0))
        r0 = ttk.Frame(tf)
        r0.pack(fill="x")
        ttk.Label(r0, text="常用行程").pack(side="left")
        self.preset_cb = ttk.Combobox(r0, state="readonly", width=26)
        self.preset_cb.pack(side="left", padx=6)
        self.preset_cb.bind("<<ComboboxSelected>>", self._load_preset)
        ttk.Button(r0, text="载入", command=self._load_preset).pack(side="left", padx=2)
        ttk.Button(r0, text="存为预设", command=self._save_preset).pack(side="left", padx=2)
        ttk.Button(r0, text="删除预设", command=self._delete_preset).pack(side="left", padx=2)

        r1 = ttk.Frame(tf)
        r1.pack(fill="x", pady=(6, 0))
        ttk.Label(r1, text="出发").pack(side="left")
        self.from_ent = StationEntry(r1, width=12, history=self.lc.get("station_history"))
        self.from_ent.pack(side="left", padx=(4, 2))
        ttk.Button(r1, text="⇄", width=3, command=self._swap_stations).pack(side="left", padx=2)
        ttk.Label(r1, text="到达").pack(side="left", padx=(6, 0))
        self.to_ent = StationEntry(r1, width=12, history=self.lc.get("station_history"))
        self.to_ent.pack(side="left", padx=(4, 4))
        ttk.Button(r1, text="定位", width=5, command=self._locate).pack(side="left")
        ttk.Label(r1, text="车次留空=全部；日期「到」留空=只查那一天（区间最多相差 5 天，共 6 天）",
                  foreground="#57606a").pack(side="left", padx=(10, 0))

        r2 = ttk.Frame(tf)
        r2.pack(fill="x", pady=(6, 0))
        ttk.Label(r2, text="乘车日期").pack(side="left")
        ttk.Label(r2, text="从").pack(side="left", padx=(6, 2))
        self.date_var = tk.StringVar()
        ttk.Entry(r2, textvariable=self.date_var, width=12).pack(side="left")
        ttk.Label(r2, text="到").pack(side="left", padx=(6, 2))
        self.date_to_var = tk.StringVar()
        ttk.Entry(r2, textvariable=self.date_to_var, width=12).pack(side="left")
        ttk.Label(r2, text="车次").pack(side="left", padx=(12, 0))
        self.trains_var = tk.StringVar()
        ttk.Entry(r2, textvariable=self.trains_var, width=18).pack(side="left", padx=(4, 0))
        ttk.Button(r2, text="历史记录", command=self._show_query_history).pack(side="left", padx=(8, 0))

        # 车次列表
        qf = ttk.LabelFrame(self, text=" 车次列表（查询实时余票，Ctrl/Shift 多选车次） ", padding=8)
        qf.pack(fill="x", pady=(4, 0))
        q0 = ttk.Frame(qf)
        q0.pack(fill="x")
        self.query_btn = ttk.Button(q0, text="查询车次 (F5)", command=lambda: self.query_trains())
        self.query_btn.pack(side="left")
        self.query_state = ttk.Label(q0, text="", foreground="#57606a")
        self.query_state.pack(side="left", padx=8)
        ttk.Button(q0, text="清空列表", command=self._clear_train_rows).pack(side="right")
        # 余票自动刷新：默认 2 秒/次、默认开启（控制界面不暴露；变量仍驱动 _tick）
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

        tvf = ttk.Frame(qf)
        tvf.pack(fill="x", pady=(6, 0))
        tv_cols = ("train", "route", "time", "duration", "seats")
        tv_heads = ("车次", "出发-到达", "时间", "历时", "余票")
        tv_widths = (70, 160, 110, 70, 420)
        self.train_tree = ttk.Treeview(tvf, columns=tv_cols, show="headings",
                                       height=6, selectmode="extended")
        for c, h, w in zip(tv_cols, tv_heads, tv_widths):
            self.train_tree.heading(c, text=h)
            self.train_tree.column(c, width=w, anchor="w" if c == "seats" else "center")
        tv_sb = ttk.Scrollbar(tvf, orient="vertical", command=self.train_tree.yview)
        self.train_tree.configure(yscrollcommand=tv_sb.set)
        self.train_tree.pack(side="left", fill="x", expand=True)
        tv_sb.pack(side="right", fill="y")
        self.train_tree.bind("<<TreeviewSelect>>", self._on_trains_selected)

        # 席别
        self.sf = ttk.LabelFrame(self, text=" 席别（查询后可按车次实际余票刷新） ", padding=8)
        self.sf.pack(fill="x", pady=(4, 0))
        self.seat_detail_lbl = ttk.Label(self.sf, text="", foreground="#57606a", justify="left")
        self.seat_detail_lbl.pack(fill="x", pady=(0, 4))
        t0 = ttk.Frame(self.sf)
        t0.pack(fill="x")
        ttk.Label(t0, text="首选席别").pack(side="left")
        self.seat_pri_var = tk.StringVar()
        ttk.Entry(t0, textvariable=self.seat_pri_var, width=18).pack(side="left", padx=(4, 6))
        ttk.Label(t0, text="可填 硬座/无座/二等座/软卧/硬卧 等，逗号分隔，先填的先抢；"
                           "可指定车次：K225=硬座/无座, K1969=硬卧（只作用该趟车，∩勾选）；"
                           "无座=硬座，认不出按硬座",
                  foreground="#57606a").pack(side="left")
        self.seat_pri_hint = ttk.Label(self.sf, foreground="#57606a")
        self.seat_pri_hint.pack(fill="x", pady=(2, 4))
        self.seat_pri_var.trace_add("write", self._on_seat_pri_change)
        self.seat_box = ttk.Frame(self.sf)
        self.seat_box.pack(fill="x")
        self._rebuild_seats()
        self._on_seat_pri_change()

        # 开抢时间
        ef = ttk.LabelFrame(self, text=" 开抢时间 ", padding=8)
        ef.pack(fill="x", pady=(4, 0))
        r0e = ttk.Frame(ef)
        r0e.pack(fill="x")
        ttk.Label(r0e, text="到点自动开抢").pack(side="left")
        self.start_var = tk.StringVar()
        self.start_var.trace_add("write", self._on_start_change)
        ttk.Entry(r0e, textvariable=self.start_var, width=21).pack(side="left", padx=(6, 6))
        ttk.Label(r0e, text="提前").pack(side="left")
        self.remind_var = tk.IntVar(value=10)
        ttk.Spinbox(r0e, from_=0, to=120, textvariable=self.remind_var, width=4).pack(
            side="left", padx=4)
        ttk.Label(r0e, text="分钟提醒").pack(side="left")
        ttk.Label(r0e, text="（格式如 2026-10-09 17:00:00）", foreground="#57606a").pack(
            side="left", padx=(6, 0))
        self.countdown_lbl = ttk.Label(r0e, text="", font=("", 10, "bold"),
                                       foreground="#cf222e")
        self.countdown_lbl.pack(side="left", padx=(10, 0))
        r1e = ttk.Frame(ef)
        r1e.pack(fill="x", pady=(4, 0))
        ttk.Label(r1e, text="开抢前").pack(side="left")
        self.warm_var = tk.IntVar(value=10)
        ttk.Spinbox(r1e, from_=0, to=30, textvariable=self.warm_var, width=4).pack(
            side="left", padx=4)
        ttk.Label(r1e, text="分钟登录预热（到点立即填单下单，0=不预热）").pack(side="left")

        # 主按钮 + 状态
        bf = ttk.Frame(self)
        bf.pack(fill="x", pady=(8, 0))
        self.go_btn = ttk.Button(bf, text="立 即 抢 票", command=self.toggle_grab)
        self.go_btn.pack(side="left", fill="x", expand=True)
        ttk.Button(bf, text="新建监控任务", command=self.open_new_monitor_task).pack(
            side="left", padx=(8, 0))
        ttk.Button(bf, text="测试会话", command=self._test_session).pack(side="left", padx=(8, 0))

        stf = ttk.Frame(self)
        stf.pack(fill="x", pady=(6, 4))
        ttk.Label(stf, text="状态：").pack(side="left")
        self.status_lbl = ttk.Label(stf, text="就绪")
        self.status_lbl.pack(side="left")

        # 快捷键：F5 查车次 / Ctrl+Enter 开抢或停止 / Ctrl+L 定位
        self.bind("<F5>", lambda e: self.query_trains())
        self.bind("<Control-Return>", lambda e: self.toggle_grab())
        self.bind("<Control-l>", lambda e: self._locate())

    # ---- 数据同步 ----

    def _save_lc(self, lc):
        """保存回调（任务级）：写回任务库；任务库中现在存的就是 lc。"""
        lc["id"] = self.tid
        lc["name"] = self.task_name
        lc["status"] = lc.get("status") or "idle"
        self.tab._upsert_task(lc)
        self.task = lc

    def _sync_from_lc(self):
        lc = self.lc
        hist = lc.get("station_history") or []
        self.from_ent.set_history(hist)
        self.to_ent.set_history(hist)
        self.from_ent.set(lc.get("from") or "")
        self.to_ent.set(lc.get("to") or "")
        self.trains_var.set(",".join(_display_trains(lc.get("trains"))))
        self.date_var.set(lc.get("date") or (datetime.datetime.now() + datetime.timedelta(days=1)).strftime("%Y-%m-%d"))
        self.date_to_var.set(lc.get("date_to") or "")
        for s, v in self.seat_vars.items():
            v.set(s in _display_seat_types(lc.get("seat_types")))
        self.seat_pri_var.set(str(lc.get("seat_priority") or ""))
        self.start_var.set(lc.get("start_time") or "")
        # Task 96d：launcher_config.json 脏值不崩任务表单——非数字用安全默认值，
        # presets 非 dict 条目跳过，均记 warning。
        try:
            self.remind_var.set(int(lc.get("remind_minutes") or 10))
        except (ValueError, TypeError):
            self._put_log("[警告] 配置 remind_minutes 非法（%r），已用默认值 10"
                          % (lc.get("remind_minutes"),))
            self.remind_var.set(10)
        try:
            self.warm_var.set(max(0, min(30, int(lc.get("warm_minutes") or 10))))
        except (ValueError, TypeError):
            self._put_log("[警告] 配置 warm_minutes 非法（%r），已用默认值 10"
                          % (lc.get("warm_minutes"),))
            self.warm_var.set(10)
        names = []
        for p in (lc.get("presets") or []):
            if not isinstance(p, dict):
                self._put_log("[警告] 配置 presets 条目非 dict，已跳过：%r" % (p,))
                continue
            names.append(p.get("name") or "")
        self.preset_cb.configure(values=names)
        if names:
            self.preset_cb.current(0)
        self._refresh_pax()
        if launcher.merge_trains_from_monitor(self.lc, self._save_cfg):
            self.trains_var.set(",".join(self.lc["trains"]))
        try:
            self._monitor_mtime = os.path.getmtime(os.path.join(HERE, "config.json"))
        except OSError:
            pass
        try:
            self._pax_mtime = os.path.getmtime(os.path.join(HERE, "passengers.json"))
        except OSError:
            pass
        st = launcher.parse_dt(self.lc.get("start_time") or "")
        self.armed = bool(st and st > datetime.datetime.now())
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
                n: (launcher.LABEL_TO_PURPOSE.get(v.get()) or "ADULT")
                for n, v in self.pax_purpose_vars.items()}
        # 兼容监控引擎 / 旧配置：全局口径 = 勾选乘车人的余票查询口径
        lc["purpose_code"] = launcher.purpose_of(lc)
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
                      foreground="#57606a").pack(side="left")
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
            label = "%s · %s" % (name, launcher.mask_id(p.get("id_no")))
            if p.get("mobile"):
                label += " · " + launcher.mask_mobile(p.get("mobile"))
            ttk.Checkbutton(row, text=label, variable=v).pack(side="left")
            # 票种按人：优先本次界面值 > 已保存值 > 全局兜底
            code = (launcher.LABEL_TO_PURPOSE.get(cur_pp.get(name) or "")
                    or saved.get(name) or default_code)
            pv = tk.StringVar(value=launcher.PURPOSE_LABELS.get(code) or "成人票")
            self.pax_purpose_vars[name] = pv
            cb = ttk.Combobox(row, textvariable=pv, width=6, state="readonly",
                              values=["成人票", "学生票"])
            cb.pack(side="left", padx=(4, 0))
            cb.bind("<<ComboboxSelected>>", self._on_pax_purpose_change)

    # ---- 车次查询与席别联动 ----

    def _on_seat_pri_change(self, *_a):
        """「首选席别」输入框的即时解析反馈（含 车次=席别 专属规则）。"""
        checked = [s for s, v in self.seat_vars.items() if v.get()]
        text, bad = ticket.seat_priority_feedback(self.seat_pri_var.get(), checked=checked)
        self.seat_pri_hint.configure(text=text,
                                     foreground="#cf222e" if bad else "#57606a")

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
                            key=lambda x: launcher.SEAT_ORDER.get(x, 99))
            self.sf.configure(text=" 席别（该车次共 %d 种席别，%d 种有票；无票的也能勾，"
                                   "放票/退票回流后按勾选顺序抢） "
                              % (len(names), len([s for s in names if avail.get(s)])))
        else:
            names = [s for s in launcher.SEAT_OPTIONS if available is None or s in avail]
            names += sorted((s for s in avail if s not in launcher.SEAT_OPTIONS),
                            key=lambda x: launcher.SEAT_ORDER.get(x, 99))
            if available is not None:
                self.sf.configure(text=" 席别（已按所选车次刷新：%d 种可购席别） " % len(names))
            else:
                self.sf.configure(text=" 席别（查询并选中车次后可按该车实际余票刷新；票价以官方下单页为准） ")
        if not names:
            ttk.Label(self.seat_box, text="（该车次暂无可购席别）",
                      foreground="#57606a").grid(row=0, column=0, sticky="w")
            return
        # Task 103b：展示用归一化——裸字符串按单个席别。
        default = set(_display_seat_types(self.lc.get("seat_types")))
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
                                       "请填写出发站 / 到达站，日期格式 YYYY-MM-DD",
                                       parent=self._mp)
            return
        self._querying = True
        self._last_query_ts = time.time()
        self.query_btn.configure(state="disabled")
        self.query_state.configure(text="查询中…", foreground="#d97706")
        lc = dict(self.lc)
        _pm = lc.get("pax_purpose") or {}
        _ptxt = "、".join(
            # Task 88c：日志里的乘车人姓名打码；学生票后缀保留（非 PII）。
            "%s%s" % (launcher._mask_one_name(n),
                      "（学生票）" if _pm.get(n) == "0X00" else "")
            for n in (lc.get("passenger_names") or [])) or "账号默认乘车人"
        self._put_log("[查询] %s → %s %s（乘车人：%s；余票口径 %s）…" % (
            lc.get("from"), lc.get("to"), lc.get("date"), _ptxt,
            launcher.PURPOSE_LABELS.get(launcher.purpose_of(lc)) or "成人票"))
        threading.Thread(target=self._query_work, args=(lc,), daemon=True,
                         name="train-query").start()

    def _query_work(self, lc):
        try:
            name2code, code2name = ticket.load_station_map()
            fc, tc = name2code.get(lc.get("from")), name2code.get(lc.get("to"))
            if not fc or not tc:
                # 新开车站可能不在本地缓存里：后台强制刷新一次，下次可查到
                ticket.note_station_missing(lc.get("from") if not fc else lc.get("to"))
                raise RuntimeError("车站无法识别：%s / %s" % (lc.get("from"), lc.get("to")))
            infos = []
            for dt in launcher.resolve_dates(lc):
                rows = ticket.query_tickets(fc, tc, dt, purpose=launcher.purpose_of(lc))
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
        launcher.learn_station_kinds(infos)

    def _refresh_train_view(self, log=False):
        """按筛选 / 排序条件重建车次列表。"""
        infos = list(getattr(self, "_train_infos", []))
        only_emu = self.filter_emu_var.get()
        only_putong = self.filter_putong_var.get()
        if only_emu and not only_putong:
            infos = [i for i in infos if launcher.is_emu(i.get("train_code"))]
        elif only_putong and not only_emu:
            infos = [i for i in infos if not launcher.is_emu(i.get("train_code"))]
        if self.filter_avail_var.get():
            infos = [i for i in infos if i.get("available_seats")]
        if self.sort_var.get() == "dur":
            infos.sort(key=lambda x: ((x.get("_date") or ""), x.get("duration") or "99:99:99"))
        else:
            infos.sort(key=lambda x: ((x.get("_date") or ""), x.get("start_time") or "99:99"))
        total_all = len(getattr(self, "_train_infos", []))
        # 重建车次表（保留选中车次：按 key = 日期@车次）
        self._syncing_sel = True
        try:
            self.train_tree.delete(*self.train_tree.get_children())
            self._tv_iids = {}
            for info in infos:
                code = info.get("train_code") or "?"
                key = "%s@%s" % (info.get("_date") or "", code)
                seats = info.get("available_seats") or {}
                seat_txt = " ".join("%s %s" % (k, v) for k, v in seats.items()) or "无"
                iid = self.train_tree.insert("", "end", values=(
                    code,
                    "%s-%s" % (info.get("from_name") or "", info.get("to_name") or ""),
                    "%s-%s" % (info.get("start_time") or "--:--",
                               info.get("arrive_time") or "--:--"),
                    info.get("duration") or "-", seat_txt))
                self._tv_iids[iid] = info
                if key in self._sel_keys:
                    self.train_tree.selection_add(iid)
        finally:
            self._syncing_sel = False
        hit = [i for i in infos if i.get("available_seats")]
        if infos and len(infos) != total_all:
            state_txt = "%d/%d 趟 · 有票 %d 趟" % (len(infos), total_all, len(hit))
        else:
            state_txt = "%d 趟 · 有票 %d 趟" % (len(infos), len(hit))
        self.query_state.configure(text=state_txt,
                                   foreground="#1a7f37" if hit else "#57606a")
        # 选中车次联动席别区（重建后按当前选中重新求并集）
        self._on_trains_selected()
        if log:
            if hit:
                self._put_log("[查询] %d 趟车里 %d 趟有票：%s" % (
                    len(infos), len(hit), " ".join(i["train_code"] for i in hit[:10])))
            else:
                self._put_log("[查询] 共 %d 趟，暂无可购余票" % len(infos))

    def _on_trains_selected(self, _e=None):
        """车次选中变化：把选中车次写回车次框，并按并集余票刷新席别区。"""
        if getattr(self, "_syncing_sel", False):
            return
        if not hasattr(self, "seat_box"):
            return          # _build 尚未建好席别区
        infos = [self._tv_iids[i] for i in self.train_tree.selection()
                 if i in self._tv_iids]
        keys = set()
        merged = {}
        allnames = []
        houbu = False
        for i in infos:
            code = i.get("train_code") or "?"
            keys.add("%s@%s" % (i.get("_date") or "", code))
            for k, v in (i.get("available_seats") or {}).items():
                merged.setdefault(k, v)
            for n in (i.get("seats_all") or []):
                if n not in allnames:
                    allnames.append(n)
            if i.get("houbu"):
                houbu = True
        self._sel_keys = keys
        order = {n: idx for idx, n in enumerate(ticket.SEAT_SHOW_ORDER)}
        allnames.sort(key=lambda x: order.get(x, 99))
        codes = [i.get("train_code") for i in infos if i.get("train_code")]
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
        """车次列表下方：列出选中车次的全部席别（含无票）。"""
        if not hasattr(self, "seat_detail_lbl"):
            return
        infos = [i for i in infos if i]
        if not infos:
            self.seat_detail_lbl.configure(text="该车全部席别：（选中车次后列出这趟车提供的全部席别，无票也列）\n"
                                                "车型参考　" + self.KIND_SEAT_HINT)
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
            k = launcher.train_kind(c)
            if k and k not in kinds:
                kinds.append(k)
        houbu = any(i.get("houbu") for i in infos)
        head = "该车全部席别：%s（%s）　共 %d 种席别，有票 %d 种%s" % (
            "、".join(codes), "、".join(kinds), len(names), len(hit),
            "　售完可候补" if houbu else "")
        parts = []
        for n in names:
            v = merged.get(n)
            if v:
                parts.append("%s %s" % (n, v))
            elif houbu:
                parts.append("%s 候补" % n)
            else:
                parts.append("%s 无" % n)
        lines = [head]
        for i in range(0, len(parts), 8):
            lines.append("　".join(parts[i:i + 8]))
        self.seat_detail_lbl.configure(text="\n".join(lines))

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
        txt = "、".join("%s=%s" % (n, launcher.PURPOSE_LABELS.get(pm.get(n)) or "成人票")
                        for n in picked) or "未勾选乘车人"
        self._put_log("[票种] %s；余票口径 %s（建议重新查询车次）" % (
            txt, launcher.PURPOSE_LABELS.get(launcher.purpose_of(lc)) or "成人票"))

    def _clear_train_rows(self):
        self._train_infos = []
        self._sel_keys = set()
        self.train_tree.delete(*self.train_tree.get_children())
        self._tv_iids = {}
        self.query_state.configure(text="", foreground="#57606a")
        self.sel_state.configure(text="已选 0 趟车 · 0 种席别")
        self._rebuild_seats()
        self._render_seat_detail([])

    def _swap_stations(self):
        a, b = self.from_ent.get(), self.to_ent.get()
        self.from_ent.set(b)
        self.to_ent.set(a)
        if a or b:
            self._put_log("[行程] 已交换出发/到达：%s → %s" % (b or "?", a or "?"))

    def _remember_query(self):
        """记录本次查询到历史（去重、最多 10 条）。"""
        lc = self.lc
        item = {"from": lc.get("from") or "", "to": lc.get("to") or "",
                "date": lc.get("date") or ""}
        if not (item["from"] and item["to"]):
            return
        hist = [h for h in (lc.get("query_history") or [])
                if not (isinstance(h, dict)
                        and h.get("from") == item["from"] and h.get("to") == item["to"]
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
            self._drain()
            self._auto_refresh_trains()
            now = datetime.datetime.now()
            g = self.grabber
            if g and not g.is_alive():
                self.grabber = None
                ok, msg = g.result if g.result else (False, "已停止")
                self.go_btn.configure(text="立 即 抢 票", state="normal")
                if ok:
                    self.auto_fired = True
                    self._set_status("ok", "抢票成功！")
                    # 成功状态落盘：否则重启后任务库里显示"就绪"，已抢到的事被抹掉
                    self.lc["status"] = "ok"
                    try:
                        self._save_cfg(self.lc)
                    except Exception:
                        pass
                    try:
                        self._top.bell()
                    except Exception:
                        pass
                else:
                    self._set_status("idle", msg or "已停止")
            if not self.grabber:
                st = launcher.parse_dt(self.start_var.get().strip())
                if st:
                    diff = (st - now).total_seconds()
                    lead = 0
                    try:
                        lead = max(0, int(self.warm_var.get() or 10)) * 60
                    except Exception:
                        pass
                    if diff > lead:
                        _cd = "距开抢 %s" % launcher.fmt_countdown(diff)
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
                            try:
                                self._top.bell()
                            except Exception:
                                pass
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
                            try:
                                self._top.bell()
                            except Exception:
                                pass
                            self.start_grab(auto=True)
                        elif self.armed and not self.auto_fired:
                            self.armed = False
                            self.countdown_lbl.configure(text="开抢时间已过，请手动开抢")
                        elif diff > 0:
                            # 还没到点（只是进入了预热窗口）：照常显示倒计时，
                            # 别提前十分钟就喊"开抢时间已过"
                            self.countdown_lbl.configure(text="距开抢 %s" % launcher.fmt_countdown(diff))
                        else:
                            self.countdown_lbl.configure(text="开抢时间已过")
                else:
                    self.countdown_lbl.configure(text="未设置开抢时间")
        finally:
            try:
                self.after(500, self._tick)
            except tk.TclError:
                pass  # 表单已销毁，停止循环

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
        if launcher.merge_trains_from_monitor(self.lc, self._save_cfg):
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
                kind, payload = self.dataq.get_nowait()
                if kind == "trains":
                    self._apply_train_rows(payload)
                elif kind == "trains_err":
                    self._apply_train_error(payload)
        except queue.Empty:
            pass

    def _put_log(self, line):
        self.app.log_line(line)

    def _set_status(self, color_key, text):
        colors = {"running": "#d97706", "ok": "#1a7f37", "idle": "#6e7781"}
        try:
            self.status_lbl.configure(text=text, foreground=colors.get(color_key, "#6e7781"))
        except Exception:
            pass

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
                try:
                    self._top.bell()
                except Exception:
                    pass
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
            self.grabber = launcher.Grabber(dict(self.lc))
            self.grabber.start()
        except Exception as e:
            # 线程启动失败（极罕见）：回滚置位，允许下个 _tick 重试
            self.grabber = None
            if auto:
                self.auto_fired = False
            self._put_log("[启动] 抢票线程启动失败：%s" % e)
            return False
        self._set_status("running", "抢票中…（后台运行，无窗口）")
        self.go_btn.configure(text="停 止")
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

    def close(self, stop_thread=True):
        """停止抢票线程并把界面编辑同步回任务（落盘一次）。

        删除任务前 / 程序退出前调用；「已抢到」状态不抹除（其余归「就绪」）。"""
        if stop_thread and self.grabber:
            try:
                self.grabber.stop()
                # 等收尾（WarmSession 关浏览器、释放 profile 锁），避免留下孤儿浏览器
                self.grabber.join(timeout=15)
            except Exception:
                pass
            self.grabber = None
        try:
            self._ui_to_lc(save=False)
        except Exception:
            pass
        if self.lc.get("status") != "ok":
            self.lc["status"] = "idle"
        try:
            self._save_cfg(self.lc)
        except Exception:
            pass

    # ---- 会话 / 定位 / 更新 ----

    def open_new_monitor_task(self):
        """打开「新建监控任务」对话框（任务写入监控系统的任务表）。"""
        NewTaskDialog(self.app, on_created=self.app.refresh_tasks,
                      prefill={"from": self.from_ent.get(), "to": self.to_ent.get(),
                               "date": self.date_var.get().strip(),
                               "trains": _display_trains(self.trains_var.get().replace("，", ",").split(","))})

    def _test_session(self):
        self._put_log("[会话] 正在校验登录状态…")

        def worker():
            # 子线程不能直接碰 Tk：结果走 launcher 日志队列，由主线程轮询显示
            try:
                ok, who = launcher.browser_order.check_session()
                launcher.log("[会话] 校验结果：%s（%s）" % ("已登录" if ok else "未登录", who))
            except Exception as e:
                launcher.log("[错误] 会话校验异常：%s" % e)
        threading.Thread(target=worker, daemon=True).start()

    def _locate(self):
        self._put_log("[运行] 正在定位当前 IP 位置…")

        def worker():
            launcher.log("[提醒] 当前位置：%s" % launcher.ip_locate())
        threading.Thread(target=worker, daemon=True).start()

    def _check_update_async(self):
        def worker():
            try:
                res = launcher.check_update(self.lc)
            except Exception:
                return
            self.update_info = (res[1], res[2], res[3]) if res[0] else None
            try:
                self.after(0, self._show_update)
            except RuntimeError:
                pass  # 已关闭，忽略
        threading.Thread(target=worker, daemon=True).start()

    def _show_update(self):
        if self.update_info:
            ver, notes, url = self.update_info
            self.update_lbl.configure(text="新版本 v%s 可用，点击查看" % ver)
            self.update_lbl.bind("<Button-1>", lambda e: messagebox.showinfo(
                "版本更新", "新版本：v%s\n\n%s\n\n下载地址：%s" % (ver, notes or "（无说明）", url or "（未提供）"),
                parent=self._mp))

    # ---- 预设 ----

    def _current_preset(self):
        sel = self.preset_cb.get().strip()
        for p in self.lc.get("presets") or []:
            if not isinstance(p, dict):
                continue
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
        # Task 106：裸字符串按单个席别精确匹配，杜绝子串误判。
        for s, v in self.seat_vars.items():
            v.set(s in _display_seat_types(p.get("seat_types")))
        self.seat_pri_var.set(str(p.get("seat_priority") or ""))
        self.date_var.set(p.get("date") or "")
        self._put_log("[运行] 已载入常用行程「%s」" % p.get("name"))

    def _save_preset(self):
        self._ui_to_lc()
        name = simpledialog.askstring("存为预设", "给这个常用行程起个名字：",
                                      parent=self._mp,
                                      initialvalue=self.preset_cb.get())
        if not name:
            return
        entry = {
            "name": name.strip(),
            "from": self.lc.get("from"),
            "to": self.lc.get("to"),
            # Task 103a：写回前归一化——裸字符串按单值处理，绝不 list() 逐字符拆。
            "trains": launcher._normalize_trains(self.lc.get("trains"), self._put_log),
            "seat_types": launcher._normalize_seat_types(self.lc.get("seat_types"),
                                                         self._put_log),
            "seat_priority": self.lc.get("seat_priority") or "",
            "date": self.lc.get("date") or "",
        }
        presets = [p for p in (self.lc.get("presets") or [])
                   if not isinstance(p, dict) or (p.get("name") or "") != entry["name"]]
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
            self.preset_cb.configure(values=[x.get("name") or "" for x in self.lc["presets"]
                                             if isinstance(x, dict)])
            self.preset_cb.set("")
            self._put_log("[运行] 常用行程「%s」已删除" % p.get("name"))

    # ---- 乘车人编辑 ----

    def _edit_pax(self):
        PassengerDialog(self.app, on_saved=self._pax_saved_by_dialog)

    def _pax_saved_by_dialog(self):
        self._refresh_pax()
        try:
            self._pax_mtime = os.path.getmtime(os.path.join(HERE, "passengers.json"))
        except OSError:
            pass

    # ---- 收尾 ----

    def _on_start_change(self, *_):
        st = launcher.parse_dt(self.start_var.get())
        self.armed = bool(st and st > datetime.datetime.now())
        self.auto_fired = False
        self._auto_vfail_last_msg = None
        self.reminded = False


class PassengerDialog(tk.Toplevel):
    """乘车人快速编辑（抢票表单内弹窗）：一行列表 + 表单，保存走加密库。"""

    def __init__(self, master, on_saved=None):
        super().__init__(master)
        self.on_saved = on_saved
        self.title("乘车人信息")
        self.geometry("440x360")
        self.transient(master.winfo_toplevel() if master is not None else None)
        self.grab_set()
        try:
            self.plist = passengers_mod.load_passengers()
        except Exception as e:
            self.plist = []
            messagebox.showwarning("读取失败", "乘车人数据读取失败：%s" % e, parent=self)
        # Task 90：记下 load 时的字节指纹，供保存时做乐观并发检查
        self._pax_stamp = passengers_mod.passengers_stamp()

        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)
        r0 = ttk.Frame(f)
        r0.pack(fill="x")
        ttk.Label(r0, text="选择").pack(side="left")
        self.pick = ttk.Combobox(r0, state="readonly", width=24,
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

        g = ttk.Frame(f)
        g.pack(fill="both", expand=True)
        self.id_entry = ttk.Entry(g, textvariable=self.id_var, width=26, show="*")
        grid = [
            ("姓名", ttk.Entry(g, textvariable=self.name_var, width=26)),
            ("证件类型", ttk.Combobox(g, textvariable=self.type_var, width=23,
                                      values=list(passengers_mod.ID_TYPE_NAMES.values()),
                                      state="readonly")),
            ("证件号", self.id_entry),
            ("手机号", ttk.Entry(g, textvariable=self.mob_var, width=26)),
        ]
        for i, (lab, w) in enumerate(grid):
            ttk.Label(g, text=lab).grid(row=i + 1, column=0, sticky="e", pady=4)
            w.grid(row=i + 1, column=1, sticky="w", padx=6, pady=4)
        # 小眼睛：证件号默认掩码，点一下切明文、再点还原
        self._id_visible = False
        self.eye_btn = ttk.Button(g, text="显示", width=5, command=self._toggle_id_show)
        self.eye_btn.grid(row=3, column=2, sticky="w", padx=(0, 2))
        ttk.Checkbutton(g, text="成人票", variable=self.adult_var).grid(row=5, column=1, sticky="w", pady=2)
        ttk.Checkbutton(g, text="设为默认乘车人", variable=self.default_var).grid(row=6, column=1, sticky="w")

        bf = ttk.Frame(g)
        bf.grid(row=7, column=0, columnspan=3, pady=(10, 0))
        ttk.Button(bf, text="保 存", command=self._save).pack(side="left", padx=6)
        ttk.Button(bf, text="关 闭", command=self.destroy).pack(side="left", padx=6)

        if self.plist:
            self.pick.current(0)
            self._load()

    def _toggle_id_show(self):
        self._id_visible = not self._id_visible
        self.id_entry.configure(show="" if self._id_visible else "*")
        self.eye_btn.configure(text="隐藏" if self._id_visible else "显示")

    def _load(self, _event=None):
        name = self.pick.get().strip()
        for p in self.plist:
            if (p.get("name") or "") == name:
                self.name_var.set(p.get("name") or "")
                self.id_var.set(p.get("id_no") or "")
                self.mob_var.set(p.get("mobile") or "")
                for code, lab in passengers_mod.ID_TYPE_NAMES.items():
                    if str(p.get("id_type_code")) == str(code):
                        self.type_var.set(lab)
                self.adult_var.set(bool(p.get("is_adult", True)))
                self.default_var.set(bool(p.get("is_default")))
                return

    def _refresh_plist(self):
        """重载最新乘车人数据并更新指纹/下拉框（Task 90：乐观并发放弃后调用）。"""
        self.plist = passengers_mod.load_passengers()
        self._pax_stamp = passengers_mod.passengers_stamp()
        self.pick.configure(values=[p.get("name") or "" for p in self.plist])

    def _save_passengers_or_refresh(self, parent):
        """保存 self.plist（Task 90：乐观并发检查）。

        成功 → (True, "ok")。指纹变化（打开期间被外部修改）→
        重载最新数据+警告，返回 (False, "stale")，调用方放弃本次保存。
        不可解密盒子/锁超时 → error，返回 (False, "refused")。
        """
        if _save_passengers_or_warn(self.plist, parent, self._pax_stamp):
            # Task 96a 回归修复（Task 90 遗漏）：成功保存后刷新指纹，否则同一
            # 会话第二次保存必误报"被外部修改"。
            self._pax_stamp = passengers_mod.passengers_stamp()
            return True, "ok"
        if self._pax_stamp is not passengers_mod._STAMP_UNSET \
                and passengers_mod.passengers_stamp() != self._pax_stamp:
            # 外部写入导致放弃：重载最新数据+新指纹。
            # （expect_stamp 为哨兵值时本就没有乐观检查，不存在"被外部修改"。）
            self._refresh_plist()
            return False, "stale"
        return False, "refused"

    def _delete(self):
        name = self.pick.get().strip()
        if not name:
            return
        if not messagebox.askyesno("删除乘车人", "确定删除「%s」？" % name, parent=self):
            return
        self.plist = [p for p in self.plist if (p.get("name") or "") != name]
        try:
            ok, _ = self._save_passengers_or_refresh(self)
            if not ok:
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
            "id_type_code": self._type_code(),
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
            ok, _ = self._save_passengers_or_refresh(self)
            if not ok:
                return
        except Exception as e:
            messagebox.showerror("保存失败", "加密保存失败：%s" % e, parent=self)
            return
        self.pick.configure(values=[p.get("name") or "" for p in self.plist])
        self.pick.set(name)
        self._notify_saved()
        messagebox.showinfo("已保存", "乘车人「%s」已加密保存" % name, parent=self)

    def _type_code(self):
        lab = self.type_var.get()
        for code, name in passengers_mod.ID_TYPE_NAMES.items():
            if name == lab:
                return code
        return "1"

    def _notify_saved(self):
        if self.on_saved:
            try:
                self.on_saved()
            except Exception:
                pass


# ----------------------------- 小窗模式 -----------------------------

# 小窗只展示"抢票相关"的事件：带这些标签的日志才进去。
# 引擎启动横幅、每轮 [运行] 播报、[查询重试]、[配置] 同步之类全部过滤。
MINI_LOG_TAGS = ("[有票]", "[抢到]", "[错误]", "[冷却]", "[防重]",
                 "[跳过]", "[会话]", "[状态]")

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
        self._last_sig = None

        self.title("12306 抢票小窗")
        self.geometry("460x420")
        self.minsize(340, 260)
        self.protocol("WM_DELETE_WINDOW", self.close_mini)

        head = ttk.Frame(self, padding=(8, 6))
        head.pack(fill="x")
        ttk.Label(head, text="抢票小窗", font=("", 10, "bold")).pack(side="left")
        self.pin_btn = ttk.Button(head, text="置顶", width=6, command=self.toggle_pin)
        self.pin_btn.pack(side="right")
        self.acct_label = ttk.Label(head, text="检查中...")
        self.acct_label.pack(side="right", padx=(0, 8))

        self.task_box = ttk.Frame(self)
        self.task_box.pack(fill="x", pady=(2, 4))

        log_wrap = ttk.Frame(self)
        log_wrap.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log_text = tk.Text(log_wrap, state="disabled", wrap="word",
                                font=("Consolas", 9))
        sb = ttk.Scrollbar(log_wrap, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.log_text.tag_configure("hit", foreground="#d97706")
        self.log_text.tag_configure("bought", foreground="#0969da")
        self.log_text.tag_configure("err", foreground="#cf222e")

    # ---- 登录状态 ----

    def set_account(self, text, logged_in):
        try:
            self.acct_label.config(text=text or "未登录")
        except Exception:
            pass

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
        if not rows:
            ttk.Label(self.task_box, text="暂无正在进行的任务").pack(anchor="w", padx=14, pady=8)
            return
        for name, st, msg in rows:
            row = ttk.Frame(self.task_box)
            row.pack(fill="x", padx=10, pady=2)
            ttk.Label(row, text=engine_mod.STATUS_LABELS.get(st, st),
                      foreground=STATUS_COLOR.get(st, "")).pack(side="left")
            ttk.Label(row, text=name, wraplength=220, justify="left").pack(side="left", padx=8)
            if msg:
                ttk.Label(row, text=msg, foreground="#57606a",
                          wraplength=260, justify="left").pack(side="left")

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
            tag = None
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
        self.pin_btn.config(text="已置顶" if self.pinned else "置顶")

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


# ----------------------------- 主窗口 -----------------------------

class MonitorApp:
    def __init__(self, root):
        self.root = root
        root.title("12306 车票监控与自动购票系统（桌面版）")
        root.geometry("1240x820")
        root.minsize(980, 680)

        self.stop_event = None
        self.engine_thread = None
        self.live_engine = None
        self._acct_time = 0.0
        self.mini = None
        self._session_dead_warned = False

        # ---------------- 顶部工具条 ----------------
        bar = ttk.Frame(root, padding=(8, 6))
        bar.pack(fill="x")
        self.start_btn = ttk.Button(bar, text="▶ 启动监控", command=self.start_engine)
        self.start_btn.pack(side="left", padx=(0, 4))
        self.stop_btn = ttk.Button(bar, text="■ 停止监控", command=self.stop_engine,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        self.engine_label = ttk.Label(bar, text="● 引擎：未运行")
        self.engine_label.pack(side="left", padx=12)
        self.acct_label = ttk.Label(bar, text="账号：未检查")
        self.acct_label.pack(side="left", padx=12)
        ttk.Button(bar, text="清空日志", command=self.clear_log).pack(side="right", padx=3)
        ttk.Button(bar, text="小窗模式", command=self.open_mini).pack(side="right", padx=3)
        ttk.Button(bar, text="重新登录", command=self.open_session).pack(side="right", padx=3)

        # ---------------- 页签 ----------------
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(2, 4))
        self.pages = {}
        self.pages["tasks"] = TaskPage(self.nb, self)
        self.nb.add(self.pages["tasks"], text="监控任务")
        self.pages["grab"] = GrabTab(self.nb, self)
        self.nb.add(self.pages["grab"], text="抢票任务")
        self.pages["query"] = QuickCheckTab(self.nb, self)
        self.nb.add(self.pages["query"], text="查票")
        self.pages["pax"] = PassengerPanel(self.nb)
        self.nb.add(self.pages["pax"], text="乘车人")
        self.pages["history"] = HistoryTab(self.nb, self)
        self.nb.add(self.pages["history"], text="历史")
        self.pages["settings"] = SettingsTab(self.nb, self)
        self.nb.add(self.pages["settings"], text="设置")
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.task_page = self.pages["tasks"]

        # ---------------- 运行日志 ----------------
        logf = ttk.LabelFrame(root, text=" 运行日志 ", padding=4)
        logf.pack(fill="x", padx=8, pady=(0, 8))
        wrap = ttk.Frame(logf)
        wrap.pack(fill="both", expand=True)
        self.log_text = tk.Text(wrap, height=10, state="disabled", wrap="word",
                                font=("Consolas", 9))
        log_sb = ttk.Scrollbar(wrap, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_sb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_sb.pack(side="right", fill="y")
        for tag, color in LOG_COLOR.items():
            self.log_text.tag_configure(tag, foreground=color)
        self.log_text.tag_configure("__level_error", foreground="#cf222e")

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        start_async_poller(self.root)
        self.show_page("tasks")
        self.refresh_account()
        self.poll_log()
        self.root.after(2000, self.tick)

    # ----------------------------- 工具条 / 状态 -----------------------------

    def set_engine(self, text, color=None):
        try:
            self.engine_label.configure(text=text)
        except Exception:
            pass

    def _on_tab_changed(self, _e=None):
        try:
            cur = self.nb.select()
            page = self.nb.nametowidget(cur)
        except Exception:
            return
        try:
            if page is self.pages.get("tasks"):
                self.task_page.refresh()
            elif page is self.pages.get("grab"):
                self.pages["grab"]._refresh_list()
            elif page is self.pages.get("history"):
                self.pages["history"].refresh()
            elif page is self.pages.get("pax"):
                self.pages["pax"].refresh()
        except Exception:
            LOG.exception("页签刷新失败（不影响下一轮）")

    def show_page(self, key):
        if key not in self.pages:
            return
        try:
            self.nb.select(self.pages[key])
        except Exception:
            pass

    def _set_account(self, text, logged_in):
        """登录态的唯一出口：顶部账号标签与小窗一起更新，两边不会再显示打架。"""
        try:
            self.acct_label.configure(text="账号：%s" % (text or "未登录"))
        except Exception:
            pass
        if self.mini is not None and self.mini.winfo_exists():
            self.mini.set_account(text, logged_in)

    def refresh_account(self):
        """异步校验登录会话，更新顶部账号状态（已登录时显示手机尾号后四位）。

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
        self.set_engine("● 引擎：运行中")
        self.refresh_tasks()

    def stop_engine(self):
        if self.stop_event is not None:
            self.stop_event.set()
        self.set_engine("● 引擎：正在停止…")
        self.stop_btn.config(state="disabled")

    def tick(self):
        if self.engine_thread and not self.engine_thread.is_alive():
            self.engine_thread = None
            self.stop_event = None
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")
            self.set_engine("● 引擎：已停止")
        elif self.engine_thread and self.engine_thread.is_alive():
            try:
                tasks = load_config().get("tasks") or []
                state = load_state().get("tasks", {})
                n = sum(1 for t in tasks
                        if state.get(t.get("name"), {}).get("status")
                        in engine_mod.ACTIVE_STATUSES)
                self.set_engine("● 引擎：运行中（%d 个任务）" % n)
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

    def _insert_log(self, record):
        tag = None
        if "[ERROR]" in record or "[FATAL]" in record:
            tag = "__level_error"
        else:
            for t in LOG_COLOR:
                if t in record:
                    tag = t
                    break
        self.log_text.config(state="normal")
        self.log_text.insert("end", record + "\n", tag)
        if float(self.log_text.index("end-1c")) > 3000.0:
            self.log_text.delete("1.0", "1000.0")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def log_line(self, line):
        """界面内直接落一行日志（主线程调用；抢票表单/对话框用）。"""
        try:
            self._insert_log(line)
        except Exception:
            pass
        if self.mini is not None and self.mini.winfo_exists():
            self.mini.feed_log(line)

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
                self._insert_log(record)
            except Exception:
                pass
        # 抢票业务日志（launcher 模块的专用队列）
        while True:
            try:
                record = launcher.LOGQ.get_nowait()
            except queue.Empty:
                break
            try:
                if self.mini is not None and self.mini.winfo_exists():
                    self.mini.feed_log(record)
                self._insert_log(record)
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
                # Task 65f：与 Task 53（_relogin_ok_via_script）口径统一——
                # 等子进程退出码判定成败，再把结果写回 UI 状态。
                # 不能阻塞 Tk 主循环，所以放后台线程；结果经 _RESULT_QUEUE
                # 回到主线程执行 _after_warn_relogin（与 browser 分支同路）。
                self._warn_relogin_http()

    def _warn_relogin_http(self):
        """http 模式弹窗重登：等 capture_session.py 退出码判成败，再写回 UI。"""
        def _run():
            ok = _relogin_ok_via_script(os.path.join(HERE, "capture_session.py"))
            _RESULT_QUEUE.put((self.root,
                               lambda _r, _e: self._after_warn_relogin(ok), None, None))

        threading.Thread(target=_run, daemon=True).start()

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

    # ----------------------------- 对话框 / 页签入口 -----------------------------

    def open_wizard(self, prefill=None):
        NewTaskDialog(self, on_created=self.refresh_tasks, prefill=prefill)

    def open_quick_monitor(self, prefill=None):
        NewTaskDialog(self, on_created=self.refresh_tasks, prefill=prefill)

    def open_start_dialog(self):
        StartMonitorDialog(self)

    def open_history(self):
        self.show_page("history")

    def open_notify(self):
        self.show_page("settings")

    def open_session(self):
        self.show_page("settings")
        try:
            self.pages["settings"].check()
        except Exception:
            pass

    def open_quick_check(self):
        self.show_page("query")

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
        # 只显示小窗：主窗口隐藏（widget 不销毁，页签状态原样保留）
        try:
            self.root.withdraw()
        except Exception:
            pass

    def restore_main(self):
        """小窗关闭后恢复主窗口，回到打开小窗前的界面状态。"""
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
        # 抢票任务收尾：停线程、关浏览器、保存配置
        try:
            self.pages["grab"].close_all()
        except Exception as e:
            LOG.warning("退出时收尾抢票任务失败：%s", e)
        self.root.destroy()


# ----------------------------- 入口 -----------------------------

def _ensure_stdio():
    """pythonw 下没有控制台，sys.stdout / sys.stderr 是 None。

    与 launcher._ensure_stdio 同名不同语义（那边把 stdio 重定向到
    logs/launcher_stdio.log 供 CLI 用；这里接到 logging）。勿合并。

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


def _config_json_corrupt():
    """config.json 存在但内容不可用：JSON 解析失败、非 UTF-8 编码，或解析成功
    但顶层不是 dict（Task 75a；Task 94a 补后两种口径——与 load_config 的降级
    口径保持一致，否则 main() 的友好提示与实际行为脱节）。"""
    try:
        with open(CONFIG_PATH, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return True
    except OSError:
        return False
    return not isinstance(data, dict)


def main():
    setup_logging()
    _ensure_stdio()
    if not os.path.exists(CONFIG_PATH):
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("错误", "未找到 config.json（%s）" % CONFIG_PATH)
        root.destroy()
        return
    try:
        # 首跑导入：把监控任务里的乘车人名字迁入加密库（老启动器入口的行为）
        launcher.ensure_passengers()
    except Exception:
        pass
    root = tk.Tk()
    if _config_json_corrupt():
        # Task 75a/94a：文件存在但内容损坏——友好提示后按空配置启动
        # （load_config 会吞掉错误返回 {}，旧文件不被覆写）。
        messagebox.showerror(
            "错误",
            "config.json 已损坏（JSON 解析失败、非 UTF-8 编码或顶层不是对象），"
            "将以空配置启动。\n"
            "请检查文件内容并修复，或删除后重建。",
            parent=root)
    MonitorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

