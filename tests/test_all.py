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
import appcommon                     # noqa: E402
import filelock                      # noqa: E402
import probe_login                   # noqa: E402
import time                          # noqa: E402
import contextlib                    # noqa: E402


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
        # Task 41 后 engine._append_history 已删除，统一走 appcommon.append_history
        import appcommon
        e = make_engine(self.tmp)
        for i in range(510):
            appcommon.append_history(e.history_path,
                                     {"time": "t", "task": "x%d" % i, "result": "success"})
        with open(e.history_path, encoding="utf-8") as f:
            data = json.load(f)
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


class TestNotifyPortValidation(TempDirCase):
    """Task 30: 非数字 smtp_port 不得杀死监控线程。"""

    def _email_cfg(self, **over):
        cfg = {"enabled": True, "smtp_host": "smtp.example.com",
               "smtp_port": 465, "username": "u", "password": "p",
               "from": "u@example.com", "to": ["u@example.com"]}
        cfg.update(over)
        return cfg

    def test_non_numeric_port_falls_back_to_465(self):
        # "abc" 不得抛 ValueError；应回退 465 发起连接
        with mock.patch("notify.smtplib.SMTP_SSL") as m_ssl:
            ok, msg = notify_mod.send_email(self._email_cfg(smtp_port="abc"), "s", "b")
        self.assertTrue(ok, msg)
        m_ssl.assert_called_once()
        self.assertEqual(m_ssl.call_args[0][1], 465)

    def test_numeric_string_port_accepted(self):
        # 回归 pin：数字字符串本就可用
        with mock.patch("notify.smtplib.SMTP") as m_smtp:
            ok, msg = notify_mod.send_email(self._email_cfg(smtp_port="587"), "s", "b")
        self.assertTrue(ok, msg)
        self.assertEqual(m_smtp.call_args[0][1], 587)

    def test_safe_port_values(self):
        self.assertEqual(notify_mod._safe_port({"smtp_port": "abc"}), 465)
        self.assertEqual(notify_mod._safe_port({"smtp_port": None}), 465)
        self.assertEqual(notify_mod._safe_port({}), 465)
        self.assertEqual(notify_mod._safe_port({"smtp_port": 587}), 587)

    def test_notify_exception_does_not_kill_monitoring(self):
        # 有票命中 + 未开自动下单 → 走 _notify 调用点；send_email 抛异常也不得向上传播
        e = make_engine(self.tmp)
        t = task_of(auto_order=False)
        e.tasks = [t]
        e.state["tasks"][t["name"]] = {"status": "monitoring", "fail_streak": 0}
        with mock.patch.object(e, "_query_with_retry",
                               return_value=[synthetic_row()]), \
             mock.patch.object(engine_mod.notify_mod, "send_email",
                               side_effect=RuntimeError("boom")):
            broke, _rec = e._run_task(t)  # 不得抛异常
        self.assertFalse(broke)


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


class TestPassengersUndecryptableGuard(TempDirCase):
    """save_passengers 不得覆写在本环境不可解密的盒子（P1 数据销毁类）。

    背景：dpapi 盒子拷到 Linux / 换 Windows 用户后，load_passengers 解密失败
    静默返回 []；随后任意 save 会覆写盒子，把源机器可恢复的密文永久销毁。
    修复：save 前检测到"文件存在、非空、但本环境解不开"时拒绝覆写并记 error，
    只有 force=True 才允许覆盖。
    """

    def _write_dpapi_box(self, path):
        """写一个本 Linux 环境解不开的 dpapi 盒子（模拟从 Windows 拷来的文件）。

        _decrypt("dpapi", ...) 在 Linux 上因 ctypes.windll 不存在而抛异常，
        与"换了机器/用户解不开"是同一条失败路径。
        """
        import base64
        box = {"version": 1, "enc": "dpapi",
               "data": base64.b64encode(b"not-a-real-dpapi-blob").decode("ascii")}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(box, f)
        with open(path, "rb") as f:
            return f.read()

    def test_refuse_overwrite_undecryptable_box(self):
        p = os.path.join(self.tmp, "passengers.json")
        before = self._write_dpapi_box(p)
        with self.assertLogs("monitor", level="ERROR") as cm:
            ok = pax_mod.save_passengers([{"name": "张三"}], p)
        self.assertFalse(ok)
        with open(p, "rb") as f:
            self.assertEqual(f.read(), before)  # 文件内容必须原样保留
        self.assertTrue(any("[安全]" in m for m in cm.output))

    def test_force_allows_overwrite(self):
        p = os.path.join(self.tmp, "passengers.json")
        self._write_dpapi_box(p)
        ok = pax_mod.save_passengers([{"name": "张三"}], p, force=True)
        self.assertTrue(ok)
        back = pax_mod.load_passengers(p)
        self.assertEqual([x["name"] for x in back], ["张三"])

    def test_healthy_box_can_be_overwritten(self):
        # 回归 pin：本环境可解密的健康盒子，正常覆写不受影响
        p = os.path.join(self.tmp, "passengers.json")
        pax_mod.save_passengers([{"name": "张三"}], p)
        ok = pax_mod.save_passengers([{"name": "李四"}], p)
        self.assertTrue(ok)
        back = pax_mod.load_passengers(p)
        self.assertEqual([x["name"] for x in back], ["李四"])

    def test_missing_file_saves_normally(self):
        # 回归 pin：首跑无文件时正常创建
        p = os.path.join(self.tmp, "new.json")
        ok = pax_mod.save_passengers([{"name": "王五"}], p)
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(p))


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


class TestValidateAutoNoModal(TempDirCase):
    """Task 27: _validate(auto=True) 不得弹任何模态框。

    无人值守自动开抢时，模态 messagebox 会冻住 Tk 主线程导致定时开抢被杀。
    auto=True 时校验失败只 bell + 记日志 + 返回 False；手动路径行为不变。
    """

    def _make_app(self, lc):
        app = launcher.LauncherApp.__new__(launcher.LauncherApp)
        app.lc = lc
        app._mp = None
        app._top = mock.Mock()
        logs = []
        app._put_log = logs.append
        return app, logs

    def _ok_lc(self, **over):
        lc = {"from": "北京", "to": "上海", "date": "2026-10-09",
              "seat_types": ["二等座"], "passenger_names": ["张三"]}
        lc.update(over)
        return lc

    def _patched(self):
        p1 = mock.patch.object(launcher.messagebox, "showwarning")
        p2 = mock.patch.object(launcher.messagebox, "askyesno", return_value=True)
        return p1, p2

    def test_auto_missing_station_no_modal(self):
        app, logs = self._make_app(self._ok_lc(**{"from": ""}))
        p1, p2 = self._patched()
        with p1 as mw, p2 as my:
            self.assertFalse(app._validate(auto=True))
        mw.assert_not_called()
        my.assert_not_called()
        app._top.bell.assert_called_once()
        self.assertTrue(any("校验失败" in l for l in logs))

    def test_auto_bad_date_no_modal(self):
        app, logs = self._make_app(self._ok_lc(date="2026/10/09"))
        p1, p2 = self._patched()
        with p1 as mw, p2 as my:
            self.assertFalse(app._validate(auto=True))
        mw.assert_not_called()
        my.assert_not_called()

    def test_auto_no_seat_no_modal(self):
        app, logs = self._make_app(self._ok_lc(seat_types=[]))
        p1, p2 = self._patched()
        with p1 as mw, p2 as my:
            self.assertFalse(app._validate(auto=True))
        mw.assert_not_called()
        my.assert_not_called()

    def test_auto_no_passenger_fail_closed_no_modal(self):
        # 无人值守无法回答"使用默认乘车人"，fail closed：跳过本次开抢
        app, logs = self._make_app(self._ok_lc(passenger_names=[]))
        p1, p2 = self._patched()
        with p1 as mw, p2 as my:
            self.assertFalse(app._validate(auto=True))
        mw.assert_not_called()
        my.assert_not_called()
        app._top.bell.assert_called_once()

    def test_auto_all_valid(self):
        app, logs = self._make_app(self._ok_lc())
        p1, p2 = self._patched()
        with p1 as mw, p2 as my:
            self.assertTrue(app._validate(auto=True))
        mw.assert_not_called()
        my.assert_not_called()
        app._top.bell.assert_not_called()

    def test_manual_missing_station_still_modal(self):
        # 回归 pin：手动路径行为不变，仍弹模态框
        app, logs = self._make_app(self._ok_lc(**{"from": ""}))
        p1, p2 = self._patched()
        with p1 as mw, p2 as my:
            self.assertFalse(app._validate(auto=False))
        mw.assert_called_once()
        my.assert_not_called()

    def test_manual_no_passenger_askyesno_yes_continues(self):
        # 回归 pin：手动 askyesno 点"是"则继续
        app, logs = self._make_app(self._ok_lc(passenger_names=[]))
        p1, p2 = self._patched()
        with p1 as mw, p2 as my:
            self.assertTrue(app._validate(auto=False))
        my.assert_called_once()
        mw.assert_not_called()


class _FakeTkWidget:
    """Task 45: 记录方法调用的 Tk 构件替身（无真实显示）。

    winfo_* 返回 0 让居中算术可跑；方法按名缓存，保证 top.destroy
    每次取到的是同一个可调用对象，便于断言按钮 command 身份。
    """

    def __init__(self, master=None, **kw):
        self.master = master
        self.kw = kw
        self.calls = []          # [(name, args, kwargs)]
        self.destroyed = False
        self._methods = {}

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        if name not in self._methods:
            def _rec(*a, **k):
                self.calls.append((name, a, k))
                if name == "destroy":
                    self.destroyed = True
                if name == "winfo_exists":
                    return 0 if self.destroyed else 1
                return 0 if name.startswith("winfo_") else None
            self._methods[name] = _rec
        return self._methods[name]

    def called(self, name):
        return any(c[0] == name for c in self.calls)


class _FakeParent(_FakeTkWidget):
    pass


class TestRemindNonmodal(TempDirCase):
    """Task 45: launcher 开抢提醒改非模态。

    改前 _tick 里 messagebox.showinfo 是模态的：冻住 Tk 主线程的 after 链，
    无人值守时自动开抢永远到不了——提醒功能杀死核心功能。
    改后：bell + 日志 + 非模态 Toplevel（不 grab、不 wait_window），不阻塞 _tick。
    """

    def _make_app(self):
        app = launcher.LauncherApp.__new__(launcher.LauncherApp)
        app._mp = _FakeParent()
        app._top = mock.Mock()
        return app

    def _patched_tk(self):
        tops, labels, buttons = [], [], []

        def _toplevel(master=None, **kw):
            w = _FakeTkWidget(master, **kw)
            tops.append(w)
            return w

        def _label(master=None, **kw):
            w = _FakeTkWidget(master, **kw)
            labels.append(w)
            return w

        def _button(master=None, **kw):
            w = _FakeTkWidget(master, **kw)
            buttons.append(w)
            return w

        p1 = mock.patch.object(launcher.tk, "Toplevel", _toplevel)
        p2 = mock.patch.object(launcher.ttk, "Label", _label)
        p3 = mock.patch.object(launcher.ttk, "Button", _button)
        return p1, p2, p3, tops, labels, buttons

    def test_notify_nonmodal_creates_toplevel(self):
        app = self._make_app()
        p1, p2, p3, tops, labels, buttons = self._patched_tk()
        with p1, p2, p3:
            app._notify_nonmodal("开抢提醒", "距离开抢不到 5 分钟！")
        self.assertEqual(len(tops), 1)
        top = tops[0]
        self.assertIs(top.master, app._mp)
        self.assertIn(("title", ("开抢提醒",), {}), top.calls)
        self.assertTrue(any("距离开抢不到 5 分钟" in str(l.kw.get("text", ""))
                            for l in labels))

    def test_notify_nonmodal_never_blocks(self):
        # 非模态的核心证据：绝不调用 grab_set / wait_window
        app = self._make_app()
        p1, p2, p3, tops, labels, buttons = self._patched_tk()
        with p1, p2, p3:
            app._notify_nonmodal("开抢提醒", "msg")
        top = tops[0]
        self.assertFalse(top.called("grab_set"))
        self.assertFalse(top.called("wait_window"))
        self.assertFalse(top.called("focus_force"))
        # 提醒窗不自动消失：等用户点"知道了"
        self.assertFalse(top.destroyed)
        # "知道了"按钮确实能关掉窗口
        self.assertEqual(len(buttons), 1)
        cmd = buttons[0].kw.get("command")
        self.assertIs(cmd, top.destroy)
        cmd()
        self.assertTrue(top.destroyed)

    def test_notify_nonmodal_replaces_old_window(self):
        # 重复提醒不堆窗口：新窗出现前旧窗先关掉
        app = self._make_app()
        p1, p2, p3, tops, labels, buttons = self._patched_tk()
        with p1, p2, p3:
            app._notify_nonmodal("开抢提醒", "第一次")
            app._notify_nonmodal("开抢提醒", "第二次")
        self.assertEqual(len(tops), 2)
        self.assertTrue(tops[0].destroyed)
        self.assertFalse(tops[1].destroyed)

    def test_tick_has_no_blocking_dialog(self):
        # 回归 pin：_tick 里绝不能再出现模态弹窗（任何模态都会冻住 after 链）
        import inspect
        src = inspect.getsource(launcher.LauncherApp._tick)
        self.assertNotIn("showinfo", src)
        self.assertNotIn("showwarning", src)
        self.assertNotIn("showerror", src)
        self.assertNotIn("askyesno", src)
        self.assertNotIn("grab_set", src)
        self.assertNotIn("wait_window", src)
        self.assertIn("_notify_nonmodal", src)


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

    def test_dup_paid_keeps_monitoring_when_not_stop_after(self):
        # stop_after_order=False：dup→官方回读 paid（已支付旧单）
        # → 任务保持 monitoring，继续抢其它日期（Task 48，旧代码误置 success 漏单）
        with mock.patch.object(engine_mod.order_mod, "classify_with_time",
                               return_value=("paid", "E9", "已支付", None)):
            self._run((False, "已有订单", {"reason": "dup"}),
                      expect_status="monitoring", expect_dedup="SUBMITTED",
                      stop_after_order=False)

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

    def test_ambiguous_recent_unpaid_keeps_monitoring_when_not_stop_after(self):
        # stop_after_order=False：ambiguous→官方回读成功（未支付+本次）
        # → 任务保持 monitoring，继续抢其它日期（Task 37，旧代码误置 success）
        with mock.patch.object(engine_mod.order_mod, "classify_with_time",
                               return_value=("unpaid", "E9", "未支付",
                                             {"order_no": "E9", "order_time": "2026-10-07 12:00:00"})):
            e = self._run((False, "提交后 90 秒未收到明确结果", {"reason": "ambiguous"}),
                          expect_status="monitoring", expect_dedup="SUBMITTED",
                          notify_calls=1, stop_after_order=False)
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
        avail = {"硬座": "5", "高级动卧": "有", "硬卧": "有"}
        # Task 47: 不限席别只收可下单席别（按 avail 顺序，展示类席别名被过滤）
        self.assertEqual(ticket.seat_candidates_for("K225", [], "", avail),
                         ["硬座", "硬卧"])

    def test_candidates_unrestricted_filters_display_only(self):
        # Task 47: 「不限席别」分支必须过滤掉不可下单的展示类席别名，
        # 否则 launcher 按名索引 SEAT_NAME_TO_CODE 会潜伏 KeyError。
        avail = {"硬座": "5", "高级动卧": "有", "其他": "有",
                 "一等卧": "有", "二等卧": "有", "硬卧": "有"}
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


def _capture_mut(mutator):
    """把 update_config_locked 的 mutator 作用到空 dict 并返回，用于测试捕获。"""
    cfg = {}
    mutator(cfg)
    return cfg


class TestMonitorInterrupt(TempDirCase):
    """Task 28 (P1): monitor 交互输入 Ctrl+C/EOF 时 read 返回 None，
    ask_yes_no / ask_priority 不得抛 AttributeError，应抛 KeyboardInterrupt
    让 main_menu 的已有处理接住（"已中断，返回主菜单。"），不打 traceback。"""

    def test_ask_yes_no_none_raises_keyboard_interrupt(self):
        # Ctrl+C/EOF：旧代码 read(...).lower() 抛 AttributeError 崩
        import monitor as monitor_mod
        with mock.patch.object(monitor_mod, "read", return_value=None):
            with self.assertRaises(KeyboardInterrupt):
                monitor_mod.ask_yes_no("  测试？")

    def test_ask_priority_none_raises_keyboard_interrupt(self):
        # 同上：旧代码 raw.isdigit() 抛 AttributeError 崩
        import monitor as monitor_mod
        with mock.patch.object(monitor_mod, "read", return_value=None):
            with self.assertRaises(KeyboardInterrupt):
                monitor_mod.ask_priority()

    def test_ask_yes_no_normal_inputs_unchanged(self):
        # 回归 pin：正常输入行为不变（旧代码即通过）
        import monitor as monitor_mod
        with mock.patch.object(monitor_mod, "read", return_value="y"):
            self.assertTrue(monitor_mod.ask_yes_no("  测试？"))
        with mock.patch.object(monitor_mod, "read", return_value="n"):
            self.assertFalse(monitor_mod.ask_yes_no("  测试？", "y"))
        with mock.patch.object(monitor_mod, "read", return_value="是"):
            self.assertTrue(monitor_mod.ask_yes_no("  测试？", "n"))

    def test_ask_priority_normal_inputs_unchanged(self):
        # 回归 pin：正常输入行为不变（旧代码即通过）
        import monitor as monitor_mod
        with mock.patch.object(monitor_mod, "read", return_value="7"):
            self.assertEqual(monitor_mod.ask_priority(), 7)
        with mock.patch.object(monitor_mod, "read", return_value="5"):
            self.assertEqual(monitor_mod.ask_priority(), 5)


class TestMonitorInterruptRound2(TempDirCase):
    """Task 28 round 2 (P1): monitor.py 同文件同 pattern 漏网三处——
    input_dates 确认提示、menu_notify 端口/收件人输入在 Ctrl+C/EOF（read 返回 None）
    时抛 AttributeError 打 traceback。"""

    def test_input_dates_confirm_none_returns_none_none(self):
        # Ctrl+C/EOF 发生在"仍要创建？"确认时：旧代码 None.lower() 抛 AttributeError；
        # 应沿用本函数首个 read 的取消约定返回 (None, None)（调用方 menu_create_task
        # 已有 `if dates is None: 已取消创建` 处理）。
        import monitor as monitor_mod
        with mock.patch.object(monitor_mod, "read", side_effect=["2020-01-01", None]):
            self.assertEqual(monitor_mod.input_dates(), (None, None))

    def test_input_dates_normal_unchanged(self):
        # 回归 pin：正常输入行为不变（旧代码即通过）
        import monitor as monitor_mod
        with mock.patch.object(monitor_mod, "read", side_effect=["2099-01-01"]):
            dates, date_range = monitor_mod.input_dates()
            self.assertEqual(dates, ["2099-01-01"])

    def test_menu_notify_port_none_raises_keyboard_interrupt(self):
        # Ctrl+C/EOF 发生在端口输入时：旧代码 None.isdigit() 抛 AttributeError；
        # 应抛 KeyboardInterrupt，由 main_menu 接住（"已中断，返回主菜单。"）。
        # menu_notify 经 main_menu 分发，全部调用点在保护之下（round 1 已核验）。
        # load_config 被 mock：隔离被测单元（config.json 缺失是 Task 29 的范围）。
        import monitor as monitor_mod
        with mock.patch.object(monitor_mod, "load_config", return_value={}), \
             mock.patch.object(monitor_mod, "read", side_effect=["", None]):
            with self.assertRaises(KeyboardInterrupt):
                monitor_mod.menu_notify()

    def test_menu_notify_to_none_raises_keyboard_interrupt(self):
        # 同上：收件人输入时 Ctrl+C/EOF，旧代码 None.replace() 抛 AttributeError。
        # Task 50 后授权码走 getpass：read 序列少 1 个，getpass 需 mock。
        import monitor as monitor_mod
        with mock.patch.object(monitor_mod, "load_config", return_value={}), \
             mock.patch.object(monitor_mod, "read",
                               side_effect=["", "465", "", "", None]), \
             mock.patch("getpass.getpass", return_value="pw"):
            with self.assertRaises(KeyboardInterrupt):
                monitor_mod.menu_notify()

    def test_menu_notify_normal_port_unchanged(self):
        # 回归 pin：正常输入行为不变（旧代码即通过）；save_config 被 mock，
        # 不写真实 config.json，ask_yes_no 走第 6 个 read 返回 "n"，不发测试邮件。
        # Task 50 后授权码走 getpass：read 序列少 1 个，"pw" 改由 getpass 提供。
        import monitor as monitor_mod
        saved = {}
        with mock.patch.object(monitor_mod, "load_config", return_value={}), \
             mock.patch.object(monitor_mod, "read", side_effect=[
                 "", "587", "u@x.com", "", "a@x.com", "n"]), \
             mock.patch("getpass.getpass", return_value="pw"), \
             mock.patch.object(monitor_mod, "save_config",
                               side_effect=lambda c: saved.update(c)):
            monitor_mod.menu_notify()
        self.assertEqual(saved["notify"]["email"]["smtp_port"], 587)
        self.assertEqual(saved["notify"]["email"]["to"], ["a@x.com"])
        self.assertEqual(saved["notify"]["email"]["password"], "pw")


class TestMonitorInterruptRound3(TempDirCase):
    """Task 28 round 3 (P1): menu_passengers 编辑/删除/设默认的序号输入
    在 Ctrl+C/EOF（read 返回 None）时 None.isdigit() 抛 AttributeError
    打 traceback。应抛 KeyboardInterrupt，由 main_menu 接住。"""

    SAMPLE = [{"name": "张三", "id_type_code": "1", "id_no": "110101199001011234",
               "mobile": "13800138000", "is_default": False, "is_adult": True}]

    def _run_menu(self, reads):
        import monitor as monitor_mod
        pm = mock.MagicMock()
        pm.load_passengers.return_value = [dict(self.SAMPLE[0])]
        pm.ID_TYPE_NAMES = {"1": "二代身份证"}
        with mock.patch.object(monitor_mod, "passengers_mod", pm), \
             mock.patch.object(monitor_mod, "read", side_effect=list(reads)):
            monitor_mod.menu_passengers()

    def test_edit_index_none_raises_keyboard_interrupt(self):
        # "2" 进编辑分支，"编辑第几位"时 Ctrl+C：旧代码 None.isdigit() 抛 AttributeError
        with self.assertRaises(KeyboardInterrupt):
            self._run_menu(["2", None])

    def test_delete_index_none_raises_keyboard_interrupt(self):
        # "3" 进删除分支：旧代码同上
        with self.assertRaises(KeyboardInterrupt):
            self._run_menu(["3", None])

    def test_setdefault_index_none_raises_keyboard_interrupt(self):
        # "4" 进设默认分支：旧代码同上
        with self.assertRaises(KeyboardInterrupt):
            self._run_menu(["4", None])

    def test_menu_passengers_quit_unchanged(self):
        # 回归 pin：正常退出路径不变（旧代码即通过）
        self._run_menu(["0"])

    def test_edit_invalid_index_unchanged(self):
        # 回归 pin：非法序号走原逻辑（忽略并继续循环），随后 "0" 退出（旧代码即通过）
        self._run_menu(["2", "9", "0"])


class TestMonitorInterruptRound4(TempDirCase):
    """Task 28 round 4 (P1): read() 在 Ctrl+C/EOF 时返回 None（绕过 default），
    6 处把 None 直接存入乘车人/通知数据。修法：`or <原值>` 兜底，
    与本文件 :434（id_no）、:503（smtp_host）、:512（from）既有惯例一致。
    read() 回车走 default、Ctrl+C/EOF 才返回 None——兜底只改变后者，正常路径零漂移。"""

    SAMPLE = [{"name": "张三", "id_type_code": "1", "id_no": "110101199001011234",
               "mobile": "13800138000", "is_default": False, "is_adult": True}]

    def _run_passengers(self, reads):
        import monitor as monitor_mod
        pm = mock.MagicMock()
        pm.load_passengers.return_value = [dict(self.SAMPLE[0])]
        with mock.patch.object(monitor_mod, "passengers_mod", pm), \
             mock.patch.object(monitor_mod, "read", side_effect=list(reads)):
            monitor_mod.menu_passengers()
        return pm

    def test_edit_name_none_keeps_original(self):
        # 编辑时在"姓名"处 Ctrl+C：旧代码存 name=None；新代码保留原名
        pm = self._run_passengers(["2", "1", None, "", "13800138000", "y", "y", "0"])
        saved = pm.save_passengers.call_args[0][0]
        self.assertEqual(saved[0]["name"], "张三")

    def test_edit_mobile_none_keeps_original(self):
        # 编辑时在"手机号"处 Ctrl+C：旧代码存 mobile=None；新代码保留原号
        pm = self._run_passengers(["2", "1", "张三", "", None, "y", "y", "0"])
        saved = pm.save_passengers.call_args[0][0]
        self.assertEqual(saved[0]["mobile"], "13800138000")

    def test_add_idno_mobile_none_stored_empty(self):
        # 添加时在证件号/手机号处 Ctrl+C：旧代码存 None；新代码按 default 存 ""
        pm = self._run_passengers(["1", "李四", "1", None, None, "y", "y", "0"])
        saved = pm.save_passengers.call_args[0][0]
        new = saved[1]  # append 的新记录；saved[0] 是原有样本
        self.assertEqual(new["name"], "李四")
        self.assertEqual(new["id_no"], "")
        self.assertEqual(new["mobile"], "")

    def test_notify_username_password_none_keep_original(self):
        # 通知设置在发件邮箱/授权码处 Ctrl+C：旧代码存 None；新代码保留原值
        # Task 50 后授权码走 getpass：在授权码处模拟 Ctrl+C（getpass 抛
        # KeyboardInterrupt），用户名仍走 read 返回 None 走 or-保留。
        import monitor as monitor_mod
        cfg = {"notify": {"email": {"enabled": True, "smtp_host": "smtp.qq.com",
                "smtp_port": 465, "username": "old@x.com", "password": "oldsecret",
                "from": "", "to": []}}}
        saved_cfg = {}
        with mock.patch.object(monitor_mod, "load_config", return_value=cfg), \
             mock.patch.object(monitor_mod, "save_config",
                               side_effect=lambda c: saved_cfg.update(c)), \
             mock.patch.object(monitor_mod, "read",
                               side_effect=["", "465", None]), \
             mock.patch("getpass.getpass", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                monitor_mod.menu_notify()
        # 中断发生在保存前：save_config 未被调用，原配置未动。
        self.assertEqual(saved_cfg, {})
        self.assertEqual(cfg["notify"]["email"]["username"], "old@x.com")
        self.assertEqual(cfg["notify"]["email"]["password"], "oldsecret")

    def test_corrupt_name_none_crashes_task_join(self):
        # 危害演示（round 4 时：旧代码在末尾 join 抛 TypeError）。
        # Round 5 已在 join 前过滤 falsy 姓名并警告：不再抛 TypeError，
        # 脏记录被跳过，任务照常创建。本测试现锁定修复后行为。
        import monitor as monitor_mod
        corrupt = [dict(self.SAMPLE[0])]
        corrupt[0]["name"] = None  # pre-fix 的 menu_passengers 会存下这种脏记录
        pm = mock.MagicMock()
        pm.load_passengers.return_value = corrupt
        saved_cfg = {}
        with mock.patch.object(monitor_mod, "passengers_mod", pm), \
             mock.patch.object(monitor_mod, "pick_station", side_effect=["北京", "上海"]), \
             mock.patch.object(monitor_mod, "input_dates",
                               return_value=(["2026-10-09"], [])), \
             mock.patch.object(monitor_mod, "ticket") as mock_ticket, \
             mock.patch.object(monitor_mod, "pick_multi", return_value=["二等座"]), \
             mock.patch.object(monitor_mod, "read",
                               side_effect=["", "1", "5", "n", "n", "", "n"]), \
             mock.patch.object(monitor_mod, "load_config", return_value={}), \
             mock.patch.object(monitor_mod, "update_config_locked",
                               side_effect=lambda mut: saved_cfg.update(
                                   _capture_mut(mut))):
            mock_ticket.load_station_map.return_value = (
                {"北京": "BJP", "上海": "SHH"}, {"BJP": "北京", "SHH": "上海"})
            mock_ticket.query_tickets.side_effect = Exception("offline")
            monitor_mod.menu_create_task()  # 不再抛 TypeError
        tasks = saved_cfg.get("tasks", [])
        self.assertTrue(tasks)
        self.assertNotIn(None, tasks[0]["passenger_names"])


class TestMonitorInterruptRound5(TempDirCase):
    """Task 28 round 5 (P1): 历史脏数据——pre-fix 版本可能把 name=None 的乘车人
    记录存盘；建任务选中该记录后 `"、".join(passenger_names)` 抛 TypeError
    （崩溃点实测在 menu_create_task 末尾的 join）。修法：在 join 前过滤掉
    falsy 姓名并打一条 [警告]（让用户去主菜单 [4] 清理记录），不静默吞。"""

    DIRTY = {"name": None, "id_type_code": "1", "id_no": "110101199001011234",
             "mobile": "13800138000", "is_default": False, "is_adult": True}
    GOOD = {"name": "张三", "id_type_code": "1", "id_no": "110101199001011234",
            "mobile": "13800138000", "is_default": False, "is_adult": True}

    def _run_create_task(self, passengers, reads):
        import monitor as monitor_mod
        pm = mock.MagicMock()
        pm.load_passengers.return_value = [dict(p) for p in passengers]
        saved_cfg = {}
        printed = []
        # reads: 车次选择 / 乘车人选择 / 优先级 / 自动下单 / 下单后停止 /
        #        任务名 / 是否立即启动
        with mock.patch.object(monitor_mod, "passengers_mod", pm), \
             mock.patch.object(monitor_mod, "pick_station", side_effect=["北京", "上海"]), \
             mock.patch.object(monitor_mod, "input_dates",
                               return_value=(["2026-10-09"], [])), \
             mock.patch.object(monitor_mod, "ticket") as mock_ticket, \
             mock.patch.object(monitor_mod, "pick_multi", return_value=["二等座"]), \
             mock.patch.object(monitor_mod, "read", side_effect=list(reads)), \
             mock.patch.object(monitor_mod, "load_config", return_value={}), \
             mock.patch.object(monitor_mod, "update_config_locked",
                               side_effect=lambda mut: saved_cfg.update(
                                   _capture_mut(mut))), \
             mock.patch("builtins.print", side_effect=lambda *a: printed.append(" ".join(map(str, a)))):
            mock_ticket.load_station_map.return_value = (
                {"北京": "BJP", "上海": "SHH"}, {"BJP": "北京", "SHH": "上海"})
            mock_ticket.query_tickets.side_effect = Exception("offline")
            monitor_mod.menu_create_task()
        tasks = saved_cfg.get("tasks", [])
        return tasks[0] if tasks else None, printed

    def test_dirty_name_selected_no_crash(self):
        # 选中 name=None 的脏记录：旧代码在末尾 join 抛 TypeError；
        # 新代码不过崩，存盘的 passenger_names 不含 None，并打警告
        task, printed = self._run_create_task(
            [self.DIRTY], ["", "1", "5", "n", "n", "", "n"])
        self.assertIsNotNone(task)
        self.assertNotIn(None, task["passenger_names"])
        self.assertTrue(any("[警告]" in p and "姓名为空" in p for p in printed),
                        "应提示用户清理姓名为空的乘车人记录")

    def test_mixed_names_keep_good_drop_dirty(self):
        # 一好一脏：保留好姓名，只丢掉 None
        task, printed = self._run_create_task(
            [self.GOOD, self.DIRTY], ["", "1,2", "5", "n", "n", "", "n"])
        self.assertIsNotNone(task)
        self.assertEqual(task["passenger_names"], ["张三"])
        self.assertTrue(any("1 条" in p for p in printed))


class TestMonitorInterruptRound6(TempDirCase):
    """Task 28 round 6 (P1): menu_history 展示购票历史时，历史记录的 passengers
    含 None（pre-fix 脏任务文件曾写入历史）→ `"、".join(...)` 抛 TypeError。
    修法：join 前过滤 falsy 姓名；有丢弃时在末尾打一条 [警告]（与 round 5 同形，
    不静默吞）。"""

    def _run_history(self, records):
        import monitor as monitor_mod
        hist_path = os.path.join(self.tmp, "order_history.json")
        with open(hist_path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False)
        printed = []
        # os.path.join(HERE, abs_path) == abs_path，可直接指向临时文件
        with mock.patch.object(monitor_mod, "load_config",
                               return_value={"history_file": hist_path}), \
             mock.patch("builtins.print",
                        side_effect=lambda *a: printed.append(" ".join(map(str, a)))):
            monitor_mod.menu_history()
        return printed

    def _record(self, passengers):
        return {"time": "2026-10-08 10:00:00", "task": "t1",
                "date": "2026-10-09", "train": "G101",
                "from": "北京", "to": "上海", "seat": "二等座",
                "passengers": passengers,
                "result": "success", "message": "ok"}

    def test_dirty_passenger_name_in_history_no_crash(self):
        printed = self._run_history([self._record([None, "张三"])])
        out = "\n".join(printed)
        self.assertIn("张三", out)
        self.assertTrue(any("[警告]" in p for p in printed),
                        "应提示有姓名为空的历史记录被跳过显示")

    def test_clean_history_renders_and_no_warning(self):
        # 回归 pin：干净记录渲染与旧代码一致，且不打警告
        printed = self._run_history([self._record(["张三", "李四"])])
        out = "\n".join(printed)
        self.assertIn("张三、李四", out)
        self.assertFalse(any("[警告]" in p for p in printed))


class TestMonitorLoadConfigMissing(TempDirCase):
    """Task 29 (P1): 无 config.json 首跑，load_config 不得抛 FileNotFoundError 打 traceback；
    应打印友好引导并返回空配置（全部 6 处调用方均已按空配置降级；菜单[1]仍可创建首个任务并落盘，
    故不采用 sys.exit——退出会把首个任务创建流程一并杀死）。"""

    def test_missing_config_no_raise_returns_empty(self):
        # 旧代码：open 直接抛 FileNotFoundError，首跑即 traceback
        import monitor as monitor_mod
        missing = os.path.join(self.tmp, "config.json")
        with mock.patch.object(monitor_mod, "CONFIG_PATH", missing):
            cfg = monitor_mod.load_config()
        self.assertEqual(cfg, {})

    def test_missing_config_prints_friendly_hint(self):
        import monitor as monitor_mod
        missing = os.path.join(self.tmp, "config.json")
        with mock.patch.object(monitor_mod, "CONFIG_PATH", missing), \
             mock.patch("builtins.print") as p:
            monitor_mod.load_config()
        out = "\n".join(str(c.args[0]) for c in p.call_args_list if c.args)
        self.assertIn("未找到 config.json", out)
        self.assertIn("菜单 [1]", out)

    def test_existing_config_loads_unchanged(self):
        # 回归 pin：有文件时行为与旧代码一致
        import monitor as monitor_mod
        cfg_path = os.path.join(self.tmp, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"tasks": [{"name": "t"}]}, f)
        with mock.patch.object(monitor_mod, "CONFIG_PATH", cfg_path):
            cfg = monitor_mod.load_config()
        self.assertEqual(cfg["tasks"][0]["name"], "t")


class TestLoadOrdersQuarantine(TempDirCase):
    """Task 31: orders.json 损坏不再静默清库——挪档留证 + LOG.error，再返回空库。"""

    def _orders_path(self):
        import appcommon
        return os.path.join(self.tmp, "orders.json")

    def _bad_files(self, path):
        import glob
        return glob.glob(path + ".bad-*")

    def test_corrupt_orders_quarantined_not_silent(self):
        import appcommon
        p = self._orders_path()
        garbage = b"\x00\x01 not json \xff\xfe"
        with open(p, "wb") as f:
            f.write(garbage)
        with self.assertLogs("monitor", level="ERROR"):
            db = appcommon.load_orders(p)
        self.assertEqual(db, {"orders": {}})
        bad = self._bad_files(p)
        self.assertEqual(len(bad), 1, "损坏文件应被挪档留证")
        with open(bad[0], "rb") as f:
            self.assertEqual(f.read(), garbage)
        self.assertFalse(os.path.exists(p), "原损坏文件应已被挪走")

    def test_corrupt_orders_upsert_preserves_evidence(self):
        # 真实破坏链：损坏 → upsert 覆写 → 证据永久丢失
        import appcommon
        p = self._orders_path()
        garbage = b"{broken json"
        with open(p, "wb") as f:
            f.write(garbage)
        appcommon.upsert_order(p, "k1", {"order_no": "E123"})
        bad = self._bad_files(p)
        self.assertEqual(len(bad), 1, "upsert 前损坏证据必须留存")
        with open(bad[0], "rb") as f:
            self.assertEqual(f.read(), garbage)
        db = appcommon.load_orders(p)
        self.assertEqual(db["orders"]["k1"]["order_no"], "E123")

    def test_wrong_shape_orders_quarantined(self):
        # 合法 JSON 但形状非法（数组），同样不能静默覆写
        import appcommon
        p = self._orders_path()
        with open(p, "w", encoding="utf-8") as f:
            f.write("[1, 2, 3]")
        db = appcommon.load_orders(p)
        self.assertEqual(db, {"orders": {}})
        self.assertEqual(len(self._bad_files(p)), 1)

    def test_healthy_orders_untouched(self):
        # 回归 pin：健康文件行为不变，不挪档
        import appcommon
        p = self._orders_path()
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"orders": {"k": {"order_no": "E1"}}}, f)
        db = appcommon.load_orders(p)
        self.assertEqual(db["orders"]["k"]["order_no"], "E1")
        self.assertEqual(self._bad_files(p), [])


class TestAppendHistoryQuarantine(TempDirCase):
    """Task 32: history.json 损坏不再静默清空——挪档留证 + LOG.error，新记录仍能追加。"""

    def _hist_path(self):
        return os.path.join(self.tmp, "order_history.json")

    def _bad_files(self, path):
        import glob
        return glob.glob(path + ".bad-*")

    def test_corrupt_history_quarantined_new_record_appended(self):
        import appcommon
        p = self._hist_path()
        garbage = b"\x00\x01 not json \xff\xfe"
        with open(p, "wb") as f:
            f.write(garbage)
        rec = {"ts": "2026-10-08 10:00:00", "event": "order_ok"}
        with self.assertLogs("monitor", level="ERROR"):
            appcommon.append_history(p, rec)
        bad = self._bad_files(p)
        self.assertEqual(len(bad), 1, "损坏文件应被挪档留证")
        with open(bad[0], "rb") as f:
            self.assertEqual(f.read(), garbage)
        with open(p, encoding="utf-8") as f:
            history = json.load(f)
        self.assertEqual(history, [rec], "新记录应写入全新历史文件")

    def test_wrong_shape_history_quarantined(self):
        # 合法 JSON 但形状非法（dict），旧代码 .append 直接抛 AttributeError
        import appcommon
        p = self._hist_path()
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"not": "a list"}')
        rec = {"ts": "2026-10-08 10:00:00", "event": "order_ok"}
        appcommon.append_history(p, rec)
        self.assertEqual(len(self._bad_files(p)), 1, "形状非法同样挪档留证")
        with open(p, encoding="utf-8") as f:
            self.assertEqual(json.load(f), [rec])

    def test_healthy_history_append_works(self):
        # 回归 pin：健康文件行为不变，追加+截断 keep，不挪档
        import appcommon
        p = self._hist_path()
        with open(p, "w", encoding="utf-8") as f:
            json.dump([{"event": "a"}, {"event": "b"}], f)
        appcommon.append_history(p, {"event": "c"}, keep=2)
        with open(p, encoding="utf-8") as f:
            self.assertEqual(json.load(f), [{"event": "b"}, {"event": "c"}])
        self.assertEqual(self._bad_files(p), [])


class TestQuarantineToctou(TempDirCase):
    """Task 33: quarantine_corrupt TOCTOU —— 读失败后、挪档前若文件被另一进程
    改写为健康内容，必须放弃隔离（不能误伤健康文件丢防重记录）。"""

    def _write(self, path, data):
        with open(path, "w", encoding="utf-8") as f:
            f.write(data)

    def _bad_files(self, path):
        d = os.path.dirname(path)
        base = os.path.basename(path)
        return [f for f in os.listdir(d) if f.startswith(base + ".bad-")]

    def test_quarantine_skips_when_rewritten_after_failed_read(self):
        import appcommon
        p = os.path.join(self.tmp, "state.json")
        self._write(p, "CORRUPT{{{")                        # 损坏：10 字节
        fp = appcommon.stat_fingerprint(p)                 # 读失败瞬间的指纹
        healthy = '{"tasks": [], "dedup": {}}'              # 健康：24 字节
        self._write(p, healthy)                             # 另一进程在竞态窗口写入
        self.assertNotEqual(appcommon.stat_fingerprint(p), fp,
                            "测试前置：指纹必须已变化")
        bad = appcommon.quarantine_corrupt(p, fp)
        self.assertIsNone(bad, "文件已被改写，应放弃隔离")
        self.assertEqual(self._bad_files(p), [], "不得产生隔离文件")
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), healthy, "健康文件必须原样保留")

    def test_quarantine_proceeds_when_unchanged(self):
        import appcommon
        p = os.path.join(self.tmp, "state.json")
        garbage = "CORRUPT{{{"
        self._write(p, garbage)
        fp = appcommon.stat_fingerprint(p)
        bad = appcommon.quarantine_corrupt(p, fp)
        self.assertIsNotNone(bad, "未被改写时应正常隔离")
        self.assertFalse(os.path.exists(p))
        self.assertEqual(len(self._bad_files(p)), 1)
        with open(bad, encoding="utf-8") as f:
            self.assertEqual(f.read(), garbage, "证据逐字节保留")

    def test_load_orders_toctou_end_to_end(self):
        # 端到端：经 load_orders 真实调用链复现竞态 —— 损坏读失败后、
        # quarantine 执行前"另一进程"写入健康文件，健康文件不得被挪走。
        # （round 2 更新：放弃隔离后 load_orders 重读一次返回新鲜数据，
        # 不再返回空库——旧断言 db == {} 已过时。）
        import appcommon
        p = os.path.join(self.tmp, "orders.json")
        self._write(p, "CORRUPT{{{")
        healthy = {"orders": {"G123|2026-10-09": {"order_no": "E123"}}}
        orig = appcommon.quarantine_corrupt

        def sneaky(path, fp=None):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(healthy, f)          # 竞态窗口：第二进程写入健康文件
            return orig(path, fp)

        with mock.patch.object(appcommon, "quarantine_corrupt", sneaky):
            db = appcommon.load_orders(p)
        self.assertEqual(db, healthy, "放弃隔离后应重读返回新鲜数据")
        with open(p, encoding="utf-8") as f:
            self.assertEqual(json.load(f), healthy, "健康文件不得被隔离挪走")
        self.assertEqual(self._bad_files(p), [], "不得产生隔离文件")


class TestQuarantineAbandonReread(TempDirCase):
    """Task 33 round 2: quarantine 因"文件自读失败后被改写"而放弃隔离后，
    调用方必须重读一次用新鲜数据继续，绝不能用过期空读数覆盖健康文件。"""

    def _write(self, path, data):
        with open(path, "w", encoding="utf-8") as f:
            f.write(data)

    def _bad_files(self, path):
        d = os.path.dirname(path)
        base = os.path.basename(path)
        return [f for f in os.listdir(d) if f.startswith(base + ".bad-")]

    def _sneaky_rewrite(self, path, new_content):
        """包装 quarantine_corrupt：在其执行前用新内容改写文件，模拟竞态窗口。"""
        import appcommon
        orig = appcommon.quarantine_corrupt

        def sneaky(p, fp=None):
            with open(p, "w", encoding="utf-8") as f:
                f.write(new_content)
            return orig(p, fp)

        return mock.patch.object(appcommon, "quarantine_corrupt", sneaky)

    def test_append_history_abandon_rereads_instead_of_overwriting(self):
        import appcommon
        p = os.path.join(self.tmp, "order_history.json")
        self._write(p, "{CORRUPT")
        healthy = [{"train": "G1"}]
        rec = {"train": "G2"}
        with self._sneaky_rewrite(p, json.dumps(healthy)):
            with self.assertLogs("monitor", level="WARNING") as logs:
                appcommon.append_history(p, rec)
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data, healthy + [rec],
                         "放弃隔离后必须重读新鲜数据再追加，不能用空列表覆盖健康文件")
        self.assertEqual(self._bad_files(p), [], "放弃隔离不得产生隔离文件")
        out = "\n".join(logs.output)
        self.assertIn("放弃隔离", out)
        self.assertNotIn("已隔离留证：None", out, "日志不得再出现误导性的已隔离留证：None")

    def test_load_orders_abandon_upsert_preserves_healthy(self):
        import appcommon
        p = os.path.join(self.tmp, "orders.json")
        self._write(p, "{CORRUPT")
        healthy = {"orders": {"G123|2026-10-09": {"order_no": "E123"}}}
        with self._sneaky_rewrite(p, json.dumps(healthy)):
            appcommon.upsert_order(p, "G456|2026-10-10", {"order_no": "E456"})
        with open(p, encoding="utf-8") as f:
            db = json.load(f)
        self.assertIn("G123|2026-10-09", db["orders"], "健康旧记录不得被空库覆盖丢失")
        self.assertIn("G456|2026-10-10", db["orders"], "新记录必须登记")
        self.assertEqual(self._bad_files(p), [], "放弃隔离不得产生隔离文件")

    def test_append_history_shape_invalid_abandon_rereads(self):
        import appcommon
        p = os.path.join(self.tmp, "order_history.json")
        self._write(p, '{"not": "a list"}')
        healthy = [{"train": "G7"}]
        rec = {"train": "G8"}
        with self._sneaky_rewrite(p, json.dumps(healthy)):
            appcommon.append_history(p, rec)
        with open(p, encoding="utf-8") as f:
            self.assertEqual(json.load(f), healthy + [rec])
        self.assertEqual(self._bad_files(p), [])

    def test_abandon_reread_failure_falls_back_safely(self):
        # 放弃隔离后重读仍失败（文件被删）：不崩、不循环，回退空数据继续
        import appcommon
        orig = appcommon.quarantine_corrupt

        def sneaky_delete(path, fp=None):
            os.remove(path)
            return orig(path, fp)

        p = os.path.join(self.tmp, "order_history.json")
        self._write(p, "{CORRUPT")
        with mock.patch.object(appcommon, "quarantine_corrupt", sneaky_delete):
            appcommon.append_history(p, {"train": "G9"})
        with open(p, encoding="utf-8") as f:
            self.assertEqual(json.load(f), [{"train": "G9"}])
        p2 = os.path.join(self.tmp, "orders.json")
        self._write(p2, "{CORRUPT")
        with mock.patch.object(appcommon, "quarantine_corrupt", sneaky_delete):
            self.assertEqual(appcommon.load_orders(p2), {"orders": {}})

    def test_move_failure_log_distinguishes_from_abandon(self):
        # 挪移失败（非放弃）：日志必须说"挪移失败"而非"放弃隔离"，证据保留原地
        import appcommon
        import appcommon as ac
        p = os.path.join(self.tmp, "orders.json")
        self._write(p, "{CORRUPT")
        with mock.patch.object(ac.os, "replace", side_effect=OSError("busy")):
            with self.assertLogs("monitor", level="WARNING") as logs:
                self.assertEqual(ac.load_orders(p), {"orders": {}})
        out = "\n".join(logs.output)
        self.assertIn("挪移失败", out)
        self.assertNotIn("放弃隔离", out)
        self.assertNotIn("已隔离留证：None", out)
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{CORRUPT",
                             "挪移失败时证据保留原地")


class TestLoadStateReadLock(unittest.TestCase):
    """Task 34: _load_state 读 state.json 时必须持有与写侧相同的 file_lock。

    背景：_save_state 用 fallback_direct=True，os.replace 重试耗尽后退化为
    非原子直写（open "w" 截断后再写）。读侧不持锁可能撞上写一半的撕裂文件
    → json 误判损坏 → 健康 state.json 被隔离。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t34_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_config(self, state_obj):
        sp = os.path.join(self.tmp, "state.json")
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(state_obj, f, ensure_ascii=False)
        cfg = {"tasks": [],
               "state_file": sp,
               "history_file": os.path.join(self.tmp, "order_history.json")}
        p = os.path.join(self.tmp, "config.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        return p

    def _make_engine(self, state_obj):
        cfg_path = self._write_config(state_obj)
        with mock.patch.object(ticket, "load_station_map",
                               return_value=({}, {})):
            return engine_mod.MonitorEngine(config_path=cfg_path,
                                            setup_logging=False)

    def test_load_state_reads_under_state_lock(self):
        # 读瞬间必须处于 file_lock(state.json.lock) 上下文内（与 _save_state 同一把）
        cfg_path = self._write_config({"dedup": {"k": 1},
                                       "tasks": {}, "retry": {}})
        state_path = os.path.join(self.tmp, "state.json")
        lock_path = state_path + ".lock"
        in_lock_during_read = []
        flag = {"in_lock": False}
        real_file_lock = filelock.file_lock
        real_read = appcommon.read_state_or_none

        @contextlib.contextmanager
        def spy_lock(path, timeout=10.0):
            if path == lock_path:
                flag["in_lock"] = True
            try:
                with real_file_lock(path, timeout=timeout):
                    yield
            finally:
                if path == lock_path:
                    flag["in_lock"] = False

        def spy_read(path):
            in_lock_during_read.append(flag["in_lock"])
            return real_read(path)

        with mock.patch.object(filelock, "file_lock", spy_lock), \
             mock.patch.object(appcommon, "read_state_or_none", spy_read), \
             mock.patch.object(ticket, "load_station_map",
                               return_value=({}, {})):
            engine_mod.MonitorEngine(config_path=cfg_path,
                                     setup_logging=False)

        self.assertTrue(in_lock_during_read, "read_state_or_none 没有被调用到")
        self.assertTrue(
            all(in_lock_during_read),
            "Task34: _load_state 读 state.json 时未持有写侧同把 file_lock")

    def test_load_state_waits_for_writer_lock(self):
        # 写侧持锁慢写时，读侧必须阻塞等待，不能直接读半截文件
        e = self._make_engine({"dedup": {}, "tasks": {}, "retry": {}})
        lock_path = e.state_path + ".lock"
        entered = threading.Event()

        def slow_writer():
            with filelock.file_lock(lock_path):
                entered.set()
                time.sleep(1.0)  # 模拟 fallback_direct 慢速直写

        t = threading.Thread(target=slow_writer)
        t.start()
        try:
            self.assertTrue(entered.wait(timeout=5), "写线程未能拿到锁")
            # 屏蔽 _save_state：只测"读"是否等待写锁（否则旧代码也会因
            # _save_state 持锁而被动等待，测不出读侧问题）
            with mock.patch.object(e, "_save_state"):
                start = time.monotonic()
                e._load_state()
                elapsed = time.monotonic() - start
        finally:
            t.join(timeout=5)
        self.assertGreaterEqual(
            elapsed, 0.8,
            "读未等待写锁（%.2fs），可能读到撕裂文件" % elapsed)


class TestReloadStateReadLock(unittest.TestCase):
    """Task 34 round 2: _reload_state（_sync_state 的热重载路径）读 state.json
    时同样必须持有与写侧相同的 file_lock。

    背景：round 1 只修了 _load_state。_reload_state 是裸 open + json.load，
    撞上 _save_state fallback_direct 的撕裂写 → 异常被吞 → self.state = {}
    → 后续某处无参 _save_state() 把空状态落盘 → 防重永久丢失 → 可能重复下单。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t34r2_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_config(self, state_obj):
        sp = os.path.join(self.tmp, "state.json")
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(state_obj, f, ensure_ascii=False)
        cfg = {"tasks": [],
               "state_file": sp,
               "history_file": os.path.join(self.tmp, "order_history.json")}
        p = os.path.join(self.tmp, "config.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        return p

    def _make_engine(self, state_obj):
        cfg_path = self._write_config(state_obj)
        with mock.patch.object(ticket, "load_station_map",
                               return_value=({}, {})):
            return engine_mod.MonitorEngine(config_path=cfg_path,
                                            setup_logging=False)

    def test_reload_state_reads_under_state_lock(self):
        # 读瞬间必须处于 file_lock(state.json.lock) 上下文内（与 _save_state 同一把）
        e = self._make_engine({"dedup": {"k": 1},
                               "tasks": {}, "retry": {}})
        lock_path = e.state_path + ".lock"
        in_lock_during_read = []
        flag = {"in_lock": False}
        real_file_lock = filelock.file_lock
        real_json_load = json.load

        @contextlib.contextmanager
        def spy_lock(path, timeout=10.0):
            if path == lock_path:
                flag["in_lock"] = True
            try:
                with real_file_lock(path, timeout=timeout):
                    yield
            finally:
                if path == lock_path:
                    flag["in_lock"] = False

        def spy_load(fp, *a, **k):
            in_lock_during_read.append(flag["in_lock"])
            return real_json_load(fp, *a, **k)

        with mock.patch.object(filelock, "file_lock", spy_lock), \
             mock.patch.object(engine_mod.json, "load", spy_load):
            e._reload_state()

        self.assertTrue(in_lock_during_read, "json.load 没有被调用到")
        self.assertTrue(
            all(in_lock_during_read),
            "Task34r2: _reload_state 读 state.json 时未持有写侧同把 file_lock")

    def test_reload_state_waits_for_writer_lock(self):
        # 写侧持锁慢写时，读侧必须阻塞等待，不能直接读半截文件
        e = self._make_engine({"dedup": {}, "tasks": {}, "retry": {}})
        lock_path = e.state_path + ".lock"
        entered = threading.Event()

        def slow_writer():
            with filelock.file_lock(lock_path):
                entered.set()
                time.sleep(1.0)  # 模拟 fallback_direct 慢速直写

        t = threading.Thread(target=slow_writer)
        t.start()
        try:
            self.assertTrue(entered.wait(timeout=5), "写线程未能拿到锁")
            start = time.monotonic()
            e._reload_state()
            elapsed = time.monotonic() - start
        finally:
            t.join(timeout=5)
        self.assertGreaterEqual(
            elapsed, 0.8,
            "读未等待写锁（%.2fs），可能读到撕裂文件" % elapsed)



class TestOrderTimestamp(TempDirCase):
    """Task 36: 订单时间戳必须按北京时间解析（与机器时区无关），
    find_recent_order 必须有上界（未来订单不归因）。"""

    def setUp(self):
        super().setUp()
        self._old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"   # 模拟 UTC 机器：旧代码 time.mktime 会偏 8 小时
        if hasattr(time, "tzset"):  # Unix-only；Windows 无 tzset（CRT 本就忽略 TZ）
            time.tzset()

    def tearDown(self):
        if self._old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._old_tz
        if hasattr(time, "tzset"):  # Unix-only；Windows 无 tzset
            time.tzset()
        super().tearDown()

    @staticmethod
    def _true_epoch():
        # "2026-10-08 10:00:00" 北京时间的真实 epoch
        return datetime.datetime(
            2026, 10, 8, 10, 0, 0,
            tzinfo=datetime.timezone(datetime.timedelta(hours=8))).timestamp()

    def test_parse_bj_wall_utc_machine(self):
        ts = order_mod.parse_bj_wall("2026-10-08 10:00:00")
        self.assertIsNotNone(ts)
        self.assertLess(abs(ts - self._true_epoch()), 60,
                        "UTC 机器上解析偏差 %s 秒，应 < 60 秒" % abs(ts - self._true_epoch()))

    def test_normalize_order_item_ts_independent_of_machine_tz(self):
        item = {"order_date": "2026-10-08 10:00:00", "sequence_no": "E123",
                "train_code_page": "G101", "start_train_date_page": "2026-10-08",
                "passengerDTOList": []}
        norm = order_mod._normalize_order_item(item, "")
        self.assertIsNotNone(norm["order_ts"])
        self.assertLess(abs(norm["order_ts"] - self._true_epoch()), 60,
                        "UTC 机器上 order_ts 偏差 %s 秒，应 < 60 秒"
                        % abs(norm["order_ts"] - self._true_epoch()))

    def test_find_recent_order_rejects_far_future(self):
        # 未来 8 小时的"旧单"（旧代码在 UTC 机器上把北京时间串偏成未来 8h）
        # 绝不能被归因为"本次提交生成"
        now = time.time()
        orders = [{"train": "G101", "date": "2026-10-08", "passengers": ["张三"],
                   "order_no": "E999", "order_ts": now + 8 * 3600}]
        self.assertIsNone(order_mod.find_recent_order(
            orders, "2026-10-08", "G101", ["张三"], now))

    def test_find_recent_order_normal_window_still_matches(self):
        # 回归 pin：窗口内的正常订单仍被归因（旧代码本就通过）
        now = time.time()
        orders = [{"train": "G101", "date": "2026-10-08", "passengers": ["张三"],
                   "order_no": "E100", "order_ts": now + 60}]
        got = order_mod.find_recent_order(orders, "2026-10-08", "G101", ["张三"], now)
        self.assertIsNotNone(got)
        self.assertEqual(got["order_no"], "E100")


class TestTrainCodeCase(TempDirCase):
    """Task 38 (P2): 车次大小写三处统一 —— 小写配置必须命中大写的 train_code，
    否则 12306（恒大写）返回的车次被静默漏掉。"""

    def test_engine_lowercase_trains_match_uppercase_code(self):
        # engine.py:512 只 strip 不 upper → 小写 k225 配大写 K225 静默漏单
        e = make_engine(self.tmp)
        t = task_of(trains=["k225"])
        e.tasks = [t]
        e.state["tasks"][t["name"]] = {"status": "monitoring", "fail_streak": 0}
        with mock.patch.object(e, "_query_with_retry",
                               return_value=[synthetic_row()]), \
             mock.patch.object(engine_mod.order_mod, "order_ticket",
                               return_value=(True, "ok 订单号: E123",
                                             {"order_no": "E123",
                                              "passengers": "张三"})) as m_order, \
             mock.patch.object(engine_mod.notify_mod, "send_email",
                               return_value=(True, "ok")):
            e._run_task(t)
        # 旧代码：车次被跳过，order_ticket 一次都没调 → 漏单
        self.assertEqual(m_order.call_count, 1)

    def test_monitor_menu_create_task_uppercases_trains(self):
        # monitor.py:237 同上 —— 菜单里输小写 k225，落盘任务应为大写
        import monitor as monitor_mod
        reads = iter(["长葛", "确山", "2026-12-01", "k225", "1",
                      "5", "y", "y", "", "n"])
        captured = {}
        with mock.patch.object(monitor_mod, "read",
                               side_effect=lambda *a, **k: next(reads)), \
             mock.patch.object(monitor_mod.ticket, "load_station_map",
                               return_value=({"长葛": "VNP", "确山": "ZAF"},
                                             {"VNP": "长葛", "ZAF": "确山"})), \
             mock.patch.object(monitor_mod.ticket, "query_tickets",
                               return_value=[synthetic_row()]), \
             mock.patch.object(monitor_mod.passengers_mod, "load_passengers",
                               return_value=[]), \
             mock.patch.object(monitor_mod, "load_config", return_value={}), \
             mock.patch.object(monitor_mod, "update_config_locked",
                               side_effect=lambda mut: captured.update(
                                   _capture_mut(mut))):
            monitor_mod.menu_create_task()
        # 旧代码：task["trains"] == ["k225"]，断言失败
        self.assertEqual(captured["tasks"][-1]["trains"], ["K225"])

    def test_ticket_main_lowercase_filter_matches(self):
        # ticket.py:548 CLI —— 小写过滤器应命中大写 train_code
        import io
        argv = ["ticket.py", "长葛", "确山", "2026-10-10", "k225"]
        with mock.patch.object(ticket, "load_station_map",
                               return_value=({"长葛": "VNP", "确山": "ZAF"},
                                             {"VNP": "长葛", "ZAF": "确山"})), \
             mock.patch.object(ticket, "query_tickets",
                               return_value=[synthetic_row()]), \
             mock.patch.object(sys, "argv", argv), \
             contextlib.redirect_stdout(io.StringIO()) as buf:
            ticket.main()
        out = buf.getvalue()
        # 旧代码：K225 被过滤器跳过，输出里没有 K225 行
        self.assertIn("K225", out)


class TestSessionCheckTransient(TempDirCase):
    """Task 39: 浏览器分支会话体检区分 transient/permanent。

    session_ok 对 goto 超时等任何异常都返回 (False, "校验异常: ...")；
    这是 12306 偶发卡顿类的瞬时故障，任务不得被标 failed，只走冷却重试。
    只有明确的会话失效才标记 failed。
    """

    def _browser_engine(self):
        e = make_engine(self.tmp)
        e.config["order_mode"] = "browser"
        t = task_of("t1")
        e.tasks = [t]
        e.state["tasks"]["t1"] = {"status": "monitoring"}
        e._last_session_check = 0  # 距上次超过 20 分钟，本次必体检
        return e, t

    def test_check_exception_is_transient(self):
        # 旧代码：permanent = not ok → 任务被标 failed，需人工逐个恢复
        e, t = self._browser_engine()
        with mock.patch.object(browser_order, "busy", return_value=False), \
             mock.patch.object(browser_order, "check_session",
                               return_value=(False, "校验异常: page.goto 超时")):
            e.check_session_if_needed()
        self.assertEqual(e.task_status(t), "monitoring")

    def test_genuine_session_invalid_is_permanent(self):
        # 明确的会话失效仍标记 failed（回归 pin，旧代码即通过）
        e, t = self._browser_engine()
        with mock.patch.object(browser_order, "busy", return_value=False), \
             mock.patch.object(browser_order, "check_session",
                               return_value=(False, "接口返回 status=false（会话已失效）")):
            e.check_session_if_needed()
        self.assertEqual(e.task_status(t), "failed")


class TestLoadStateErrorSplit(TempDirCase):
    """Task 40: _load_state 区分"真坏档"与"瞬时占用"。

    旧代码：PermissionError（重试耗尽）与 JSON 损坏共用 (None, err) 返回，
    _load_state 不区分直接隔离完好的 state.json 并重建空状态 → 丢防重记录。
    新行为：仅 JSON 解析失败才 quarantine_corrupt；OSError/PermissionError
    走"本次不加载、稍后重试"，不挪档、不重建、不落盘。
    """

    def _engine_with_state(self, state_obj):
        e = make_engine(self.tmp)
        sp = e.state_path
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(state_obj, f, ensure_ascii=False)
        e.state = dict(state_obj)
        return e, sp

    def test_permission_error_no_quarantine_keeps_old_state(self):
        # 旧代码：PermissionError 也被当坏档隔离 → 健康文件被挪走、防重丢失
        import appcommon
        old = {"dedup": {"k": 1}, "tasks": {}, "retry": {}}
        e, sp = self._engine_with_state(old)
        with mock.patch.object(appcommon, "read_state_or_none",
                               return_value=(None, PermissionError("被占用"))), \
             mock.patch.object(appcommon, "quarantine_corrupt") as m_q:
            got = e._load_state()
        m_q.assert_not_called()
        self.assertEqual(got, old)
        # 磁盘文件原样未动（未被隔离、未被空状态覆盖）
        with open(sp, encoding="utf-8") as f:
            self.assertEqual(json.load(f), old)

    def test_generic_oserror_no_quarantine(self):
        # 非 PermissionError 的 OSError（如文件竞态消失）同样不得隔离
        import appcommon
        old = {"dedup": {"k": 2}, "tasks": {}, "retry": {}}
        e, sp = self._engine_with_state(old)
        with mock.patch.object(appcommon, "read_state_or_none",
                               return_value=(None, OSError("I/O error"))), \
             mock.patch.object(appcommon, "quarantine_corrupt") as m_q:
            got = e._load_state()
        m_q.assert_not_called()
        self.assertEqual(got, old)

    def test_json_decode_error_still_quarantines(self):
        # 真坏档（JSON 解析失败）仍走隔离留证（回归 pin，旧代码即如此）
        import appcommon
        old = {"dedup": {"k": 3}, "tasks": {}, "retry": {}}
        e, sp = self._engine_with_state(old)
        err = json.JSONDecodeError("Expecting value", "{corrupt", 0)
        with mock.patch.object(appcommon, "read_state_or_none",
                               return_value=(None, err)), \
             mock.patch.object(appcommon, "quarantine_corrupt",
                               return_value=sp + ".bad-1") as m_q:
            got = e._load_state()
        m_q.assert_called_once()
        self.assertEqual(got["dedup"], {})


class TestHistoryAppendConcurrency(TempDirCase):
    """Task 41: engine 自家 _append_history（进程内锁 A）与
    appcommon.append_history（进程内锁 B）写同一 order_history.json →
    读-改-写竞态丢记录。修复：删 engine 版、统一调 appcommon 版，
    后者用 filelock.file_lock(path) 包住整个读-改-写。"""

    def _engine_writer(self, fake_self, path):
        # engine 侧追加入口：旧代码是 MonitorEngine._append_history（独立锁），
        # Task 41 后已统一为 appcommon.append_history；取当前实现的入口。
        import appcommon
        eng_append = getattr(engine_mod.MonitorEngine, "_append_history", None)
        if eng_append is not None:
            return lambda rec: eng_append(fake_self, rec)
        return lambda rec: appcommon.append_history(path, rec)

    def test_two_writers_no_records_lost(self):
        import appcommon, time
        p = os.path.join(self.tmp, "order_history.json")
        orig_write = appcommon.atomic_write_json

        def slow_write(path, obj, **kw):
            time.sleep(0.02)  # 拉大读-改-写窗口，让交错必然发生
            return orig_write(path, obj, **kw)

        class FakeEngine:
            pass
        fake = FakeEngine()
        fake.history_path = p
        fake._history_lock = threading.Lock()
        engine_writer = self._engine_writer(fake, p)

        errs = []

        def run_writer(writer, tag):
            try:
                for n in range(50):
                    writer({"w": tag, "n": n})
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        import appcommon as ac
        with mock.patch.object(ac, "atomic_write_json", slow_write):
            ta = threading.Thread(target=run_writer, args=(engine_writer, "engine"))
            tb = threading.Thread(target=run_writer,
                                  args=(lambda rec: ac.append_history(p, rec), "launcher"))
            ta.start()
            tb.start()
            ta.join()
            tb.join()

        self.assertEqual(errs, [])
        with open(p, encoding="utf-8") as f:
            got = json.load(f)
        self.assertEqual(len(got), 100, "双写方并发追加不应丢记录，实得 %d 条" % len(got))
        self.assertEqual(
            sorted((r["w"], r["n"]) for r in got),
            sorted([("engine", n) for n in range(50)]
                   + [("launcher", n) for n in range(50)]))

    def test_engine_append_unified(self):
        # engine 不再保留独立的 _append_history（已统一调 appcommon.append_history）
        self.assertFalse(hasattr(engine_mod.MonitorEngine, "_append_history"))

    def test_append_history_holds_file_lock(self):
        # 整个读-改-写包在 filelock.file_lock(path) 内：持锁时另一线程追加必须等待
        import appcommon, filelock, time
        p = os.path.join(self.tmp, "order_history.json")
        appcommon.append_history(p, {"n": 0})
        acquired = []
        with filelock.file_lock(p):
            t = threading.Thread(
                target=lambda: (appcommon.append_history(p, {"n": 1}),
                                acquired.append(True)))
            t.start()
            time.sleep(0.3)
            self.assertEqual(acquired, [], "持锁期间另一线程的追加应被阻塞")
        t.join(timeout=15)
        self.assertEqual(acquired, [True])
        with open(p, encoding="utf-8") as f:
            got = json.load(f)
        self.assertEqual(len(got), 2)


class TestSlideWaitDeadlineCompensation(TempDirCase):
    """Task 42: 滑块等待不消耗业务倒计时。

    旧代码：确认窗 15 秒循环与结果等待 deadline 都在滑块等待之前定死；
    用户手动过滑块（最长 180 秒）回来后，确认按钮永远点不下去、
    结果等待很快报超时——日志却是普通失败，误导排查。
    新行为：_wait_slide_gone 返回实际等待秒数；确认窗循环在滑块消失后
    重置 t_confirm，结果等待循环 deadline += elapsed。
    无真实浏览器：用假页面 + 假时钟驱动 _order_impl 全流程。
    """

    # ---------- 测试替身 ----------
    class _Clock:
        """可手动拨动的假时钟，整体替换 browser_order.time。"""
        def __init__(self):
            self.now = 1_700_000_000.0

        def time(self):
            return self.now

        def sleep(self, s):
            self.now += s

        def perf_counter(self):
            return self.now

        def strftime(self, fmt, t=None):
            return time.strftime(
                fmt, time.localtime(self.now if t is None else t))

        def localtime(self, t=None):
            return time.localtime(self.now if t is None else t)

    class _Locator:
        def __init__(self, count=0, visible=False, evaluate_result=None):
            self._count = count
            self._visible = visible
            self._evaluate_result = evaluate_result

        @property
        def first(self):
            return self

        def count(self):
            return self._count

        def is_visible(self):
            return self._visible

        def evaluate(self, js):
            return self._evaluate_result

    class _Page:
        """按脚本走完「确认窗→结果等待」的假页面。"""
        INIT_URL = "https://kyfw.12306.cn/otn/confirmPassenger/initDc"
        PAY_URL = "https://kyfw.12306.cn/otn/payOrder/init?sequence_no=E123"

        def __init__(self, clock):
            self._clock = clock
            self.qr_submit_clicked = False
            self.submit_order_clicked = False
            # 确认窗第 1 轮：倒计时未结束(btn92)；滑块回来后：已启用(btn92s)
            self._qr_states = [
                {"found": True, "cls": "btn92", "shown": True},
                {"found": True, "cls": "btn92s", "shown": True},
            ]
            self._slide_ups = [["nc-container"], []]  # 确认窗里滑块出现一次
            self._result_slide_seen = False  # 结果等待里滑块也只出现一次
            self._result_t0 = None  # 结果循环第一次读 url 时打点

        def set_default_timeout(self, ms):
            pass

        def wait_for_timeout(self, ms):
            self._clock.now += ms / 1000.0

        def wait_for_url(self, *a, **k):
            pass

        def wait_for_selector(self, *a, **k):
            pass

        def wait_for_function(self, *a, **k):
            pass

        def locator(self, sel):
            if sel == "#slide_passcode":
                if self._result_slide_seen:
                    return TestSlideWaitDeadlineCompensation._Locator()
                self._result_slide_seen = True
                return TestSlideWaitDeadlineCompensation._Locator(
                    count=1, visible=True)
            if sel == "#seatType_1":
                return TestSlideWaitDeadlineCompensation._Locator(
                    count=1, visible=True,
                    evaluate_result=[{"v": "WZ", "t": "无座"}])
            return TestSlideWaitDeadlineCompensation._Locator()

        def eval_on_selector_all(self, sel, js):
            if sel.startswith("#normal_passenger_id"):
                return [{"id": "p1", "text": "张三"}]
            return []  # 旧确认控件回退：没有可点的

        @property
        def url(self):
            if self._result_t0 is None:
                self._result_t0 = self._clock.now
                return self.INIT_URL
            # 出票结果在结果等待滑块点之后 +80s 才出现：旧 deadline 在滑块后
            # 只剩约 30s，等不到；延长后的 deadline（剩约 90s）才等得到
            if self._clock.now - self._result_t0 >= 80:
                return self.PAY_URL
            return self.INIT_URL

        def evaluate(self, js, arg=None):
            if "dialog_xsertcj" in js:
                return None  # 无学生票询问弹窗
            if "checkticketinfo_id" in js:
                return ""  # 核对窗原文为空：跳过席别对账分支
            if "qr_submit_id" in js:
                if "click" in js:
                    self.qr_submit_clicked = True
                    return None
                if self._qr_states:
                    return self._qr_states.pop(0)
                return {"found": True, "cls": "btn92s", "shown": True}
            if "queryLeftTable" in js:
                return True  # 点中预订
            if "slide_passcode" in js and "nc-container" in js:
                if self._slide_ups:
                    return self._slide_ups.pop(0)
                return []
            if "ticketType_" in js:
                return {"before": ["1"], "after": ["1"], "map": {}}
            if "seatType_1" in js:
                return {}
            if "tt: String" in js:
                return [{"name": "张三", "tt": "1"}]
            if "limit_tickets" in js:
                return [{"name": "张三", "seat": "WZ", "ticket_type": "1"}]
            if js.startswith("(id) =>"):
                return None  # 勾选乘车人 click
            if js.startswith("(ids) =>"):
                return []  # 勾选回读：全部已勾上
            if "document.body" in js:
                return ""
            if "#submitOrder_id" in js:
                self.submit_order_clicked = True
                return None
            raise AssertionError("假页面遇到未预期的 evaluate: %r" % js[:80])

    class _Warm:
        def __init__(self, page):
            self.p = object()
            self.ctx = object()
            self.page = page

        def usable(self):
            return True

        def refresh(self, info):
            pass

    class _SlidePage:
        """只测 _wait_slide_gone 合约的极简页面。"""
        def __init__(self, clock, present):
            self._clock = clock
            self._present = present

        def wait_for_timeout(self, ms):
            self._clock.now += ms / 1000.0

        def evaluate(self, js, arg=None):
            return ["nc-container"] if self._present else []

    # ---------- 准备 ----------
    def _stub_playwright(self):
        import sys
        import types as _types
        pw = _types.ModuleType("playwright")
        pw_sync = _types.ModuleType("playwright.sync_api")

        def _no_playwright():
            raise AssertionError("预热路径不应启动 playwright")

        pw_sync.sync_playwright = _no_playwright
        pw.sync_api = pw_sync
        self._pw_mods = {"playwright": pw, "playwright.sync_api": pw_sync}
        for name, mod in self._pw_mods.items():
            sys.modules[name] = mod
        self.addCleanup(self._unstub_playwright)

    def _unstub_playwright(self):
        import sys
        for name in self._pw_mods:
            sys.modules.pop(name, None)

    def _run_order(self):
        """滑块耗时 60s 的一次完整下单（确认窗 + 结果等待各触发一次滑块）。"""
        clock = self._Clock()
        page = self._Page(clock)

        def fake_wait_slide_gone(p, sec):
            clock.now += 60.0  # 模拟用户手动过滑块花了 60 秒
            return 60.0

        self._stub_playwright()
        info = {"train_code": "G101", "from_name": "北京", "to_name": "上海",
                "from_code": "VNP", "to_code": "SHH"}
        with mock.patch.object(browser_order, "time", clock), \
             mock.patch.object(browser_order, "_wait_slide_gone",
                               side_effect=fake_wait_slide_gone):
            ok, msg, extra = browser_order._order_impl(
                info, "无座", "WZ", ["张三"], "2026-10-10",
                warm=self._Warm(page))
        return ok, msg, extra, page

    # ---------- 断言 ----------
    def test_wait_slide_gone_returns_elapsed(self):
        # helper 合约：成功返回实际等待秒数（float），超时返回 False
        clock = self._Clock()
        with mock.patch.object(browser_order, "time", clock):
            elapsed = browser_order._wait_slide_gone(
                self._SlidePage(clock, present=False), 30)
        self.assertIsInstance(elapsed, float)
        self.assertAlmostEqual(elapsed, 0.5)

    def test_wait_slide_gone_timeout_is_false(self):
        clock = self._Clock()
        with mock.patch.object(browser_order, "time", clock):
            got = browser_order._wait_slide_gone(
                self._SlidePage(clock, present=True), 1)
        self.assertIs(got, False)

    def test_confirm_button_still_clickable_after_slide(self):
        # 滑块耗时 60s：旧代码确认窗 15s 倒计时被吃光，qr_submit 永远点不下去
        ok, msg, extra, page = self._run_order()
        self.assertTrue(
            page.qr_submit_clicked,
            "滑块等待 60s 后确认按钮仍应可点击（旧代码：确认窗倒计时被吃光）")

    def test_result_deadline_extended_after_slide(self):
        # 结果页在结果等待滑块点之后 +80s 才出现：旧 deadline 在滑块后只剩
        # 约 30s 会超时，只有 deadline += 60s 延长后（剩约 90s）才能拿到结果
        ok, msg, extra, page = self._run_order()
        self.assertTrue(ok, "结果等待应被滑块耗时顺延，实际返回：%r" % (msg,))
        self.assertIn("已提交订单", msg)


# ============================ Task 43 ============================

def _t43_filelock_holder(lock_path, ready_evt, hold_sec):
    """子进程入口：拿 filelock.FileLock 并持有 hold_sec 秒。"""
    import time as _time
    import filelock as _fl
    lk = _fl.FileLock(lock_path, timeout=10)
    lk.acquire()
    ready_evt.set()
    _time.sleep(hold_sec)
    lk.release()


def _t43_profilelock_holder(lock_path, ready_evt, hold_sec):
    """子进程入口：拿 browser_order._ProfileLock 并持有 hold_sec 秒。"""
    import time as _time
    import browser_order as _bo
    lk = _bo._ProfileLock(lock_path)
    if lk.acquire(timeout=10):
        ready_evt.set()
        _time.sleep(hold_sec)
        lk.release()


def _t43_join_child(testcase, proc):
    proc.join(timeout=20)
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=5)
    testcase.assertFalse(proc.is_alive(), "子进程未能退出")


class TestTask43FileLockCrossProcess(TempDirCase):
    """Task 43a: filelock.FileLock 在 Linux/macOS 上必须真正跨进程互斥。

    旧代码：msvcrt 不可用时 acquire() 直接 return True（假锁），两个进程
    同时"拿到锁"，launcher 与 gui/engine 的读-改-写并发把任务吃掉。
    """

    @unittest.skipIf(filelock.msvcrt is not None,
                     "Windows 走 msvcrt 路径，本测试只验 POSIX fcntl 后端")
    def test_two_processes_mutual_exclusion(self):
        import multiprocessing
        path = os.path.join(self.tmp, "x.lock")
        ready = multiprocessing.Event()
        p = multiprocessing.Process(
            target=_t43_filelock_holder, args=(path, ready, 2.0))
        p.start()
        try:
            self.assertTrue(ready.wait(timeout=15), "子进程 15 秒内没拿到锁")
            t0 = time.time()
            with self.assertRaises(TimeoutError):
                filelock.FileLock(path, timeout=1.0).acquire()
            self.assertGreaterEqual(
                time.time() - t0, 0.9, "应该等满超时而不是立刻返回（假锁）")
        finally:
            _t43_join_child(self, p)

    @unittest.skipIf(filelock.msvcrt is not None,
                     "Windows 走 msvcrt 路径，本测试只验 POSIX fcntl 后端")
    def test_lock_released_after_holder_exits(self):
        # 持有者释放后，另一进程能立刻拿到锁（无死锁残留）
        import multiprocessing
        path = os.path.join(self.tmp, "y.lock")
        ready = multiprocessing.Event()
        p = multiprocessing.Process(
            target=_t43_filelock_holder, args=(path, ready, 0.5))
        p.start()
        try:
            self.assertTrue(ready.wait(timeout=15), "子进程 15 秒内没拿到锁")
        finally:
            _t43_join_child(self, p)
        with filelock.file_lock(path, timeout=5):
            pass  # 能进来即证明锁已释放


class TestTask43ProfileLockCrossProcess(TempDirCase):
    """Task 43a: browser_order._ProfileLock 在 Linux/macOS 上必须跨进程互斥。

    旧代码：非 Windows 直接 return True。两个进程同时认为自己独占 profile，
    Playwright 互相挤掉，对方浏览器 exitCode=21 启动即退。
    """

    @unittest.skipIf(browser_order.msvcrt is not None,
                     "Windows 走 msvcrt 路径，本测试只验 POSIX fcntl 后端")
    def test_two_processes_mutual_exclusion(self):
        import multiprocessing
        path = os.path.join(self.tmp, "profile.lock")
        ready = multiprocessing.Event()
        p = multiprocessing.Process(
            target=_t43_profilelock_holder, args=(path, ready, 2.0))
        p.start()
        try:
            self.assertTrue(ready.wait(timeout=15), "子进程 15 秒内没拿到锁")
            lk = browser_order._ProfileLock(path)
            t0 = time.time()
            got = lk.acquire(timeout=1.0)
            self.assertFalse(got, "子进程持有锁时，父进程 acquire 必须返回 False")
            self.assertGreaterEqual(
                time.time() - t0, 0.9, "应该等满超时而不是立刻返回（假锁）")
        finally:
            _t43_join_child(self, p)

    @unittest.skipIf(browser_order.msvcrt is not None,
                     "Windows 走 msvcrt 路径，本测试只验 POSIX fcntl 后端")
    def test_acquire_release_cycle(self):
        # 本进程内拿锁→释放→再拿，不应自锁死
        path = os.path.join(self.tmp, "profile2.lock")
        lk = browser_order._ProfileLock(path)
        self.assertTrue(lk.acquire(timeout=5))
        lk.release()
        self.assertTrue(lk.acquire(timeout=5))
        lk.release()


class TestTask43ClearProfileLocksOwnership(TempDirCase):
    """Task 43b: launch() 失败只删自己创建的锁，绝不删对端活着的 SingletonLock。

    旧代码：某通道启动失败就无条件删 SingletonLock/Socket/Cookie。
    若删掉的是对端（引擎体检 / 下单中）浏览器还活着的 SingletonLock，
    会把对方正在下单的浏览器掐死。
    Chromium 在 POSIX 下把 SingletonLock 做成指向 "<hostname>-<pid>" 的软链接，
    目标 pid 存活 ⇒ 某个浏览器实例还活着。
    """

    def _profile_dir(self):
        prof = os.path.join(self.tmp, ".browser_profile")
        os.makedirs(prof, exist_ok=True)
        return prof

    def test_live_peer_singleton_lock_not_deleted(self):
        prof = self._profile_dir()
        link = os.path.join(prof, "SingletonLock")
        os.symlink("fakehost-%d" % os.getpid(), link)  # 指向本进程：存活
        sock = os.path.join(prof, "SingletonSocket")
        with open(sock, "w") as f:
            f.write("x")
        with mock.patch.object(browser_order, "PROFILE_DIR", prof):
            browser_order._clear_profile_locks()
        self.assertTrue(os.path.islink(link), "活着的对端 SingletonLock 被删了")
        self.assertTrue(os.path.exists(sock), "对端活着时，其 Socket 也不该动")

    def test_stale_singleton_lock_deleted(self):
        prof = self._profile_dir()
        link = os.path.join(prof, "SingletonLock")
        os.symlink("fakehost-999999999", link)  # 不可能存在的 pid：持有者已死
        with mock.patch.object(browser_order, "PROFILE_DIR", prof):
            browser_order._clear_profile_locks()
        self.assertFalse(os.path.lexists(link), "持有者已死的僵尸锁应该被清理")

    def test_only_own_new_locks_deleted(self):
        prof = self._profile_dir()
        old_sock = os.path.join(prof, "SingletonSocket")
        with open(old_sock, "w") as f:
            f.write("old")
        with mock.patch.object(browser_order, "PROFILE_DIR", prof):
            baseline = browser_order._snapshot_profile_locks()
            # 本次启动尝试新产生的僵尸锁（上一个 channel 失败留下）
            new_link = os.path.join(prof, "SingletonLock")
            os.symlink("fakehost-999999999", new_link)
            browser_order._clear_profile_locks(baseline)
        self.assertFalse(os.path.lexists(new_link), "自己尝试留下的僵尸锁应被清理")
        self.assertTrue(os.path.exists(old_sock),
                        "尝试前已存在且未被改动的文件不是自己创建的，不该删")

    def test_launch_failure_keeps_live_peer_lock(self):
        # launch() 所有通道都失败时，对端活着浏览器的锁文件必须一个都不少
        prof = self._profile_dir()
        link = os.path.join(prof, "SingletonLock")
        os.symlink("fakehost-%d" % os.getpid(), link)  # 对端浏览器：存活
        sock = os.path.join(prof, "SingletonSocket")
        with open(sock, "w") as f:
            f.write("x")
        cookie = os.path.join(prof, "SingletonCookie")
        with open(cookie, "w") as f:
            f.write("y")
        fake_p = mock.Mock()
        fake_p.chromium.launch_persistent_context.side_effect = \
            RuntimeError("no browser here")
        with mock.patch.object(browser_order, "PROFILE_DIR", prof):
            with self.assertRaises(RuntimeError):
                browser_order.launch(fake_p, restore=False)
        self.assertTrue(os.path.islink(link), "launch 失败把对端活着的 SingletonLock 删了")
        self.assertTrue(os.path.exists(sock), "launch 失败把对端活着浏览器的 Socket 删了")
        self.assertTrue(os.path.exists(cookie), "launch 失败把对端活着浏览器的 Cookie 删了")


class TestProbeStep7(unittest.TestCase):
    """Task 44: probe 诊断脚本不得写生产 session_cookies.json。

    改前：step7_save_cookies() 把拍平 {name: value} 的诊断 Cookie 写进生产
    session_cookies.json（丢 domain/path，增大风控特征风险，还会覆盖生产会话）。
    改后：诊断 Cookie 落到独立 probe_cookies.json，生产文件 mtime/内容不动。
    """

    def setUp(self):
        self.repo_dir = os.path.dirname(os.path.abspath(probe_login.__file__))
        self.prod_path = os.path.join(self.repo_dir, "session_cookies.json")
        self.probe_path = os.path.join(self.repo_dir, "probe_cookies.json")
        self.had_prod = os.path.exists(self.prod_path)
        self.prod_backup = None
        if self.had_prod:
            with open(self.prod_path, "r", encoding="utf-8") as f:
                self.prod_backup = f.read()
        # 种子生产会话文件并记录 mtime（纳秒精度，避免同秒 flaky）
        with open(self.prod_path, "w", encoding="utf-8") as f:
            json.dump({"JSESSIONID": "production-value"}, f)
        self.prod_mtime_ns = os.stat(self.prod_path).st_mtime_ns
        # 注入诊断会话 Cookie（不走网络）
        probe_login.SESSION.cookies.set("JSESSIONID", "diag-value")
        probe_login.SESSION.cookies.set("RAIL_DEVICEID", "diag-device")

    def tearDown(self):
        probe_login.SESSION.cookies.clear()
        if os.path.exists(self.probe_path):
            os.remove(self.probe_path)
        if self.had_prod:
            with open(self.prod_path, "w", encoding="utf-8") as f:
                f.write(self.prod_backup)
        elif os.path.exists(self.prod_path):
            os.remove(self.prod_path)

    def test_step7_does_not_touch_production_session_file(self):
        probe_login.step7_save_cookies()
        self.assertEqual(
            os.stat(self.prod_path).st_mtime_ns, self.prod_mtime_ns,
            "probe 诊断写了生产 session_cookies.json（mtime 变了）")
        with open(self.prod_path, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"JSESSIONID": "production-value"},
                             "probe 诊断覆盖了生产 session_cookies.json 内容")

    def test_step7_writes_diagnosis_cookies_to_probe_file(self):
        probe_login.step7_save_cookies()
        self.assertTrue(os.path.exists(self.probe_path),
                        "诊断 Cookie 未落到独立 probe_cookies.json")
        with open(self.probe_path, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f),
                             {"JSESSIONID": "diag-value",
                              "RAIL_DEVICEID": "diag-device"})


class _FakeElapsed:
    def total_seconds(self):
        return 0.0


class _FakeResp:
    """step6 测试用假响应：满足 show() 的属性需求。"""
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code
        self.content = text.encode("utf-8")
        self.headers = {"Content-Type": "application/json"}
        self.elapsed = _FakeElapsed()

    def json(self):
        return json.loads(self.text)


class TestProbeFailurePath(unittest.TestCase):
    """Task 49(a): 二维码失败路径不得写盘（成功才保存）。

    改前：main() 的 `if not uamtk:` 分支仍调 step7_save_cookies()，
    用登录前无用 Cookie（仅 JSESSIONID）污染 probe_cookies.json。
    改后：失败路径不写盘；成功路径仍保存。
    """

    def _run_main(self, poll_result):
        for p in (
            mock.patch.object(probe_login, "step1_connectivity"),
            mock.patch.object(probe_login, "step2_bootstrap_cookies"),
            mock.patch.object(probe_login, "step3_create_qr", return_value="fake-uuid"),
            mock.patch.object(probe_login, "step4_poll_qr", return_value=poll_result),
            mock.patch.object(probe_login, "step7_save_cookies"),
        ):
            p.start()
            self.addCleanup(p.stop)
        probe_login.main()
        return probe_login.step7_save_cookies

    def test_qr_timeout_does_not_write_cookies(self):
        # RED on old code: 失败路径仍调 step7_save_cookies（写盘）
        save = self._run_main(None)
        save.assert_not_called()

    def test_success_path_still_saves_cookies(self):
        # 成功路径仍保存：pin（新旧代码都通过）
        for p in (
            mock.patch.object(probe_login, "step5_finish_login", return_value=True),
            mock.patch.object(probe_login, "step6_verify_session", return_value=True),
        ):
            p.start()
            self.addCleanup(p.stop)
        save = self._run_main("fake-uamtk")
        save.assert_called_once()


class TestProbeSessionHeuristic(unittest.TestCase):
    """Task 49(b): 会话有效性启发收紧 —— 错误包 JSON 不得判有效。

    实际接口（initMy12306Api / passengers/query）返回
    {"status": true/false, "data": {...}}；未登录时 status=false 且 data={}。
    旧启发 `'"data"' in r.text` 命中任意含 "data" 键的 JSON（含错误包）。
    """

    def _run_step6(self, get_text, post_text=None, status=200):
        fake = mock.Mock()
        fake.get.return_value = _FakeResp(get_text, status)
        fake.post.return_value = _FakeResp(
            post_text if post_text is not None else get_text, status)
        with mock.patch.object(probe_login, "SESSION", fake):
            return probe_login.step6_verify_session()

    def test_error_json_not_valid(self):
        # RED on old code: 错误包被判有效（'"data"' in r.text 恒成立）
        err = ('{"validateMessagesShowId":"_validatorMessage","status":false,'
               '"httpstatus":200,"data":{},"messages":[],"validateMessages":{}}')
        self.assertFalse(self._run_step6(err))

    def test_valid_session_detected(self):
        # pin：有效会话仍判有效（新旧代码都通过）
        ok_get = ('{"validateMessagesShowId":"_validatorMessage","status":true,'
                  '"httpstatus":200,"data":{"user_name":"张*","name":"张三"},'
                  '"messages":[],"validateMessages":{}}')
        ok_post = ('{"validateMessagesShowId":"_validatorMessage","status":true,'
                   '"httpstatus":200,"data":{"flag":true,"pageSize":10,'
                   '"datas":[{"passenger_name":"张三"}]},"messages":[],'
                   '"validateMessages":{}}')
        self.assertTrue(self._run_step6(ok_get, ok_post))

    def test_non_json_not_valid(self):
        # pin：HTML 登录页不判有效（新旧代码都通过）
        self.assertFalse(self._run_step6("<html><title>登录</title></html>"))

    def test_non_200_not_valid(self):
        # pin：非 200 不判有效（新旧代码都通过）
        self.assertFalse(self._run_step6('{"status":true,"data":{"user_name":"x"}}',
                                         status=500))


class TestLockedRMW(TempDirCase):
    """Task 46 (P2): gui/monitor/launcher 读-改-写统一经 file_lock 原子接口。

    改前：gui 读在锁外、写才加锁；monitor 全程无锁——双端同时改任务丢数据。
    改后：三入口经 update_config_locked / update_state_locked，整包在锁内。
    """

    def _patch_gui_paths(self):
        cfg = os.path.join(self.tmp, "config.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"tasks": [], "state_file": "state.json"}, f)
        for name, val in (("CONFIG_PATH", cfg), ("HERE", self.tmp)):
            p = mock.patch.object(gui, name, val)
            p.start()
            self.addCleanup(p.stop)
        return cfg

    def test_gui_update_config_locked_concurrent_no_lost_update(self):
        # RED on old code: gui 根本没有 update_config_locked（AttributeError）
        cfg = self._patch_gui_paths()
        n = 16

        def worker(i):
            gui.update_config_locked(
                lambda c: c.setdefault("tasks", []).append({"name": "t%d" % i}))

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        with open(cfg, encoding="utf-8") as f:
            tasks = json.load(f)["tasks"]
        self.assertEqual(len(tasks), n)
        self.assertEqual(sorted(t["name"] for t in tasks),
                         sorted("t%d" % i for i in range(n)))

    def test_gui_update_state_locked_concurrent_no_lost_update(self):
        # RED on old code: gui 根本没有 update_state_locked（AttributeError）
        self._patch_gui_paths()
        n = 16

        def worker(i):
            gui.update_state_locked(
                lambda s: s.setdefault("tasks", {}).setdefault(
                    "t%d" % i, {}) .__setitem__("status", "monitoring"))

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        with open(os.path.join(self.tmp, "state.json"), encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual(len(state["tasks"]), n)
        for i in range(n):
            self.assertEqual(state["tasks"]["t%d" % i]["status"], "monitoring")

    def test_gui_save_config_still_writes_standalone(self):
        # 回归 pin：旧的 load_config/save_config 独立读写行为不变
        cfg = self._patch_gui_paths()
        config = gui.load_config()
        config["order_mode"] = "browser"
        gui.save_config(config)
        with open(cfg, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["order_mode"], "browser")

    def test_monitor_save_config_holds_file_lock(self):
        # RED on old code: monitor.save_config 只原子写、不加锁
        import monitor as monitor_mod
        import filelock
        cfg = os.path.join(self.tmp, "config.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"tasks": []}, f)
        p = mock.patch.object(monitor_mod, "CONFIG_PATH", cfg)
        p.start()
        self.addCleanup(p.stop)
        real = filelock.file_lock
        seen = []

        def spy(path, *a, **k):
            seen.append(path)
            return real(path, *a, **k)

        with mock.patch.object(filelock, "file_lock", side_effect=spy):
            monitor_mod.save_config({"tasks": [{"name": "t"}]})
        self.assertIn(cfg + ".lock", seen)
        with open(cfg, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["tasks"], [{"name": "t"}])

    def test_monitor_update_config_locked_concurrent_no_lost_update(self):
        # RED on old code: monitor 根本没有 update_config_locked（AttributeError）
        import monitor as monitor_mod
        cfg = os.path.join(self.tmp, "config.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"tasks": []}, f)
        p = mock.patch.object(monitor_mod, "CONFIG_PATH", cfg)
        p.start()
        self.addCleanup(p.stop)
        n = 16

        def worker(i):
            monitor_mod.update_config_locked(
                lambda c: c.setdefault("tasks", []).append({"name": "m%d" % i}))

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        with open(cfg, encoding="utf-8") as f:
            tasks = json.load(f)["tasks"]
        self.assertEqual(len(tasks), n)


class TestTask50MonitorSecretsAndEmptyDates(TempDirCase):
    """Task 50 (P2): (a) monitor 授权码明文回显 → 改用 getpass；
    (b) menu_task_list 空 dates 时 IndexError → 显示占位。"""

    def _run_menu_notify(self, reads, getpass_ret):
        import monitor as monitor_mod
        saved = {}
        with mock.patch.object(monitor_mod, "load_config", return_value={}), \
             mock.patch.object(monitor_mod, "read", side_effect=reads), \
             mock.patch("getpass.getpass", return_value=getpass_ret) as gp, \
             mock.patch.object(monitor_mod, "save_config",
                               side_effect=lambda c: saved.update(c)):
            monitor_mod.menu_notify()
        return saved, gp

    def test_menu_notify_password_via_getpass(self):
        # 旧代码用 read() 明文输入授权码（第 4 个 read）；getpass 从未被调用。
        saved, gp = self._run_menu_notify(
            ["", "465", "", "oldpw", "", "a@x.com", "n"], "newpw123")
        gp.assert_called_once()
        self.assertEqual(saved["notify"]["email"]["password"], "newpw123")

    def test_menu_notify_empty_getpass_keeps_old_password(self):
        # getpass 回车（空串）→ 保留旧授权码，与旧 read 空输入语义一致。
        import monitor as monitor_mod
        cfg = {"notify": {"email": {"enabled": True, "smtp_host": "smtp.qq.com",
                                    "smtp_port": 465, "username": "u@x.com",
                                    "password": "keepme", "from": "u@x.com",
                                    "to": ["a@x.com"]}}}
        saved = {}
        with mock.patch.object(monitor_mod, "load_config", return_value=cfg), \
             mock.patch.object(monitor_mod, "read",
                               side_effect=["", "465", "", "", "", "n"]), \
             mock.patch("getpass.getpass", return_value=""), \
             mock.patch.object(monitor_mod, "save_config",
                               side_effect=lambda c: saved.update(c)):
            monitor_mod.menu_notify()
        self.assertEqual(saved["notify"]["email"]["password"], "keepme")

    def test_menu_task_list_empty_dates_no_crash(self):
        # 旧代码 dates[0] 在空 dates 时抛 IndexError。
        import monitor as monitor_mod
        task = {"name": "t1", "from": "北京", "to": "上海",
                "dates": [], "trains": [], "seat_types": [],
                "priority": 5, "passenger_names": []}
        eng = mock.MagicMock()
        eng.task_status.return_value = "idle"
        eng.state = {"tasks": {}}
        with mock.patch.object(monitor_mod, "load_config",
                               return_value={"tasks": [task]}), \
             mock.patch.object(monitor_mod, "fresh_engine", return_value=eng), \
             mock.patch("builtins.print") as mprint:
            monitor_mod.menu_task_list()  # 不得抛 IndexError
        printed = " ".join(str(c.args[0]) for c in mprint.call_args_list)
        self.assertIn("未配置日期", printed)
        self.assertNotIn("Traceback", printed)

    def test_menu_task_list_normal_dates_unchanged(self):
        # 回归 pin：正常 dates 显示首日（旧代码即通过）。
        import monitor as monitor_mod
        task = {"name": "t1", "from": "北京", "to": "上海",
                "dates": ["2099-01-01", "2099-01-02"], "trains": [],
                "seat_types": [], "priority": 5, "passenger_names": []}
        eng = mock.MagicMock()
        eng.task_status.return_value = "idle"
        eng.state = {"tasks": {}}
        with mock.patch.object(monitor_mod, "load_config",
                               return_value={"tasks": [task]}), \
             mock.patch.object(monitor_mod, "fresh_engine", return_value=eng), \
             mock.patch("builtins.print") as mprint:
            monitor_mod.menu_task_list()
        printed = " ".join(str(c.args[0]) for c in mprint.call_args_list)
        self.assertIn("2099-01-01", printed)
        self.assertNotIn("未配置日期", printed)


class TestOrderQueryFixes(TempDirCase):
    """Task 51 (P2): order 查询三件套。

    (a) start_date 回退必须做 YYYYMMDD → YYYY-MM-DD 转换；
    (b) fetch_unpaid_order_no 必须按 not_before 归因本次新建，不取历史旧单；
    (c) 订单查询失败必须向上传播（记 warning），调用方保守不下单。
    """

    # ---------- (a) train_date 格式 ----------

    def test_dash_date_converts_yyyymmdd(self):
        # 旧代码：ticket["start_date"]（YYYYMMDD）原样发给 submitOrderRequest，
        # train_date 非法且 find_duplicate 在该路径静默失效。
        self.assertEqual(order_mod._dash_date("20261010"), "2026-10-10")

    def test_dash_date_passthrough(self):
        # 已是横线格式 / 病态串原样返回（不在此处崩，下游接口报错）。
        self.assertEqual(order_mod._dash_date("2026-10-10"), "2026-10-10")
        self.assertEqual(order_mod._dash_date("2026101"), "2026101")
        self.assertEqual(order_mod._dash_date(""), "")

    def test_order_ticket_uses_converted_date_for_submit(self):
        # ticket 无 query_date 时，submit 收到的 train_date 必须是 YYYY-MM-DD。
        ticket = {"query_date": None, "start_date": "20261010",
                  "train_code": "G101", "from_name": "北京", "to_name": "上海",
                  "start_time": "08:00", "arrive_time": "12:00",
                  "train_location": "P3", "secret_str": "x"}
        task = {"passenger_names": ["张三"]}
        config = {}
        seen = {}

        def fake_submit(sess, ticket_, seat_code, date, tries, delay,
                        purpose="ADULT"):
            seen["date"] = date
            return True, ""

        with mock.patch.object(order_mod, "load_session",
                               return_value=object()), \
             mock.patch.object(order_mod, "check_login",
                               return_value=(True, "u")), \
             mock.patch.object(order_mod, "order_seat_code",
                               return_value=("O", "二等座")), \
             mock.patch.object(order_mod, "submit_with_busy_retry",
                               side_effect=fake_submit), \
             mock.patch.object(order_mod, "get_init_dc",
                               return_value=("tok", "left", "key", "")), \
             mock.patch.object(order_mod, "get_passengers",
                               return_value=[{"name": "张三", "is_adult": True,
                                              "id_no": "x", "mobile": ""}]), \
             mock.patch.object(order_mod, "check_existing_orders",
                               return_value=[]), \
             mock.patch.object(order_mod, "check_order_info",
                               return_value=(True, "")), \
             mock.patch.object(order_mod, "confirm_with_busy_retry",
                               return_value=(True, "ok")), \
             mock.patch.object(order_mod, "fetch_unpaid_order_no",
                               return_value="E1"):
            order_mod.order_ticket(config, task, ticket, "二等座")
        # 旧代码此处是 "20261010"（submitOrderRequest 只认 YYYY-MM-DD）。
        self.assertEqual(seen.get("date"), "2026-10-10")

    # ---------- (b) 订单号归属 ----------

    @staticmethod
    def _bj_str(ts):
        return datetime.datetime.fromtimestamp(
            ts, datetime.timezone(datetime.timedelta(hours=8))
        ).strftime("%Y-%m-%d %H:%M:%S")

    def _nocomplete_session(self, items):
        class _FakeResp:
            def __init__(self, payload):
                self._p = payload

            def json(self):
                return self._p

        class _FakeSession:
            headers = {}

            def post(self, url, data=None, timeout=None):
                if "queryMyOrderNoComplete" in url:
                    return _FakeResp({"data": {"orderDBList": items}})
                return _FakeResp({"data": {}})

        return _FakeSession()

    def _unpaid_item(self, seq, order_ts):
        return {"sequence_no": seq, "order_status_name_cn": "未完成",
                "order_date": self._bj_str(order_ts),
                "train_code_page": "G101",
                "start_train_date_page": "2026-10-08",
                "from_station_name_page": "北京", "to_station_name_page": "上海",
                "array_passser_name_page": ["张三"]}

    def test_fetch_unpaid_order_no_attributes_to_this_submit(self):
        # 历史未支付旧单在前、本次新建在后：必须返回本次新建的单号。
        now = time.time()
        items = [self._unpaid_item("E_OLD", now - 86400),
                 self._unpaid_item("E_NEW", now - 30)]
        sess = self._nocomplete_session(items)
        got = order_mod.fetch_unpaid_order_no(
            sess, date="2026-10-08", train_code="G101",
            passenger_names=["张三"], not_before_ts=now - 120)
        # 旧代码取第一笔（任意行程）→ 返回 E_OLD（过期单号）。
        self.assertEqual(got, "E_NEW")

    def test_fetch_unpaid_order_no_returns_none_when_no_recent(self):
        # 只有历史旧单（不在本次提交窗口内）：宁可缺省，不张冠李戴。
        now = time.time()
        sess = self._nocomplete_session([self._unpaid_item("E_OLD", now - 86400)])
        got = order_mod.fetch_unpaid_order_no(
            sess, date="2026-10-08", train_code="G101",
            passenger_names=["张三"], not_before_ts=now - 120)
        self.assertIsNone(got)

    def test_fetch_unpaid_order_no_failure_does_not_break_flow(self):
        # 回归 pin：查询异常 → None（失败不影响主流程，旧代码即如此）。
        class _BadSession:
            headers = {}

            def post(self, url, data=None, timeout=None):
                raise RuntimeError("net down")

        self.assertIsNone(order_mod.fetch_unpaid_order_no(_BadSession()))

    # ---------- (c) 查询失败不吞 ----------

    def test_check_existing_orders_propagates_query_failure(self):
        # 未完成订单接口瞬时失败：必须向上传播 + 记 warning，不再吞成空列表。
        class _BadSession:
            headers = {}

            def post(self, url, data=None, timeout=None):
                raise RuntimeError("transient 500")

        with self.assertLogs(level="WARNING") as logs:
            with self.assertRaises(Exception):
                order_mod.check_existing_orders(_BadSession(), "2026-10-08")
        self.assertTrue(any("WARNING" in r for r in
                            [rec.levelname for rec in logs.records]),
                        "查询失败应记 warning")

    def test_order_ticket_refuses_when_query_fails(self):
        # 防重守卫被架空时：未知 → 保守不下单（旧代码吞成 [] 继续下单）。
        ticket = {"query_date": "2026-10-08", "start_date": "20261008",
                  "train_code": "G101", "from_name": "北京", "to_name": "上海",
                  "start_time": "08:00", "arrive_time": "12:00",
                  "train_location": "P3", "secret_str": "x"}
        task = {"passenger_names": ["张三"]}
        with mock.patch.object(order_mod, "load_session",
                               return_value=object()), \
             mock.patch.object(order_mod, "check_login",
                               return_value=(True, "u")), \
             mock.patch.object(order_mod, "order_seat_code",
                               return_value=("O", "二等座")), \
             mock.patch.object(order_mod, "submit_with_busy_retry",
                               return_value=(True, "")), \
             mock.patch.object(order_mod, "get_init_dc",
                               return_value=("tok", "left", "key", "")), \
             mock.patch.object(order_mod, "get_passengers",
                               return_value=[{"name": "张三", "is_adult": True,
                                              "id_no": "x", "mobile": ""}]), \
             mock.patch.object(order_mod, "check_existing_orders",
                               side_effect=RuntimeError("transient 500")), \
             mock.patch.object(order_mod, "check_order_info",
                               return_value=(True, "")) as m_check, \
             mock.patch.object(order_mod, "confirm_with_busy_retry",
                               return_value=(True, "ok")) as m_confirm:
            ok, msg, extra = order_mod.order_ticket({}, task, ticket, "二等座")
        self.assertFalse(ok)
        self.assertIn("保守", msg)
        self.assertIsNone(extra)
        m_check.assert_not_called()     # 未走到下单链
        m_confirm.assert_not_called()


# ============================ Task 52 ============================

def _t52_busy_holder(lock_path, ready_evt, hold_sec):
    """子进程入口：拿 browser_order._ProfileLock 并持有 hold_sec 秒。"""
    import time as _time
    import browser_order as _bo
    lk = _bo._ProfileLock(lock_path)
    if lk.acquire(timeout=10):
        ready_evt.set()
        _time.sleep(hold_sec)
        lk.release()


class TestTask52SliderAmbiguous(TempDirCase):
    """Task 52a: 结果等待期滑块超时/重现必须标 reason=ambiguous（clicked=True 时）。

    旧代码：只回 {"need_captcha": True}，漏标 ambiguous → 上层（engine/launcher）
    当普通失败盲重试（重复下单）或误判没抢到。
    新行为：clicked=True（确认已点出，订单可能已提交）时同样标 ambiguous，
    走官方回读确认；clicked=False（确实没提交）时保持原样。
    无真实浏览器：用假页面 + 假时钟驱动 _order_impl 全流程。
    """

    class _Clock:
        """可手动拨动的假时钟，整体替换 browser_order.time。"""
        def __init__(self):
            self.now = 1_700_000_000.0

        def time(self):
            return self.now

        def sleep(self, s):
            self.now += s

        def perf_counter(self):
            return self.now

        def strftime(self, fmt, t=None):
            return time.strftime(
                fmt, time.localtime(self.now if t is None else t))

        def localtime(self, t=None):
            return time.localtime(self.now if t is None else t)

    class _Locator:
        def __init__(self, count=0, visible=False, evaluate_result=None):
            self._count = count
            self._visible = visible
            self._evaluate_result = evaluate_result

        @property
        def first(self):
            return self

        def count(self):
            return self._count

        def is_visible(self):
            return self._visible

        def evaluate(self, js):
            return self._evaluate_result

    class _Page:
        """按脚本走完「确认窗→结果等待」的假页面，可配置滑块行为。"""
        INIT_URL = "https://kyfw.12306.cn/otn/confirmPassenger/initDc"

        def __init__(self, clock, qr_enable=True, result_slides=1):
            self._clock = clock
            self.qr_submit_clicked = False
            self._qr_enable = qr_enable
            self._qr_calls = 0
            # 结果等待里滑块出现 result_slides 次
            self._result_slides_left = result_slides

        def set_default_timeout(self, ms):
            pass

        def wait_for_timeout(self, ms):
            self._clock.now += ms / 1000.0

        def wait_for_url(self, *a, **k):
            pass

        def wait_for_selector(self, *a, **k):
            pass

        def wait_for_function(self, *a, **k):
            pass

        def locator(self, sel):
            if sel == "#slide_passcode":
                if self._result_slides_left > 0:
                    self._result_slides_left -= 1
                    return TestTask52SliderAmbiguous._Locator(
                        count=1, visible=True)
                return TestTask52SliderAmbiguous._Locator()
            if sel == "#seatType_1":
                return TestTask52SliderAmbiguous._Locator(
                    count=1, visible=True,
                    evaluate_result=[{"v": "WZ", "t": "无座"}])
            return TestTask52SliderAmbiguous._Locator()

        def eval_on_selector_all(self, sel, js):
            if sel.startswith("#normal_passenger_id"):
                return [{"id": "p1", "text": "张三"}]
            return []  # 旧确认控件回退：没有可点的

        @property
        def url(self):
            return self.INIT_URL  # 结果页永远不出现：逼出滑块/超时路径

        def evaluate(self, js, arg=None):
            if "dialog_xsertcj" in js:
                return None  # 无学生票询问弹窗
            if "checkticketinfo_id" in js:
                return ""  # 核对窗原文为空：跳过席别对账分支
            if "qr_submit_id" in js:
                if "click" in js:
                    self.qr_submit_clicked = True
                    return None
                self._qr_calls += 1
                if self._qr_enable and self._qr_calls >= 2:
                    return {"found": True, "cls": "btn92s", "shown": True}
                return {"found": True, "cls": "btn92", "shown": True}
            if "queryLeftTable" in js:
                return True  # 点中预订
            if "slide_passcode" in js and "nc-container" in js:
                return []  # 确认窗里无滑块
            if "ticketType_" in js:
                return {"before": ["1"], "after": ["1"], "map": {}}
            if "seatType_1" in js:
                return {}
            if "tt: String" in js:
                return [{"name": "张三", "tt": "1"}]
            if "limit_tickets" in js:
                return [{"name": "张三", "seat": "WZ", "ticket_type": "1"}]
            if js.startswith("(id) =>"):
                return None  # 勾选乘车人 click
            if js.startswith("(ids) =>"):
                return []  # 勾选回读：全部已勾上
            if "document.body" in js:
                return ""
            if "#submitOrder_id" in js:
                return None
            raise AssertionError("假页面遇到未预期的 evaluate: %r" % js[:80])

    class _Warm:
        def __init__(self, page):
            self.p = object()
            self.ctx = object()
            self.page = page

        def usable(self):
            return True

        def refresh(self, info):
            pass

    def _stub_playwright(self):
        import sys
        import types as _types
        pw = _types.ModuleType("playwright")
        pw_sync = _types.ModuleType("playwright.sync_api")

        def _no_playwright():
            raise AssertionError("预热路径不应启动 playwright")

        pw_sync.sync_playwright = _no_playwright
        pw.sync_api = pw_sync
        self._pw_mods = {"playwright": pw, "playwright.sync_api": pw_sync}
        for name, mod in self._pw_mods.items():
            sys.modules[name] = mod
        self.addCleanup(self._unstub_playwright)

    def _unstub_playwright(self):
        import sys
        for name in self._pw_mods:
            sys.modules.pop(name, None)

    def _run(self, qr_enable=True, result_slides=1, slide_result=False):
        """驱动一次完整下单到结果等待滑块路径。slide_result 传给 _wait_slide_gone。"""
        clock = self._Clock()
        page = self._Page(clock, qr_enable=qr_enable,
                          result_slides=result_slides)
        self._stub_playwright()
        info = {"train_code": "G101", "from_name": "北京", "to_name": "上海",
                "from_code": "VNP", "to_code": "SHH"}
        with mock.patch.object(browser_order, "time", clock), \
             mock.patch.object(browser_order, "_wait_slide_gone",
                               return_value=slide_result):
            ok, msg, extra = browser_order._order_impl(
                info, "无座", "WZ", ["张三"], "2026-10-10",
                warm=self._Warm(page))
        return ok, msg, extra, page

    def test_result_slide_timeout_marks_ambiguous_when_clicked(self):
        # 确认已点出（clicked=True）后结果等待滑块超时：旧代码只回 need_captcha，
        # 漏标 ambiguous → 上层盲重试可能重复下单
        ok, msg, extra, page = self._run(qr_enable=True, result_slides=1,
                                         slide_result=False)
        self.assertTrue(page.qr_submit_clicked, "前置条件：确认按钮应已点击")
        self.assertFalse(ok)
        self.assertTrue((extra or {}).get("need_captcha"),
                        "need_captcha 信号应保留")
        self.assertEqual((extra or {}).get("reason"), "ambiguous",
                         "clicked=True 时滑块超时必须标 ambiguous，走官方回读")

    def test_result_slide_reappears_marks_ambiguous_when_clicked(self):
        # 滑块第二次出现（captcha_waited 已 True）→ "再次出现"路径，同样标 ambiguous
        ok, msg, extra, page = self._run(qr_enable=True, result_slides=2,
                                         slide_result=60.0)
        self.assertTrue(page.qr_submit_clicked, "前置条件：确认按钮应已点击")
        self.assertFalse(ok)
        self.assertIn("再次出现", msg)
        self.assertEqual((extra or {}).get("reason"), "ambiguous",
                         "clicked=True 时滑块重现必须标 ambiguous")

    def test_result_slide_timeout_no_ambiguous_when_not_clicked(self):
        # 确认按钮从未点出（clicked=False）：确实没提交，不标 ambiguous，
        # 保持旧的 need_captcha 语义
        ok, msg, extra, page = self._run(qr_enable=False, result_slides=1,
                                         slide_result=False)
        self.assertFalse(page.qr_submit_clicked, "前置条件：确认按钮不应被点击")
        self.assertFalse(ok)
        self.assertTrue((extra or {}).get("need_captcha"))
        self.assertNotEqual((extra or {}).get("reason"), "ambiguous",
                            "clicked=False 时不应标 ambiguous（无提交，无需回读）")


class TestTask52BusyProbe(TempDirCase):
    """Task 52b: busy() 必须是真实跨进程探测。

    旧代码（Task 43 前）：_ProfileLock.acquire 非 Windows 恒 True → busy() 恒 False
    → GUI/引擎在对端下单中途另起浏览器抢同一 profile → 对端被挤掉（exitCode=21）。
    Task 43 已加 fcntl 后端；本测试锁定 busy() 的真实探测语义不退化。
    """

    @unittest.skipIf(browser_order.msvcrt is not None,
                     "Windows 走 msvcrt 路径，本测试只验 POSIX fcntl 后端")
    def test_busy_true_when_peer_holds_lock(self):
        import multiprocessing
        lock_path = os.path.join(self.tmp, "busy_probe.lock")
        # busy() 探的是模块级 _PROFILE_LOCK（固定路径）；测试时临时替换隔离
        orig = browser_order._PROFILE_LOCK
        browser_order._PROFILE_LOCK = browser_order._ProfileLock(lock_path)
        try:
            ready = multiprocessing.Event()
            p = multiprocessing.Process(
                target=_t52_busy_holder, args=(lock_path, ready, 3.0))
            p.start()
            try:
                self.assertTrue(ready.wait(timeout=15), "子进程 15 秒内没拿到锁")
                self.assertTrue(
                    browser_order.busy(),
                    "对端进程持有 profile 锁时 busy() 必须报 True（旧代码恒 False）")
            finally:
                _t43_join_child(self, p)
            self.assertFalse(browser_order.busy(), "锁释放后 busy() 应报 False")
        finally:
            browser_order._PROFILE_LOCK = orig

    def test_busy_false_when_free(self):
        # 无人持有（且本进程未持锁）时 busy() 报 False：无误报，不会永久挡住
        # 用户自己的浏览器启动
        orig = browser_order._PROFILE_LOCK
        lock_path = os.path.join(self.tmp, "busy_free.lock")
        browser_order._PROFILE_LOCK = browser_order._ProfileLock(lock_path)
        try:
            self.assertFalse(browser_order.busy())
        finally:
            browser_order._PROFILE_LOCK = orig



class TestDeleteTaskClearsState(TempDirCase):
    """Task 53(a): gui 删任务同步清 state 条目 + 通知运行中引擎立即停。

    改前：只删 config —— state.json 条目成孤儿；引擎内存副本在下次 mtime
    检查前幽灵监控（≤一个轮询间隔）；同名重建继承陈旧状态/防重。
    改后：delete_task_everywhere() 做 config 移除 + state 条目清除（加锁）+
    运行中引擎立即停该任务。
    """

    def _patch_gui_paths(self):
        st = os.path.join(self.tmp, "state.json")
        cfg = os.path.join(self.tmp, "config.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"tasks": [{"name": "t1", "uid": "u1"}],
                       "state_file": st}, f)
        with open(st, "w", encoding="utf-8") as f:
            json.dump({"tasks": {"t1": {"status": "monitoring",
                                       "fail_streak": 0}},
                       "dedup": {}, "retry": {}}, f)
        for name, val in (("CONFIG_PATH", cfg), ("HERE", self.tmp)):
            p = mock.patch.object(gui, name, val)
            p.start()
            self.addCleanup(p.stop)
        return cfg, st

    def _dead_app(self):
        app = mock.Mock()
        app.engine_thread = None
        return app

    def _read(self, path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def test_delete_task_removes_config_and_state_entries(self):
        # RED on old code: 根本没有 delete_task_everywhere（AttributeError）；
        # 旧 _op 只调 update_config_locked 删 config，state 条目成孤儿
        cfg, st = self._patch_gui_paths()
        gui.delete_task_everywhere(self._dead_app(), {"name": "t1", "uid": "u1"})
        self.assertEqual(self._read(cfg)["tasks"], [])
        self.assertNotIn("t1", self._read(st)["tasks"])

    def test_delete_task_notifies_live_engine(self):
        # 引擎运行时：live engine 的内存状态条目也被清除（立即停，不等 mtime）
        cfg, st = self._patch_gui_paths()
        eng = mock.Mock()
        app = mock.Mock()
        app.engine_thread = mock.Mock()
        app.engine_thread.is_alive.return_value = True
        app.get_ops_engine.return_value = eng
        gui.delete_task_everywhere(app, {"name": "t1", "uid": "u1"})
        eng.note_task_deleted.assert_called_once_with("t1")
        self.assertEqual(self._read(cfg)["tasks"], [])

    def test_note_task_deleted_clears_memory_and_file(self):
        # RED on old code: MonitorEngine 根本没有 note_task_deleted
        cfg, st = self._patch_gui_paths()
        e = engine_mod.MonitorEngine(config_path=cfg, setup_logging=False)
        self.assertIn("t1", e.state["tasks"])
        e.note_task_deleted("t1")
        self.assertNotIn("t1", e.state["tasks"])
        self.assertNotIn("t1", self._read(st)["tasks"])

    def test_note_task_deleted_missing_name_is_noop(self):
        # 回归 pin：删不存在的任务名不抛异常、不污染 state
        cfg, st = self._patch_gui_paths()
        e = engine_mod.MonitorEngine(config_path=cfg, setup_logging=False)
        e.note_task_deleted("ghost")
        self.assertIn("t1", e.state["tasks"])

    def test_inflight_failure_does_not_resurrect_deleted_task(self):
        # 删任务撞上在途轮询：在途失败不复活已清掉的 state 条目
        cfg, st = self._patch_gui_paths()
        e = engine_mod.MonitorEngine(config_path=cfg, setup_logging=False)
        e.note_task_deleted("t1")
        self.assertNotIn("t1", e.state["tasks"])
        e._note_failure({"name": "t1", "from": "A", "to": "B"}, "查询异常")
        self.assertNotIn("t1", e.state["tasks"])
        self.assertNotIn("t1", self._read(st)["tasks"])

    def test_run_task_skipped_for_deleted_task(self):
        # 已删除任务的轮询直接跳过：不重建 state 条目、不触发下单
        cfg, st = self._patch_gui_paths()
        e = engine_mod.MonitorEngine(config_path=cfg, setup_logging=False)
        e.note_task_deleted("t1")
        with mock.patch.object(engine_mod.order_mod, "order_ticket",
                               side_effect=AssertionError("must not order")):
            broke, recoverable = e._run_task({"name": "t1", "from": "A",
                                              "to": "B", "dates": [],
                                              "trains": []})
        self.assertTrue(broke)
        self.assertFalse(recoverable)
        self.assertNotIn("t1", e.state["tasks"])
        self.assertNotIn("t1", self._read(st)["tasks"])

    def test_tombstone_cleared_when_task_recreated(self):
        # 同名重建后墓碑清除：在途失败恢复正常记录，新任务不受影响
        cfg, st = self._patch_gui_paths()
        e = engine_mod.MonitorEngine(config_path=cfg, setup_logging=False)
        e.note_task_deleted("t1")
        self.assertIn("t1", e._deleted_names)
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"tasks": [{"name": "t1", "uid": "u1"}],
                       "state_file": st}, f)
        e._config_mtime = None  # 强制触发 _sync_config
        self.assertTrue(e._sync_config())
        self.assertNotIn("t1", e._deleted_names)
        e._note_failure({"name": "t1", "from": "A", "to": "B"}, "查询异常")
        self.assertIn("t1", e.state["tasks"])


class TestReloginScriptResult(TempDirCase):
    """Task 53(b): capture_session.py 重登链按退出码判定成败。

    改前：subprocess.run(...) 后 ok = True 硬编码——退出码 1（失败）的登录
    也被当成成功，_after_relogin 置侧边栏"已登录"。
    改后：退出码非 0 → ok=False 并如实提示；超时同样失败（不挂死）。
    """

    def test_script_exit_1_is_not_ok(self):
        # RED on old code: 根本没有 _relogin_ok_via_script（AttributeError）；
        # 旧内联代码在 returncode=1 时仍置 ok=True（失败被当成功）
        proc = mock.Mock()
        proc.returncode = 1
        with mock.patch.object(gui.subprocess, "run",
                               return_value=proc) as run:
            self.assertFalse(gui._relogin_ok_via_script("capture_session.py"))
        run.assert_called_once()

    def test_script_exit_0_is_ok(self):
        # 回归 pin：成功路径语义不变
        proc = mock.Mock()
        proc.returncode = 0
        with mock.patch.object(gui.subprocess, "run", return_value=proc):
            self.assertTrue(gui._relogin_ok_via_script("capture_session.py"))

    def test_script_timeout_is_failure_not_hang(self):
        # 300s 超时 → False（不挂死、不抛到界面）
        import subprocess as _sp
        with mock.patch.object(gui.subprocess, "run",
                               side_effect=_sp.TimeoutExpired("cmd", 300)):
            self.assertFalse(gui._relogin_ok_via_script("capture_session.py"))


if __name__ == "__main__":
    unittest.main(verbosity=2)


