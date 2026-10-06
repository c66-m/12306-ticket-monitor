# -*- coding: utf-8 -*-
"""跨进程 / 跨线程文件锁：给「读-改-写」型 JSON 文件用。

背景：抢票启动器（launcher.py）与监控系统（gui.py / engine.py）是各自独立的
进程，却都要改 config.json 与 state.json。原子替换只保证「写一半不会留截断
文件」，挡不住并发读改写——两个写方各自读到同一份旧内容再各自写回，后写的
那份把先写的那条任务整条吃掉（实测：3 个线程并发追加任务，config.json 里
只剩 1 条）。

实现用文件字节锁（msvcrt.locking）而不是「锁文件存在即占用」：进程被强杀或
崩溃时操作系统会自动释放字节锁，不会留下删不掉又没人认领的死锁。非 Windows
（没有 msvcrt）退化成进程内线程锁。
"""

import os
import threading
import time
from contextlib import contextmanager

try:
    import msvcrt
except ImportError:  # 非 Windows：跨进程这一层让位，只保留进程内线程锁
    msvcrt = None

_thread_locks = {}
_thread_locks_guard = threading.Lock()


def _thread_lock(path):
    """同一进程内按锁文件路径共享一把 RLock（Windows 字节锁是句柄级的，
    这里主要是给非 Windows 平台和嵌套调用兜底）。"""
    with _thread_locks_guard:
        lk = _thread_locks.get(path)
        if lk is None:
            lk = _thread_locks[path] = threading.RLock()
        return lk


class FileLock(object):
    def __init__(self, path, timeout=10.0, poll=0.05):
        self.path = path
        self.timeout = timeout
        self.poll = poll
        self._tlock = _thread_lock(path)
        self._fd = None

    def acquire(self):
        deadline = time.time() + self.timeout
        self._tlock.acquire()
        if msvcrt is None:
            return True
        try:
            self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError:
            self._tlock.release()
            raise
        while True:
            try:
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                if time.time() >= deadline:
                    os.close(self._fd)
                    self._fd = None
                    self._tlock.release()
                    raise TimeoutError("等待文件锁超时：%s" % self.path)
                time.sleep(self.poll)
            else:
                return True

    def release(self):
        try:
            fd, self._fd = self._fd, None
            if fd is not None:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
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
