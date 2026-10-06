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


class DayFileHandler(logging.Handler):
    def __init__(self, log_dir, prefix, encoding="utf-8"):
        super().__init__()
        self._dir = log_dir
        self._prefix = prefix
        self._encoding = encoding
        self._day = None
        self._fh = None

    def _open(self):
        os.makedirs(self._dir, exist_ok=True)
        today = datetime.date.today()
        path = os.path.join(self._dir, "{0}_{1}.log".format(
            self._prefix, today.strftime("%Y%m%d")))
        self._fh = open(path, "a", encoding=self._encoding)
        self._day = today

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
