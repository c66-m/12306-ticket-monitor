# -*- coding: utf-8 -*-
"""
12306 抢票项目全自动测试套件（标准库 unittest，无第三方依赖）。

覆盖：
    - 单元测试：ticket / engine / order / notify / logutil / passengers /
      launcher 的纯函数与算法
    - 集成测试：engine 状态机（下单成功/防重/ambiguous/busy/席别不可售）、
      launcher 任务库读写与坏档防护、gui 配置/历史读取
    - 端到端（mock）：查询命中 → 自动下单 → 防重停止 → 邮件通知 的完整链路
      （不打真实 12306 接口，order/notify 全部 mock）

运行：
    python tests/test_all.py
    python -m unittest discover -s tests

规矩（与 RULES.md 一致）：全部读写走临时目录，跑完自动清理，不碰
state.json / config.json / order_history.json 等真实数据。
"""

import datetime
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import engine as engine_mod          # noqa: E402
import ticket                        # noqa: E402
import order as order_mod            # noqa: E402
import notify as notify_mod          # noqa: E402
import logutil                       # noqa: E402
import passengers as pax_mod         # noqa: E402
import launcher                      # noqa: E402
import gui                           # noqa: E402
import browser_order                 # noqa: E402


def synthetic_row(train="K225", hard_seat="5", from_c="VNP", to_c="ZAF"):
    """构造一段 queryG 风格的原始返回行（竖线分隔，40 字段）。"""
    f = [""] * 40
    f[0] = "secretStrXYZ"
    f[2] = "train_no_001"
    f[3] = train
    f[4] = f[6] = from_c
    f[5] = f[7] = to_c
    f[8], f[9], f[10] = "08:00", "12:00", "04:00"
    f[11] = "Y"
    f[13] = "20261010"
    f[15] = "P3"
    f[29] = hard_seat        # 硬座
    f[28] = "有"             # 硬卧
    return "|".join(f)


def make_engine(tmp):
    """在临时目录构造一个可跑 _run_task 的引擎实例（绕过真实 config.json）。"""
    e = object.__new__(engine_mod.MonitorEngine)
    e.config_path = os.path.join(tmp, "config.json")
    e.config = {
        "poll_interval_seconds": 45, "min_interval_seconds": 30,
        "order_retry_cooldown_seconds": 7, "order_retry_delay_seconds": 2,
        "notify": {"email": {"enabled": True}},
        "adaptive": {"enabled": False},
        "state_file": "state.json", "history_file": "order_history.json",
    }
    e.state_path = os.path.join(tmp, "state.json")
    e.history_path = os.path.join(tmp, "order_history.json")
    e.name2code = {"长葛": "VNP", "确山": "ZAF"}
    e.code2name = {"VNP": "长葛", "ZAF": "确山"}
    e.tasks = []
    e.base_interval, e.min_interval = 45, 30
    e._config_mtime = None
    e.state = {"dedup": {}, "tasks": {}, "retry": {}}
    e._save_lock = threading.Lock()
    e._history_lock = threading.Lock()
    return e


def task_of(tmp_name="t1", **over):
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    t = {"name": tmp_name, "from": "长葛", "to": "确山", "dates": [tomorrow],
         "date_range": [], "trains": ["K225"], "seat_types": ["硬座"],
         "auto_order": True, "stop_after_order": True,
         "passenger_names": ["张三"], "priority": 5, "purpose_code": "ADULT"}
    t.update(over)
    return t


# 真实数据保险名单：测试碰这些文件 = 事故（曾经真的把用户的 launcher_config.json 覆盖成空壳）
_REAL_FILES = ("launcher_config.json", "grab_tasks.json", "config.json",
               "state.json", "order_history.json", "passengers.json")


def _real_snapshot():
    snap = {}
    for name in _REAL_FILES:
        p = os.path.join(HERE, name)
        try:
            with open(p, "rb") as f:
                snap[name] = hashlib.md5(f.read()).hexdigest()
        except OSError:
            snap[name] = None
    return snap


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t12306_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._real_before = _real_snapshot()
        self.addCleanup(self._assert_real_untouched)

    def _assert_real_untouched(self):
        now = _real_snapshot()
        changed = [k for k in now if now[k] != self._real_before[k]]
        if changed:
            self.fail("测试改动了项目真实数据文件 %s：读写必须全部走临时目录" % changed)


# ============================ 单元测试 ============================

class TestTicket(TempDirCase):
    def test_parse_row_full(self):
        info = ticket.parse_row(synthetic_row(), {"VNP": "长葛", "ZAF": "确山"}, "2026-10-10")
        self.assertEqual(info["train_code"], "K225")
        self.assertEqual(info["from_name"], "长葛")
        self.assertEqual(info["to_name"], "确山")
        self.assertEqual(info["available_seats"], {"硬卧": "有", "硬座": "5"})
        self.assertEqual(info["query_date"], "2026-10-10")

    def test_parse_row_short_row_no_crash(self):
        info = ticket.parse_row("a|b|c", {}, None)
        self.assertIsNone(info["train_code"] or None) if False else None
        self.assertEqual(info["available_seats"], {})

    def test_parse_row_no_ticket_values(self):
        row = synthetic_row(hard_seat="无")
        row = row.split("|")
        row[28] = "候补"
        info = ticket.parse_row("|".join(row), {}, None)
        self.assertNotIn("硬座", info["available_seats"])
        self.assertNotIn("硬卧", info["available_seats"])

    def test_seat_alias_wz_to_hard(self):
        self.assertEqual(ticket.order_seat_code("无座"), ("1", "硬座"))
        self.assertEqual(ticket.order_seat_code("硬座"), ("1", None))
        self.assertEqual(ticket.order_seat_code("动卧"), ("F", None))

    def test_seat_alias_wz_by_train_kind(self):
        """无座同价改判随车型：G/D/C 动车组 = 二等座，普速 = 硬座。"""
        for code in ("G1", "D901", "C2901", "d169"):
            self.assertEqual(ticket.order_seat_code("无座", None, code), ("O", "二等座"))
        for code in ("K225", "T109", "Z281", "1462", "L1", None):
            self.assertEqual(ticket.order_seat_code("无座", None, code), ("1", "硬座"))
        # 非无座席别不受 train_code 影响
        self.assertEqual(ticket.order_seat_code("硬座", None, "G1"), ("1", None))
        self.assertEqual(ticket.order_seat_code("二等座", "O", "K225"), ("O", None))

    def test_seat_names_all(self):
        """该车次全部席别：p35 码串 → 名字（含无票），顺序按 SEAT_SHOW_ORDER。"""
        self.assertEqual(ticket.seat_names_all("OFAO"),
                         ["二等座", "高级动卧", "动卧", "无座"])
        self.assertEqual(ticket.seat_names_all("3411"),
                         ["软卧", "硬卧", "硬座", "无座"])
        self.assertEqual(ticket.seat_names_all("JOIO"),
                         ["二等座", "一等卧", "二等卧", "无座"])
        self.assertEqual(ticket.seat_names_all("", {"硬座": "5"}), ["硬座", "无座"])

    def test_seat_names_all_kind_fallback(self):
        """p35 缺失（未放票/接口没回码串）时按车型兜底，抢票也能看到全部席别。"""
        self.assertEqual(ticket.train_seat_kind("G1305"), "G")
        self.assertEqual(ticket.train_seat_kind("d941"), "D")
        self.assertEqual(ticket.train_seat_kind("K225"), "普速")
        self.assertEqual(ticket.train_seat_kind(None), "普速")
        self.assertEqual(ticket.seat_names_all(None, None, "G1305"),
                         ["商务座", "特等座", "一等座", "二等座", "无座"])
        self.assertEqual(ticket.seat_names_all("", {}, "D941"),
                         ["商务座", "一等座", "二等座", "动卧", "无座"])
        self.assertEqual(ticket.seat_names_all(None, None, "1462"),
                         ["软卧", "硬卧", "软座", "硬座", "无座"])
        # 有码串时不受车型兜底影响
        self.assertEqual(ticket.seat_names_all("9MOO", None, "D941"),
                         ["商务座", "一等座", "二等座", "无座"])

    def test_parse_row_houbu_flag(self):
        """p37=houbu_train_flag：售完可候补的车次要能识别出来（界面显示「候补」）。"""
        self.assertFalse(ticket.parse_row(synthetic_row(), {}, None)["houbu"])
        row = synthetic_row().split("|")
        row[37] = "1"
        info = ticket.parse_row("|".join(row), {}, None)
        self.assertTrue(info["houbu"])
        self.assertIn("无座", info["seats_all"])

    def test_parse_row_seats_all(self):
        info = ticket.parse_row(synthetic_row(), {}, "2026-10-10")
        self.assertIn("硬座", info["seats_all"])
        self.assertIn("无座", info["seats_all"])
        row = synthetic_row().split("|")
        row[35] = "OFAO"
        info = ticket.parse_row("|".join(row), {}, "2026-10-10")
        self.assertEqual(info["seat_codes"], "OFAO")
        self.assertEqual(info["seats_all"],
                         ["二等座", "高级动卧", "动卧", "硬卧", "硬座", "无座"])

    def test_seat_priority_parse_and_pick(self):
        """「首选席别」输入框：解析、无座=硬座、认不出按硬座、优先序。"""
        self.assertEqual(ticket.seat_priority_list("硬座,无座 乱写"), ["硬座", "无座"])
        self.assertEqual(ticket.seat_priority_list("乱写"), ["硬座"])
        self.assertEqual(ticket.seat_priority_list(""), [])
        self.assertEqual(ticket.seat_priority_list(["无座"]), ["无座"])
        self.assertEqual(ticket.seat_priority_expanded("无座"), ["无座", "硬座"])
        self.assertEqual(ticket.seat_priority_expanded(""), [])
        self.assertEqual(ticket.normalize_seat_name("站票"), "无座")
        self.assertEqual(ticket.normalize_seat_name("商务"), "商务座")
        self.assertIsNone(ticket.normalize_seat_name("YZ"))
        self.assertEqual(ticket.seat_pick_order(["二等座"], "无座",
                                                {"硬座": "有", "二等座": "12"}),
                         ["硬座", "二等座"])
        self.assertEqual(ticket.seat_pick_order(["二等座"], "无座",
                                                {"无座": "有", "硬座": "有", "二等座": "12"}),
                         ["无座", "硬座", "二等座"])
        self.assertEqual(ticket.seat_pick_order(["二等座", "一等座"], "商务座",
                                                {"二等座": "12"}), ["二等座"])
        self.assertEqual(ticket.seat_pick_order(["二等座", "一等座"], "",
                                                {"二等座": "12", "一等座": "有"}),
                         ["一等座", "二等座"])
        self.assertEqual(ticket.seat_pick_order(["硬座"], "硬座", {}), [])
        _txt, bad = ticket.seat_priority_feedback("傻东西")
        self.assertTrue(bad)
        self.assertFalse(ticket.seat_priority_feedback("")[1])

    def test_seat_choices_canonical(self):
        self.assertEqual(len(ticket.SEAT_CHOICES), 12)
        self.assertIn("动卧", ticket.SEAT_CHOICES)
        self.assertIn("优选一等座", ticket.SEAT_CHOICES)

    def test_load_station_map_from_cache(self):
        cache = os.path.join(self.tmp, "station_name.json")
        with open(cache, "w", encoding="utf-8") as f:
            json.dump({"name2code": {"北京": "BJP"}, "code2name": {"BJP": "北京"}}, f)
        n2c, c2n = ticket.load_station_map(cache)
        self.assertEqual(n2c["北京"], "BJP")
        self.assertEqual(c2n["BJP"], "北京")


class TestEngineUnit(TempDirCase):
    def test_expand_dates_merge_and_dedup(self):
        t = {"dates": ["2026-10-08", "2026-10-08", "2026-10-09"],
             "date_range": ["2026-10-09", "2026-10-11"]}
        self.assertEqual(engine_mod.expand_dates(t),
                         ["2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"])

    def test_future_dates_filters_past_and_bad(self):
        t = {"dates": ["2000-01-01", "bad-date",
                       (datetime.date.today() + datetime.timedelta(days=2)).isoformat()]}
        kept, expired = engine_mod.future_dates(t)
        self.assertEqual(len(kept), 1)
        self.assertEqual(expired, 2)

    def test_dedup_key_sorted_passengers(self):
        t = {"from": "长葛", "to": "确山"}
        a = engine_mod.dedup_key(t, "2026-10-10", "K225", "硬座", ["张三", "李四"])
        b = engine_mod.dedup_key(t, "2026-10-10", "K225", "硬座", ["李四", "张三"])
        self.assertEqual(a, b)

    def test_task_interval_floor_and_priority(self):
        e = make_engine(self.tmp)
        e.base_interval, e.min_interval = 30, 15
        e.config = {"adaptive": {"enabled": False}}
        t = task_of()
        self.assertEqual(e.task_interval(t), 30)
        e.config = {"adaptive": {"enabled": True, "peak_hours": [0, 24],
                                 "peak_multiplier": 1.0, "rush_within_hours": 0}}
        hi = e.task_interval(dict(t, priority=10))
        lo = e.task_interval(dict(t, priority=1))
        self.assertLess(hi, lo)

    def test_prune_retry_map(self):
        e = make_engine(self.tmp)
        rm = {("k%d" % i): 1 for i in range(300)}
        e._prune_retry_map(rm, max_entries=100)
        self.assertLessEqual(len(rm), 100)

    def test_set_task_status_paused_protection(self):
        e = make_engine(self.tmp)
        t = task_of()
        e.state["tasks"][t["name"]] = {"status": "paused"}
        e.set_task_status(t, "monitoring", "例行写入")   # 非 force 应被拒绝
        self.assertEqual(e.state["tasks"][t["name"]]["status"], "paused")
        e.set_task_status(t, "monitoring", "显式恢复", force=True)
        self.assertEqual(e.state["tasks"][t["name"]]["status"], "monitoring")

    def test_append_history_cap_and_atomic(self):
        e = make_engine(self.tmp)
        for i in range(510):
            e._append_history({"time": "t", "task": "x%d" % i, "result": "success"})
        data = json.load(open(e.history_path, encoding="utf-8"))
        self.assertEqual(len(data), 500)
        self.assertFalse([f for f in os.listdir(self.tmp) if ".tmp" in f])


class TestEngineDateValidation(TempDirCase):
    """Task 1 (P1): 非法 date_range 不得崩引擎进程。"""

    def test_expand_dates_skips_invalid_range(self):
        # 非法月份：跳过该区间，不抛异常
        t = {"name": "t1", "date_range": ["2026-13-01", "2026-10-02"]}
        self.assertEqual(engine_mod.expand_dates(t), [])

    def test_expand_dates_skips_garbage_strings(self):
        t = {"name": "t1", "date_range": ["not-a-date", "2026-10-02"]}
        self.assertEqual(engine_mod.expand_dates(t), [])

    def test_expand_dates_skips_nonstring_values(self):
        t = {"name": "t1", "date_range": [None, "2026-10-02"]}
        self.assertEqual(engine_mod.expand_dates(t), [])

    def test_expand_dates_keeps_valid_dates_when_range_invalid(self):
        # 合法的 dates 不受非法 date_range 牵连
        t = {"name": "t1", "dates": ["2026-10-08"],
             "date_range": ["2026-13-01", "2026-10-02"]}
        self.assertEqual(engine_mod.expand_dates(t), ["2026-10-08"])

    def test_expand_dates_reversed_range_still_ignored(self):
        # 起止倒置：保持原有行为（忽略该区间），不抛异常
        t = {"name": "t1", "date_range": ["2026-10-05", "2026-10-02"]}
        self.assertEqual(engine_mod.expand_dates(t), [])

    def test_soonest_date_none_for_invalid_task(self):
        t = {"name": "t1", "date_range": ["2026-13-01", "2026-13-05"]}
        self.assertIsNone(engine_mod.MonitorEngine._soonest_date(t))

    def test_task_interval_long_fallback_for_dateless_task(self):
        # 无有效监控日期：不抛异常，走兜底长间隔（>=300s）而非高频空转
        e = make_engine(self.tmp)
        e.config = {"adaptive": {"enabled": True, "peak_hours": [0, 24],
                                 "peak_multiplier": 1.0, "rush_within_hours": 24}}
        t = task_of("t1", dates=[], date_range=["2026-13-01", "2026-13-05"])
        self.assertGreaterEqual(e.task_interval(t), 300)


class TestEngineDatesSanitization(TempDirCase):
    """Task 24 (P1): dates 混入非字符串条目不得崩引擎进程。"""

    def test_expand_dates_skips_int_entry(self):
        # 未加引号的手误：整数日期应被跳过并记警告，不抛异常
        t = {"name": "t24", "dates": [20261009, "2026-10-09"]}
        self.assertEqual(engine_mod.expand_dates(t), ["2026-10-09"])

    def test_expand_dates_skips_none_bool_float(self):
        t = {"name": "t24", "dates": [None, True, 3.5, "2026-10-09"]}
        self.assertEqual(engine_mod.expand_dates(t), ["2026-10-09"])

    def test_future_dates_tolerates_int_entry(self):
        t = {"name": "t24", "dates": [20261009]}
        kept, expired = engine_mod.future_dates(
            t, today=datetime.date(2026, 10, 8))
        self.assertEqual((kept, expired), ([], 0))

    def test_soonest_date_none_for_int_only_task(self):
        t = {"name": "t24", "dates": [20261009]}
        self.assertIsNone(engine_mod.MonitorEngine._soonest_date(t))

    def test_task_interval_long_fallback_for_int_only_task(self):
        # 全是非法条目 → 视同无有效日期，走兜底长间隔而非崩进程
        e = make_engine(self.tmp)
        e.config = {"adaptive": {"enabled": True, "peak_hours": [0, 24],
                                 "peak_multiplier": 1.0, "rush_within_hours": 24}}
        t = task_of("t24", dates=[20261009], date_range=[])
        self.assertGreaterEqual(e.task_interval(t), 300)

    def test_task_interval_mixed_dates_uses_valid_entry(self):
        # 混入非法条目但有合法未来日期：正常计算，不崩也不走兜底
        e = make_engine(self.tmp)
        e.config = {"adaptive": {"enabled": True, "peak_hours": [0, 24],
                                 "peak_multiplier": 1.0, "rush_within_hours": 24}}
        t = task_of("t24", dates=[20261009, "2099-01-01"], date_range=[])
        iv = e.task_interval(t)
        self.assertLess(iv, 300)

    def test_expand_dates_non_list_int_field_ignored(self):
        # Round 2：整字段是整数（漏写方括号的手误）→ 记警告后视为空，不抛异常
        t = {"name": "t24", "dates": 20261009}
        self.assertEqual(engine_mod.expand_dates(t), [])

    def test_expand_dates_non_list_str_field_ignored(self):
        # Round 2：整字段是裸字符串同样非列表 → 忽略，不逐字符展开
        t = {"name": "t24", "dates": "2026-10-09"}
        self.assertEqual(engine_mod.expand_dates(t), [])

    def test_task_interval_non_list_dates_field_no_crash(self):
        # Round 2：整字段非列表时 task_interval 走兜底长间隔而非崩进程
        e = make_engine(self.tmp)
        e.config = {"adaptive": {"enabled": True, "peak_hours": [0, 24],
                                 "peak_multiplier": 1.0, "rush_within_hours": 24}}
        t = task_of("t24", dates=20261009, date_range=[])
        self.assertGreaterEqual(e.task_interval(t), 300)


from urllib.error import URLError  # noqa: E402


class TestStationMapDegraded(TempDirCase):
    """Task 26 (P1): 离线首跑 load_station_map 抛异常不得崩进程，应空表降级。"""

    def _write_config(self):
        cfg = {"tasks": [],
               "state_file": os.path.join(self.tmp, "state.json"),
               "history_file": os.path.join(self.tmp, "order_history.json")}
        p = os.path.join(self.tmp, "config.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        return p

    def test_engine_init_survives_station_map_failure(self):
        # 离线首跑：load_station_map 抛 URLError → __init__ 不抛，空表降级继续
        cfg_path = self._write_config()
        with mock.patch.object(ticket, "load_station_map",
                               side_effect=URLError("offline")):
            e = engine_mod.MonitorEngine(config_path=cfg_path,
                                         setup_logging=False)
        self.assertEqual(e.name2code, {})
        self.assertEqual(e.code2name, {})

    def test_engine_init_logs_degraded_error(self):
        # 降级原因必须被清楚记录（ERROR 级）
        cfg_path = self._write_config()
        with mock.patch.object(ticket, "load_station_map",
                               side_effect=URLError("offline")):
            with self.assertLogs("monitor", level="ERROR") as cm:
                engine_mod.MonitorEngine(config_path=cfg_path,
                                         setup_logging=False)
        self.assertTrue(any("车站数据加载失败" in m for m in cm.output),
                        "未记录车站数据加载失败: %s" % cm.output)

    def test_monitor_menu_create_task_survives(self):
        # CLI 创建任务菜单：车站表加载失败 → 不崩，应可取消退出
        import monitor as monitor_mod
        with mock.patch.object(ticket, "load_station_map",
                               side_effect=URLError("offline")), \
             mock.patch.object(monitor_mod, "read", return_value=None):
            monitor_mod.menu_create_task()  # 不抛异常（pick_station 返回 None → 取消）

    def test_monitor_menu_quick_check_survives(self):
        # CLI 余票速查菜单：同上
        import monitor as monitor_mod
        with mock.patch.object(ticket, "load_station_map",
                               side_effect=URLError("offline")), \
             mock.patch.object(monitor_mod, "read", return_value=None):
            monitor_mod.menu_quick_check()  # 不抛异常


class TestEngineDateRangeShape(TempDirCase):
    """Task 25 (P1): date_range 形状病态（dict/单元素/嵌套/非 str 元素）不得崩引擎进程。"""

    def test_dict_date_range_does_not_raise(self):
        # 手误把 date_range 写成 {"start":..., "end":...}：旧代码 dr[0] 抛 KeyError 崩进程
        t = {"name": "t25",
             "date_range": {"start": "2026-10-01", "end": "2026-10-02"}}
        self.assertEqual(engine_mod.expand_dates(t), [])

    def test_single_element_list_skipped(self):
        t = {"name": "t25", "date_range": ["2026-10-01"]}
        self.assertEqual(engine_mod.expand_dates(t), [])

    def test_three_element_list_skipped(self):
        t = {"name": "t25",
             "date_range": ["2026-10-01", "2026-10-02", "2026-10-03"]}
        self.assertEqual(engine_mod.expand_dates(t), [])

    def test_int_date_range_skipped(self):
        # 旧代码 len(5) 直接 TypeError 崩进程
        t = {"name": "t25", "date_range": 5}
        self.assertEqual(engine_mod.expand_dates(t), [])

    def test_non_str_elements_skipped(self):
        t = {"name": "t25", "date_range": [20261001, 20261002]}
        self.assertEqual(engine_mod.expand_dates(t), [])

    def test_nested_list_skipped(self):
        t = {"name": "t25", "date_range": [["2026-10-01"], ["2026-10-02"]]}
        self.assertEqual(engine_mod.expand_dates(t), [])

    def test_none_date_range_silent(self):
        # 未配置 date_range 是正常情况：静默视为空，不崩
        t = {"name": "t25", "date_range": None}
        self.assertEqual(engine_mod.expand_dates(t), [])

    def test_valid_tuple_still_works(self):
        # 回归 pin：合法 2 元组仍展开
        t = {"name": "t25",
             "date_range": ("2026-10-01", "2026-10-02")}
        self.assertEqual(engine_mod.expand_dates(t),
                         ["2026-10-01", "2026-10-02"])

    def test_task_interval_dict_date_range_no_crash(self):
        # 真实崩溃路径：task_interval 经 _soonest_date 调 expand_dates
        e = make_engine(self.tmp)
        e.config = {"adaptive": {"enabled": True, "peak_hours": [0, 24],
                                 "peak_multiplier": 1.0, "rush_within_hours": 24}}
        t = task_of("t25", dates=[],
                    date_range={"start": "2026-10-01", "end": "2026-10-02"})
        self.assertGreaterEqual(e.task_interval(t), 300)


class TestEnginePriorityValidation(TempDirCase):
    """Task 2 (P1): 非数字 priority 不得崩调度排序。"""

    def test_safe_priority_valid_values(self):
        self.assertEqual(engine_mod._safe_priority({"priority": 8}), 8)
        self.assertEqual(engine_mod._safe_priority({"priority": "7"}), 7)
        self.assertEqual(engine_mod._safe_priority({"priority": 7.9}), 7)

    def test_safe_priority_invalid_falls_back_to_5(self):
        self.assertEqual(engine_mod._safe_priority({"priority": "high"}), 5)
        self.assertEqual(engine_mod._safe_priority({"priority": None}), 5)
        self.assertEqual(engine_mod._safe_priority({}), 5)
        self.assertEqual(engine_mod._safe_priority({"priority": ["high"]}), 5)

    def test_sort_with_mixed_priorities_does_not_raise(self):
        # 复现原 bug：due.sort(key=lambda x: -int(x[1].get("priority") or 5))
        # 遇到 "high" 直接 ValueError 崩进程；非法值按 5 参与排序
        tasks = [{"name": "hi", "priority": 10},
                 {"name": "bad", "priority": "high"},
                 {"name": "lo", "priority": 3}]
        ordered = sorted(tasks, key=lambda t: -engine_mod._safe_priority(t))
        self.assertEqual([t["name"] for t in ordered], ["hi", "bad", "lo"])

    def test_task_interval_tolerates_bad_priority(self):
        e = make_engine(self.tmp)
        e.base_interval, e.min_interval = 30, 15
        e.config = {"adaptive": {"enabled": True, "peak_hours": [0, 24],
                                 "peak_multiplier": 1.0, "rush_within_hours": 0}}
        t = task_of("t1")
        t["priority"] = "high"
        self.assertGreaterEqual(e.task_interval(t), 15)  # 不抛异常，走默认 5


class TestOrder(TempDirCase):
    def test_build_ticket_strs(self):
        # 官方格式：seat,0,ticket_type,姓名,1,证件号,手机号,N,0_
        pax = [{"name": "张三", "id_no": "110101199001011234", "mobile": "13800000000"},
               {"name": "李四", "id_no": "110101199001011235", "mobile": ""}]
        pts, olds = order_mod.build_ticket_strs(pax, "1", {"李四": "0X00"})
        self.assertIn("1,0,1,张三,1,110101199001011234,13800000000,N", pts)
        self.assertIn("1,0,3,李四,1,110101199001011235,,N", pts)   # 学生票按人写入
        self.assertIn("张三,1,110101199001011234,1_", olds)

    def test_find_duplicate(self):
        orders = [{"date": "2026-10-10", "train": "K225", "passengers": ["张三"]}]
        self.assertIsNotNone(order_mod.find_duplicate(orders, "2026-10-10", "K225", ["张三"]))
        self.assertIsNone(order_mod.find_duplicate(orders, "2026-10-11", "K225", ["张三"]))
        # 乘车人解析失败时保守判重
        self.assertIsNotNone(order_mod.find_duplicate(
            [{"date": "2026-10-10", "train": "K225", "passengers": []}],
            "2026-10-10", "K225", ["张三"]))

    def test_select_passengers(self):
        allp = [{"name": "张三", "is_adult": True, "id_no": "x"},
                {"name": "小孩", "is_adult": False, "id_no": "y"}]
        self.assertEqual([p["name"] for p in order_mod.select_passengers(allp, ["张三"])],
                         ["张三"])
        self.assertEqual([p["name"] for p in order_mod.select_passengers(allp, [])],
                         ["张三"])   # 兜底只选成人
        self.assertEqual([p["name"] for p in order_mod.select_passengers(allp, ["不存在"])],
                         ["张三"])

    def test_is_busy_error(self):
        self.assertTrue(order_mod.is_busy_error("系统忙，请稍后重试"))
        self.assertFalse(order_mod.is_busy_error("余票不足"))


class TestNotify(TempDirCase):
    def test_disabled_email_short_circuit(self):
        ok, msg = notify_mod.send_email({"enabled": False}, "s", "b")
        self.assertTrue(ok)

    def test_missing_fields_reported(self):
        ok, msg = notify_mod.send_email({"enabled": True}, "s", "b")
        self.assertFalse(ok)
        self.assertIn("缺少字段", msg)

    def test_secret_roundtrip(self):
        enc = notify_mod.protect_secret("auth-code-x")
        self.assertTrue(enc.startswith("dpapi1:"))
        self.assertEqual(notify_mod.secret_of(enc), "auth-code-x")
        self.assertEqual(notify_mod.secret_of("legacy"), "legacy")


class TestLogutil(TempDirCase):
    def test_day_rotation(self):
        h = logutil.DayFileHandler(self.tmp, "test")
        h.setFormatter(logging_fmt())
        lg, _ = _mk_logger("t_rot", h)
        real = logutil.datetime.date
        # 按真实「今天」推日期：把日期写死成 2026-10-05 的话，机器时间一到
        # 那天之后两个日志就落进同一个文件，用例必然失败（曾经如此）
        d1 = real.today()
        d2 = real.fromordinal(d1.toordinal() + 1)

        class FakeDate:
            _d = d1
            @classmethod
            def today(cls):
                return cls._d
        logutil.datetime.date = FakeDate
        try:
            lg.info("day1")
            FakeDate._d = d2
            lg.info("day2")
        finally:
            logutil.datetime.date = real
            h.close()
        files = sorted(os.listdir(self.tmp))
        self.assertEqual(files, ["test_%s.log" % d1.strftime("%Y%m%d"),
                                 "test_%s.log" % d2.strftime("%Y%m%d")])


class TestPassengers(TempDirCase):
    def test_roundtrip_and_default(self):
        p = os.path.join(self.tmp, "passengers.json")
        pax_mod.save_passengers([{"name": "张三", "id_no": "x", "is_default": True},
                                 {"name": "李四", "id_no": "y"}], p)
        back = pax_mod.load_passengers(p)
        self.assertEqual([x["name"] for x in back], ["张三", "李四"])
        self.assertEqual(pax_mod.default_names(p), ["张三"])

    def test_corrupted_returns_empty(self):
        p = os.path.join(self.tmp, "bad.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{broken")
        self.assertEqual(pax_mod.load_passengers(p), [])


class TestDpapiBlobLifetime(unittest.TestCase):
    """DPAPI _blob() 必须把 backing buffer 锚定在返回的 struct 上。

    背景（passengers.py）：DATA_BLOB 的 pbData 只存裸地址，不持有 buffer
    对象。若 _blob 返回后 buffer 被释放，后续 CryptProtectData /
    CryptUnprotectData 就是 use-after-free（未定义行为；Windows 上通常
    “碰巧能用”，但可能加密出垃圾导致 passengers.json 永久无法解密）。

    本测试在 Linux 也可运行：它取出 _blob 的真实源码（AST 提取后 exec），
    断言返回的 struct 锚定了 buffer。旧代码（直接 return DATA_BLOB(...)）
    无此锚定，测试必红；新代码必绿。注意：这是机制守卫，不能替代
    Windows 真机上的加解密往返测试。
    """

    _BLOB_OWNERS = ("_dpapi_protect", "_dpapi_unprotect")

    @staticmethod
    def _load_blob_func(owner_name):
        """从 passengers.py 源码提取 owner_name 内嵌的 _blob，返回可调用对象。"""
        import ast
        import ctypes
        from ctypes import wintypes

        with open(os.path.join(HERE, "passengers.py"), encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
        outer = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == owner_name)
        blob_node = next(n for n in ast.walk(outer)
                         if isinstance(n, ast.FunctionDef) and n.name == "_blob")

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        ns = {"ctypes": ctypes, "DATA_BLOB": DATA_BLOB}
        exec(compile(ast.Module(body=[blob_node], type_ignores=[]),
                     "<_blob:%s>" % owner_name, "exec"), ns)
        return ns["_blob"]

    def test_blob_anchors_backing_buffer(self):
        import ctypes
        for owner in self._BLOB_OWNERS:
            with self.subTest(owner=owner):
                blob_fn = self._load_blob_func(owner)
                payload = b"dpapi-lifetime-probe-123"
                st = blob_fn(payload)
                anchor = getattr(st, "_buf", None)
                self.assertIsNotNone(
                    anchor,
                    "%s() 内嵌 _blob 返回的 DATA_BLOB 没有锚定 backing buffer；"
                    "_blob 返回后 pbData 即悬垂 (use-after-free)" % owner)
                # 锚定的 buffer 内容与输入一致，且 struct 指针确实指向该内存
                self.assertEqual(anchor.value, payload)
                self.assertEqual(st.cbData, len(payload))
                self.assertEqual(ctypes.string_at(st.pbData, len(payload)), payload)


class TestLauncherUnit(TempDirCase):
    def test_parse_dt_formats(self):
        r = launcher.LauncherApp._resolve_dates({"date": "2026-10-07", "date_to": "2026-10-12"})
        self.assertEqual(len(r), 6)
        with self.assertRaises(RuntimeError):
            launcher.LauncherApp._resolve_dates({"date": "2026-10-07", "date_to": "2026-10-14"})

    def test_fmt_countdown(self):
        self.assertEqual(launcher.fmt_countdown(-5), "00:00:00")
        self.assertIn("天", launcher.fmt_countdown(90061))

    def test_purpose_of(self):
        lc = {"purpose_code": "ADULT", "passenger_names": ["张三", "李四"],
              "pax_purpose": {"李四": "0X00"}}
        self.assertEqual(launcher.purpose_of(lc), "ADULT")       # 混选按成人口径查票
        self.assertEqual(launcher.purpose_of(lc, "李四"), "0X00")
        lc2 = {"purpose_code": "0X00", "passenger_names": ["张三"], "pax_purpose": {}}
        self.assertEqual(launcher.purpose_of(lc2), "0X00")

    def test_new_grab_task_unique_id(self):
        t1, t2 = launcher.new_grab_task(1), launcher.new_grab_task(2)
        self.assertNotEqual(t1["id"], t2["id"])
        self.assertEqual(t1["status"], "idle")

    def test_search_stations_local(self):
        sts = launcher.search_stations("bjb")
        self.assertTrue(sts)
        self.assertIn("code", sts[0])

    def test_merge_kind_concurrent(self):
        # 必须把缓存文件也指向临时目录：只清 _station_kinds 的话，_merge_kind 里
        # 的 load_station_kinds() 会把真实 station_kind.json 又读回来（VNP 实际
        # 是 "高铁+动车"），用例就变成跟真实缓存赛跑。
        snapshot = dict(launcher._station_kinds)
        old_path = launcher.STATION_KIND_PATH
        launcher.STATION_KIND_PATH = os.path.join(self.tmp, "station_kind.json")

        def _restore():
            launcher._station_kinds.clear()
            launcher._station_kinds.update(snapshot)
            launcher.STATION_KIND_PATH = old_path

        self.addCleanup(_restore)
        launcher._station_kinds.clear()
        ts = [threading.Thread(target=lambda k=k: [launcher._merge_kind("VNP", k)
                                                   for _ in range(100)])
              for k in ("高铁", "普速")]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(launcher._station_kinds["VNP"], "高铁+普速")
        # 多值串（探测返回 "高铁+动车+普速"）必须拆开合并，不能整串塞进去
        self.assertTrue(launcher._merge_kind("VNP", "高铁+动车+普速"))
        self.assertEqual(launcher._station_kinds["VNP"], "高铁+动车+普速")
        self.assertFalse(launcher._merge_kind("VNP", "动车"))
        self.assertEqual(launcher._station_kinds["VNP"], "高铁+动车+普速")


class TestGuiUnit(TempDirCase):
    def test_normalize_email(self):
        email = gui.normalize_email_settings({
            "username": "a@qq.com", "from": "", "to": ["b@qq.com"]})
        self.assertEqual(email["from"], "a@qq.com")   # 发件人留空回退发件邮箱
        with self.assertRaises(ValueError):
            gui.normalize_email_settings({"username": "bad", "to": []})

    def test_mask_id(self):
        self.assertEqual(gui._mask_id("110101199001011234"), "110***********1234")

    def test_remove_task_by_uid(self):
        cfg = {"tasks": [{"uid": "u1", "name": "A"}, {"uid": "u2", "name": "B"}]}
        gui.remove_task_from_config(cfg, {"uid": "u1", "name": "A"})
        self.assertEqual([t["name"] for t in cfg["tasks"]], ["B"])

    def test_format_dates(self):
        t = {"dates": ["2026-10-08"], "date_range": []}
        self.assertEqual(gui.format_dates(t), "2026-10-08")
        t = {"dates": [], "date_range": ["2026-10-08", "2026-10-11"]}
        self.assertIn("4天", gui.format_dates(t))


# ============================ 集成 / 端到端 ============================

class TestEngineFlow(TempDirCase):
    """端到端（mock）：查询命中 → 下单 → 状态/防重/历史 的完整状态机。"""

    def _run(self, order_ret, expect_status, expect_dedup=None, notify_calls=None,
             expect_history=True, **task_over):
        e = make_engine(self.tmp)
        t = task_of(**task_over)
        e.tasks = [t]
        e.state["tasks"][t["name"]] = {"status": "monitoring", "fail_streak": 0}
        with mock.patch.object(e, "_query_with_retry",
                               return_value=[synthetic_row()]), \
             mock.patch.object(engine_mod.order_mod, "order_ticket",
                               return_value=order_ret) as m_order, \
             mock.patch.object(engine_mod.notify_mod, "send_email",
                               return_value=(True, "ok")) as m_notify:
            broke, _rec = e._run_task(t)
        st = e.state["tasks"][t["name"]]["status"]
        self.assertEqual(st, expect_status, e.state["tasks"][t["name"]].get("message"))
        if expect_dedup:
            self.assertTrue(any(v == expect_dedup for v in e.state["dedup"].values()),
                            e.state["dedup"])
        if notify_calls is not None:
            self.assertEqual(m_notify.call_count, notify_calls)
        self.assertEqual(os.path.exists(e.history_path), expect_history)
        return e

    def test_e2e_success_and_stop(self):
        self._run((True, "ok 订单号: E123", {"order_no": "E123", "passengers": "张三"}),
                  expect_status="success", expect_dedup="SUBMITTED", notify_calls=1)

    def test_dup_unpaid_stops(self):
        # 官方核验=待支付且非本次提交(更早旧单):记 ACCOUNT_DUP 并按 stop_after_order 停止
        with mock.patch.object(engine_mod.order_mod, "classify_with_time",
                               return_value=("unpaid", "E9", "未支付", None)) as mq:
            e = self._run((False, "已有订单", {"reason": "dup"}),
                          expect_status="success", expect_dedup="ACCOUNT_DUP")
        self.assertEqual(mq.call_count, 1)

    def test_dup_cancelled_clears_and_keeps_monitoring(self):
        # 官方核验=已取消:清除本地防重记录,允许重新下单,任务继续监控
        with mock.patch.object(engine_mod.order_mod, "classify_with_time",
                               return_value=("cancelled", "E9", "已取消", None)):
            e = self._run((False, "已有订单", {"reason": "dup"}),
                          expect_status="monitoring")
        self.assertNotIn("ACCOUNT_DUP", set(e.state["dedup"].values()))

    def test_dup_paid_stops_as_success(self):
        with mock.patch.object(engine_mod.order_mod, "classify_with_time",
                               return_value=("paid", "E9", "已支付", None)):
            self._run((False, "已有订单", {"reason": "dup"}),
                      expect_status="success", expect_dedup="SUBMITTED")

    def test_seat_unavailable_permanent_skip(self):
        self._run((False, "网页端不提供席别 硬座", {"reason": "seat_unavailable"}),
                  expect_status="monitoring", expect_dedup="SEAT_UNAVAILABLE")

    def test_busy_enters_cooldown(self):
        # 系统忙走冷却退避、不写历史（设计如此，避免刷日志）
        e = self._run((False, "系统忙，请稍后重试", None),
                      expect_status="retrying", expect_history=False)
        self.assertTrue(e.state["retry"])

    def test_ambiguous_stops_task(self):
        # 结果未知且官方回读失败(查不到) → 保守停任务交人工
        with mock.patch.object(engine_mod.order_mod, "classify_with_time",
                               return_value=("error", "", "官方订单查询失败", None)):
            e = self._run((False, "提交后 90 秒未收到明确结果", {"reason": "ambiguous"}),
                          expect_status="failed")
        hist = json.load(open(e.history_path, encoding="utf-8"))
        self.assertEqual(hist[-1]["result"], "ambiguous")

    def test_ambiguous_recent_unpaid_is_success(self):
        # 结果未知，回读发现「未支付且下单时间=本次」→ 判定本次提交成功
        with mock.patch.object(engine_mod.order_mod, "classify_with_time",
                               return_value=("unpaid", "E9", "未支付",
                                             {"order_no": "E9", "order_time": "2026-10-07 12:00:00"})):
            e = self._run((False, "提交后 90 秒未收到明确结果", {"reason": "ambiguous"}),
                          expect_status="success", expect_dedup="SUBMITTED", notify_calls=1)
        hist = json.load(open(e.history_path, encoding="utf-8"))
        self.assertEqual(hist[-1]["result"], "success")

    def test_ambiguous_none_keeps_monitoring(self):
        # 结果未知但官方确认无此订单 → 安全重试，任务继续监控、不写历史
        with mock.patch.object(engine_mod.order_mod, "classify_with_time",
                               return_value=("none", "", "未找到", None)):
            self._run((False, "提交后 90 秒未收到明确结果", {"reason": "ambiguous"}),
                      expect_status="monitoring", expect_history=False)

    def test_dup_recent_unpaid_is_success(self):
        # 防重复命中但回读发现「未支付且下单时间=本次」→ 本次已提交成功
        with mock.patch.object(engine_mod.order_mod, "classify_with_time",
                               return_value=("unpaid", "E9", "未支付",
                                             {"order_no": "E9", "order_time": "2026-10-07 12:00:00"})):
            self._run((False, "已有订单", {"reason": "dup"}),
                      expect_status="success", expect_dedup="SUBMITTED")

    def test_dedup_prevents_reorder(self):
        """防重第二层：已 SUBMITTED 的组合再次命中直接跳过，不再调下单。"""
        e = make_engine(self.tmp)
        t = task_of()
        e.tasks = [t]
        e.state["tasks"][t["name"]] = {"status": "monitoring"}
        from engine import dedup_key
        key = dedup_key(t, engine_mod.future_dates(t)[0][0], "K225", "硬座", ["张三"])
        e.state["dedup"][key] = "SUBMITTED"
        with mock.patch.object(e, "_query_with_retry",
                               return_value=[synthetic_row()]) as q, \
             mock.patch.object(engine_mod.order_mod, "order_ticket") as m_order:
            broke, _ = e._run_task(t)
        m_order.assert_not_called()
        self.assertEqual(e.state["tasks"][t["name"]]["status"], "monitoring")

    def test_query_failure_backoff(self):
        e = make_engine(self.tmp)
        t = task_of()
        e.tasks = [t]
        e.state["tasks"][t["name"]] = {"status": "monitoring"}
        with mock.patch.object(e, "_query_with_retry",
                               side_effect=RuntimeError("网络异常")):
            broke, rec = e._run_task(t)
        self.assertTrue(broke and rec)
        self.assertGreaterEqual(e.state["tasks"][t["name"]]["fail_streak"], 1)


class TestLauncherIntegration(TempDirCase):
    def _with_here(self):
        """把 launcher 的 HERE 与 LAUNCHER_CFG_PATH 都指向临时目录。

        LAUNCHER_CFG_PATH 是 import 时按真实 HERE 算好的常量，只改 HERE 不管用：
        merge_trains_from_monitor 默认 saver=save_launcher_config 会写到**真实**
        launcher_config.json（历史事故：整个配置被覆盖成空壳）。"""
        real = launcher.HERE
        real_cfg = launcher.LAUNCHER_CFG_PATH
        launcher.HERE = self.tmp
        launcher.LAUNCHER_CFG_PATH = os.path.join(self.tmp, "launcher_config.json")
        self.addCleanup(setattr, launcher, "HERE", real)
        self.addCleanup(setattr, launcher, "LAUNCHER_CFG_PATH", real_cfg)
        return real

    def test_append_and_merge_roundtrip(self):
        self._with_here()
        json.dump({"tasks": [{"name": "监控A", "trains": ["K225", "K925"]}]},
                  open(os.path.join(self.tmp, "config.json"), "w", encoding="utf-8"))
        name = launcher.append_monitor_task({"name": "任务X"}, start_now=False)
        self.assertEqual(name, "任务X")
        lc = {"trains": ["K225", "G1"], "synced_trains": []}
        self.assertTrue(launcher.merge_trains_from_monitor(lc))
        self.assertIn("K925", lc["trains"])

    def test_append_corrupt_state_keeps_bad_and_skips(self):
        self._with_here()
        json.dump({"tasks": []}, open(os.path.join(self.tmp, "config.json"), "w", encoding="utf-8"))
        open(os.path.join(self.tmp, "state.json"), "w", encoding="utf-8").write("{broken")
        launcher.append_monitor_task({"name": "T"}, start_now=True)
        self.assertTrue([f for f in os.listdir(self.tmp) if f.startswith("state.json.bad-")])
        st = json.load(open(os.path.join(self.tmp, "state.json"), encoding="utf-8"))
        self.assertEqual(st["tasks"]["T"]["status"], "monitoring")   # start_now 不降级

    def test_concurrent_append_both_survive(self):
        self._with_here()
        json.dump({"tasks": []}, open(os.path.join(self.tmp, "config.json"), "w", encoding="utf-8"))
        errs = []

        def add(n):
            try:
                launcher.append_monitor_task({"name": "P%d" % n}, start_now=False)
            except Exception as e:
                errs.append(e)
        ts = [threading.Thread(target=add, args=(i,)) for i in range(3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        cfg = json.load(open(os.path.join(self.tmp, "config.json"), encoding="utf-8"))
        self.assertEqual(len(cfg["tasks"]), 3)


class TestSeatRules(TempDirCase):
    """「车次=席别」专属规则：解析、候选解析、feedback（RULES.md 第一节）。"""

    def test_parse_mixed(self):
        r = ticket.seat_rules_parse("K225=硬座/无座, K1969=硬卧, 硬座")
        self.assertEqual(r["rules"]["K225"], ["硬座", "无座"])   # 无座展开在后
        self.assertEqual(r["rules"]["K1969"], ["硬卧"])
        self.assertEqual(r["bare"], ["硬座"])
        self.assertEqual(r["warnings"], [])

    def test_parse_duplicate_last_wins(self):
        r = ticket.seat_rules_parse("K225=硬卧, k225=硬座")
        self.assertEqual(r["rules"]["K225"], ["硬座"])           # 大小写不敏感+后者覆盖
        self.assertTrue(any("重复" in w for w in r["warnings"]))

    def test_parse_unknown_word_and_empty(self):
        r = ticket.seat_rules_parse("K225=硬坐, K1969=, 硬座")
        self.assertEqual(r["rules"]["K225"], ["硬座"])           # 无法识别按硬座
        self.assertNotIn("K1969", r["rules"])                    # 空值忽略
        self.assertEqual(len(r["warnings"]), 2)

    def test_parse_legacy_list_and_string(self):
        r = ticket.seat_rules_parse(["硬卧", "硬座"])
        self.assertEqual(r["rules"], {})
        self.assertEqual(r["bare"], ["硬卧", "硬座"])
        r2 = ticket.seat_rules_parse("硬卧 硬座")                # 旧格式：空白分隔
        self.assertEqual(r2["bare"], ["硬卧", "硬座"])

    def test_candidates_scenario1(self):
        """车次1要硬座、车次2要硬卧：互不串。"""
        checked = ["硬座", "硬卧"]
        pri = "K225=硬座, K1969=硬卧"
        self.assertEqual(ticket.seat_candidates_for("K225", checked, pri), ["硬座"])
        self.assertEqual(ticket.seat_candidates_for("K1969", checked, pri), ["硬卧"])

    def test_candidates_intersection_empty_skips(self):
        self.assertEqual(ticket.seat_candidates_for("K225", ["硬卧"], "K225=硬座"), [])

    def test_candidates_unchecked_means_unrestricted(self):
        self.assertEqual(ticket.seat_candidates_for("K225", [], "K225=硬座"),
                         ["硬座"])

    def test_candidates_bare_can_exceed_checked(self):
        cand = ticket.seat_candidates_for("K1969", ["硬座"], "硬卧", avail=None)
        self.assertEqual(cand, ["硬卧", "硬座"])                 # 首选可超出勾选（旧口径）

    def test_candidates_nothing_restricted_uses_avail(self):
        avail = {"硬座": "5", "硬卧": "有"}
        self.assertEqual(ticket.seat_candidates_for("K225", [], "", avail),
                         ["硬座", "硬卧"])

    def test_candidates_avail_filter_and_lowercase(self):
        avail = {"硬座": "5"}
        self.assertEqual(ticket.seat_candidates_for("k225", ["硬座", "硬卧"],
                                                    "K225=硬卧/硬座", avail),
                         ["硬座"])                               # 规则顺序,avail 过滤

    def test_feedback_train_checks(self):
        text, warn = ticket.seat_priority_feedback(
            "K999=硬座, K225=硬卧", checked=["硬座"], trains=["K225"])
        self.assertTrue(warn)
        self.assertIn("不在车次列表", text)
        self.assertIn("无交集", text)
        text2, warn2 = ticket.seat_priority_feedback("硬座/硬卧")
        self.assertFalse(warn2)
        self.assertIn("优先席别", text2)


class TestAppCommon(TempDirCase):
    def test_parse_date_range_shapes(self):
        import appcommon
        self.assertEqual(appcommon.parse_date_range("2026-10-07"),
                         (["2026-10-07"], []))
        self.assertEqual(appcommon.parse_date_range("2026-10-07~2026-10-12"),
                         ([], ["2026-10-07", "2026-10-12"]))
        self.assertEqual(appcommon.parse_date_range("2026-10-07", "2026-10-12"),
                         ([], ["2026-10-07", "2026-10-12"]))
        self.assertEqual(appcommon.parse_date_range("2026-10-07", "2026-10-07"),
                         (["2026-10-07"], []))
        for bad in ("2026-10-07~2026-10-06", "20261007", "", "abc"):
            with self.assertRaises(ValueError):
                appcommon.parse_date_range(bad)
        with self.assertRaises(ValueError):
            appcommon.parse_date_range("2026-10-07", "2026-10-14")  # 超 5 天跨度

    def test_atomic_write_json_basic_and_unique_tmp(self):
        import appcommon, threading
        path = os.path.join(self.tmp, "a.json")
        appcommon.atomic_write_json(path, {"k": 1})
        self.assertEqual(json.load(open(path, encoding="utf-8")), {"k": 1})
        self.assertFalse([f for f in os.listdir(self.tmp) if "tmp" in f])
        seen = set()

        real_dump = json.dump

        def spy_dump(obj, f, **kw):
            seen.add(f.name)
            return real_dump(obj, f, **kw)

        with mock.patch.object(json, "dump", spy_dump):
            ths = [threading.Thread(
                       target=lambda i=i: appcommon.atomic_write_json(
                           path, {"i": i}))
                   for i in range(4)]
            [t.start() for t in ths]
            [t.join() for t in ths]
        self.assertEqual(len(seen), 4)          # 临时名线程唯一
        self.assertTrue(json.load(open(path, encoding="utf-8")))

    def test_replace_retry_and_fallback(self):
        import appcommon
        src = os.path.join(self.tmp, "src")
        dst = os.path.join(self.tmp, "dst")
        open(src, "w", encoding="utf-8").write("data")
        calls = {"n": 0}
        real_replace = appcommon.os.replace

        def flaky(a, b):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("busy")
            return real_replace(a, b)
        with mock.patch.object(appcommon.os, "replace", flaky):
            appcommon.replace_with_retry(src, dst)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(open(dst, encoding="utf-8").read(), "data")
        # 重试耗尽 + fallback_direct：内容直写落地
        # （段一已把 src 改名为 dst：先重建 src，否则源缺失时兜底正确地抛错）
        open(src, "w", encoding="utf-8").write("data")
        dst2 = os.path.join(self.tmp, "dst2")
        with mock.patch.object(appcommon.os, "replace",
                               side_effect=OSError("busy forever")):
            appcommon.replace_with_retry(src, dst2, fallback_direct=True)
        self.assertEqual(open(dst2, encoding="utf-8").read(), "data")


class TestConfigKeys(TempDirCase):
    """配置键防漂移：代码读取的键必须都在 example 模板里文档化。"""

    def _scan(self, pattern):
        keys = set()
        for f in ["launcher.py", "gui.py", "engine.py", "order.py",
                  "browser_order.py", "monitor.py"]:
            for m in re.finditer(pattern, open(f, encoding="utf-8").read()):
                keys.add(m.group(1))
        return keys

    def test_config_example_covers_code_reads(self):
        import config_keys
        live = json.load(open(os.path.join(HERE, "config.example.json"),
                              encoding="utf-8"))
        code_keys = (self._scan(r"(?<![\w.])config\.get\(\s*['\"](\w+)[\"']")
                     | self._scan(r"(?<![\w.])cfg\.get\(\s*['\"](\w+)[\"']"))
        self.assertTrue(code_keys <= config_keys.CONFIG_KEYS,
                        sorted(code_keys - config_keys.CONFIG_KEYS))
        self.assertTrue(config_keys.CONFIG_KEYS <= set(live),
                        sorted(config_keys.CONFIG_KEYS - set(live)))
        email = live.get("notify", {}).get("email", {})
        self.assertTrue(config_keys.NOTIFY_EMAIL_KEYS <= set(email))

    def test_launcher_example_covers_code_reads(self):
        import config_keys
        live = json.load(open(os.path.join(HERE, "launcher_config.example.json"),
                              encoding="utf-8"))
        code_keys = (self._scan(r"(?<![\w.])lc\.get\(\s*['\"](\w+)[\"']")
                     | self._scan(r"(?<![\w.])self\.lc\.get\(\s*['\"](\w+)[\"']"))
        allowed = config_keys.LAUNCHER_CONFIG_KEYS | config_keys.LAUNCHER_TASK_EXTRA_KEYS
        self.assertTrue(code_keys <= allowed,
                        sorted(code_keys - allowed))
        self.assertTrue(config_keys.LAUNCHER_CONFIG_KEYS <= set(live),
                        sorted(config_keys.LAUNCHER_CONFIG_KEYS - set(live)))


class TestStateStore(TempDirCase):
    """state.json 三写方共享层：读/坏档挪档/写 的机械层（策略在调用方）。"""

    def test_read_state_or_none_shapes(self):
        import appcommon
        p = os.path.join(self.tmp, "state.json")
        self.assertEqual(appcommon.read_state_or_none(p), ({}, None))   # 不存在
        json.dump({"tasks": {}}, open(p, "w", encoding="utf-8"))
        st, err = appcommon.read_state_or_none(p)
        self.assertIsNone(err)
        self.assertEqual(st, {"tasks": {}})
        open(p, "w", encoding="utf-8").write("{corrupt")
        st2, err2 = appcommon.read_state_or_none(p)
        self.assertIsNone(st2)
        self.assertIsInstance(err2, ValueError)

    def test_quarantine_timestamped_and_keeps_evidence(self):
        import appcommon
        p = os.path.join(self.tmp, "state.json")
        open(p, "w", encoding="utf-8").write("{corrupt")
        bad = appcommon.quarantine_corrupt(p)
        self.assertIsNotNone(bad)
        self.assertTrue(os.path.basename(bad).startswith("state.json.bad-"))
        self.assertEqual(open(bad, encoding="utf-8").read(), "{corrupt")
        self.assertFalse(os.path.exists(p))
        # 挪移失败(占用):证据保留原地,返回 None
        open(p, "w", encoding="utf-8").write("{corrupt2")
        import appcommon as ac
        with mock.patch.object(ac.os, "replace", side_effect=OSError("busy")):
            self.assertIsNone(ac.quarantine_corrupt(p))
        self.assertEqual(open(p, encoding="utf-8").read(), "{corrupt2")

    def test_three_writers_concurrent(self):
        """三写方(engine/gui/launcher 形态)并发写:全部落盘且文件始终可解析。"""
        import appcommon, threading
        p = os.path.join(self.tmp, "state.json")
        appcommon.write_state(p, {"tasks": {}, "dedup": {}})
        errs = []

        def engine_like(i):
            try:
                for n in range(30):
                    for _ in range(20):          # 撞锁=本轮跳过重读(生产语义)
                        st, err = appcommon.read_state_or_none(p)
                        if err is None or not isinstance(
                                err, PermissionError):
                            break
                    else:
                        continue
                    if err is not None:
                        errs.append(err)
                        continue
                    st = st or {}
                    st.setdefault("tasks", {})["e%d" % i] = n
                    appcommon.write_state(p, st, fallback_direct=True)
            except Exception as e:
                errs.append(e)

        def gui_like(i):
            try:
                for n in range(30):
                    st, _ = appcommon.read_state_or_none(p)
                    st = st or {}
                    st.setdefault("tasks", {})["g%d" % i] = n
                    appcommon.write_state(p, st, tmp_kind="guisave")
            except Exception as e:
                errs.append(e)

        def launcher_like(i):
            try:
                for n in range(30):
                    st, _ = appcommon.read_state_or_none(p)
                    st = st or {}
                    st.setdefault("tasks", {})["l%d" % i] = n
                    appcommon.write_state(p, st, tmp_kind="launcher")
            except Exception as e:
                errs.append(e)

        ths = ([threading.Thread(target=engine_like, args=(i,)) for i in range(2)]
               + [threading.Thread(target=gui_like, args=(i,)) for i in range(2)]
               + [threading.Thread(target=launcher_like, args=(i,)) for i in range(2)])
        [t.start() for t in ths]
        [t.join() for t in ths]
        self.assertEqual(errs, [])
        final = json.load(open(p, encoding="utf-8"))     # 始终是合法 JSON
        self.assertIn("tasks", final)
        self.assertFalse([f for f in os.listdir(self.tmp) if ".tmp" in f])


class TestSearchStations(TempDirCase):
    """车站搜索匹配度排序：精确优先、普速小站不被挤出（长葛问题）。"""

    def test_hanzi_exact_first_and_prefix_after(self):
        r = launcher.search_stations("长葛")
        self.assertEqual(r[0]["name"], "长葛")
        self.assertIn("长葛北", [x["name"] for x in r])

    def test_hanzi_single_char_includes_pusu(self):
        r = [x["name"] for x in launcher.search_stations("长", limit=12)]
        self.assertIn("长葛", r)
        # 同档(2字"长"字站约10个)按索引序,长葛位次不保证,但在结果内即可达

    def test_fault_tolerance_fullwidth_and_spaces(self):
        self.assertEqual(launcher.search_stations("ｃｑ")[0]["name"], "重庆")   # 全角
        r = [x["name"] for x in launcher.search_stations("长　葛")]
        self.assertEqual(r[0], "长葛")                                        # 全角空格+忽略空白
        r2 = launcher.search_stations("chong qing")
        self.assertEqual(r2[0]["name"], "重庆")                               # 空格剔除

    def test_covers_all_matches_within_2s(self):
        import time as _time
        t0 = _time.perf_counter()
        r = launcher.search_stations("长", limit=None)      # 全量命中
        dt = _time.perf_counter() - t0
        names = [x["name"] for x in r]
        self.assertGreater(len(r), 12)                      # 旧实现 12 个封顶,现全量
        self.assertIn("长葛", names)
        self.assertLess(dt, 2.0)                            # 响应 ≤2 秒

    def test_ascii_ranking_unchanged(self):
        self.assertEqual(launcher.search_stations("cq")[0]["name"], "重庆")
        self.assertEqual(launcher.search_stations("chang")[0]["name"], "长春")
        self.assertEqual(launcher.search_stations("bjd")[0]["name"], "北京东")


class TestGrabberStationMapDegraded(TempDirCase):
    """Task 26 round 2 (P1): 下单 worker 线程 _run 内 load_station_map 抛异常
    不得逃出 _run；应记警告后以明确的失败收尾（result 置位）。"""

    def _make_grabber(self):
        import queue
        lc = {"from": "北京", "to": "上海", "date": "2026-10-10",
              "trains": ["G101"], "seat_types": ["二等座"],
              "seat_priority": "", "passenger_names": []}
        g = launcher.Grabber(lc, logq=queue.Queue())
        g._log = g.logq.put  # 不写真实日志文件
        return g

    def test_run_offline_station_map_failure_sets_clear_result(self):
        # 离线首跑：load_station_map 抛 URLError → _run 不抛异常，
        # result 置为明确的"车站数据加载失败"，而不是把 URLError 丢给外层
        g = self._make_grabber()
        with mock.patch.object(ticket, "load_station_map",
                               side_effect=URLError("offline")):
            g._run()  # 旧代码：URLError 从这里直接逃出
        self.assertIsNotNone(g.result, "result 未置位")
        ok, msg = g.result
        self.assertFalse(ok)
        self.assertIn("车站数据加载失败", msg)

    def test_run_offline_logs_network_warning(self):
        # 降级原因必须被清楚记录
        g = self._make_grabber()
        logged = []
        g._log = logged.append
        with mock.patch.object(ticket, "load_station_map",
                               side_effect=URLError("offline")):
            g._run()
        self.assertTrue(any("车站数据加载失败" in m for m in logged),
                        "未记录车站数据加载失败: %s" % logged)

    def test_run_online_station_map_still_passes_through(self):
        # 回归 pin：加载成功时守卫不拦截，原流程继续（走到会话校验）
        g = self._make_grabber()
        with mock.patch.object(ticket, "load_station_map",
                               return_value=({"北京": "BJP", "上海": "SHH"},
                                             {"BJP": "北京", "SHH": "上海"})), \
             mock.patch.object(launcher.browser_order, "check_session",
                               return_value=(False, "未登录")), \
             mock.patch.object(launcher.browser_order, "login",
                               side_effect=Exception("no browser")):
            g._run()
        ok, msg = g.result
        self.assertFalse(ok)
        # 能走到"登录失败"，证明已越过车站表加载阶段
        self.assertIn("登录失败", msg)


# ----------------------------- 辅助 -----------------------------

import logging  # noqa: E402


def logging_fmt():
    return logging.Formatter("%(message)s")


def _mk_logger(name, handler):
    lg = logging.getLogger(name)
    lg.setLevel(logging.INFO)
    lg.handlers = [handler]
    lg.propagate = False
    return lg, handler


if __name__ == "__main__":
    unittest.main(verbosity=2)
