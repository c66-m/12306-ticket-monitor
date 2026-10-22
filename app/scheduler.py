# -*- coding: utf-8 -*-
"""
统一任务调度器（app/scheduler，仅依赖标准库 + infra/clock）。

P2 目标：把散养线程收编成「登记 + 统一启停 + 统计 + 异常隔离」的调度器。
两类 job：

- periodic：主调度线程按 最小堆（next_due）驱动，间隔可固定或 callable
  （自适应轮询的未来接口）。异常隔离，job 抛错不影响 loop 继续。
- daemon：登记后由 Scheduler 起独立守护线程跑原函数（原 while-True 循环
  语义零改动——扼杀式迁移：先统一管理框架，行为不变量）。

stop()：清 stop_event；periodic 循环随之退出；daemon 线程随进程终结
（原实现本就是进程退出即死的守护线程，语义不降级）。
"""

import heapq
import logging
import threading

from infra import clock

LOG = logging.getLogger("infra.scheduler")


class Scheduler:
    def __init__(self, poll_seconds=0.05):
        self._poll_s = max(0.005, float(poll_seconds))
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._jobs = {}   # name -> {type, interval, fn}
        self._heap = []   # (due_ts, name)
        self._loop = None
        self.stats = {}   # name -> {runs, errors, last, due}

    # ---------------- 注册 ----------------
    def add_periodic(self, name, interval, fn, immediate=True):
        """周期 job：interval 为秒数或 callable→秒（自适应）。immediate=True 到点
        即首跑；False 则首个周期结束才跑（避免启动瞬间立即执行重活）。"""
        with self._lock:
            self._jobs[name] = {"type": "periodic", "interval": interval,
                                "fn": fn}
            now = clock.now_ts()
            due = now if immediate else now + self._interval_of(name)
            self.stats[name] = {"runs": 0, "errors": 0, "last": None,
                                "due": due}
            heapq.heappush(self._heap, (due, name))

    def add_daemon(self, name, fn):
        """daemon job：登记并（start 时）起守护线程运行原函数，语义零改动。"""
        with self._lock:
            self._jobs[name] = {"type": "daemon", "fn": fn}
            self.stats[name] = {"runs": 0, "errors": 0, "last": None,
                                "due": None}

    # ---------------- 启停 ----------------
    def start(self):
        with self._lock:
            daemons = [n for n, j in self._jobs.items()
                       if j["type"] == "daemon"]
        for name in daemons:
            threading.Thread(target=self._jobs[name]["fn"], daemon=True,
                             name="sched:%s" % name).start()
        self._stop.clear()
        self._loop = threading.Thread(target=self._loop_run, daemon=True,
                                      name="sched-loop")
        self._loop.start()

    def stop(self, join_timeout=2.0):
        """清 stop 事件并等待周期 loop 退出（daemon 随进程终结，不强停）。"""
        self._stop.set()
        if self._loop is not None:
            self._loop.join(timeout=join_timeout)

    def alive(self):
        return self._loop is not None and self._loop.is_alive()

    # ---------------- 周期执行 ----------------
    def _interval_of(self, name):
        iv = self._jobs[name]["interval"]
        try:
            return max(0.0, float(iv() if callable(iv) else iv))
        except Exception:  # 自适应函数算不出就按 1s 兜底，防止空转
            LOG.warning("[调度] %s 间隔计算异常，按 1s 兜底", name)
            return 1.0

    def _loop_run(self):
        while not self._stop.is_set():
            now = clock.now_ts()
            due = None
            with self._lock:
                if self._heap:
                    due, name = self._heap[0]
            if due is not None and now >= due:
                with self._lock:
                    heapq.heappop(self._heap)
                self._run_periodic(name)
                interval = self._interval_of(name)
                with self._lock:
                    st = self.stats.get(name)
                    if st is not None:
                        st["due"] = now + interval
                        heapq.heappush(self._heap, (st["due"], name))
            else:
                self._stop.wait(self._poll_s)

    def _run_periodic(self, name):
        j = self._jobs.get(name)
        st = self.stats.get(name)
        if j is None or st is None:
            return
        try:
            j["fn"]()
            st["runs"] += 1
            st["last"] = clock.now_ts()
        except Exception:  # noqa: BLE001 job 异常绝不允许杀死调度 loop
            st["errors"] += 1
            LOG.warning("[调度] job %s 执行异常（已隔离）", name, exc_info=True)

    # 会话/armed 等 job 排队执行计数（供测试与诊断；daemon 不统计）
    def snapshot(self):
        with self._lock:
            return {n: dict(s) for n, s in self.stats.items()}