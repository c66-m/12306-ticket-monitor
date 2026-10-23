# -*- coding: utf-8 -*-
"""R1 下单核修复回归测试（FULL_REVIEW_20261023.md 第 7–11 条）。

7.  HOLD 中途 ambiguous 分支回读前补 _save_state_for_judge（与 fail 分支、
    最终 JUDGE 对齐）——旧 cookie 回读会把本可确认的订单降级待核对。
8.  order/init 是通往 payOrder 的正常中间态：HOLD 观察期内先等页面自身
    跳转，仅快照停滞或本页 deadline 到期才转回读 JUDGE——旧代码第一轮
    读页（约 0.5s）即定案，落库慢时本会自跳的成功被提前定成待核对。
9.  JUDGE 回读总 deadline（默认 45s）：_judge_official 每轮重试前检查
    已耗时、超限按 error 收尾；classify_with_time 把 deadline 透传到
    check_existing_orders 三段查询与 _query_my_order 分页，超限不再
    发起新查询——旧代码最坏分钟级且全程占 profile 独占锁。
10. 付款页留窗有等待者时提前结束：exclusive() 等待者登记（模块级），
    _hold_pay_page 每轮检查——旧代码留窗默认 300s（上限 1200s）全程
    占锁，其它任务等锁超时、错过放票窗口。
11. find_duplicate 未支付订单防重不以席别排除（契约①：防重 key 不含
    席别）——旧代码同行程已有未支付硬座单时再下二等座被本地放行，
    白耗一次提交被 12306 未完成订单规则拒绝。已支付订单维持席别口径。
"""

import os
import sys
import threading
import time
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import browser_order as bo          # noqa: E402
import order as order_mod           # noqa: E402
import orderflow                    # noqa: E402


# ---------------------------------------------------------------------------
# orderflow HOLD 主循环离线替身（与 tests/test_all.py 的 TestOrderflowE2EMock
# 同款：假页面 + 假时钟驱动 _run_orderflow_core，走 warm 路径）
# ---------------------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s

    def strftime(self, fmt, t=None):
        return time.strftime(fmt, time.localtime(self.now if t is None else t))

    def localtime(self, t=None):
        return time.localtime(self.now if t is None else t)


class _SeqPage:
    """按 (url, body) 状态序列推进的假页面：每次读正文前进一步，末态粘住。"""

    def __init__(self, states):
        self._states = list(states)
        self._i = 0
        self.reads = 0

    @property
    def url(self):
        return self._states[self._i][0]

    def set_default_timeout(self, ms):
        pass

    def evaluate(self, js, arg=None):
        if "document.body" in js:
            body = self._states[self._i][1]
            self.reads += 1
            if self._i < len(self._states) - 1:
                self._i += 1
            return body
        if "slide_passcode" in js:
            return []
        raise AssertionError("假页面未预期 evaluate: %r" % js[:60])


class _FakeCtx:
    @property
    def pages(self):
        return [self]

    def new_page(self):
        raise AssertionError("本组测试不应新开页重下")


class _Warm:
    def __init__(self, page):
        self.p = object()
        self.ctx = _FakeCtx()
        self.page = page
        self.refreshed = []

    def usable(self):
        return True

    def refresh(self, info):
        self.refreshed.append(info)


def _multi(patches):
    class _M:
        def __enter__(self):
            for p in patches:
                p.__enter__()
            return self

        def __exit__(self, *a):
            for p in reversed(patches):
                p.__exit__(*a)
            return False
    return _M()


PAY_URL = "https://kyfw.12306.cn/otn/confirmPassenger/payOrder/init"
ORDER_INIT_URL = "https://kyfw.12306.cn/otn/order/init"
NOCOMPLETE_URL = "https://kyfw.12306.cn/otn/queryOrder/MyOrderNoComplete/init"
PAY_BODY = "席位已锁定，请在提示时间内尽快完成支付，支付剩余时间：00时19分34秒"
_AMBIG_JUDGE = (False, "订单结果未知：官方订单接口回读无法确认本次下单结果",
                {"reason": "ambiguous", "flow_judge": {"cls": "none"}})


class TestHoldAmbiguousAndInit(unittest.TestCase):
    """第 7、8 条：HOLD ambiguous 分支落盘 + order/init 不立即定案。"""

    # _run_orderflow_core 开头无条件局部导入
    # `from playwright.sync_api import sync_playwright`（warm 路径也躲不开）；
    # 全量顺序下 test_all 里某些既有测试把假的 playwright 模块残留在
    # sys.modules（未恢复），导入拿到假模块即 AttributeError。本组走 warm
    # 路径、sync_playwright 从不被调用（_core 已把 launch patch 成冷启动
    # 即炸），只需导入本身成功：setUp 装干净替身，tearDown 精确恢复
    # （原本不存在的键删掉，不把本组的替身留给后面的测试）。
    _PW_KEYS = ("playwright", "playwright.sync_api")
    _MISSING = object()

    def setUp(self):
        self._pw_saved = {k: sys.modules.get(k, self._MISSING)
                          for k in self._PW_KEYS}
        pw = types.ModuleType("playwright")
        pw_sync = types.ModuleType("playwright.sync_api")

        def _no_playwright():
            raise AssertionError("warm 路径不应启动 playwright")

        pw_sync.sync_playwright = _no_playwright
        pw.sync_api = pw_sync
        sys.modules["playwright"] = pw
        sys.modules["playwright.sync_api"] = pw_sync

    def tearDown(self):
        for k, v in self._pw_saved.items():
            if v is self._MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def _core(self, page, judge_side=None, flow_cfg=None):
        """跑 _run_orderflow_core；返回 (out, events, judge_mock)。

        events 按调用序记录 "save" / "judge"；judge_side 为 _judge_official
        替身（默认断言式：被调用即炸——除非显式传入）。"""
        clock = _Clock()
        events = []
        if judge_side is None:
            def judge_side(*a, **k):
                raise AssertionError("不应调用 _judge_official")

        def fake_judge(*a, **k):
            events.append("judge")
            return judge_side(*a, **k)

        def fake_fill(*a, **k):
            return {"ok": True, "msg": "", "extra": {}, "clicked": True,
                    "picked": ["张三"], "dlg_seat": "二等座",
                    "t_submit": clock.now}

        patches = [
            mock.patch.object(orderflow, "time", clock),
            mock.patch.object(orderflow, "launch",
                              side_effect=AssertionError("冷启动不应发生")),
            mock.patch.object(orderflow, "session_ok",
                              return_value=(True, "user")),
            mock.patch.object(orderflow, "_goto_and_query", return_value=True),
            mock.patch.object(orderflow, "_run_fill_submit_confirm",
                              side_effect=fake_fill),
            mock.patch.object(orderflow, "fetch_unpaid_order_no",
                              return_value="E123"),
            mock.patch.object(orderflow, "session_from_browser_state",
                              return_value=object()),
            mock.patch.object(orderflow, "save_state",
                              side_effect=lambda ctx: events.append("save")),
            mock.patch.object(orderflow, "_judge_official",
                              side_effect=fake_judge),
        ]
        warm = _Warm(page)
        with _multi(patches):
            out = orderflow._run_orderflow_core(
                {"train_code": "G101", "from_name": "北京", "to_name": "上海"},
                "二等座", "O", ["张三"], "2026-10-17",
                headless=True, purpose="ADULT", warm=warm, alias_name=None,
                purpose_map=None, should_stop=None,
                mark=lambda s: None, stage=lambda s: None,
                flow_cfg=flow_cfg or {}, logger=None)
        return out, events

    # -- 第 7 条 ---------------------------------------------------------

    def test_ambiguous_branch_saves_state_before_judge(self):
        # 落在旧单列表（MyOrderNoComplete）：走官方回读前必须先落盘会话，
        # 与 fail 分支、最终 JUDGE 的 _save_state_for_judge 口径一致。
        page = _SeqPage([(NOCOMPLETE_URL, "未完成订单列表")])
        out, events = self._core(page, judge_side=lambda *a, **k: _AMBIG_JUDGE)
        self.assertEqual(events, ["save", "judge"])
        self.assertFalse(out[0])
        self.assertEqual(out[2]["reason"], "ambiguous")

    # -- 第 8 条 ---------------------------------------------------------

    def test_order_init_then_lock_page_succeeds_without_judge(self):
        # 快照序列 [order/init（仍在变化）→ 席位已锁定]：应等页面自身跳转
        # 走 success，而不是第一轮读页就提前转 JUDGE 定案。
        page = _SeqPage([
            (ORDER_INIT_URL, "订单初始化中"),
            (ORDER_INIT_URL, "订单初始化中，请稍候…"),
            (PAY_URL, PAY_BODY),
            (PAY_URL, PAY_BODY),
            (PAY_URL, PAY_BODY),
            (PAY_URL, PAY_BODY),
            (PAY_URL, PAY_BODY),
        ])
        out, events = self._core(page)  # judge 被调用即炸
        self.assertTrue(out[0], out[1])
        self.assertEqual(out[2]["page_kind"], "payOrder")
        self.assertEqual(events, [])

    def test_order_init_stalled_snapshot_goes_to_judge(self):
        # order/init 快照持续不变（页面停滞）：才转回读 JUDGE，且回读前
        # 先落盘；不得在第一轮读页就定案（reads>=3 才允许 judge）。
        page = _SeqPage([(ORDER_INIT_URL, "订单初始化中")])
        reads_at_judge = []

        def judge(*a, **k):
            reads_at_judge.append(page.reads)
            return _AMBIG_JUDGE

        out, events = self._core(page, judge_side=judge)
        self.assertEqual(events, ["save", "judge"])
        self.assertEqual(len(reads_at_judge), 1)
        self.assertGreaterEqual(reads_at_judge[0], 3)
        self.assertFalse(out[0])
        self.assertEqual(out[2]["reason"], "ambiguous")

    def test_order_init_changing_until_deadline_goes_to_final_judge(self):
        # order/init 正文每轮都在变（未停滞）→ 一直观察到本页 deadline，
        # 由最终 JUDGE 兜底（落盘后回读），期间不提前定案、不重下。
        states = [(ORDER_INIT_URL, "订单初始化中 %d" % i) for i in range(30)]
        page = _SeqPage(states)
        out, events = self._core(
            page, judge_side=lambda *a, **k: _AMBIG_JUDGE,
            flow_cfg={"read_poll_seconds": 0.5, "page_wait_seconds": 3,
                      "retry_open_times": 0})
        self.assertEqual(events, ["save", "judge"])
        self.assertFalse(out[0])
        self.assertEqual(out[2]["reason"], "ambiguous")


# ---------------------------------------------------------------------------
# 第 9 条：JUDGE 总 deadline
# ---------------------------------------------------------------------------

class TestJudgeDeadline(unittest.TestCase):

    def test_judge_official_stops_at_total_deadline(self):
        calls = []

        def slow_error(*a, **k):
            calls.append(k.get("deadline_ts"))
            time.sleep(0.25)
            return ("error", "", "官方订单查询失败: boom", None)

        with mock.patch.object(orderflow, "classify_with_time",
                               side_effect=slow_error):
            t0 = time.time()
            ok, msg, extra = orderflow._judge_official(
                "2026-10-17", "G101", ["张三"], 1_700_000_000.0, None,
                retry_rounds=5, retry_interval=0, deadline_seconds=0.4)
            elapsed = time.time() - t0
        self.assertFalse(ok)
        # 超限按 error 收尾（不是 none：没查完不能当「确认无单」）
        self.assertEqual(extra["reason"], "ambiguous")
        self.assertEqual(extra["flow_judge"]["cls"], "error")
        # 无 deadline 时会打满 1 首查 + 5 重试 = 6 次；0.4s 上限内只查 2 次
        self.assertEqual(len(calls), 2)
        self.assertLess(elapsed, 1.5)
        # deadline 必须透传给 classify_with_time（供三段查询内部截断）
        self.assertTrue(all(ts is not None for ts in calls))

    def test_judge_official_default_deadline_is_45(self):
        self.assertEqual(orderflow._JUDGE_DEADLINE_SECONDS, 45.0)

    def test_classify_with_time_passes_deadline(self):
        with mock.patch.object(order_mod, "check_existing_orders",
                               return_value=[]) as m:
            cls, ono, raw, recent = order_mod.classify_with_time(
                "2026-10-17", "G101", ["张三"],
                session=object(), deadline_ts=123.0)
        self.assertEqual(m.call_args.kwargs.get("deadline_ts"), 123.0)
        self.assertEqual(cls, "none")

    def test_check_existing_orders_deadline_blocks_first_segment(self):
        sess = mock.Mock()
        sess.post.return_value.json.return_value = {"data": {"orderDBList": []}}
        with self.assertRaises(RuntimeError):
            order_mod.check_existing_orders(
                sess, "2026-10-17", deadline_ts=time.time() - 1)
        sess.post.assert_not_called()

    def test_query_my_order_deadline_stops_paging(self):
        sess = mock.Mock()

        def post(*a, **k):
            time.sleep(0.1)
            r = mock.Mock()
            r.json.return_value = {"data": {"OrderDTODataList": [{}] * 8}}
            return r

        sess.post.side_effect = post
        with self.assertRaises(RuntimeError):
            order_mod._query_my_order(
                sess, "G", "2026-08-01", "2026-10-01",
                deadline_ts=time.time() + 1.25)
        # 每页满 8 条本会继续翻页；剩余时限跌破 1s 后不再发新页（R3/N1：
        # 单请求 timeout 与 deadline 联动，剩余 <1s 按超限收尾）→ 只发 3 页
        self.assertEqual(sess.post.call_count, 3)


# ---------------------------------------------------------------------------
# 第 10 条：付款页留窗等待者提前结束
# ---------------------------------------------------------------------------

class _HoldablePage:
    def __init__(self):
        self.waits = 0

    def is_closed(self):
        return False

    def wait_for_timeout(self, ms):
        self.waits += 1
        time.sleep(ms / 1000.0)


class TestHoldPayPageWaiters(unittest.TestCase):

    def tearDown(self):
        # 登记表是模块级全局：本组测试不得把等待者泄漏给其它测试
        self.assertEqual(bo._lock_waiter_count(), 0)

    def test_hold_ends_early_when_lock_waiter_registered(self):
        page = _HoldablePage()
        token = bo._register_lock_waiter("test-waiter")
        try:
            t0 = time.time()
            with mock.patch.object(bo, "_log"):
                bo._hold_pay_page(page, 60)
            elapsed = time.time() - t0
        finally:
            bo._unregister_lock_waiter(token)
        self.assertLess(elapsed, 5, "有等待者时留窗应在约 1–2 秒内结束")

    def test_hold_runs_full_duration_without_waiter(self):
        page = _HoldablePage()
        t0 = time.time()
        with mock.patch.object(bo, "_log"):
            bo._hold_pay_page(page, 1)
        self.assertGreaterEqual(time.time() - t0, 0.9)

    def test_exclusive_registers_waiter_while_blocked(self):
        entered = threading.Event()

        def waiter():
            with bo.exclusive(timeout=15):
                entered.set()

        with bo.exclusive(timeout=15):
            t = threading.Thread(target=waiter, daemon=True)
            t.start()
            limit = time.time() + 5
            while bo._lock_waiter_count() < 1 and time.time() < limit:
                time.sleep(0.02)
            self.assertEqual(bo._lock_waiter_count(), 1,
                             "等锁线程应在阻塞期间登记为等待者")
        t.join(timeout=5)
        self.assertTrue(entered.is_set())
        self.assertEqual(bo._lock_waiter_count(), 0,
                         "拿到锁后登记应注销，不留残留等待者")


# ---------------------------------------------------------------------------
# 第 11 条：find_duplicate 未支付订单不以席别排除
# ---------------------------------------------------------------------------

class TestFindDuplicateUnpaidSeat(unittest.TestCase):

    def _order(self, **over):
        o = {"date": "2026-10-10", "train": "G101", "from": "北京",
             "to": "上海", "passengers": ["张三"], "order_no": "E9",
             "seat": "硬座"}
        o.update(over)
        return o

    def test_unpaid_seat_mismatch_still_duplicate(self):
        # 已有未支付硬座单 + 目标二等座，同日期+车次+区间+乘车人 → 判重复
        orders = [self._order(_no_complete=True, status="未完成/未支付")]
        with self.assertLogs(order_mod.LOG, level="WARNING") as logs:
            dup = order_mod.find_duplicate(
                orders, "2026-10-10", "G101", ["张三"],
                from_station="北京", to_station="上海", seat_name="二等座")
        self.assertIsNotNone(dup)
        self.assertEqual(dup["order_no"], "E9")
        # 席别差异只在日志里提示
        self.assertTrue(any("硬座" in line and "二等座" in line
                            for line in logs.output), logs.output)

    def test_paid_seat_mismatch_still_skipped(self):
        # 已支付订单维持既有口径：席别不同不判重
        orders = [self._order(status="已支付")]
        self.assertIsNone(order_mod.find_duplicate(
            orders, "2026-10-10", "G101", ["张三"],
            from_station="北京", to_station="上海", seat_name="二等座"))

    def test_unpaid_same_seat_still_duplicate(self):
        orders = [self._order(_no_complete=True, status="未完成/未支付")]
        self.assertIsNotNone(order_mod.find_duplicate(
            orders, "2026-10-10", "G101", ["张三"],
            from_station="北京", to_station="上海", seat_name="硬座"))


if __name__ == "__main__":
    unittest.main()
