# -*- coding: utf-8 -*-
"""P3-8 对账守护测试（FakeProvider，零网络）。"""

import unittest

from app.reconcile_service import ReconcileRunner, ReconcileReport
from app import notify_service
from infra import eventbus


class FakeProvider:
    def __init__(self, snapshot):
        self._s = snapshot
        self.calls = []

    def fetch(self, date):
        self.calls.append(date)
        return self._s


def _entry(train="K123", date="2026-10-20", status="已支付(未出行)",
           no_complete=False):
    return {"order_no": "E1", "train": train, "date": date, "status": status,
            "passengers": ["张三", "李四"], "order_time": "", "order_ts": None,
            "_no_complete": no_complete}


def _rec(date="2026-10-20", classify="unpaid"):
    return {"date": date, "train": "K123", "passengers": ["张三", "李四"],
            "classify": classify, "decision": "旧决策"}


class _CollectBus:
    """记录 emit 事件的伪总线（不走全局 eventbus，避免测试互扰）。"""

    def __init__(self):
        self.events = []

    def _on(self, tag, payload):
        self.events.append((tag, payload["key"]))


class TestReconcile(unittest.TestCase):
    def setUp(self):
        notify_service.start(only_audit=True)  # 幂等接线，默认只审计

    def test_pending_unpaid_becomes_paid(self):
        prov = FakeProvider([_entry()])  # 官方快照：本行程已支付
        writes = {}
        bus = _CollectBus()
        eventbus.subscribe("order.paid", bus._on)
        try:
            runner = ReconcileRunner(
                prov, loader=lambda: {"k": _rec()},
                updater=lambda k, r: writes.__setitem__(k, r))
            rep = runner.reconcile_once()
            self.assertEqual(rep.paid, 1)
            self.assertEqual(writes["k"]["classify"], "paid")
            self.assertTrue(rep.updated >= 1)
            self.assertIn(("order.paid", "k"), bus.events)
        finally:
            eventbus.unsubscribe("order.paid", bus._on)

    def test_error_becomes_none(self):
        prov = FakeProvider([])  # 官方查无
        writes = {}
        runner = ReconcileRunner(prov,
                                 loader=lambda: {"k": _rec(classify="error")},
                                 updater=lambda k, r: writes.__setitem__(k, r))
        rep = runner.reconcile_once()
        self.assertEqual(rep.none, 1)
        self.assertEqual(writes["k"]["classify"], "none")
        self.assertEqual(rep.errors, 0)

    def test_empty_passengers_skipped(self):
        rec = _rec()
        rec["passengers"] = []
        prov = FakeProvider([])
        runner = ReconcileRunner(prov, loader=lambda: {"k": rec},
                                 updater=lambda k, r: None)
        rep = runner.reconcile_once()
        self.assertEqual(rep.skipped_no_pax, 1)
        self.assertEqual(rep.updated, 0)

    def test_dry_run_no_update(self):
        prov = FakeProvider([_entry()])
        runner = ReconcileRunner(prov, loader=lambda: {"k": _rec()},
                                 updater=None)
        rep = runner.reconcile_once()
        self.assertEqual(rep.updated, 0)
        self.assertEqual(rep.paid, 1, "判定仍被执行，只是不落台账")

    def test_snapshot_grouped_by_date(self):
        # 同乘车日两条记录：fetch 只调一次（分组去重），且都判 paid
        snap = [_entry(train="K123", date="2026-10-20"),
                _entry(train="K456", date="2026-10-20")]
        prov = FakeProvider(snap)
        rec1 = _rec(date="2026-10-20"); rec1["train"] = "K123"
        rec2 = _rec(date="2026-10-20"); rec2["train"] = "K456"
        runner = ReconcileRunner(prov, loader=lambda: {"k1": rec1, "k2": rec2},
                                 updater=None)
        rep = runner.reconcile_once()
        self.assertEqual(prov.calls, ["2026-10-20"], "同日期只 fetch 一次")
        self.assertEqual(rep.total, 2)
        self.assertEqual(rep.paid, 2)

    def test_blocked_stays_pending_and_later_paid(self):
        # R2-4：他人行程未支付挡路 → 本记录判 blocked；blocked 仍属待决，
        # 不得计入 none；下一轮官方变为本行程已支付 → 仍能触发 order.paid
        other_unpaid = _entry(train="K999", status="未完成/未支付",
                              no_complete=True)
        store = {"k": _rec()}

        def updater(k, r):
            store[k] = r

        runner = ReconcileRunner(FakeProvider([other_unpaid]),
                                 loader=lambda: dict(store), updater=updater)
        rep1 = runner.reconcile_once()
        self.assertEqual(store["k"]["classify"], "blocked")
        self.assertEqual(rep1.none, 0, "blocked 不得计入 none 计数")

        runner2 = ReconcileRunner(FakeProvider([_entry()]),
                                  loader=lambda: dict(store), updater=updater)
        rep2 = runner2.reconcile_once()
        self.assertEqual(rep2.pending, 1, "blocked 记录应仍在待复核集合内")
        self.assertEqual(rep2.paid, 1)
        self.assertEqual(store["k"]["classify"], "paid")

    def test_blocked_reconciled_event_edge_triggered(self):
        # N5：order.reconciled 边沿触发——blocked 自 R2-4 纳入待决集后每轮
        # 都会被反复确认；classify 未变时不得重复广播，否则 notify_service
        # 在启用 email 时每轮发一封「blocked」对账邮件，形成周期性骚扰
        other_unpaid = _entry(train="K999", status="未完成/未支付",
                              no_complete=True)
        store = {"k": _rec()}  # 落盘 classify=unpaid

        def updater(k, r):
            store[k] = r

        bus = _CollectBus()
        eventbus.subscribe("order.reconciled", bus._on)
        try:
            runner = ReconcileRunner(FakeProvider([other_unpaid]),
                                     loader=lambda: dict(store),
                                     updater=updater)
            runner.reconcile_once()  # unpaid -> blocked：真变化，fire 一次
            self.assertEqual(store["k"]["classify"], "blocked")
            runner.reconcile_once()  # blocked -> blocked：未变，不得再 fire
            runner.reconcile_once()  # 再确认一轮，仍不得 fire
        finally:
            eventbus.unsubscribe("order.reconciled", bus._on)
        self.assertEqual(bus.events, [("order.reconciled", "k")])

    def test_blocked_to_none_fires_reconciled_event(self):
        # N5：blocked -> none 是真变化，order.reconciled 仍应 fire——
        # 边沿触发只压「重复确认」，不压真变化
        store = {"k": _rec(classify="blocked")}

        def updater(k, r):
            store[k] = r

        bus = _CollectBus()
        eventbus.subscribe("order.reconciled", bus._on)
        try:
            runner = ReconcileRunner(FakeProvider([]),  # 官方查无 -> none
                                     loader=lambda: dict(store),
                                     updater=updater)
            rep = runner.reconcile_once()
        finally:
            eventbus.unsubscribe("order.reconciled", bus._on)
        self.assertEqual(rep.none, 1)
        self.assertEqual(bus.events, [("order.reconciled", "k")])
        self.assertEqual(store["k"]["classify"], "none")

    def test_blocked_to_paid_fires_paid_event(self):
        # N5：blocked -> paid 的变化轮事件行为保持不变，order.paid 正常广播
        store = {"k": _rec(classify="blocked")}

        def updater(k, r):
            store[k] = r

        bus = _CollectBus()
        eventbus.subscribe("order.paid", bus._on)
        try:
            runner = ReconcileRunner(FakeProvider([_entry()]),
                                     loader=lambda: dict(store),
                                     updater=updater)
            rep = runner.reconcile_once()
        finally:
            eventbus.unsubscribe("order.paid", bus._on)
        self.assertEqual(rep.paid, 1)
        self.assertIn(("order.paid", "k"), bus.events)
        self.assertEqual(store["k"]["classify"], "paid")

    def test_report_summary_sane(self):
        rep = ReconcileReport(total=5, paid=1)
        self.assertIn("paid=1", rep.summary())


if __name__ == "__main__":
    unittest.main()