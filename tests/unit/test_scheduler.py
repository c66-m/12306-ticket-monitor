# -*- coding: utf-8 -*-
"""P2 调度器 Scheduler 与子进程协议单测（tests/unit）。"""

import heapq
import threading
import time
import unittest
from unittest import mock

from app.scheduler import Scheduler
from adapters.browser import protocol


class TestScheduler(unittest.TestCase):
    def test_periodic_runs_and_stops(self):
        calls = []
        sched = Scheduler(poll_seconds=0.01)
        sched.add_periodic("j1", 0.02, lambda: calls.append(time.time()))
        sched.start()
        time.sleep(0.12)
        sched.stop()
        self.assertGreaterEqual(len(calls), 2, "0.02s 间隔 0.12s 内应至少跑 2 次")
        n = len(calls)
        time.sleep(0.05)
        self.assertEqual(len(calls), n, "stop 后周期 job 不再执行")

    def test_error_isolation(self):
        calls = {"n": 0}
        def boom():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
        sched = Scheduler(poll_seconds=0.01)
        sched.add_periodic("bad", 0.02, boom)
        sched.start()
        time.sleep(0.1)
        sched.stop()
        self.assertGreaterEqual(calls["n"], 2, "异常后 loop 与 job 都存活")
        self.assertEqual(sched.stats["bad"]["errors"], 1, "异常被统计")

    def test_adaptive_interval_callable(self):
        calls = []
        def fn():
            calls.append(1)
        sched = Scheduler(poll_seconds=0.005)
        sched.add_periodic("ad", lambda: 0.01, fn)  # callable interval
        sched.start()
        time.sleep(0.12)
        sched.stop()
        self.assertGreaterEqual(len(calls), 3)

    def test_popped_entry_is_the_executed_one(self):
        # R2-5：peek 与 pop 之间被推入更早到期项时，不得把新条目弹掉后
        # 按旧 name 执行——新任务条目不能丢，两个任务都应被调度到。
        sched = Scheduler(poll_seconds=0.005)
        ran = []
        sched.add_periodic("aaa_old", 60.0, lambda: ran.append("aaa_old"))
        real_pop = heapq.heappop
        injected = {"done": False}

        def pop_wrapper(heap):
            if not injected["done"]:
                injected["done"] = True
                # 模拟并发 add_periodic 在 peek/pop 之间推入更早到期项
                sched._jobs["zzz_new"] = {
                    "type": "periodic", "interval": 60.0,
                    "fn": lambda: ran.append("zzz_new")}
                sched.stats["zzz_new"] = {"runs": 0, "errors": 0,
                                          "last": None, "due": 0.0}
                heapq.heappush(heap, (0.0, "zzz_new"))
            return real_pop(heap)

        with mock.patch.object(heapq, "heappop", pop_wrapper):
            sched.start()
            time.sleep(0.25)
            sched.stop()
        self.assertIn("zzz_new", ran, "被弹出的条目对应的任务必须被执行")
        self.assertIn("aaa_old", ran, "原 peek 到的任务也不能丢")

    def test_next_due_anchored_at_completion(self):
        # R2-6：执行耗时 > interval 时，下次到期以完成时刻锚定，
        # stats 的 due 必须落在最后一次完成时刻之后，而非执行前旧 now 之后
        sched = Scheduler(poll_seconds=0.005)
        finished = []

        def slow():
            time.sleep(0.2)
            finished.append(time.time())

        sched.add_periodic("slow", 0.01, slow)
        sched.start()
        time.sleep(0.35)
        sched.stop()
        self.assertTrue(finished, "慢任务至少应完成一次")
        self.assertGreaterEqual(sched.stats["slow"]["due"], finished[-1],
                                "next due 应以完成时刻 + interval 计算")

    def test_daemon_job_spawned(self):
        box = {"n": 0}
        def work():
            while box["n"] < 3:
                box["n"] += 1
                time.sleep(0.01)
        sched = Scheduler()
        sched.add_daemon("d", work)
        sched.start()
        time.sleep(0.08)
        sched.stop()
        self.assertGreaterEqual(box["n"], 3, "daemon 线程被启动并运行")


class TestProtocol(unittest.TestCase):
    def test_encode_decode_roundtrip(self):
        line = protocol.encode_ok(attempt_id="a1",
                                  evidence={"url": "payOrder", "page_kind": "pay_order"})
        self.assertTrue(line.startswith(protocol.PREFIX))
        payload = protocol.decode_line(line)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["v"], 1)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["evidence"]["page_kind"], "pay_order")
        self.assertEqual(protocol.validate(payload), [])

    def test_decode_rejects_noise(self):
        self.assertIsNone(protocol.decode_line("普通日志行 text"))
        self.assertIsNone(protocol.decode_line(""))
        self.assertIsNone(protocol.decode_line(protocol.PREFIX + "{broken"))

    def test_fail_payload_has_result(self):
        line = protocol.encode_fail("dup", "账号已有未支付订单")
        payload = protocol.decode_line(line)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["result"], "dup")
        self.assertEqual(protocol.validate(payload), [])

    def test_validate_catches_bad(self):
        errs = protocol.validate({"v": 9, "kind": "x"})
        self.assertTrue(errs)


if __name__ == "__main__":
    unittest.main()