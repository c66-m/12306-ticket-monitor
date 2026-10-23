# -*- coding: utf-8 -*-
"""
P1 判定内核离线单测（tests/unit，独立于 test_all.py，毫秒级）。

覆盖：TripKey、订单状态、任务状态机、枚举分类器、归因窗口、三段闸裁决。
本文件不与 test_all.py 互斥——P4 测试四层拆分时并入 unit/ 层级。
"""

import unittest

from core.domain.tripkey import (TripKey, pax_fingerprint, normalize_name,
                                 passenger_id_token)
from core.state import order_lifecycle as OLC
from core.state import task_sm
from core.verdict import classifier, snapshot_view as sv
from core.verdict import engine as VE


def entry(train="K123", date="2026-10-20", pax=("张三", "李四"),
          status="已支付(未出行)", order_ts=1000.0, no_complete=False,
          order_no="E1234567890", cache_block=False, frm="", to=""):
    """构造一条 12306 订单条目（对齐 order._normalize_order_item 出口）。"""
    return {"order_no": order_no, "train": train, "date": date,
            "from": frm, "to": to,
            "status": status, "passengers": list(pax),
            "order_time": "", "order_ts": order_ts,
            "_no_complete": no_complete, "_cache_block": cache_block}


class TestTripKey(unittest.TestCase):
    def test_empty_pax_rejected(self):
        with self.assertRaises(ValueError):
            TripKey.from_task("2026-10-20", "K123", [])

    def test_from_task_normalized(self):
        tk = TripKey.from_task("2026-10-20", " k123 ", ["张三", "李四"])
        self.assertEqual(tk.train, "K123")
        self.assertEqual(tk.date, "2026-10-20")
        self.assertEqual(len(tk.pax_key), 12)

    def test_fingerprint_order_insensitive(self):
        a = pax_fingerprint(["张三", "李四"])
        b = pax_fingerprint(["李四", "张三"])
        self.assertEqual(a, b)

    def test_id_tail_token(self):
        t1 = passenger_id_token("张三", "410101199001011234")
        self.assertTrue(t1.endswith("/1234"))
        t2 = passenger_id_token("张三")
        self.assertEqual(t2, "张三")

    def test_match_requires_all_targets(self):
        tk = TripKey.from_task("2026-10-20", "K123", ["张三", "李四"])
        self.assertTrue(
            tk.match_passengers(["张三", "李四", "王五"], ["张三", "李四"]))
        self.assertFalse(
            tk.match_passengers(["张三"], ["张三", "李四"]))  # 部分重叠不命中

    def test_invalid_date_rejected(self):
        with self.assertRaises(ValueError):
            TripKey.from_task("2026/10/20", "K123", ["张三"])


class TestTripKeySegment(unittest.TestCase):
    """R2-1：TripKey 区间分量（契约①：key = 日期+车次+区间+乘车人，不含席别）。"""

    def _tk(self):
        return TripKey.from_task("2026-10-20", "K123", ["张三", "李四"],
                                 from_station="北京南", to_station="上海虹桥")

    def test_segment_normalized_and_in_composite(self):
        tk = TripKey.from_task("2026-10-20", "K123", ["张三"],
                               from_station=" 北京南 ", to_station="上海虹桥 ")
        self.assertEqual(tk.from_station, "北京南")
        self.assertEqual(tk.to_station, "上海虹桥")
        self.assertIn("北京南>上海虹桥", tk.composite())

    def test_composite_legacy_format_without_segment(self):
        tk = TripKey.from_task("2026-10-20", "K123", ["张三", "李四"])
        self.assertEqual(tk.composite(), "2026-10-20|K123|" + tk.pax_key)

    def test_precheck_short_segment_paid_does_not_block(self):
        # 已购短区间（北京南→南京南）不得挡长区间任务（北京南→上海虹桥）
        orders = [entry(frm="北京南", to="南京南", status="已支付(未出行)")]
        v = VE.precheck(self._tk(), orders, ["张三", "李四"])
        self.assertEqual(v.decision, VE.VerdictDecision.PROCEED_ORDER)

    def test_postconfirm_short_segment_paid_not_success(self):
        orders = [entry(frm="北京南", to="南京南", status="已支付(未出行)")]
        v = VE.postconfirm(self._tk(), orders, ["张三", "李四"], 10000.0)
        self.assertNotEqual(v.decision, VE.VerdictDecision.SUCCESS)
        self.assertEqual(v.decision, VE.VerdictDecision.RETRY_SAFE)

    def test_same_segment_paid_still_blocks(self):
        # 防过修 pin：区间相同 → 原行为不变，照常 BLOCK_PAID
        orders = [entry(frm="北京南", to="上海虹桥", status="已支付(未出行)")]
        v = VE.precheck(self._tk(), orders, ["张三", "李四"])
        self.assertEqual(v.decision, VE.VerdictDecision.BLOCK_PAID)

    def test_no_segment_key_keeps_legacy_match(self):
        # 无区间 key（旧构造点）维持原行为：不因条目带区间而不命中
        tk = TripKey.from_task("2026-10-20", "K123", ["张三", "李四"])
        orders = [entry(frm="北京南", to="南京南", status="已支付(未出行)")]
        v = VE.precheck(tk, orders, ["张三", "李四"])
        self.assertEqual(v.decision, VE.VerdictDecision.BLOCK_PAID)


class TestOrderLifecycle(unittest.TestCase):
    def test_blocking_set(self):
        self.assertIn(OLC.OrderState.PAID, OLC.BLOCKING)
        self.assertIn(OLC.OrderState.UNPAID, OLC.BLOCKING)
        self.assertIn(OLC.OrderState.TRAVEL_CONFLICT, OLC.BLOCKING)
        self.assertNotIn(OLC.OrderState.NONE, OLC.BLOCKING)

    def test_from_legacy(self):
        self.assertIs(OLC.from_legacy("paid"), OLC.OrderState.PAID)
        self.assertIs(OLC.from_legacy("bogus"), OLC.OrderState.UNKNOWN)

    def test_label(self):
        self.assertEqual(OLC.label(OLC.OrderState.PAID), "已支付(已购得)")


class TestTaskStateMachine(unittest.TestCase):
    def test_valid_transitions(self):
        self.assertTrue(task_sm.can_transition("monitoring", "cooling"))
        self.assertTrue(task_sm.can_transition("retrying", "monitoring"))
        self.assertTrue(task_sm.can_transition("paused", "monitoring"))

    def test_fulfilled_terminal(self):
        self.assertTrue(task_sm.is_order_blocked(task_sm.TaskStatus.FULFILLED))
        # fulfilled 不允许任何迁移（物理拦截）
        with self.assertRaises(task_sm.IllegalTransition):
            task_sm.transition(task_sm.TaskStatus.FULFILLED,
                               task_sm.TaskStatus.MONITORING)

    def test_success_terminal(self):
        with self.assertRaises(task_sm.IllegalTransition):
            task_sm.transition(task_sm.TaskStatus.SUCCESS,
                               task_sm.TaskStatus.RETRYING)

    def test_failed_manual_recover(self):
        self.assertTrue(task_sm.can_transition("failed", "monitoring"))
        self.assertEqual(task_sm.from_legacy("monitoring"),
                         task_sm.TaskStatus.MONITORING)


class TestClassifier(unittest.TestCase):
    def test_text_mapping(self):
        self.assertIs(classifier.classify_text("已退票"), OLC.OrderState.REFUNDED)
        self.assertIs(classifier.classify_text("已取消"), OLC.OrderState.CANCELLED)
        self.assertIs(classifier.classify_text("已出站"), OLC.OrderState.PAID)
        self.assertIs(classifier.classify_text("已支付(未出行)"),
                      OLC.OrderState.PAID)
        # 关键坑：未支付 不能被 "支付" 子串误判成 PAID
        self.assertIs(classifier.classify_text("未支付"), OLC.OrderState.UNPAID)
        self.assertIs(classifier.classify_text("未完成/未支付"),
                      OLC.OrderState.UNPAID)
        self.assertIs(classifier.classify_text("待支付"), OLC.OrderState.UNPAID)

    def test_unknown_not_guessed(self):
        self.assertIs(classifier.classify_text("全新未知文案xyz"),
                      OLC.OrderState.UNKNOWN)

    def test_cache_block_blocked(self):
        e = entry(cache_block=True, status="今日将不能继续受理您的订票请求")
        self.assertIs(classifier.classify_entry(e), OLC.OrderState.BLOCKED)

    def test_classify_snapshot(self):
        tk = TripKey.from_task("2026-10-20", "K123", ["张三", "李四"])
        # 本行程未支付 → UNPAID
        orders = [entry("K123", "2026-10-20", status="未完成/未支付",
                        no_complete=True)]
        self.assertIs(classifier.classify_snapshot(tk, orders, ["张三", "李四"]),
                      OLC.OrderState.UNPAID)
        # 其它行程未支付 → BLOCKED
        orders = [entry("K999", "2026-10-20", status="未完成/未支付",
                        no_complete=True)]
        self.assertIs(classifier.classify_snapshot(tk, orders, ["张三", "李四"]),
                      OLC.OrderState.BLOCKED)
        # 查无本行程 → NONE
        orders = [entry("K999", "2026-10-21", status="已支付(未出行)")]
        self.assertIs(classifier.classify_snapshot(tk, orders, ["张三", "李四"]),
                      OLC.OrderState.NONE)
        # 本行程已支付 → PAID（已购的核心判定）
        orders = [entry("K123", "2026-10-20", status="已支付(未出行)")]
        self.assertIs(classifier.classify_snapshot(tk, orders, ["张三", "李四"]),
                      OLC.OrderState.PAID)

    def test_snapshot_definite_state_beats_unknown_order(self):
        # R2-7：同行程 [未知文案在前, 已支付在后] → 先扫明确态判 PAID，
        # 不得被靠前的 UNKNOWN 短路成 UNKNOWN
        tk = TripKey.from_task("2026-10-20", "K123", ["张三", "李四"])
        orders = [entry(status="神秘文案xyz"),
                  entry(status="已支付(未出行)")]
        self.assertIs(classifier.classify_snapshot(tk, orders, ["张三", "李四"]),
                      OLC.OrderState.PAID)


class TestSnapshotView(unittest.TestCase):
    def test_dedupe_by_order_no(self):
        orders = [entry("K123", "2026-10-20", order_no="E111"),
                  entry("K123", "2026-10-20", order_no="E111"),
                  entry("K123", "2026-10-20", order_no="")]
        out = sv.dedupe(orders)
        self.assertEqual(len(out), 2)

    def test_find_recent_window(self):
        tk = TripKey.from_task("2026-10-20", "K123", ["张三", "李四"])
        t_submit = 10000.0
        # t-3s（窗口内）→ 命中
        orders = [entry("K123", "2026-10-20", status="未完成/未支付",
                        no_complete=True, order_ts=t_submit - 3)]
        self.assertIsNotNone(
            sv.find_recent_order(tk, orders, ["张三", "李四"], t_submit))
        # t-30s（早于 -5s 下限）→ 不算本次（遗留旧单）
        orders = [entry("K123", "2026-10-20", status="未完成/未支付",
                        no_complete=True, order_ts=t_submit - 30)]
        self.assertIsNone(
            sv.find_recent_order(tk, orders, ["张三", "李四"], t_submit))
        # t+200s（晚于 +180s 上限，"未来"单）→ 不归因
        orders = [entry("K123", "2026-10-20", status="未完成/未支付",
                        no_complete=True, order_ts=t_submit + 200)]
        self.assertIsNone(
            sv.find_recent_order(tk, orders, ["张三", "李四"], t_submit))

    def test_find_earliest_unpaid_missing_ts_no_crash(self):
        # R2-9：首条缺 order_ts、次条有 ts → 归一比较，正常返回不抛 TypeError
        tk = TripKey.from_task("2026-10-20", "K123", ["张三", "李四"])
        orders = [entry(status="未完成/未支付", no_complete=True,
                        order_ts=None),
                  entry(status="未完成/未支付", no_complete=True,
                        order_ts=500.0, order_no="E2")]
        best = sv.find_earliest_unpaid(tk, orders, ["张三", "李四"])
        self.assertIsNotNone(best)


class TestVerdictEngine(unittest.TestCase):
    def _tk(self):
        return TripKey.from_task("2026-10-20", "K123", ["张三", "李四"])

    def test_precheck_paid(self):
        v = VE.precheck(self._tk(),
                        [entry("K123", "2026-10-20", status="已支付(未出行)")],
                        ["张三", "李四"])
        self.assertEqual(v.decision, VE.VerdictDecision.BLOCK_PAID)
        self.assertTrue(v.is_blocking())

    def test_precheck_unpaid(self):
        v = VE.precheck(self._tk(),
                        [entry("K123", "2026-10-20", status="未完成/未支付",
                               no_complete=True)],
                        ["张三", "李四"])
        self.assertEqual(v.decision, VE.VerdictDecision.BLOCK_UNPAID)

    def test_precheck_none_proceed(self):
        v = VE.precheck(self._tk(), [], ["张三", "李四"])
        self.assertEqual(v.decision, VE.VerdictDecision.PROCEED_ORDER)
        self.assertFalse(v.is_blocking())

    def test_precheck_conflict(self):
        v = VE.precheck(self._tk(),
                        [entry("K999", "2026-10-20", status="未完成/未支付",
                               no_complete=True)],
                        ["张三", "李四"])
        self.assertEqual(v.decision, VE.VerdictDecision.BLOCK_CONFLICT)

    def test_postconfirm_page_success(self):
        v = VE.postconfirm(self._tk(), [], ["张三", "李四"], 10000.0,
                           page_success=True)
        self.assertEqual(v.decision, VE.VerdictDecision.SUCCESS)

    def test_postconfirm_unpaid_with_recent(self):
        orders = [entry("K123", "2026-10-20", status="未完成/未支付",
                        no_complete=True, order_ts=9997.0)]
        v = VE.postconfirm(self._tk(), orders, ["张三", "李四"], 10000.0)
        self.assertEqual(v.decision, VE.VerdictDecision.SUCCESS)

    def test_postconfirm_unpaid_legacy_block(self):
        orders = [entry("K123", "2026-10-20", status="未完成/未支付",
                        no_complete=True, order_ts=9000.0)]
        v = VE.postconfirm(self._tk(), orders, ["张三", "李四"], 10000.0)
        self.assertEqual(v.decision, VE.VerdictDecision.BLOCK_UNPAID)

    def test_postconfirm_none_retry_safe(self):
        v = VE.postconfirm(self._tk(), [], ["张三", "李四"], 10000.0)
        self.assertEqual(v.decision, VE.VerdictDecision.RETRY_SAFE)

    def test_postconfirm_paid_success(self):
        orders = [entry("K123", "2026-10-20", status="已支付(未出行)",
                        order_ts=9000.0)]
        v = VE.postconfirm(self._tk(), orders, ["张三", "李四"], 10000.0)
        self.assertEqual(v.decision, VE.VerdictDecision.SUCCESS)

    def test_unknown_never_proceed(self):
        v = VE.postconfirm(self._tk(),
                           [entry("K123", "2026-10-20", status="神秘文案xyz")],
                           ["张三", "李四"], 10000.0)
        self.assertEqual(v.decision, VE.VerdictDecision.HALT_MANUAL)


if __name__ == "__main__":
    unittest.main()