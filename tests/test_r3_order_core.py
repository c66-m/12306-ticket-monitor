# -*- coding: utf-8 -*-
"""R3 下单核修复回归测试（FULL_REVIEW_20261023.md 下单核 P3 五条 + R1 note N1）。

1. HTTP 链 confirm 非 JSON 重发前先查未完成订单：首次 confirm 可能已落单、
   只是响应被网关换成拦截页；同 token 重发只会拿到拒绝、把已成的单误判
   失败。重发前按行程+提交时刻归因查一次，查到本次单即按已提交收尾。
2. find_duplicate 异常不再 print 后静默继续：防重守卫自身抛错 = 防重未
   执行，与官方订单查询失败同口径中止本轮 + LOG.warning 留痕。
3. 等 profile 独占锁期间响应停止信号：exclusive 分片等待（约 1s 一片），
   拿到 should_stop 的调用被要求停时提前抛错，不再整段睡满 180s。
4. warm 模式 _reopen_and_order 失败关页：预热 ctx 长存，失败页（可能停
   在确认页）不关会一直挂在现场里，后续复用时多一张无人看管的页。
5. 新链 JUDGE 成功记账回填 picked：_judge_official 成功 extra 带上实际
   勾选乘车人，任务未点名时不再被任务名单兜底成空串（与 _finish_success
   的 entry["picked"] 口径对齐；任务名单只作次选兜底）。
6. N1：JUDGE 单请求 timeout 与总 deadline 联动——timeout 取
   min(默认 15s, deadline 剩余)，剩余 <1s 不再发请求按超限收尾；R1 的
   45s deadline 此前只在发请求前检查，单个请求仍可吃满 15s。
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


def _no_sleep_time():
    """order 模块的 time 替身：sleep 立即返回，time() 用真实时钟。"""
    return types.SimpleNamespace(time=time.time, sleep=lambda s: None)


# ---------------------------------------------------------------------------
# 第 1 条：confirm 非 JSON 重发前先查未完成订单
# ---------------------------------------------------------------------------

_NON_JSON = (False, "confirmSingleForQueue 返回非 JSON（可能接口变更）: "
                    "<html>waf intercept</html>")


class TestConfirmRetryUnpaidRecheck(unittest.TestCase):

    def _call_retry(self, fetch_ret, **kw):
        confirm = mock.Mock(return_value=_NON_JSON)
        fetch = mock.Mock(return_value=fetch_ret)
        with mock.patch.object(order_mod, "confirm_order", confirm), \
             mock.patch.object(order_mod, "fetch_unpaid_order_no", fetch), \
             mock.patch.object(order_mod, "time", _no_sleep_time()):
            out = order_mod._confirm_json_with_retry(
                object(), "tok", "left", "key", "P3", "pts", "ops", "ADULT",
                **kw)
        return out, confirm, fetch

    def test_unpaid_found_before_resend_concludes_submitted(self):
        # 首次响应非 JSON，但未完成订单里已归因到本次单 → 不发第二次
        # confirm，按已提交（成功向）收尾，消息带订单号。
        (ok, msg), confirm, fetch = self._call_retry(
            "E999", date="2026-10-17", train_code="G101",
            passenger_names=["张三"], not_before_ts=123.0)
        self.assertTrue(ok, msg)
        self.assertIn("E999", msg)
        self.assertEqual(confirm.call_count, 1)
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.kwargs["date"], "2026-10-17")
        self.assertEqual(fetch.call_args.kwargs["train_code"], "G101")
        self.assertEqual(fetch.call_args.kwargs["passenger_names"], ["张三"])
        self.assertEqual(fetch.call_args.kwargs["not_before_ts"], 123.0)

    def test_no_unpaid_order_keeps_original_resend(self):
        # 查不到本次单 → 保持原行为：最多重发 3 次、最终仍报非 JSON 失败。
        (ok, msg), confirm, fetch = self._call_retry(
            None, date="2026-10-17", train_code="G101",
            passenger_names=["张三"], not_before_ts=123.0)
        self.assertFalse(ok)
        self.assertIn("非 JSON", msg)
        self.assertEqual(confirm.call_count, 3)

    def test_without_trip_context_no_query_and_old_behavior(self):
        # 无行程上下文（既有调用方）→ 不触发查询，保持原重发行为。
        (ok, msg), confirm, fetch = self._call_retry("E999")
        self.assertFalse(ok)
        self.assertEqual(confirm.call_count, 3)
        fetch.assert_not_called()

    def test_busy_wrapper_forwards_trip_context(self):
        inner = mock.Mock(return_value=(True, "ok"))
        with mock.patch.object(order_mod, "_confirm_json_with_retry", inner):
            ok, msg = order_mod.confirm_with_busy_retry(
                object(), "tok", "left", "key", "P3", "pts", "ops",
                1, 0.01, "ADULT",
                date="2026-10-17", train_code="G101",
                passenger_names=["张三"], not_before_ts=456.0)
        self.assertTrue(ok)
        kw = inner.call_args.kwargs
        self.assertEqual(kw["date"], "2026-10-17")
        self.assertEqual(kw["train_code"], "G101")
        self.assertEqual(kw["passenger_names"], ["张三"])
        self.assertEqual(kw["not_before_ts"], 456.0)


def _order_ticket_scaffold(test, **overrides):
    """order_ticket 全链替身（与 test_all 同款补丁集），返回 patches 列表。"""
    ticket = {"query_date": "2026-10-08", "start_date": "20261008",
              "train_code": "G101", "from_name": "北京", "to_name": "上海",
              "start_time": "08:00", "arrive_time": "12:00",
              "train_location": "P3", "secret_str": "x"}
    task = {"passenger_names": ["张三"]}
    base = {
        "load_session": mock.Mock(return_value=object()),
        "check_login": mock.Mock(return_value=(True, "u")),
        "order_seat_code": mock.Mock(return_value=("O", None)),
        "submit_with_busy_retry": mock.Mock(return_value=(True, "")),
        "get_init_dc": mock.Mock(return_value=("tok", "left", "key", "")),
        "get_passengers": mock.Mock(return_value=[
            {"name": "张三", "is_adult": True, "id_no": "x", "mobile": ""}]),
        "check_existing_orders": mock.Mock(return_value=[]),
        "find_duplicate": mock.Mock(return_value=None),
        "check_order_info": mock.Mock(return_value=(True, "")),
        "confirm_with_busy_retry": mock.Mock(return_value=(True, "ok")),
        "fetch_unpaid_order_no": mock.Mock(return_value="E1"),
    }
    base.update(overrides)
    patches = [mock.patch.object(order_mod, name, m)
               for name, m in base.items()]
    return ticket, task, base, patches


class _Multi:
    def __init__(self, patches):
        self._patches = patches

    def __enter__(self):
        for p in self._patches:
            p.__enter__()
        return self

    def __exit__(self, *a):
        for p in reversed(self._patches):
            p.__exit__(*a)
        return False


class TestOrderTicketConfirmContext(unittest.TestCase):

    def test_order_ticket_passes_trip_context_to_confirm(self):
        ticket, task, mocks, patches = _order_ticket_scaffold(self)
        with _Multi(patches):
            ok, msg, extra = order_mod.order_ticket({}, task, ticket, "二等座")
        self.assertTrue(ok, msg)
        kw = mocks["confirm_with_busy_retry"].call_args.kwargs
        self.assertEqual(kw["date"], "2026-10-08")
        self.assertEqual(kw["train_code"], "G101")
        self.assertEqual(kw["passenger_names"], ["张三"])
        self.assertIsInstance(kw["not_before_ts"], float)


# ---------------------------------------------------------------------------
# 第 2 条：find_duplicate 异常中止本轮 + LOG 留痕
# ---------------------------------------------------------------------------

class TestFindDuplicateExceptionAborts(unittest.TestCase):

    def test_duplicate_check_error_aborts_round_with_log(self):
        ticket, task, mocks, patches = _order_ticket_scaffold(
            self, find_duplicate=mock.Mock(side_effect=RuntimeError("boom")))
        with _Multi(patches):
            with self.assertLogs(order_mod.LOG, level="WARNING") as logs:
                ok, msg, extra = order_mod.order_ticket(
                    {}, task, ticket, "二等座")
        self.assertFalse(ok)
        self.assertIn("中止", msg)
        self.assertIsNone(extra)
        # 防重未执行 → 绝不继续走到校验/提交
        mocks["check_order_info"].assert_not_called()
        mocks["confirm_with_busy_retry"].assert_not_called()
        self.assertTrue(any("查重" in line or "防重" in line
                            for line in logs.output), logs.output)


# ---------------------------------------------------------------------------
# 第 3 条：等锁期间响应停止信号
# ---------------------------------------------------------------------------

class TestExclusiveStopSignal(unittest.TestCase):

    def tearDown(self):
        self.assertEqual(bo._lock_waiter_count(), 0)

    def _waiter(self, result, **kw):
        t0 = time.time()
        try:
            with bo.exclusive(**kw):
                result["entered"] = True
        except RuntimeError as e:
            result["error"] = str(e)
        result["elapsed"] = time.time() - t0

    def test_stop_aborts_lock_wait_quickly(self):
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        result = {}
        with bo.exclusive(timeout=10):
            t = threading.Thread(
                target=self._waiter, daemon=True,
                args=(result,),
                kwargs={"timeout": 30, "should_stop": stop.is_set})
            t.start()
            t.join(timeout=10)
        self.assertFalse(t.is_alive(), "等锁线程应在停止信号后退出")
        self.assertNotIn("entered", result)
        self.assertIn("停止", result.get("error", ""))
        self.assertLess(result["elapsed"], 5,
                        "应远小于 30s 锁超时内退出，实测 %.2fs"
                        % result["elapsed"])

    def test_stop_event_object_accepted(self):
        # should_stop 传 Event 本体（非 callable）也应被识别。
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        result = {}
        with bo.exclusive(timeout=10):
            t = threading.Thread(
                target=self._waiter, daemon=True,
                args=(result,), kwargs={"timeout": 30, "should_stop": stop})
            t.start()
            t.join(timeout=10)
        self.assertFalse(t.is_alive())
        self.assertIn("停止", result.get("error", ""))

    def test_without_stop_waits_until_release(self):
        # pin：无停止信号时保持原阻塞语义——持锁期间不进入、释放后进入。
        result = {}
        with bo.exclusive(timeout=10):
            t = threading.Thread(
                target=self._waiter, daemon=True,
                args=(result,), kwargs={"timeout": 30})
            t.start()
            time.sleep(1.2)
            self.assertNotIn("entered", result)
            self.assertNotIn("error", result)
        t.join(timeout=10)
        self.assertFalse(t.is_alive())
        self.assertTrue(result.get("entered"))

    def test_run_orderflow_stop_during_lock_wait(self):
        # 端到端：下单入口等锁时收到停止 → 远小于 180s 抛错报停，
        # 不进入 playwright 管线。
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        result = {}

        def run():
            t0 = time.time()
            try:
                orderflow.run_orderflow(
                    {"train_code": "G101"}, "二等座", "O", [], "2026-10-17",
                    should_stop=stop.is_set)
                result["returned"] = True
            except RuntimeError as e:
                result["error"] = str(e)
            result["elapsed"] = time.time() - t0

        with bo.exclusive(timeout=10):
            t = threading.Thread(target=run, daemon=True)
            t.start()
            t.join(timeout=10)
        self.assertFalse(t.is_alive())
        self.assertNotIn("returned", result)
        self.assertIn("停止", result.get("error", ""))
        self.assertLess(result["elapsed"], 5)


# ---------------------------------------------------------------------------
# 第 4 条：warm 模式 _reopen_and_order 失败关页
# ---------------------------------------------------------------------------

class _ClosablePage:
    def __init__(self):
        self.close_calls = 0

    def set_default_timeout(self, ms):
        pass

    def close(self):
        self.close_calls += 1


class _PageCtx:
    def __init__(self, page):
        self._page = page

    def new_page(self):
        return self._page


class TestReopenFailureClosesPage(unittest.TestCase):

    _ARGS = ({"train_code": "G101"}, "2026-10-17", "二等座", "O", ["张三"],
             "ADULT", None, None, None)

    def _call(self, page, goto_side, fill_side=None):
        if isinstance(goto_side, BaseException):
            goto_patch = mock.patch.object(
                orderflow, "_goto_and_query", side_effect=goto_side)
        else:
            goto_patch = mock.patch.object(
                orderflow, "_goto_and_query", return_value=goto_side)
        patches = [goto_patch]
        if fill_side is not None:
            patches.append(mock.patch.object(
                orderflow, "_run_fill_submit_confirm", side_effect=fill_side))
        with _Multi(patches):
            return orderflow._reopen_and_order(
                _PageCtx(page), *self._ARGS,
                lambda s: None, lambda s: None, None)

    def test_goto_failure_closes_page(self):
        page = _ClosablePage()
        out = self._call(page, ValueError("boom"))
        self.assertFalse(out[0])
        self.assertEqual(page.close_calls, 1)

    def test_fill_failure_closes_page(self):
        page = _ClosablePage()
        out = self._call(
            page, True,
            fill_side=lambda *a, **k: {"ok": False, "msg": "无票",
                                       "extra": {"reason": "seat_unavailable"}})
        self.assertFalse(out[0])
        self.assertEqual(page.close_calls, 1)

    def test_success_keeps_page_open(self):
        # 成功提交的新页要留给 HOLD 观察，不得关。
        page = _ClosablePage()
        out = self._call(
            page, True,
            fill_side=lambda *a, **k: {"ok": True, "msg": "", "extra": {},
                                       "picked": ["张三"], "dlg_seat": "",
                                       "t_submit": 1.0})
        self.assertIsInstance(out, dict)
        self.assertEqual(page.close_calls, 0)


# ---------------------------------------------------------------------------
# 第 5 条：JUDGE 成功记账回填 picked
# ---------------------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s


class _SeqPage:
    def __init__(self, states):
        self._states = list(states)
        self._i = 0

    @property
    def url(self):
        return self._states[self._i][0]

    def set_default_timeout(self, ms):
        pass

    def evaluate(self, js, arg=None):
        if "document.body" in js:
            body = self._states[self._i][1]
            if self._i < len(self._states) - 1:
                self._i += 1
            return body
        if "slide_passcode" in js:
            return []
        raise AssertionError("假页面未预期 evaluate: %r" % js[:60])


class _CoreCtx:
    @property
    def pages(self):
        return [self]

    def new_page(self):
        raise AssertionError("本组测试不应新开页重下")


class _Warm:
    def __init__(self, page):
        self.p = object()
        self.ctx = _CoreCtx()
        self.page = page

    def usable(self):
        return True

    def refresh(self, info):
        pass


NOCOMPLETE_URL = "https://kyfw.12306.cn/otn/queryOrder/MyOrderNoComplete/init"


class TestJudgeSuccessPassengersPicked(unittest.TestCase):

    # _run_orderflow_core 局部导入 playwright.sync_api：同
    # TestHoldAmbiguousAndInit 的替身法（setUp 装、tearDown 精确恢复）。
    _PW_KEYS = ("playwright", "playwright.sync_api")
    _MISSING = object()

    def setUp(self):
        self._pw_saved = {k: sys.modules.get(k, self._MISSING)
                          for k in self._PW_KEYS}
        pw = types.ModuleType("playwright")
        pw_sync = types.ModuleType("playwright.sync_api")
        pw_sync.sync_playwright = lambda: (_ for _ in ()).throw(
            AssertionError("warm 路径不应启动 playwright"))
        pw.sync_api = pw_sync
        sys.modules["playwright"] = pw
        sys.modules["playwright.sync_api"] = pw_sync

    def tearDown(self):
        for k, v in self._pw_saved.items():
            if v is self._MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def test_judge_official_success_extra_carries_picked(self):
        with mock.patch.object(
                orderflow, "classify_with_time",
                return_value=("unpaid", "E123", "未完成/未支付",
                              {"order_no": "E123"})):
            ok, msg, extra = orderflow._judge_official(
                "2026-10-17", "G101", [], 1_700_000_000.0, None,
                retry_rounds=0, retry_interval=0, picked=["李四"])
        self.assertTrue(ok, msg)
        self.assertEqual(extra["passengers"], "李四")
        self.assertEqual(extra["order_no"], "E123")

    def test_judge_official_without_picked_leaves_fallback(self):
        # 未传 picked 时不写 passengers 键，留给上层既有兜底（任务名单）。
        with mock.patch.object(
                orderflow, "classify_with_time",
                return_value=("unpaid", "E123", "未完成/未支付",
                              {"order_no": "E123"})):
            ok, msg, extra = orderflow._judge_official(
                "2026-10-17", "G101", ["张三"], 1_700_000_000.0, None,
                retry_rounds=0, retry_interval=0)
        self.assertTrue(ok, msg)
        self.assertNotIn("passengers", extra)

    def test_core_judge_success_records_picked_not_task_names(self):
        # 任务未点名（passenger_names=[]）、实际勾选账号乘车人「张三」：
        # 2026-10-24 新口径下 HOLD 直接等到锁定页成功 → 记录 passengers
        # 必须是 picked（实际勾选），而非任务名单。
        clock = _Clock()
        page = _SeqPage([(
            "https://kyfw.12306.cn/otn/confirmPassenger/payOrder/init",
            "席位已锁定，请在提示时间内尽快完成支付，"
            "支付剩余时间：00时19分34秒")])

        def fake_fill(*a, **k):
            return {"ok": True, "msg": "", "extra": {}, "clicked": True,
                    "picked": ["张三"], "dlg_seat": "二等座",
                    "t_submit": clock.now}

        patches = [
            mock.patch.object(orderflow, "time", clock),
            mock.patch.object(orderflow, "launch",
                              side_effect=AssertionError("冷启动不应发生")),
            mock.patch.object(orderflow, "_run_fill_submit_confirm",
                              side_effect=fake_fill),
            mock.patch.object(orderflow, "save_state",
                              side_effect=lambda ctx: None),
            mock.patch.object(orderflow, "fetch_unpaid_order_no",
                              return_value="E123"),
            mock.patch.object(orderflow, "session_from_browser_state",
                              return_value=object()),
        ]
        with _Multi(patches):
            out = orderflow._run_orderflow_core(
                {"train_code": "G101", "from_name": "北京", "to_name": "上海"},
                "二等座", "O", [], "2026-10-17",
                headless=True, purpose="ADULT", warm=_Warm(page),
                alias_name=None, purpose_map=None, should_stop=None,
                mark=lambda s: None, stage=lambda s: None,
                flow_cfg={}, logger=None)
        self.assertTrue(out[0], out[1])
        self.assertEqual(out[2]["passengers"], "张三")
        self.assertEqual(out[2]["page_kind"], "payOrder")

    def test_order_ticket_via_browser_keeps_judge_passengers(self):
        # 新链 extra 已带 picked 时，order_ticket_via_browser 的任务名单
        # setdefault 不得覆盖；extra 没带时才按任务名单兜底（旧口径 pin）。
        fake_flow = types.ModuleType("orderflow")
        fake_flow.run_orderflow = mock.Mock(
            return_value=(True, "ok", {"order_no": "E1",
                                       "passengers": "李四"}))
        ticket = {"train_code": "G101", "query_date": "2026-10-17"}
        with mock.patch.dict(sys.modules, {"orderflow": fake_flow}):
            ok, msg, extra = bo.order_ticket_via_browser(
                {}, {"passenger_names": ["张三"]}, ticket, "二等座")
        self.assertTrue(ok, msg)
        self.assertEqual(extra["passengers"], "李四")

        fake_flow.run_orderflow = mock.Mock(
            return_value=(True, "ok", {"order_no": "E1"}))
        with mock.patch.dict(sys.modules, {"orderflow": fake_flow}):
            ok, msg, extra = bo.order_ticket_via_browser(
                {}, {"passenger_names": ["张三"]}, ticket, "二等座")
        self.assertTrue(ok, msg)
        self.assertEqual(extra["passengers"], "张三")


# ---------------------------------------------------------------------------
# 第 6 条（N1）：单请求 timeout 与 deadline 联动
# ---------------------------------------------------------------------------

class _RecordingSession:
    def __init__(self):
        self.headers = {}
        self.timeouts = []

    def post(self, url, data=None, timeout=None):
        self.timeouts.append(timeout)
        r = mock.Mock()
        if "NoComplete" in url:
            r.json.return_value = {"data": {"orderDBList": []}}
        else:
            r.json.return_value = {"data": {"OrderDTODataList": []}}
        return r


class TestDeadlineRequestTimeout(unittest.TestCase):

    def test_check_existing_orders_timeout_capped_by_deadline(self):
        sess = _RecordingSession()
        order_mod.check_existing_orders(
            sess, "2026-10-17", deadline_ts=time.time() + 2)
        self.assertEqual(len(sess.timeouts), 3)
        self.assertTrue(all(0 < t <= 2.0 for t in sess.timeouts),
                        sess.timeouts)

    def test_check_existing_orders_default_timeout_unchanged(self):
        sess = _RecordingSession()
        order_mod.check_existing_orders(sess, "2026-10-17")
        self.assertTrue(sess.timeouts)
        self.assertTrue(all(t == 15 for t in sess.timeouts), sess.timeouts)

    def test_query_my_order_timeout_capped_by_deadline(self):
        sess = _RecordingSession()
        order_mod._query_my_order(sess, "G", "2026-08-01", "2026-10-01",
                                  deadline_ts=time.time() + 2)
        self.assertTrue(sess.timeouts)
        self.assertTrue(all(0 < t <= 2.0 for t in sess.timeouts),
                        sess.timeouts)

    def test_under_one_second_remaining_sends_nothing(self):
        # 剩余 <1s：不再发请求，按超限 RuntimeError 收尾。
        sess = _RecordingSession()
        with self.assertRaises(RuntimeError):
            order_mod.check_existing_orders(
                sess, "2026-10-17", deadline_ts=time.time() + 0.5)
        self.assertEqual(sess.timeouts, [])

        sess2 = _RecordingSession()
        with self.assertRaises(RuntimeError):
            order_mod._query_my_order(
                sess2, "G", "2026-08-01", "2026-10-01",
                deadline_ts=time.time() + 0.5)
        self.assertEqual(sess2.timeouts, [])


if __name__ == "__main__":
    unittest.main()
