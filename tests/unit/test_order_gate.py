# -*- coding: utf-8 -*-
"""app/order_service.OrderGate 三段闸编排测试（FakeProvider，零网络）。"""

import unittest

from app.order_service import OrderGate
from core.verdict import engine as VE


class FakeProvider:
    """可控快照喂给 OrderGate。每次 fetch 弹出一个快照（队列）。"""

    def __init__(self, *snapshots):
        self._q = list(snapshots) or [None]
        self.calls = 0
        self.force_calls = 0

    def fetch(self, date, force=False):
        self.calls += 1
        if force:
            self.force_calls += 1
        if len(self._q) > 1:
            return self._q.pop(0)
        return self._q[0]


def _e(train="K123", date="2026-10-20", status="已支付(未出行)",
       no_complete=False, ts=None, ono="E1"):
    return {"order_no": ono, "train": train, "date": date, "status": status,
            "passengers": ["张三", "李四"], "order_time": "",
            "order_ts": ts, "_no_complete": no_complete}

TRAIN, DATE, PAX = "K123", "2026-10-20", ["张三", "李四"]


class TestOrderGate(unittest.TestCase):
    def test_precheck_paid_blocks(self):
        gate = OrderGate(provider=FakeProvider([_e()]))
        v = gate.precheck(DATE, TRAIN, PAX)
        self.assertEqual(v.decision, VE.VerdictDecision.BLOCK_PAID)

    def test_double_check_none_twice_retry_safe(self):
        # 两次快照都无单 → RETRY_SAFE；且 fetch 被调 2 次（双读）
        gate = OrderGate(provider=FakeProvider([], []))
        v = gate.double_check_retry_safe(DATE, TRAIN, PAX, 10000.0, gap_seconds=0)
        self.assertEqual(v.decision, VE.VerdictDecision.RETRY_SAFE)
        self.assertEqual(gate._provider.calls, 2)

    def test_double_check_second_round_catches_paid(self):
        # 首读无单，二读变已支付（用户恰在窗口内付款）→ 采用保守成功
        gate = OrderGate(provider=FakeProvider([], [_e()]))
        v = gate.double_check_retry_safe(DATE, TRAIN, PAX, 10000.0, gap_seconds=0)
        self.assertEqual(v.decision, VE.VerdictDecision.SUCCESS)

    def test_double_check_second_read_forced(self):
        # R2-2：第二读必须带 force——双读不得同读一份缓存快照
        gate = OrderGate(provider=FakeProvider([], []))
        v = gate.double_check_retry_safe(DATE, TRAIN, PAX, 10000.0,
                                         gap_seconds=0)
        self.assertEqual(v.decision, VE.VerdictDecision.RETRY_SAFE)
        self.assertEqual(gate._provider.force_calls, 1)

    def test_double_check_real_provider_bypasses_cache(self):
        # R2-2：真 SnapshotProvider（60s TTL 缓存）+ 计数数据源替身：
        # 一次 double_check 必须真打数据源两次；第二份出现已支付订单时
        # 结论随第二份走（SUCCESS），不得被首份 NONE 缓存架空成 RETRY_SAFE
        import order as order_mod
        from adapters.railway.snapshot import SnapshotProvider
        snaps = [[], [_e()]]
        calls = {"n": 0}

        def fake_check(session, date):
            calls["n"] += 1
            return snaps.pop(0) if len(snaps) > 1 else snaps[0]

        orig = order_mod.check_existing_orders
        order_mod.check_existing_orders = fake_check
        try:
            session = type("S", (), {"post": lambda self, *a, **k: None})()
            gate = OrderGate(provider=SnapshotProvider(session))
            v = gate.double_check_retry_safe(DATE, TRAIN, PAX, 10000.0,
                                             gap_seconds=0)
        finally:
            order_mod.check_existing_orders = orig
        self.assertEqual(calls["n"], 2, "双读必须真正打到数据源两次")
        self.assertEqual(v.decision, VE.VerdictDecision.SUCCESS)

    def test_postconfirm_unpaid_with_recent_success(self):
        gate = OrderGate(provider=FakeProvider(
            [_e(status="未完成/未支付", no_complete=True, ts=9997.0)]))
        v = gate.postconfirm(DATE, TRAIN, PAX, 10000.0)
        self.assertEqual(v.decision, VE.VerdictDecision.SUCCESS)

    def test_no_provider_returns_error(self):
        gate = OrderGate(provider=None)
        v = gate.precheck(DATE, TRAIN, PAX)
        self.assertEqual(v.decision, VE.VerdictDecision.ERROR)


if __name__ == "__main__":
    unittest.main()