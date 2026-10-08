# -*- coding: utf-8 -*-
"""
日志公共工具。

DayFileHandler：按天滚动的文件日志，文件名保持 logs/<prefix>_YYYYMMDD.log 的
既有习惯，跨天后自动切到新文件（长跑进程不再把日志一直写进启动那天的文件）。

不用标准库 TimedRotatingFileHandler 的原因：它会改变命名方式（活动文件叫
<prefix>.log，滚动文件加后缀），会破坏现有的按日期找日志的习惯。
"""

import datetime
import logging
import os
import re

# 日志留存天数：超过该天数的 <prefix>_YYYYMMDD.log 在打开新一天文件时自动清理，
# 长期运行不再把磁盘写满。只删匹配命名规则且超期的文件，不碰当天文件。
LOG_RETENTION_DAYS = 30


class DayFileHandler(logging.Handler):
    def __init__(self, log_dir, prefix, encoding="utf-8",
                 retention_days=LOG_RETENTION_DAYS):
        super().__init__()
        self._dir = log_dir
        self._prefix = prefix
        self._encoding = encoding
        self._day = None
        self._fh = None
        self._retention_days = retention_days

    def _open(self):
        os.makedirs(self._dir, exist_ok=True)
        today = datetime.date.today()
        path = os.path.join(self._dir, "{0}_{1}.log".format(
            self._prefix, today.strftime("%Y%m%d")))
        self._fh = open(path, "a", encoding=self._encoding)
        self._day = today
        self._prune_old_logs(today)

    def _prune_old_logs(self, today):
        """删除超过留存期的旧日志文件（best-effort，失败不影响写日志）。"""
        cutoff = today - datetime.timedelta(days=self._retention_days)
        pattern = re.compile(r"^{0}_(\d{{8}})\.log$".format(
            re.escape(self._prefix)))
        try:
            names = os.listdir(self._dir)
        except OSError:
            return
        for fname in names:
            m = pattern.match(fname)
            if not m:
                continue
            try:
                fday = datetime.datetime.strptime(m.group(1), "%Y%m%d").date()
            except ValueError:
                continue
            if fday < cutoff:
                try:
                    os.remove(os.path.join(self._dir, fname))
                except OSError:
                    pass

    def emit(self, record):
        # Handler.handle() 已在锁内调用 emit，跨线程切换文件/写人是安全的
        try:
            today = datetime.date.today()
            if self._fh is None or today != self._day:
                if self._fh is not None:
                    try:
                        self._fh.close()
                    except Exception:
                        pass
                    self._fh = None
                self._open()
            self._fh.write(self.format(record) + "\n")
            # 关键路径 flush：进程硬崩（kill -9/断电）不丢已打日志的尾部。
            # 日志量小（轮询级），每次 flush 开销可忽略。
            try:
                self._fh.flush()
            except Exception:
                pass
        except Exception:
            self.handleError(record)

    def close(self):
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        super().close()
