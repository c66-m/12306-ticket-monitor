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

    def fetch(self, date):
        self.calls += 1
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