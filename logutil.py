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


def _beijing_tz():
    """北京时间时区。

    与 order._beijing_tz() 同语义（ZoneInfo("Asia/Shanghai")，Windows 无 tzdata
    时退化为固定 +8；北京无夏令时，恒等）。不直接复用 order 的是避免日志
    基础设施反向依赖业务模块（order 会拉起 requests）；架构 Task 20 会收敛
    为全仓库统一入口。
    """
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Asia/Shanghai")
    except Exception:
        return datetime.timezone(datetime.timedelta(hours=8),
                                 name="Asia/Shanghai")


_BEIJING_TZ = _beijing_tz()


def _beijing_today():
    """项目口径的"今天"：北京时间的日期。

    日志按天切分与留存窗口统一用它，不用机器本地时区（Task 79b）；
    机器时区非 Asia/Shanghai 时本地 date.today() 会切错天。
    """
    return datetime.datetime.now(_BEIJING_TZ).date()


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
        today = _beijing_today()
        path = os.path.join(self._dir, "{0}_{1}.log".format(
            self._prefix, today.strftime("%Y%m%d")))
        self._fh = open(path, "a", encoding=self._encoding)
        self._day = today
        self._prune_old_logs(today)

    def _prune_old_logs(self, today):
        """删除超过留存期的旧日志文件（best-effort，失败不影响写日志）。"""
        if self._retention_days <= 0:
            # 非正数视为禁用清理：绝不删除当天文件（公共 API 脚枪防护，
            # Task 79d；旧逻辑里 cutoff 会落在今天或未来，误删当天文件）。
            return
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
            if self._closed:
                # close() 之后不再静默重开当天文件：走 handleError（与
                # FileHandler 口径一致）；logging.shutdown() 之后残留的
                # logger 引用再打日志也不会建出新文件（Task 79c）。
                raise ValueError("emit on closed DayFileHandler")
            today = _beijing_today()
            if self._fh is None or today != self._day:
                if self._fh is not None:
                    try:
                        self._fh.close()
                    except Exception:
                        pass
                    self._fh = None
                self._open()
            self._fh.write(self.format(record) + "\n")
            # 关键路径 flush：进程硬崩（kill -9）不丢已打日志的尾部。
            # 注意：flush 只把数据送到 OS page cache，防不住断电丢日志；
            # 防断电需要 fsync（Task 79e）。
            # 日志量小（轮询级），每次 flush 开销可忽略。
            try:
                self._fh.flush()
            except Exception:
                pass
        except Exception:
            self.handleError(record)

    def close(self):
        # 与 stdlib Handler.close() 同序：先持 handler 锁再关流。
        # 否则与并发 emit 交错时会写到已关闭文件（进程退出时
        # logging.shutdown()/atexit 与后台线程打日志竞态，Task 79a）。
        self.acquire()
        try:
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None
        finally:
            self.release()
        super().close()
