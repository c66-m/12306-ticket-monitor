# -*- coding: utf-8 -*-
"""launcher.py 的 UI 组件子模块：CalendarDialog / DateTimeDialog / TrainCardList。

这些类只引用 tk/ttk 标准库和 launcher_utils 中的 FONT/TrainCard，
不引用任何被测试 mock 的符号（log, HERE, messagebox, LOG, LOGQ 等）。
"""
import calendar
import re
from datetime import datetime, timedelta

import tkinter as tk
from tkinter import ttk

from launcher_utils import FONT, TrainCard


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
        self.winfo_toplevel().bind("<MouseWheel>", self._wheel, add="+")

    def _wheel(self, event):
        try:
            w = self.winfo_containing(event.x_root, event.y_root)
            while w is not None and w is not self.canvas:
                w = w.master
            if w is None:
                return
            if self.canvas.winfo_exists():
                self.canvas.yview_scroll(-1 * int(event.delta / 120), "units")
        except tk.TclError:
            pass
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