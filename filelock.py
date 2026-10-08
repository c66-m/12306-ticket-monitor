# -*- coding: utf-8 -*-
"""跨进程 / 跨线程文件锁：给「读-改-写」型 JSON 文件用。

背景：抢票启动器（launcher.py）与监控系统（gui.py / engine.py）是各自独立的
进程，却都要改 config.json 与 state.json。原子替换只保证「写一半不会留截断
文件」，挡不住并发读改写——两个写方各自读到同一份旧内容再各自写回，后写的
那份把先写的那条任务整条吃掉（实测：3 个线程并发追加任务，config.json 里
只剩 1 条）。

实现用文件字节锁（msvcrt.locking / fcntl.flock）而不是「锁文件存在即占用」：
进程被强杀或崩溃时操作系统会自动释放字节锁，不会留下删不掉又没人认领的死锁。
Windows 用 msvcrt，没有 msvcrt 的 POSIX 平台（Linux / macOS）用 fcntl，
两者都没有的极少数平台才退化成进程内线程锁。
"""

import os
import threading
import time
from contextlib import contextmanager

try:
    import msvcrt
except ImportError:  # 非 Windows
    msvcrt = None

try:
    import fcntl  # POSIX 跨进程文件锁；Windows 下没有这个模块
except ImportError:
    fcntl = None

_thread_locks = {}
_thread_locks_guard = threading.Lock()
# (锁绝对路径, 线程 ident) -> 嵌套持有计数：同线程重入 file_lock 时外层
# 已持有字节锁，内层直接计数返回（旧代码内层空转 timeout 才 TimeoutError）。
_held = {}


def _thread_lock(path):
    """同一进程内按锁文件绝对路径共享一把 RLock（Windows 字节锁是句柄级的，
    这里主要是给非 Windows 平台和嵌套调用兜底）。"""
    key = os.path.abspath(path)
    with _thread_locks_guard:
        lk = _thread_locks.get(key)
        if lk is None:
            lk = _thread_locks[key] = threading.RLock()
        return lk


class FileLock(object):
    def __init__(self, path, timeout=10.0, poll=0.05):
        self.path = path
        self._abspath = os.path.abspath(path)
        self.timeout = timeout
        self.poll = poll
        self._tlock = _thread_lock(path)
        self._fd = None
        self._acquired = False  # 本实例是否持有成功（防 double-release）

    def acquire(self):
        key = (self._abspath, threading.get_ident())
        # deadline 用 monotonic：墙钟跳变（NTP 对时）不影响超时判定
        deadline = time.monotonic() + self.timeout
        # 线程锁也 honoring timeout：旧代码这里无限阻塞，timeout 参数
        # 在非 Windows 平台被静默忽略。
        t = self.timeout
        if not self._tlock.acquire(timeout=t if t is not None else -1):
            raise TimeoutError("等待线程锁超时：%s" % self.path)
        with _thread_locks_guard:
            if key in _held:
                # 同线程嵌套重入：外层已持有字节锁，内层直接计数返回，
                # 不再抢字节锁（旧代码内层空转 timeout 才 TimeoutError）。
                _held[key] += 1
                self._acquired = True
                return True
            _held[key] = 1
        try:
            if msvcrt is None and fcntl is None:
                self._acquired = True
                return True  # 无跨进程后端：只剩进程内线程锁
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
            except OSError:
                raise
            while True:
                try:
                    os.lseek(self._fd, 0, os.SEEK_SET)
                    if msvcrt is not None:
                        msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                    else:
                        # LOCK_NB 抢不到时抛 BlockingIOError（OSError 子类）
                        fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    if time.monotonic() >= deadline:
                        os.close(self._fd)
                        self._fd = None
                        raise TimeoutError("等待文件锁超时：%s" % self.path)
                    time.sleep(self.poll)
                else:
                    self._acquired = True
                    return True
        except BaseException:
            with _thread_locks_guard:
                _held.pop(key, None)
            self._tlock.release()
            raise

    def release(self):
        # acquired 守卫：未持有/重复 release 静默返回，不再抛 RuntimeError
        if not self._acquired:
            return
        self._acquired = False
        key = (self._abspath, threading.get_ident())
        try:
            with _thread_locks_guard:
                n = _held.get(key, 0)
                if n > 1:
                    # 嵌套层退出：外层仍持有字节锁，只减计数
                    _held[key] = n - 1
                    return
                _held.pop(key, None)
            fd, self._fd = self._fd, None
            if fd is not None:
                try:
                    if msvcrt is not None:
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(fd)
        finally:
            self._tlock.release()


@contextmanager
def file_lock(path, timeout=10.0):
    """with file_lock(path): ... —— 包住整个「读-改-写」过程，不是只包写。"""
    lk = FileLock(path, timeout)
    lk.acquire()
    try:
        yield lk
    finally:
        lk.release()
