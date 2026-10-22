# -*- coding: utf-8 -*-
"""
日志公共工具。

DayFileHandler：按天滚动的文件日志，文件名保持 logs/<prefix>_YYYYMMDD.log 的
既有习惯，跨天后自动切到新文件（长跑进程不再把日志一直写进启动那天的文件）。

不用标准库 TimedRotatingFileHandler 的原因：它会改变命名方式（活动文件叫
<prefix>.log，滚动文件加后缀），会破坏现有的按日期找日志的习惯。
"""

import datetime
import hashlib
import json
import logging
import os
import re
import tempfile
import time

import appcommon
from filelock import file_lock

# 日志留存天数：超过该天数的 <prefix>_YYYYMMDD.log 在打开新一天文件时自动清理，
# 长期运行不再把磁盘写满。只删匹配命名规则且超期的文件，不碰当天文件。
LOG_RETENTION_DAYS = 30


# 口径单源在 appcommon.beijing_tz（Task 20 的收敛目标已落地）。原先复制一份是
# 为避免日志基础设施反向依赖业务模块 order（order 会拉起 requests）；appcommon
# 只依赖标准库 + filelock，无此问题。
_beijing_tz = appcommon.beijing_tz


_BEIJING_TZ = _beijing_tz()


def _lock_path_for(log_path):
    """日志文件的跨进程锁路径。

    ponytail: 锁文件放系统临时目录而不是 logs/ 旁边，是因为「每天一个日志文件」
    这个目录形态本身是被测试锁定的契约（tests 里断言 logs 目录里正好只有当天
    的日志文件），多出来的 .lock 会破坏它。代价是多用户/多 TEMP 场景下锁不共
    享——本项目 engine / gui / monitor 永远跑在同一用户下，先按最小够用处理。
    """
    tag = hashlib.sha1(log_path.encode("utf-8")).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), "12306log_%s.lock" % tag)


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
        self._path = None
        self._retry_after = 0.0
        self._retention_days = retention_days

    def _open(self):
        os.makedirs(self._dir, exist_ok=True)
        today = _beijing_today()
        path = os.path.join(self._dir, "{0}_{1}.log".format(
            self._prefix, today.strftime("%Y%m%d")))
        self._fh = open(path, "a", encoding=self._encoding)
        self._path = path
        self._day = today
        self._prune_old_logs(today)

    def _prune_old_logs(self, today):
        """删除超过留存期的旧日志文件（best-effort，失败不影响写日志）。"""
        if self._retention_days <= 0:
            # 非正数视为禁用清理：绝不删除当天文件（公共 API 脚枪防护，
            # Task 79d；旧逻辑里 cutoff 会落在今天或未来，误删当天文件）。
            return
        cutoff = today - datetime.timedelta(days=self._retention_days)
        # 兼容两种命名：新格式 <prefix>_YYYYMMDD_<pid>.log 与旧的
        # <prefix>_YYYYMMDD.log，不清理旧文件会永久堆积。
        pattern = re.compile(r"^{0}_(\d{{8}})(?:_\d+)?\.log$".format(
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
            now = time.time()
            if now < self._retry_after:
                return
            today = _beijing_today()
            if self._fh is None or today != self._day:
                if self._fh is not None:
                    try:
                        self._fh.close()
                    except Exception:
                        pass
                    self._fh = None
                try:
                    self._open()
                except Exception:
                    # 打开失败（目录被删/磁盘满/杀软占用）后进入退避期：不加这道
                    # 门时每条日志都会重试一次 open 再失败并塞进 handleError，
                    # stderr 被 Electron 主进程读走 → 抢票全程零日志且代价高昂。
                    self._retry_after = time.time() + 60.0
                    raise
            line = self.format(record) + "\n"
            # 多进程并发写同一个日志文件：Windows 的追加语义不是原子的（实测
            # 两个进程各写 4000 行最终只剩 4700 行），而且先打开的进程会从自己
            # 缓存的偏移往后写，直接覆盖别人刚写的内容。engine / gui / monitor
            # 都按 prefix="monitor" 开同一个文件，所以必须持跨进程锁，并在锁内
            # 重新 seek 到 EOF 再写。日志量是轮询级，这点开销可忽略。
            # 关键路径 flush：进程硬崩（kill -9）不丢已打日志的尾部。
            # 注意：flush 只把数据送到 OS page cache，防不住断电丢日志；
            # 防断电需要 fsync（Task 79e）。
            try:
                with file_lock(_lock_path_for(self._path), timeout=2):
                    self._fh.seek(0, os.SEEK_END)
                    self._fh.write(line)
                    self._fh.flush()
            except TimeoutError:
                # 抢不到锁（另一进程正持着同一把写同一个文件）：降级直写。
                # 宁可偶发串行甚至覆盖，也不能因为拿不到锁把这条日志整个丢掉
                # —— 日志丢了才是真的没法排查。
                self._fh.seek(0, os.SEEK_END)
                self._fh.write(line)
                self._fh.flush()
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


# ----------------------------- 问题台账（界面「问题日志」页数据源） -----------------------------
# 与 DayFileHandler 的分工：那边是按天滚动的全文日志（给人 tail），这边是跨天
# 保留的问题台账（给界面按条展示）。只收 WARNING 及以上，JSONL 一行一条，
# 按文件大小裁剪，绝不参与按天清理——否则用户想看的历史问题会被 30 天规则删掉。

PROBLEM_LOG_MAX_BYTES = 512 * 1024
PROBLEM_LOG_KEEP = 800


def problem_log_path():
    """问题台账路径（data/problem_log.jsonl）：读写单一口径。

    T12306_PROBLEM_LOG 可整体重定向——测试用它把台账落到临时目录，
    避免跑测试时把用户真实的问题记录写满假日志。
    """
    override = os.environ.get("T12306_PROBLEM_LOG")
    if override:
        return override
    return appcommon.data_path("problem_log.jsonl")


def _trim_problem_log(path):
    """超过上限时保留最近 PROBLEM_LOG_KEEP 条（best-effort，失败不影响写入）。"""
    try:
        # 放宽到 1.2 倍再裁：裁剪后仍贴着上限时，每条日志都全量重写一次
        # （带堆栈的记录很大），会形成写风暴（2026-10-19 审查）。
        if os.path.getsize(path) <= PROBLEM_LOG_MAX_BYTES * 1.2:
            return
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()[-PROBLEM_LOG_KEEP:]
        tmp = path + ".trim"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines)
        os.replace(tmp, path)
    except OSError:
        pass


def append_problem(level, source, message, trace="", path=None):
    """追加一条问题记录（跨进程安全）。写失败静默——台账不能影响业务。

    level: ERROR / WARNING；source: "engine:1234"（模块:行号）或 "api.xxx"；
    trace: 异常堆栈原文，可为空；path: 测试注入用，默认 problem_log_path()。
    """
    path = path or problem_log_path()
    rec = {
        "ts": datetime.datetime.now(_BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "level": level,
        "source": source,
        "message": message,
        "trace": trace or "",
    }
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with file_lock(_lock_path_for(path), timeout=2):
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
            _trim_problem_log(path)
    except Exception:
        pass


def read_problem_log(path=None, limit=200):
    """读最近 limit 条问题记录，最新在前。文件缺失/坏行一律跳过，绝不抛。"""
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 200
    try:
        with open(path or problem_log_path(), encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for ln in reversed(lines[-limit:]):
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def clear_problem_log(path=None):
    """清空台账，返回是否成功。"""
    path = path or problem_log_path()
    try:
        with file_lock(_lock_path_for(path), timeout=2):
            with open(path, "w", encoding="utf-8"):
                pass
        return True
    except OSError:
        return False


class ProblemLogHandler(logging.Handler):
    """把 WARNING 及以上记录写入问题台账，供界面「问题日志」页读取。"""

    def __init__(self, path=None, level=logging.WARNING):
        super().__init__(level)
        self._path = path

    def emit(self, record):
        try:
            trace = ""
            if record.exc_info:
                # Handler 没有 formatException（那是 Formatter 的方法）
                trace = logging.Formatter().formatException(record.exc_info)
            append_problem(
                record.levelname,
                "%s:%s" % (record.module, record.lineno),
                record.getMessage(),
                trace,
                self._path,
            )
        except Exception:
            self.handleError(record)
