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
import subprocess
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
import station_db as station_db_mod   # noqa: E402


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
        # 乘车人解析失败时记 warning 后跳过，不再保守判重（Task 59）
        with self.assertLogs(order_mod.LOG, level="WARNING"):
            self.assertIsNone(order_mod.find_duplicate(
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
        # 切分走 _beijing_today()（Task 79b）：用 holder  patch 该 seam，
        # 无论 emit/_open 内部调几次都稳定。
        holder = {"d": d1}
        with mock.patch.object(logutil, "_beijing_today",
                               side_effect=lambda: holder["d"]):
            lg.info("day1")
            holder["d"] = d2
            lg.info("day2")
        h.close()
        files = sorted(os.listdir(self.tmp))
        self.assertEqual(files, ["test_%s.log" % d1.strftime("%Y%m%d"),
                                 "test_%s.log" % d2.strftime("%Y%m%d")])


class TestTask79Logutil(TempDirCase):
    """Task 79: logutil P3（close 持锁 / 北京时间切分 / close 后 emit /
    retention<=0 / 注释）。(e) 为纯注释修正，无测试。"""

    def _rec(self, msg="x"):
        return logging.LogRecord("t79", logging.INFO, __file__, 1,
                                 msg, None, None)

    # (a) close() 必须持 handler 锁（与 stdlib Handler.close() 同序），
    # 否则与并发 emit 交错时会写到已关闭文件。
    def test_close_holds_handler_lock(self):
        h = logutil.DayFileHandler(self.tmp, "t")
        h.emit(self._rec())
        acquired = []
        orig = h.acquire

        def spy():
            acquired.append(True)
            return orig()

        h.acquire = spy
        h.close()
        self.assertTrue(acquired, "close() 未持 handler 锁")

    # (c) close() 后 emit 不得静默重开文件，应走 handleError
    #（与 FileHandler 口径一致）。
    def test_emit_after_close_goes_to_handle_error(self):
        h = logutil.DayFileHandler(self.tmp, "t")
        h.emit(self._rec("one"))
        h.close()
        created = [f for f in os.listdir(self.tmp) if f.startswith("t_")]
        self.assertEqual(len(created), 1)
        os.remove(os.path.join(self.tmp, created[0]))
        with mock.patch.object(h, "handleError") as mh:
            h.emit(self._rec("two"))
        self.assertTrue(mh.called, "close 后 emit 应走 handleError")
        self.assertEqual(
            [f for f in os.listdir(self.tmp) if f.startswith("t_")], [],
            "close 后 emit 静默重开了日志文件")

    # (b) 文件名切分必须用北京时间，而不是机器本地时区。
    def test_open_uses_beijing_today(self):
        import datetime as dt
        fake = dt.date(2031, 12, 25)
        with mock.patch.object(logutil, "_beijing_today", return_value=fake):
            h = logutil.DayFileHandler(self.tmp, "t")
            try:
                h.emit(self._rec())
            finally:
                h.close()
        self.assertTrue(
            os.path.exists(os.path.join(self.tmp, "t_20311225.log")),
            "文件名未使用北京时间日期")

    # (b) 北京时间 helper 本体：UTC+8（与 order._beijing_tz 同语义）。
    def test_beijing_tz_is_utc_plus_8(self):
        import datetime as dt
        off = logutil._beijing_tz().utcoffset(dt.datetime(2026, 1, 1))
        self.assertEqual(off, dt.timedelta(hours=8))

    # (d) retention_days<=0 视为禁用清理：旧文件也不删。
    def test_retention_zero_disables_pruning(self):
        import datetime as dt
        h = logutil.DayFileHandler(self.tmp, "t", retention_days=0)
        today = dt.date.today().strftime("%Y%m%d")
        keep_today = os.path.join(self.tmp, "t_%s.log" % today)
        keep_old = os.path.join(self.tmp, "t_20000101.log")
        open(keep_today, "w").write("today")
        open(keep_old, "w").write("old")
        h._prune_old_logs(dt.date.today())
        self.assertTrue(os.path.exists(keep_old),
                        "retention_days=0 应禁用清理")
        self.assertTrue(os.path.exists(keep_today))

    # (d) retention_days<0 时 cutoff 落在未来：绝不能删当天文件。
    def test_retention_negative_keeps_today_file(self):
        import datetime as dt
        h = logutil.DayFileHandler(self.tmp, "t", retention_days=-1)
        today = dt.date.today().strftime("%Y%m%d")
        keep_today = os.path.join(self.tmp, "t_%s.log" % today)
        open(keep_today, "w").write("today")
        h._prune_old_logs(dt.date.today())
        self.assertTrue(os.path.exists(keep_today),
                        "retention_days<0 删除了当天文件")


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
        # Task 47: 不限席别只收可下单席别（展示类席别名被过滤）；
        # Task 54: 按 SEAT_SHOW_ORDER 稳定排序（硬卧 < 硬座），不跟随 avail 插入序
        self.assertEqual(ticket.seat_candidates_for("K225", [], "", avail),
                         ["硬卧", "硬座"])

    def test_candidates_unrestricted_filters_display_only(self):
        # Task 47: 「不限席别」分支必须过滤掉不可下单的展示类席别名，
        # 否则 launcher 按名索引 SEAT_NAME_TO_CODE 会潜伏 KeyError。
        avail = {"硬座": "5", "高级动卧": "有", "其他": "有",
                 "一等卧": "有", "二等卧": "有", "硬卧": "有"}
        # Task 54: 按 SEAT_SHOW_ORDER 稳定排序（硬卧 < 硬座）
        self.assertEqual(ticket.seat_candidates_for("K225", [], "", avail),
                         ["硬卧", "硬座"])

    def test_candidates_unrestricted_stable_show_order(self):
        # Task 54: 「不限席别」候选不再跟随 avail 插入序，而是按 SEAT_SHOW_ORDER
        # 稳定排序（贵的在前，无座垫底）——避免展示名抢先、误报「有票」。
        avail = {"硬卧": "有", "硬座": "5", "软卧": "有"}  # 插入序：硬卧, 硬座, 软卧
        self.assertEqual(ticket.seat_candidates_for("K225", [], "", avail),
                         ["软卧", "硬卧", "硬座"])  # SEAT_SHOW_ORDER 序

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

    # 产品代码文件全集（Task 80a：旧版只扫 6 个文件，有盲区）。
    # 测试文件本身不扫，避免测试固件里的字符串污染扫描。
    _PRODUCT_FILES = ["appcommon.py", "browser_order.py", "capture_session.py",
                      "config_keys.py", "engine.py", "filelock.py", "gui.py",
                      "launcher.py", "logutil.py", "monitor.py", "notify.py",
                      "order.py", "passengers.py", "probe_login.py",
                      "station_db.py", "ticket.py"]

    def _scan(self, pattern, files=None):
        keys = set()
        for f in files or self._PRODUCT_FILES:
            for m in re.finditer(pattern, open(f, encoding="utf-8").read()):
                keys.add(m.group(1))
        return keys

    def test_config_example_covers_code_reads(self):
        import config_keys
        live = json.load(open(os.path.join(HERE, "config.example.json"),
                              encoding="utf-8"))
        # Task 80a：补 self.config.get( 与 config["x"] 下标形态；
        # cfg.get( 在 notify.py 里读的是 email 字典而非顶层 config，
        # 故顶层断言排除 notify.py（email 键由下面的反向断言覆盖）。
        others = [f for f in self._PRODUCT_FILES if f != "notify.py"]
        code_keys = (self._scan(r"(?<![\w.])config\.get\(\s*['\"](\w+)[\"']")
                     | self._scan(r"(?<![\w.])cfg\.get\(\s*['\"](\w+)[\"']", others)
                     | self._scan(r"self\.config\.get\(\s*['\"](\w+)[\"']")
                     | self._scan(r"(?<![\w.])config\[['\"](\w+)[\"']")
                     # Task 101c：补 2 类漏报形态——json.load(f).get("k")
                     # （capture_session 读 config.json）、config/cfg.setdefault("k"）
                     # （读兼写，键仍须文档化）
                     | self._scan(r"json\.load\(\w+\)\.get\(\s*['\"](\w+)[\"']")
                     | self._scan(r"(?<![\w.])config\.setdefault\(\s*['\"](\w+)[\"']")
                     | self._scan(r"(?<![\w.])cfg\.setdefault\(\s*['\"](\w+)[\"']",
                                   others))
        self.assertTrue(code_keys <= config_keys.CONFIG_KEYS,
                        sorted(code_keys - config_keys.CONFIG_KEYS))
        self.assertTrue(config_keys.CONFIG_KEYS <= set(live),
                        sorted(config_keys.CONFIG_KEYS - set(live)))
        email = live.get("notify", {}).get("email", {})
        self.assertTrue(config_keys.NOTIFY_EMAIL_KEYS <= set(email))

    def test_notify_email_keys_covers_code_reads(self):
        # Task 80a 新增反向断言：代码实际读取的 email 键 ⊆ NOTIFY_EMAIL_KEYS。
        # 旧版只有"集合 ⊆ 模板"方向，代码新增读取键时不报警。
        import config_keys
        reads = (self._scan(r"(?<![\w.])email\.get\(\s*['\"](\w+)[\"']")
                 | self._scan(r"(?<![\w.])email\[['\"](\w+)[\"']")
                 | self._scan(r"self\.email\.get\(\s*['\"](\w+)[\"']")
                 # Task 101c：notify.email 子字典的别名变量（engine.py email_cfg /
                 # launcher.py nc），读的仍是 email 叶键
                 | self._scan(r"(?<![\w.])email_cfg\.get\(\s*['\"](\w+)[\"']")
                 | self._scan(r"(?<![\w.])nc\.get\(\s*['\"](\w+)[\"']"))
        # notify.py 内 send_email/_safe_port 的 cfg 形参即 email 字典；
        # __main__ 块的 cfg["notify"] 读的是顶层 config，需排除。
        notify_cfg = (self._scan(r"(?<![\w.])cfg\.get\(\s*['\"](\w+)[\"']", ["notify.py"])
                      | {k for k in self._scan(r"(?<![\w.])cfg\[['\"](\w+)[\"']",
                                               ["notify.py"])
                         if k != "notify"})
        reads |= notify_cfg
        self.assertTrue(reads <= config_keys.NOTIFY_EMAIL_KEYS,
                        sorted(reads - config_keys.NOTIFY_EMAIL_KEYS))

    def test_scan_catches_subscript_shape(self):
        # 合成固件：下标形态 config["x"] 必须被扫描到（旧 _scan 只有
        # config.get(/cfg.get( 两种形态，会漏掉它）。
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".py",
                                          delete=False) as f:
            f.write('x = config["ghost_key"]\n')
            path = f.name
        try:
            found = self._scan(r"(?<![\w.])config\[['\"](\w+)[\"']", [path])
        finally:
            os.unlink(path)
        self.assertEqual(found, {"ghost_key"})

    def test_scan_catches_task101_shapes(self):
        # Task 101c：5 类曾漏报的形态必须被新正则扫到（合成固件）。
        import tempfile
        src = ('a = email_cfg.get("ghost_a")\n'
               'b = nc.get("ghost_b")\n'
               'c = json.load(f).get("ghost_c")\n'
               'd = config.setdefault("ghost_d", {})\n'
               'e = lc["ghost_e"]\n'
               'f = self.lc["ghost_f"]\n')
        with tempfile.NamedTemporaryFile("w", suffix=".py",
                                          delete=False) as f:
            f.write(src)
            path = f.name
        try:
            s = self._scan
            self.assertEqual(s(r"(?<![\w.])email_cfg\.get\(\s*['\"](\w+)[\"']",
                              [path]), {"ghost_a"})
            self.assertEqual(s(r"(?<![\w.])nc\.get\(\s*['\"](\w+)[\"']",
                              [path]), {"ghost_b"})
            self.assertEqual(s(r"json\.load\(\w+\)\.get\(\s*['\"](\w+)[\"']",
                              [path]), {"ghost_c"})
            self.assertEqual(s(r"(?<![\w.])config\.setdefault\(\s*['\"](\w+)[\"']",
                              [path]), {"ghost_d"})
            self.assertEqual(s(r"(?<![\w.])lc\[['\"](\w+)[\"']", [path]),
                             {"ghost_e"})
            self.assertEqual(s(r"self\.lc\[['\"](\w+)[\"']", [path]),
                             {"ghost_f"})
        finally:
            os.unlink(path)

    def test_launcher_example_covers_code_reads(self):
        import config_keys
        live = json.load(open(os.path.join(HERE, "launcher_config.example.json"),
                              encoding="utf-8"))
        code_keys = (self._scan(r"(?<![\w.])lc\.get\(\s*['\"](\w+)[\"']")
                     | self._scan(r"(?<![\w.])self\.lc\.get\(\s*['\"](\w+)[\"']")
                     # Task 101c：lc["k"] / self.lc["k"] 下标形态（含写入点；
                     # 写进去的键同样必须文档化，故一并纳入）
                     | self._scan(r"(?<![\w.])lc\[['\"](\w+)[\"']")
                     | self._scan(r"self\.lc\[['\"](\w+)[\"']"))
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
        self.assertEqual(appcommon.read_state_or_none(p), ({}, None, None))   # 不存在
        json.dump({"tasks": {}}, open(p, "w", encoding="utf-8"))
        st, err, _fp = appcommon.read_state_or_none(p)
        self.assertIsNone(err)
        self.assertEqual(st, {"tasks": {}})
        open(p, "w", encoding="utf-8").write("{corrupt")
        st2, err2, _fp2 = appcommon.read_state_or_none(p)
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
                        st, err, _fp = appcommon.read_state_or_none(p)
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
                    st, _, _ = appcommon.read_state_or_none(p)
                    st = st or {}
                    st.setdefault("tasks", {})["g%d" % i] = n
                    appcommon.write_state(p, st, tmp_kind="guisave")
            except Exception as e:
                errs.append(e)

        def launcher_like(i):
            try:
                for n in range(30):
                    st, _, _ = appcommon.read_state_or_none(p)
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
        # Task 74b：kind_rank 生效后"cq"系查询首选为普速优先的重庆北（同上注释）；
        # 本用例 pin 的是全角/空格容错（仍有效），不是首选站本身。
        self.assertEqual(launcher.search_stations("ｃｑ")[0]["name"], "重庆北")   # 全角
        r = [x["name"] for x in launcher.search_stations("长　葛")]
        self.assertEqual(r[0], "长葛")                                        # 全角空格+忽略空白
        r2 = launcher.search_stations("chong qing")
        self.assertEqual(r2[0]["name"], "重庆")  # 空格剔除：拼音全拼精确命中仍优先

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
        # Task 74b 后 kind_rank 生效（普速优先）：重庆北/东/西均为"高铁+动车+普速"
        # 含普速 → rank 0，排在无 kind 记录的重庆(CQW) 之前。旧期望"重庆"是
        # kind 查询恒 miss 时的失效排序。
        self.assertEqual(launcher.search_stations("cq")[0]["name"], "重庆北")
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
        import notify as notify_mod
        saved = {}
        with mock.patch.object(monitor_mod, "load_config", return_value={}), \
             mock.patch.object(monitor_mod, "read", side_effect=[
                 "", "587", "u@x.com", "", "a@x.com", "n"]), \
             mock.patch("getpass.getpass", return_value="pw"), \
             mock.patch.object(monitor_mod, "save_config",
                               side_effect=lambda c, **k: saved.update(c)), \
             mock.patch.object(notify_mod, "protect_secret",
                               side_effect=lambda t: t):
            monitor_mod.menu_notify()
        self.assertEqual(saved["notify"]["email"]["smtp_port"], 587)
        self.assertEqual(saved["notify"]["email"]["to"], ["a@x.com"])
        self.assertEqual(saved["notify"]["email"]["password"], "pw")


class TestMonitorInterruptRound3(TempDirCase):
    """Task 28 round 3 (P1): menu_passengers 编辑/删除/设默认的序号输入
    在 Ctrl+C/EOF（read 返回 None）时 None.isdigit() 抛 AttributeError
    打 traceback。Task 83(b) 起：取消本次操作，回到乘车人子菜单
    （与 op1 口径统一），不再 raise KeyboardInterrupt 回主菜单。"""

    SAMPLE = [{"name": "张三", "id_type_code": "1", "id_no": "110101199001011234",
               "mobile": "13800138000", "is_default": False, "is_adult": True}]

    def _run_menu(self, reads):
        import monitor as monitor_mod
        pm = mock.MagicMock()
        pm.load_passengers.return_value = [dict(self.SAMPLE[0])]
        pm.ID_TYPE_NAMES = {"1": "二代身份证"}
        with mock.patch.object(monitor_mod, "passengers_mod", pm), \
             mock.patch.object(monitor_mod, "read", side_effect=list(reads)), \
             mock.patch("builtins.print") as mprint:
            monitor_mod.menu_passengers()  # 不得抛 KeyboardInterrupt
        printed = " ".join(str(c.args[0]) for c in mprint.call_args_list)
        return pm, printed

    def test_edit_index_none_cancels_to_submenu(self):
        # Task 83(b)："2" 进编辑分支，"编辑第几位"时 Ctrl+C/EOF →
        # 取消本次操作回到子菜单，不抛 KeyboardInterrupt，不落盘。
        pm, printed = self._run_menu(["2", None, "0"])
        self.assertIn("已取消", printed)
        pm.save_passengers.assert_not_called()

    def test_delete_index_none_cancels_to_submenu(self):
        # Task 83(b)："3" 进删除分支：同上。
        pm, printed = self._run_menu(["3", None, "0"])
        self.assertIn("已取消", printed)
        pm.save_passengers.assert_not_called()

    def test_setdefault_index_none_cancels_to_submenu(self):
        # Task 83(b)："4" 进设默认分支：同上。
        pm, printed = self._run_menu(["4", None, "0"])
        self.assertIn("已取消", printed)
        pm.save_passengers.assert_not_called()

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
        # 放弃隔离后重读仍失败（文件被删）：不崩、不循环；放弃本次追加并记
        # error，不用空数据覆写（Task 81c；旧行为会建出 [G9] 覆盖并发写入）。
        import appcommon
        orig = appcommon.quarantine_corrupt

        def sneaky_delete(path, fp=None):
            os.remove(path)
            return orig(path, fp)

        p = os.path.join(self.tmp, "order_history.json")
        self._write(p, "{CORRUPT")
        with mock.patch.object(appcommon, "quarantine_corrupt", sneaky_delete):
            with self.assertLogs("monitor", level="ERROR"):
                appcommon.append_history(p, {"train": "G9"})
        self.assertFalse(os.path.exists(p), "重读失败不得覆写")
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
                               return_value=(None, PermissionError("被占用"),
                                             None)), \
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
                               return_value=(None, OSError("I/O error"),
                                             None)), \
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
                               return_value=(None, err, None)), \
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
        # 整个读-改-写包在 filelock.file_lock(path + ".lock") 内（Task 81a：
        # sidecar 锁文件，与 engine/gui/monitor/launcher 的约定一致）：
        # 持锁时另一线程追加必须等待
        import appcommon, filelock, time
        p = os.path.join(self.tmp, "order_history.json")
        appcommon.append_history(p, {"n": 0})
        acquired = []
        with filelock.file_lock(p + ".lock"):
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


class TestTask78ProbeLogin(unittest.TestCase):
    """Task 78: probe_login P3 bundle（a–g）。

    (a) step5 登录失败仍无条件写盘 → logged_in=False 时不写盘
    (b) _PII_VALUE_RE 补全 phone_no/email/address/born_date
    (c) step5 的 show() 加 mask_pii=True；username 打印脱敏
    (d) step3_create_qr 非 dict JSON 不抛 AttributeError
    (e) _confirm_qr_refresh 捕获 OSError 回退自动刷新
    (f) code==2 缺 uamtk → 明确报错"已扫码但会话换取失败"，不误报没扫码
    (g) step4 轮询 Ctrl+C 优雅退出（Task 28 口径）
    """

    # ---- (a) 登录失败不写盘 ----

    def _run_main_with_login(self, logged_in):
        for p in (
            mock.patch.object(probe_login, "step1_connectivity"),
            mock.patch.object(probe_login, "step2_bootstrap_cookies"),
            mock.patch.object(probe_login, "step3_create_qr", return_value="fake-uuid"),
            mock.patch.object(probe_login, "step4_poll_qr", return_value="fake-uamtk"),
            mock.patch.object(probe_login, "step5_finish_login", return_value=logged_in),
            mock.patch.object(probe_login, "step6_verify_session", return_value=False),
            mock.patch.object(probe_login, "step7_save_cookies"),
        ):
            p.start()
            self.addCleanup(p.stop)
        probe_login.main()
        return probe_login.step7_save_cookies

    def test_step5_failure_does_not_write_cookies(self):
        # RED on old code: logged_in=False 仍调 step7_save_cookies 写盘
        save = self._run_main_with_login(False)
        save.assert_not_called()

    def test_step5_success_still_saves_cookies(self):
        # pin：登录成功路径仍保存（新旧代码都通过）
        save = self._run_main_with_login(True)
        save.assert_called_once()

    # ---- (b) 脱敏字段补全 ----

    def test_mask_pii_covers_new_fields(self):
        # RED on old code: phone_no/email/address/born_date 明文残留
        body = ('{"passenger_name":"张三","phone_no":"13800138000",'
                '"email":"zhangsan@example.com","address":"北京市朝阳区",'
                '"born_date":"1990-01-01","mobile_no":"13800138000"}')
        masked = probe_login._mask_pii(body)
        for secret in ("13800138000", "zhangsan@example.com",
                       "北京市朝阳区", "1990-01-01", "张三"):
            self.assertNotIn(secret, masked)
        # 字段名保留（诊断价值：结构可见）
        for field in ("phone_no", "email", "address", "born_date"):
            self.assertIn('"%s"' % field, masked)

    def test_mask_pii_handles_unicode_escapes(self):
        # RED on old code: \uXXXX 转义值同样明文残留
        body = r'{"phone_no": "\u0031\u0033\u0038\u0030\u0030\u0031\u0033\u0038\u0030\u0030\u0030"}'
        masked = probe_login._mask_pii(body)
        self.assertNotIn("\\u0031", masked)
        self.assertIn('"phone_no"', masked)

    # ---- (c) step5 脱敏 ----

    def _run_step5(self):
        auth = _FakeResp('{"newapptk": "tk123"}')
        uam = _FakeResp('{"result_code": 0, "username": "张三"}')
        fake = mock.Mock()
        fake.post.side_effect = [auth, uam]
        return fake

    def test_step5_show_calls_use_mask_pii(self):
        # RED on old code: show(r)/show(r2) 未脱敏（mask_pii=False）
        calls = []

        def fake_show(resp, limit=300, mask_pii=False):
            calls.append(mask_pii)

        with mock.patch.object(probe_login, "SESSION", self._run_step5()), \
             mock.patch.object(probe_login, "show", side_effect=fake_show):
            self.assertTrue(probe_login.step5_finish_login("uamtk-x"))
        self.assertEqual(calls, [True, True])

    def test_step5_username_print_is_masked(self):
        # RED on old code: username 明文打印（uamauthclient 的 username 常为真实姓名）
        with mock.patch.object(probe_login, "SESSION", self._run_step5()), \
             mock.patch("builtins.print") as mprint:
            probe_login.step5_finish_login("uamtk-x")
        out = "\n".join(str(c.args[0]) for c in mprint.call_args_list if c.args)
        self.assertNotIn("张三", out)

    # ---- (d) 非 dict JSON ----

    def test_step3_create_qr_non_dict_json_no_crash(self):
        # RED on old code: data.get → AttributeError 逃出
        fake = mock.Mock()
        fake.post.return_value = _FakeResp("null")  # r.json() 成功但返回 None
        with mock.patch.object(probe_login, "SESSION", fake), \
             mock.patch("builtins.print") as mprint:
            self.assertIsNone(probe_login.step3_create_qr())
        out = "\n".join(str(c.args[0]) for c in mprint.call_args_list if c.args)
        self.assertIn("非 JSON 对象", out)  # 记 warning，不抛异常

    # ---- (e) OSError 回退 ----

    def test_confirm_qr_refresh_oserror_falls_back_to_auto(self):
        # RED on old code: OSError(Errno 9) 逃出 → 二维码过期时崩溃
        with mock.patch("builtins.input",
                        side_effect=OSError(9, "Bad file descriptor")), \
             mock.patch("builtins.print") as mprint:
            self.assertTrue(probe_login._confirm_qr_refresh())
        out = "\n".join(str(c.args[0]) for c in mprint.call_args_list if c.args)
        self.assertIn("自动刷新", out)

    # ---- (f) code==2 缺 uamtk ----

    def _run_step4_code2_no_uamtk(self):
        fake = mock.Mock()
        fake.post.return_value = _FakeResp(
            '{"result_code": 2, "result_message": "已确认", "uamtk": null}')
        return fake

    def test_step4_poll_qr_code2_without_uamtk_reports_explicit_error(self):
        # RED on old code: 返回 None，main() 误报"没扫码"
        with mock.patch.object(probe_login, "SESSION", self._run_step4_code2_no_uamtk()), \
             mock.patch("builtins.print") as mprint, \
             mock.patch("time.sleep"):
            result = probe_login.step4_poll_qr("uuid-x")
        self.assertIs(result, probe_login.SCAN_CONFIRMED_NO_UAMTK)
        out = "\n".join(str(c.args[0]) for c in mprint.call_args_list if c.args)
        self.assertIn("已扫码但会话换取失败", out)

    def test_main_no_uamtk_does_not_claim_not_scanned(self):
        # RED on old code: main() 打印"若是超时，说明只是没扫码"误报
        for p in (
            mock.patch.object(probe_login, "step1_connectivity"),
            mock.patch.object(probe_login, "step2_bootstrap_cookies"),
            mock.patch.object(probe_login, "step3_create_qr", return_value="fake-uuid"),
            mock.patch.object(probe_login, "step4_poll_qr",
                              return_value=probe_login.SCAN_CONFIRMED_NO_UAMTK),
            mock.patch.object(probe_login, "step7_save_cookies"),
        ):
            p.start()
            self.addCleanup(p.stop)
        with mock.patch("builtins.print") as mprint:
            probe_login.main()
        out = "\n".join(str(c.args[0]) for c in mprint.call_args_list if c.args)
        self.assertIn("已扫码但会话换取失败", out)
        self.assertNotIn("只是没扫码", out)
        probe_login.step7_save_cookies.assert_not_called()

    # ---- (g) Ctrl+C 优雅退出 ----

    def test_step4_poll_qr_keyboard_interrupt_exits_gracefully(self):
        # RED on old code: KeyboardInterrupt 逃出 → traceback
        fake = mock.Mock()
        fake.post.side_effect = KeyboardInterrupt()
        with mock.patch.object(probe_login, "SESSION", fake), \
             mock.patch("builtins.print") as mprint:
            try:
                result = probe_login.step4_poll_qr("uuid-x")
            except KeyboardInterrupt:
                self.fail("KeyboardInterrupt 逃出 step4_poll_qr（应优雅退出）")
        self.assertIsNone(result)
        out = "\n".join(str(c.args[0]) for c in mprint.call_args_list if c.args)
        self.assertIn("取消", out)


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
                               side_effect=lambda c, **k: saved.update(c)):
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
                               side_effect=lambda c, **k: saved.update(c)):
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

    def test_inflight_set_task_status_does_not_resurrect_deleted_task(self):
        # 返工（reviewer Important）：set_task_status 内部的 setdefault 会把
        # 已删除任务的 state 条目复活——在途轮询的后续状态写入不得复活
        cfg, st = self._patch_gui_paths()
        e = engine_mod.MonitorEngine(config_path=cfg, setup_logging=False)
        e.note_task_deleted("t1")
        e.set_task_status({"name": "t1"}, "failed", "在途轮询写回")
        self.assertNotIn("t1", e.state["tasks"])
        self.assertNotIn("t1", self._read(st)["tasks"])

    def test_set_task_status_force_resurrects_after_recreate(self):
        # 墓碑不误拦合法任务：force=True（GUI 显式恢复/重置/新建）可放行
        cfg, st = self._patch_gui_paths()
        e = engine_mod.MonitorEngine(config_path=cfg, setup_logging=False)
        e.note_task_deleted("t1")
        e.set_task_status({"name": "t1"}, "monitoring", "GUI 恢复",
                          force=True)
        self.assertIn("t1", e.state["tasks"])
        self.assertEqual(e.state["tasks"]["t1"]["status"], "monitoring")


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


class TestTask55ExcStreakStop(TempDirCase):
    """Task 55(a): 下单异常被 except 吞掉 + 5 秒重试 → 无限活锁。

    同一异常连续 3 次 → 计入失败走正常停止逻辑，不再无声自旋。
    """

    def _make_grabber(self, max_waits=10):
        import queue
        lc = {"from": "北京", "to": "上海", "date": "2026-10-10",
              "trains": ["G101"], "seat_types": ["二等座"],
              "seat_priority": "", "passenger_names": ["张三"]}
        g = launcher.Grabber(lc, logq=queue.Queue())
        g._log = g.logq.put  # 不写真实日志文件
        waits = []
        g.stop_event = mock.Mock()
        g.stop_event.is_set.return_value = False

        def fake_wait(s):
            waits.append(s)
            return len(waits) >= max_waits  # 旧代码：兜底停转，防测试无限循环
        g.stop_event.wait.side_effect = fake_wait
        return g, waits

    def _patch_loop(self, order_side_effect):
        train = {"train_code": "G101", "from_name": "北京", "to_name": "上海",
                 "start_time": "08:00", "available_seats": {"二等座": "有"}}
        return (
            mock.patch.object(ticket, "load_station_map",
                              return_value=({"北京": "BJP", "上海": "SHH"},
                                            {"BJP": "北京", "SHH": "上海"})),
            mock.patch.object(launcher.browser_order, "busy", return_value=False),
            mock.patch.object(launcher.browser_order, "check_session",
                              return_value=(True, "user")),
            mock.patch.object(ticket, "query_tickets", return_value=[{"row": 1}]),
            mock.patch.object(ticket, "parse_row", return_value=train),
            mock.patch.object(ticket, "seat_candidates_for",
                              return_value=["二等座"]),
            mock.patch.object(launcher.browser_order, "order_via_browser",
                              side_effect=order_side_effect),
        )

    def test_same_keyerror_3_times_triggers_stop(self):
        # 旧代码：KeyError 被吞 → 5 秒重试无限活锁，result 永不置位
        g, waits = self._make_grabber()
        patches = self._patch_loop(KeyError("boom"))
        with patches[0], patches[1], patches[2], patches[3], \
                patches[4], patches[5], patches[6]:
            g._run()
        self.assertIsNotNone(g.result, "同一 KeyError 连续 3 次仍无声自旋，未走停止逻辑")
        ok, msg = g.result
        self.assertFalse(ok)
        self.assertIn("连续", msg)
        self.assertIn("自动停止", msg)
        # 第 3 次直接停止，不再有第 3 个 5 秒等待
        self.assertEqual(waits, [5, 5])

    def test_different_exceptions_reset_streak(self):
        # pin：异常类型/消息变化 → 计数重置，不误触发停止
        g, waits = self._make_grabber(max_waits=8)
        seq = [KeyError("a"), ValueError("b")]

        def boom(*a, **k):
            e = seq.pop(0)
            seq.append(e)
            raise e

        patches = self._patch_loop(boom)
        with patches[0], patches[1], patches[2], patches[3], \
                patches[4], patches[5], patches[6]:
            g._run()
        self.assertIsNone(g.result, "交替出现的异常不应触发连续停止：%r" % (g.result,))
        self.assertEqual(len(waits), 8)


class TestTask55AutoFired(TempDirCase):
    """Task 55(b): auto_fired 在 _validate() 之前置位 → 校验失败即缴械整点自动开抢。

    改后：_validate() 通过之后才置位；失败时保持 False，允许下个 _tick 重试。
    """

    def _make_app(self, lc):
        app = launcher.LauncherApp.__new__(launcher.LauncherApp)
        app.lc = lc
        app.grabber = None
        app.auto_fired = False
        app._auto_vfail_last_msg = None
        app._mp = None
        app._top = mock.Mock()
        logs = []
        app._put_log = logs.append
        app._ui_to_lc = lambda save=True: None  # Task 74d：start_grab 调 _ui_to_lc(save=False)
        app._save_cfg = mock.Mock()
        app._set_status = mock.Mock()
        app.go_btn = mock.Mock()
        import queue
        app.logq = queue.Queue()
        return app, logs

    def _invalid_lc(self):
        return {"from": "", "to": "上海", "date": "2026-10-10",
                "seat_types": ["二等座"], "passenger_names": ["张三"]}

    def _valid_lc(self):
        return {"from": "北京", "to": "上海", "date": "2026-10-10",
                "seat_types": ["二等座"], "passenger_names": ["张三"]}

    def test_auto_validate_fail_keeps_auto_fired_false(self):
        # 旧代码：auto_fired=True 先置位 → 校验失败也永久 True，本整点被缴械
        app, logs = self._make_app(self._invalid_lc())
        app.start_grab(auto=True)
        self.assertFalse(app.auto_fired, "_validate 失败后 auto_fired 必须保持 False")
        self.assertIsNone(app.grabber)

    def test_auto_validate_ok_sets_auto_fired_and_starts(self):
        # 回归 pin：校验通过 → 置位 + 正常启动
        app, logs = self._make_app(self._valid_lc())
        with mock.patch.object(launcher, "Grabber") as MG:
            ret = app.start_grab(auto=True)
        self.assertTrue(ret)
        self.assertTrue(app.auto_fired)
        MG.assert_called_once()
        self.assertIsNotNone(app.grabber)

    def test_manual_start_grab_does_not_touch_auto_fired(self):
        # 回归 pin：手动路径不碰 auto_fired
        app, logs = self._make_app(self._valid_lc())
        with mock.patch.object(launcher, "Grabber"):
            app.start_grab(auto=False)
        self.assertFalse(app.auto_fired)

    def test_auto_grabber_start_failure_rolls_back_auto_fired(self):
        # _validate 通过但线程启动失败 → 回滚置位，允许下个 _tick 重试
        app, logs = self._make_app(self._valid_lc())
        with mock.patch.object(launcher, "Grabber",
                               side_effect=RuntimeError("no threads")):
            ret = app.start_grab(auto=True)
        self.assertFalse(ret)
        self.assertFalse(app.auto_fired)
        self.assertIsNone(app.grabber)
        self.assertTrue(any("启动失败" in l for l in logs))

    def test_auto_validate_fail_bells_once(self):
        # 配套：允许下个 _tick 重试，但 bell 只响一次（防 500ms 一次蜂鸣刷屏）
        app, logs = self._make_app(self._invalid_lc())
        app.start_grab(auto=True)
        app._top.bell.reset_mock()
        app._auto_vfail_last_msg = None  # 模拟新一轮 armed 会话
        app._validate(auto=True)
        app._validate(auto=True)
        app._top.bell.assert_called_once()


class TestFileLockTimeout(TempDirCase):
    """Task 56: filelock 锁争用超时（TimeoutError）在各调用点必须被捕获，
    不能杀死引擎监控线程、不能崩 GUI/CLI 保存。Linux 上用 mock 模拟超时
    （真跨进程争用只在 Windows/msvcrt 或 fcntl 后端长时间持有时发生）。"""

    def _timeout_lock(self):
        return mock.patch("filelock.file_lock",
                          side_effect=TimeoutError("等待文件锁超时：x.lock"))

    def test_engine_load_state_timeout_skips_not_quarantine(self):
        e = make_engine(self.tmp)
        old_state = {"dedup": {"k": 1}, "tasks": {"t1": {}}, "retry": {}}
        e.state = old_state
        # 写一份健康的 state.json：超时绝不能把它当坏档隔离
        with open(e.state_path, "w", encoding="utf-8") as f:
            json.dump(old_state, f)
        with self._timeout_lock(), \
                mock.patch.object(appcommon, "quarantine_corrupt") as q:
            got = e._load_state()
        q.assert_not_called()
        self.assertIs(got, old_state)
        with open(e.state_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), old_state)

    def test_engine_save_state_timeout_skips_write(self):
        e = make_engine(self.tmp)
        before = {"dedup": {}, "tasks": {"t1": {"status": "monitoring"}},
                  "retry": {}}
        with open(e.state_path, "w", encoding="utf-8") as f:
            json.dump(before, f)
        mtime_before = os.path.getmtime(e.state_path)
        e.state = {"dedup": {}, "tasks": {"t1": {"status": "paused"}},
                   "retry": {}}
        with self._timeout_lock():
            e._save_state()
        # 内存态保留、磁盘未动：下次 _save_state 重试
        self.assertEqual(e.state["tasks"]["t1"]["status"], "paused")
        self.assertEqual(os.path.getmtime(e.state_path), mtime_before)
        with open(e.state_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), before)

    def test_engine_sync_state_timeout_keeps_old_state(self):
        e = make_engine(self.tmp)
        old_state = {"dedup": {"k": 1}, "tasks": {}, "retry": {}}
        e.state = old_state
        with open(e.state_path, "w", encoding="utf-8") as f:
            json.dump({"dedup": {}, "tasks": {"t9": {}}, "retry": {}}, f)
        e._state_mtime = 0  # 强制 _sync_state 走重载路径
        with self._timeout_lock():
            e._sync_state()
        # 超时 = 本次跳过：旧内存态保留，不能被 {} 或 None 覆盖
        self.assertIs(e.state, old_state)

    def test_append_history_timeout_skips_record(self):
        path = os.path.join(self.tmp, "order_history.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([{"a": 1}], f)
        with self._timeout_lock():
            appcommon.append_history(path, {"b": 2})
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), [{"a": 1}])

    def test_launcher_append_monitor_task_timeout_user_message(self):
        task = {"name": "t-timeout", "from": "长葛", "to": "确山"}
        with self._timeout_lock():
            with self.assertRaises(TimeoutError) as cm:
                launcher.append_monitor_task(task)
        self.assertIn("文件被占用", str(cm.exception))

    def test_gui_update_config_locked_timeout_warns_user(self):
        with self._timeout_lock(), mock.patch.object(gui, "messagebox") as mb:
            with self.assertRaises(TimeoutError) as cm:
                gui.update_config_locked(lambda c: c)
        self.assertIn("文件被占用，稍后重试", str(cm.exception))
        mb.showwarning.assert_called_once()
        args, _ = mb.showwarning.call_args
        self.assertIn("文件被占用，稍后重试", args[1])

    def test_gui_update_state_locked_timeout_warns_user(self):
        with self._timeout_lock(), mock.patch.object(gui, "messagebox") as mb, \
                mock.patch.object(gui, "load_config",
                                  return_value={"state_file": "state.json"}):
            with self.assertRaises(TimeoutError) as cm:
                gui.update_state_locked(lambda s: s)
        self.assertIn("文件被占用，稍后重试", str(cm.exception))
        mb.showwarning.assert_called_once()

    def test_monitor_save_config_timeout_no_crash(self):
        import monitor as monitor_mod
        with self._timeout_lock(), \
                mock.patch.object(monitor_mod, "print") as mprint:
            with self.assertRaises(TimeoutError) as cm:
                monitor_mod.save_config({})
        self.assertIn("文件被占用，稍后重试", str(cm.exception))
        printed = " ".join(str(c[0][0]) for c in mprint.call_args_list)
        self.assertIn("文件被占用", printed)

    def test_monitor_main_menu_survives_lock_timeout(self):
        import monitor as monitor_mod
        def _boom():
            raise TimeoutError("文件被占用，稍后重试")
        with mock.patch.object(monitor_mod, "read",
                               side_effect=["9", "0"]), \
                mock.patch.object(monitor_mod, "MENU",
                                  [("9", "测试", _boom)]), \
                mock.patch.object(monitor_mod, "pause"), \
                mock.patch.object(monitor_mod, "print"):
            monitor_mod.main_menu()  # 不应抛异常：回到菜单继续


class TestSaveStateInLockRMW(TempDirCase):
    """Task 57: _save_state 读-改-写全程持锁。

    旧代码：snapshot 在拿 file_lock 之前已从内存 deepcopy → 写回时覆盖
    launcher/gui 在竞态窗口内并发写入 state.json 的变更 → 丢任务/状态。
    新行为：锁内重读文件 → 合并内存变更 → 写回。
    合并规则：tasks 取并集（冲突时内存赢；墓碑名剔除）；dedup/retry 内存为准。
    """

    def test_launcher_concurrent_task_not_lost(self):
        # 核心回归：launcher 在引擎 _sync_state 之后、_save_state 落盘之前
        # 并发写入的新任务，不得被引擎的写回覆盖。
        e = make_engine(self.tmp)
        e.state = {"dedup": {}, "tasks": {"A": {"status": "monitoring"}},
                   "retry": {}}
        e._save_state()
        # 模拟 launcher.py:969 的并发 RMW：同锁内读→加任务 B→写回
        with filelock.file_lock(e.state_path + ".lock"):
            with open(e.state_path, encoding="utf-8") as f:
                disk = json.load(f)
            disk.setdefault("tasks", {})["B"] = {"status": "paused"}
            appcommon.atomic_write_json(e.state_path, disk,
                                        fallback_direct=True)
        # 引擎内存仍只知 A（_sync_state 尚未跑——这就是竞态窗口）
        self.assertNotIn("B", e.state["tasks"])
        e._save_state()
        with open(e.state_path, encoding="utf-8") as f:
            final = json.load(f)
        self.assertIn("A", final["tasks"])
        self.assertIn("B", final["tasks"])  # 旧代码：B 丢失

    def test_tombstoned_task_not_resurrected_by_merge(self):
        # 合并不得复活墓碑任务（Task 53）：内存已删 + 墓碑，磁盘残留 A
        # 在并发窗口内出现 → 写回不得带回 A。
        e = make_engine(self.tmp)
        e.state = {"dedup": {}, "tasks": {"A": {"status": "monitoring"}},
                   "retry": {}}
        e._save_state()
        e.state["tasks"].pop("A")
        e._deleted_names = {"A"}
        e._save_state()
        with open(e.state_path, encoding="utf-8") as f:
            final = json.load(f)
        self.assertNotIn("A", final.get("tasks", {}))

    def test_empty_dedup_not_resurrected(self):
        # dedup/retry 内存为准：显式清空不得被磁盘旧值复活。
        e = make_engine(self.tmp)
        e.state = {"dedup": {"K1|K2|2026-01-01|G1|硬座|p": "SUBMITTED"},
                   "tasks": {}, "retry": {}}
        e._save_state()
        e.empty_task_dedup({"from": "K1", "to": "K2"})
        # empty_task_dedup 内部已 _save_state；再显式 save 一次也应保持
        e._save_state()
        with open(e.state_path, encoding="utf-8") as f:
            final = json.load(f)
        self.assertEqual(final.get("dedup"), {})

    def test_memory_wins_task_conflict(self):
        # 同一任务两边不一致 → 内存（引擎最新一轮结果）获胜。
        e = make_engine(self.tmp)
        e.state = {"dedup": {}, "tasks": {"A": {"status": "monitoring"}},
                   "retry": {}}
        e._save_state()
        with filelock.file_lock(e.state_path + ".lock"):
            with open(e.state_path, encoding="utf-8") as f:
                disk = json.load(f)
            disk["tasks"]["A"]["status"] = "paused"  # 过时的并发写
            appcommon.atomic_write_json(e.state_path, disk,
                                        fallback_direct=True)
        e._save_state()
        with open(e.state_path, encoding="utf-8") as f:
            final = json.load(f)
        self.assertEqual(final["tasks"]["A"]["status"], "monitoring")


class TestStationCacheRefresh(TempDirCase):
    """Task 58: 车站数据三件套（双下载 / TTL 刷新 / import 副作用）。

    - 冷启动 load_station_map + load_station_index 只下载一次（改前两次）；
    - 过期缓存：立即返回旧数据 + 后台刷新（改前永不刷新）；
    - import station_db 不得拖入 launcher（3600 行 GUI 模块 + _ensure_stdio 副作用）。
    """

    SAMPLE_JS = "@bjb|北京北|VAP|beijingbei|bjb|0@shh|上海|SHH|shanghai|shh|0"

    def _mock_session(self):
        resp = mock.Mock()
        resp.text = self.SAMPLE_JS
        resp.raise_for_status = mock.Mock()
        sess = mock.Mock()
        sess.get.return_value = resp
        return sess

    def _paths(self):
        return (os.path.join(self.tmp, "station_name.json"),
                os.path.join(self.tmp, "station_index.json"))

    def _wait_for(self, pred, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return True
            time.sleep(0.05)
        return pred()

    def _fresh(self, path):
        try:
            return os.path.getmtime(path) > time.time() - 3600
        except OSError:
            return False

    def test_single_download_for_both_views(self):
        mp, ip = self._paths()
        sess = self._mock_session()
        with mock.patch.object(ticket.requests, "Session", return_value=sess):
            n2c, c2n = ticket.load_station_map(mp)
            stations = ticket.load_station_index(ip)
        self.assertEqual(sess.get.call_count, 1)  # 改前：两次下载
        self.assertEqual(n2c["北京北"], "VAP")
        self.assertEqual(c2n["SHH"], "上海")
        self.assertEqual(stations[0], {"name": "北京北", "code": "VAP",
                                      "py": "beijingbei", "spy": "bjb"})
        self.assertEqual(stations[1]["code"], "SHH")

    def test_expired_cache_returns_stale_then_refreshes(self):
        mp, ip = self._paths()
        with open(mp, "w", encoding="utf-8") as f:
            json.dump({"name2code": {"老站": "OLD"},
                       "code2name": {"OLD": "老站"}}, f)
        with open(ip, "w", encoding="utf-8") as f:
            json.dump({"stations": [{"name": "老站", "code": "OLD",
                                     "py": "laozhan", "spy": "lz"}]}, f)
        old = time.time() - 8 * 24 * 3600
        os.utime(mp, (old, old))
        os.utime(ip, (old, old))
        sess = self._mock_session()
        with mock.patch.object(ticket.requests, "Session", return_value=sess):
            n2c, _c2n = ticket.load_station_map(mp)
            self.assertEqual(n2c["老站"], "OLD")  # 过期也先给旧数据，不阻塞
            self.assertTrue(self._wait_for(lambda: sess.get.call_count == 1),
                            "过期缓存未触发后台刷新")
            self.assertTrue(self._wait_for(lambda: self._fresh(mp)),
                            "后台刷新未写回缓存")
        n2c2, _ = ticket.load_station_map(mp)
        self.assertEqual(n2c2["北京北"], "VAP")
        stations = ticket.load_station_index(ip)
        self.assertEqual(stations[0]["code"], "VAP")

    def test_manual_refresh_entry(self):
        mp, ip = self._paths()
        sess = self._mock_session()
        with mock.patch.object(ticket.requests, "Session", return_value=sess):
            n2c, c2n, stations = ticket.refresh_station_cache(mp, ip)
        self.assertEqual(sess.get.call_count, 1)
        self.assertTrue(os.path.exists(mp) and os.path.exists(ip))
        self.assertEqual(n2c["北京北"], "VAP")
        self.assertEqual(len(stations), 2)

    def test_note_station_missing_triggers_background_refresh(self):
        with mock.patch.object(ticket, "_refresh_station_cache_async") as bg:
            ticket.note_station_missing("新开站")
            bg.assert_called_once()

    def test_import_station_db_has_no_launcher_side_effect(self):
        code = ("import sys; sys.path.insert(0, %r); import station_db; "
                "print('launcher' in sys.modules)" % HERE)
        proc = subprocess.run([sys.executable, "-c", code], cwd=HERE,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-500:])
        self.assertEqual(proc.stdout.strip(), "False")  # 改前：True（顶层 import launcher）


class TestOrderP3(TempDirCase):
    """Task 59: order P3 四件套（翻页 / 乘车人匹配 / 防重键 / 畸形订单封锁）。"""

    # -- (a) 翻页 --
    def test_query_my_order_paginates_all_pages(self):
        p0 = [{"i": i} for i in range(8)]
        p1 = [{"i": i} for i in range(3)]

        class _Resp(object):
            def __init__(self, payload):
                self._p = payload

            def json(self):
                return self._p

        class _Sess(object):
            def __init__(self, pages):
                self.pages = pages
                self.posts = []

            def post(self, url, data=None, timeout=None):
                self.posts.append(dict(data or {}))
                idx = int((data or {}).get("pageIndex", "0"))
                items = self.pages[idx] if idx < len(self.pages) else []
                return _Resp({"data": {"OrderDTODataList": items}})

        sess = _Sess([p0, p1])
        out = order_mod._query_my_order(sess, "G", "2026-08-01", "2026-10-01")
        self.assertEqual(len(out), 11)  # 旧代码：只拿第 0 页 8 条
        self.assertEqual([p.get("pageIndex") for p in sess.posts], ["0", "1"])

    def test_query_my_order_page_cap(self):
        # 30 个满页 → 最多查 max_pages=25 页（200 条），不死循环
        class _Resp(object):
            def json(self):
                return {"data": {"OrderDTODataList": [{"i": 1}] * 8}}

        class _Sess(object):
            def __init__(self):
                self.n = 0

            def post(self, url, data=None, timeout=None):
                self.n += 1
                return _Resp()

        sess = _Sess()
        out = order_mod._query_my_order(sess, "G", "2026-08-01", "2026-10-01")
        self.assertEqual(sess.n, 25)
        self.assertEqual(len(out), 200)

    # -- (b) 乘车人匹配收紧 --
    def test_find_duplicate_requires_all_target_passengers(self):
        orders = [{"date": "2026-10-10", "train": "K225",
                   "passengers": ["张三"]}]
        # 旧代码：任一交集即中 → not None；新：目标须全部命中 → None
        self.assertIsNone(order_mod.find_duplicate(
            orders, "2026-10-10", "K225", ["张三", "李四"]))
        # 全部命中仍判重（诚实 pin）
        self.assertIsNotNone(order_mod.find_duplicate(
            orders, "2026-10-10", "K225", ["张三"]))

    def test_find_duplicate_empty_targets_not_hit(self):
        orders = [{"date": "2026-10-10", "train": "K225",
                   "passengers": ["张三"]}]
        # 旧代码：空名集算命中 → not None；新：空集不算命中 → None
        self.assertIsNone(order_mod.find_duplicate(
            orders, "2026-10-10", "K225", []))

    def test_pax_hit_tightened_in_classify(self):
        unpaid = {"order_no": "E1", "train": "G101", "date": "2026-10-10",
                  "passengers": ["张三"], "_no_complete": True,
                  "status": "未完成/未支付"}
        with mock.patch.object(order_mod, "check_existing_orders",
                               return_value=[unpaid]):
            cls, _ono, _raw = order_mod.classify_order_status(
                "2026-10-10", "G101", ["张三", "李四"], session=object())
            # 旧代码：部分交集即 "unpaid"；新：收紧后归为 blocked（仍挡单）
            self.assertEqual(cls, "blocked")
            cls2, _o2, _r2 = order_mod.classify_order_status(
                "2026-10-10", "G101", ["张三"], session=object())
            self.assertEqual(cls2, "unpaid")  # 全部命中仍是本行程（诚实 pin）

    # -- (c) 防重键加 from/to/席别 --
    def test_find_duplicate_from_to_narrows(self):
        orders = [{"date": "2026-10-10", "train": "G101",
                   "from": "北京", "to": "南京", "passengers": ["张三"]}]
        # 旧代码：只看 date+train → not None（误拦）；新：区间不同 → None
        self.assertIsNone(order_mod.find_duplicate(
            orders, "2026-10-10", "G101", ["张三"],
            from_station="北京", to_station="上海"))
        # 区间相同仍判重（诚实 pin）
        self.assertIsNotNone(order_mod.find_duplicate(
            orders, "2026-10-10", "G101", ["张三"],
            from_station="北京", to_station="南京"))
        # 任一侧缺 from/to 时不以此为由排除（向后兼容旧 fixture）
        self.assertIsNotNone(order_mod.find_duplicate(
            [{"date": "2026-10-10", "train": "G101",
              "passengers": ["张三"]}],
            "2026-10-10", "G101", ["张三"],
            from_station="北京", to_station="上海"))

    def test_find_duplicate_seat_narrows(self):
        item = {
            "sequence_no": "E123", "train_code_page": "G101",
            "from_station_name_page": "北京", "to_station_name_page": "上海",
            "start_train_date_page": "2026-10-10 08:00:00",
            "passengerDTOList": [{"passenger_name": "张三"}],
            "tickets": [{"ticket_status_name": "已支付",
                         "seat_type_name": "硬座"}],
            "order_date": "2026-10-01 10:00:00",
        }
        norm = order_mod._normalize_order_item(item, "x")
        self.assertEqual(norm.get("seat"), "硬座")
        # 席别不同 → 不判重（旧代码无席别概念 → 误拦）
        self.assertIsNone(order_mod.find_duplicate(
            [norm], "2026-10-10", "G101", ["张三"],
            from_station="北京", to_station="上海", seat_name="二等座"))
        # 席别相同 → 判重
        self.assertIsNotNone(order_mod.find_duplicate(
            [norm], "2026-10-10", "G101", ["张三"],
            from_station="北京", to_station="上海", seat_name="硬座"))

    # -- (d) 畸形订单跳过不封锁 --
    def test_find_duplicate_malformed_skipped_with_warning(self):
        orders = [{"date": "2026-10-10", "train": "K225",
                   "passengers": [], "order_no": "E999"}]
        # 旧代码：保守判重 → not None（永久封锁）；新：warning 后跳过 → None
        with self.assertLogs(order_mod.LOG, level="WARNING"):
            self.assertIsNone(order_mod.find_duplicate(
                orders, "2026-10-10", "K225", ["张三"]))


class TestNotifyP3(TempDirCase):
    """Task 60: notify P3（to 归一化 / secret_of 抛错 / 明文降级告警 /
    SMTP 重试 / 公开 secrets API）。"""

    def _email_cfg(self, **over):
        cfg = {"enabled": True, "smtp_host": "smtp.example.com",
               "smtp_port": 465, "username": "u", "password": "p",
               "from": "u@example.com", "to": ["u@example.com"]}
        cfg.update(over)
        return cfg

    def test_to_string_normalized_to_list(self):
        # to 配成字符串：旧代码逐字 join 发往乱码地址却报成功
        with mock.patch("notify.smtplib.SMTP_SSL") as m_ssl:
            ok, msg = notify_mod.send_email(self._email_cfg(to="a@b.com"), "s", "b")
        self.assertTrue(ok, msg)
        _from, rcpts, _data = m_ssl.return_value.sendmail.call_args[0]
        self.assertEqual(rcpts, ["a@b.com"])

    def test_to_semicolon_separated_string_split(self):
        with mock.patch("notify.smtplib.SMTP_SSL") as m_ssl:
            ok, msg = notify_mod.send_email(self._email_cfg(to="a@b.com;b@c.com"), "s", "b")
        self.assertTrue(ok, msg)
        _from, rcpts, _data = m_ssl.return_value.sendmail.call_args[0]
        self.assertEqual(rcpts, ["a@b.com", "b@c.com"])

    def test_secret_of_decrypt_failure_raises(self):
        # 解密失败不得吞成 ""（旧代码 SMTP 用空密码报 535 误导）
        with mock.patch.object(pax_mod, "_dpapi_unprotect",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(notify_mod.SecretDecryptError):
                notify_mod.secret_of("dpapi1:AAAA")

    def test_send_email_decrypt_failure_honest(self):
        # 授权码解密失败：不得"发送成功"，SMTP 不得被构造
        with mock.patch.object(pax_mod, "_dpapi_unprotect",
                               side_effect=RuntimeError("boom")), \
             mock.patch("notify.smtplib.SMTP_SSL") as m_ssl:
            ok, msg = notify_mod.send_email(
                self._email_cfg(password="dpapi1:AAAA"), "s", "b")
        self.assertFalse(ok)
        self.assertIn("解密失败", msg)
        m_ssl.assert_not_called()

    def test_protect_secret_plaintext_fallback_warns(self):
        # 非 Windows/加密失败回退明文：必须记 error 日志，不再静默
        with mock.patch.object(pax_mod, "_dpapi_protect",
                               side_effect=RuntimeError("boom")):
            with self.assertLogs("monitor", level="ERROR") as cm:
                enc = pax_mod.protect_secret("auth-code-x")
        self.assertEqual(enc, "auth-code-x")  # 行为不变：仍回发明文
        self.assertTrue(any("明文" in m for m in cm.output))

    def test_smtp_transient_retry_then_success(self):
        # 瞬时异常重试 2 次：第 3 次成功
        import smtplib as _smtplib
        inst = mock.MagicMock()
        inst.sendmail.side_effect = [_smtplib.SMTPServerDisconnected("c"),
                                     _smtplib.SMTPServerDisconnected("c"), None]
        with mock.patch("notify.smtplib.SMTP_SSL", return_value=inst) as m_ssl, \
             mock.patch("notify.time.sleep") as m_sleep:
            ok, msg = notify_mod.send_email(self._email_cfg(), "s", "b")
        self.assertTrue(ok, msg)
        self.assertEqual(m_ssl.call_count, 3)
        self.assertEqual(m_sleep.call_count, 2)

    def test_smtp_auth_failure_no_retry(self):
        # 认证失败（535 类）不重试：立即明确失败
        import smtplib as _smtplib
        inst = mock.MagicMock()
        inst.login.side_effect = _smtplib.SMTPAuthenticationError(535, b"auth")
        with mock.patch("notify.smtplib.SMTP_SSL", return_value=inst) as m_ssl, \
             mock.patch("notify.time.sleep") as m_sleep:
            ok, msg = notify_mod.send_email(self._email_cfg(), "s", "b")
        self.assertFalse(ok)
        self.assertEqual(m_ssl.call_count, 1)
        m_sleep.assert_not_called()

    def test_passengers_public_secrets_api(self):
        # passengers 暴露公开 protect_secret/unprotect_secret；notify 委托
        self.assertTrue(callable(pax_mod.protect_secret))
        self.assertTrue(callable(pax_mod.unprotect_secret))
        self.assertEqual(pax_mod.unprotect_secret("legacy"), "legacy")
        self.assertEqual(pax_mod.unprotect_secret(""), "")
        self.assertEqual(pax_mod.protect_secret("dpapi1:abc"), "dpapi1:abc")  # 不二次包裹
        self.assertIs(notify_mod.SecretDecryptError, pax_mod.SecretDecryptError)
        with mock.patch.object(pax_mod, "protect_secret",
                               return_value="X") as m:
            self.assertEqual(notify_mod.protect_secret("y"), "X")
        m.assert_called_once_with("y")


class TestTask71NotifyPasswordGuards(TempDirCase):
    """Task 71: 缺 password 键（Task 60 回归）与非字符串 password（Task 66
    reviewer 相邻缺口）都必须返回 (False, …) 诚实错误，不得抛异常破坏
    (ok, msg) 不抛异常契约。"""

    def _email_cfg(self, **over):
        cfg = {"enabled": True, "smtp_host": "smtp.example.com",
               "smtp_port": 465, "username": "u", "password": "p",
               "from": "u@example.com", "to": ["u@example.com"]}
        cfg.update(over)
        return cfg

    def test_missing_password_key_returns_false(self):
        # Task 60 回归：缺 "password" 键时抛裸 KeyError 杀死 monitor CLI
        cfg = self._email_cfg()
        del cfg["password"]
        with mock.patch("notify.smtplib.SMTP_SSL") as m_ssl:
            ok, msg = notify_mod.send_email(cfg, "s", "b")
        self.assertFalse(ok)
        self.assertIn("缺少字段", msg)
        self.assertIn("password", msg)
        m_ssl.assert_not_called()  # 不得发起 SMTP 连接

    def test_missing_password_key_message_matches_old_format(self):
        # 恢复 Task 60 之前的确切文案格式："邮件配置缺少字段: 'password'"
        cfg = self._email_cfg()
        del cfg["password"]
        ok, msg = notify_mod.send_email(cfg, "s", "b")
        self.assertFalse(ok)
        self.assertEqual(msg, "邮件配置缺少字段: 'password'")

    def test_non_string_password_returns_false(self):
        # Task 66 reviewer 相邻缺口：手改配置把 password 写成数字，
        # secret_of(12345) 会 AttributeError 逃出 (ok,msg) 契约
        with mock.patch("notify.smtplib.SMTP_SSL") as m_ssl:
            ok, msg = notify_mod.send_email(self._email_cfg(password=12345), "s", "b")
        self.assertFalse(ok)
        self.assertIn("password", msg)
        m_ssl.assert_not_called()

    def test_decrypt_failure_still_honest(self):
        # 回归 pin：Task 60 的 SecretDecryptError 诚实报错路径不受影响
        with mock.patch.object(pax_mod, "_dpapi_unprotect",
                               side_effect=RuntimeError("boom")), \
             mock.patch("notify.smtplib.SMTP_SSL") as m_ssl:
            ok, msg = notify_mod.send_email(
                self._email_cfg(password="dpapi1:AAAA"), "s", "b")
        self.assertFalse(ok)
        self.assertIn("解密失败", msg)
        m_ssl.assert_not_called()

    def test_valid_password_still_sends(self):
        # 回归 pin：合法字符串密码行为不变
        with mock.patch("notify.smtplib.SMTP_SSL") as m_ssl:
            ok, msg = notify_mod.send_email(self._email_cfg(), "s", "b")
        self.assertTrue(ok, msg)
        m_ssl.assert_called_once()


class TestMonitorLogP3(TempDirCase):
    """Task 61 (P3): monitor 缺 default / 删任务僵尸 / 日志留存+flush / PII 脱敏。"""

    def test_load_session_gets_default_not_none(self):
        # (a) load_session(config.get("session_cookies_file")) key 缺失传 None；
        # 应与 :601 一致补 default "session_cookies.json"。
        import monitor as monitor_mod
        with mock.patch.object(monitor_mod.os.path, "exists",
                               return_value=True), \
             mock.patch.object(order_mod, "load_session") as m_ls, \
             mock.patch.object(order_mod, "check_login",
                               return_value=(True, "x")), \
             mock.patch.object(monitor_mod, "read", return_value=""):
            monitor_mod.menu_session()
        m_ls.assert_called_once_with("session_cookies.json")

    def test_op5_delete_clears_state_entry(self):
        # (b) 删任务 op5 只删 config，state 残留僵尸；应同步清 state 条目。
        import monitor as monitor_mod
        cfg = {"tasks": [{"name": "T1", "from": "A", "to": "B",
                           "dates": ["2026-10-10"], "trains": ["G1"],
                           "seat_types": ["硬座"]}]}
        captured = {}

        def fake_update_state_locked(mutator):
            st = {"tasks": {"T1": {"status": "monitoring"}}}
            mutator(st)
            captured["state"] = st

        with mock.patch.object(monitor_mod, "load_config",
                               return_value=cfg), \
             mock.patch.object(monitor_mod, "save_config"), \
             mock.patch.object(monitor_mod, "fresh_engine"), \
             mock.patch.object(monitor_mod, "read",
                               side_effect=["1", "5"]), \
             mock.patch.object(monitor_mod, "ask_yes_no",
                               return_value=True), \
             mock.patch.object(monitor_mod, "update_state_locked",
                               side_effect=fake_update_state_locked):
            monitor_mod.menu_task_ops()
        self.assertIn("state", captured, "删任务未同步清理 state")
        self.assertNotIn("T1", captured["state"]["tasks"])

    def test_log_retention_prunes_old_files(self):
        # (c) 日志保留 30 天自动清理：过期文件删，当天文件留。
        import datetime as dt
        log_dir = os.path.join(self.tmp, "logs")
        os.makedirs(log_dir)
        old = os.path.join(log_dir, "m_20000101.log")
        today_name = "m_{0}.log".format(dt.date.today().strftime("%Y%m%d"))
        today = os.path.join(log_dir, today_name)
        open(old, "w").write("old")
        open(today, "w").write("today")
        h = logutil.DayFileHandler(log_dir, "m")
        try:
            h.emit(logging.LogRecord("t", logging.INFO, __file__, 1,
                                     "x", None, None))
        finally:
            h.close()
        self.assertFalse(os.path.exists(old), "过期日志未被清理")
        self.assertTrue(os.path.exists(today), "当天日志被误删")

    def test_log_emit_flushes(self):
        # (c) 写缓冲 flush：硬崩不丢尾——emit 后不 close 也能读到内容。
        log_dir = os.path.join(self.tmp, "logs2")
        h = logutil.DayFileHandler(log_dir, "m")
        try:
            h.emit(logging.LogRecord("t", logging.INFO, __file__, 1,
                                     "hello-flush", None, None))
            import datetime as dt
            path = os.path.join(
                log_dir,
                "m_{0}.log".format(dt.date.today().strftime("%Y%m%d")))
            with open(path, encoding="utf-8") as f:
                content = f.read()
        finally:
            h.close()
        self.assertIn("hello-flush", content, "emit 后未 flush，硬崩会丢尾")

    def test_mask_order_no(self):
        # (d) 订单号截断脱敏。
        self.assertEqual(engine_mod._mask_order_no("E123456789"),
                         "E123****6789")
        self.assertEqual(engine_mod._mask_order_no("123"), "****")
        self.assertEqual(engine_mod._mask_order_no(""), "****")

    def test_mask_names(self):
        # (d) 乘车人姓名打码。
        self.assertEqual(engine_mod._mask_names(["张三", "李四"]), "张*、李*")
        self.assertEqual(engine_mod._mask_names([]), "")

    def test_record_success_log_desensitized(self):
        # (d) _record_success 的 LOG 不得含全名/全订单号。
        e = make_engine(self.tmp)
        e._notify = lambda task, subject, body: {}
        task = {"name": "T1"}
        info = {"train_code": "G1", "from_name": "A", "to_name": "B",
                "start_time": "08:00", "arrive_time": "12:00"}
        extra = {"passengers": "张三、李四", "order_no": "E123456789"}
        with mock.patch.object(engine_mod, "LOG") as m_log:
            e._record_success(task, info, "2026-10-10", "硬座", extra)
        logged = " ".join(str(c.args) for c in m_log.info.call_args_list)
        self.assertNotIn("张三", logged)
        self.assertNotIn("李四", logged)
        self.assertNotIn("E123456789", logged)
        self.assertIn("张*", logged)
        self.assertIn("E123****6789", logged)

    def test_startup_recovery_log_desensitized(self):
        # (d) 启动恢复 run() 的 [订单恢复] LOG 不得含全订单号。
        e = make_engine(self.tmp)
        e.check_session_if_needed = lambda: None
        stop = threading.Event()
        stop.set()
        odb = {"orders": {"2026-10-10|G1": {
            "order_no": "E123456789", "train": "G1", "date": "2026-10-10",
            "seat": "硬座", "passengers": ["张三"],
            "classify": "unpaid"}}}
        with mock.patch.object(engine_mod, "LOG") as m_log, \
                mock.patch.object(engine_mod.appcommon, "load_orders",
                                  return_value=odb), \
                mock.patch.object(engine_mod.order_mod,
                                  "classify_order_status",
                                  return_value=("unpaid", "E123456789",
                                                "未完成/未支付")), \
                mock.patch.object(engine_mod.appcommon, "upsert_order"):
            e.run(stop_event=stop)
        logged = " ".join(str(c.args) for c in m_log.info.call_args_list)
        self.assertIn("[订单恢复]", logged)
        self.assertNotIn("E123456789", logged)
        self.assertIn("E123****6789", logged)


class TestAppcommonFilelockP3(TempDirCase):
    """Task 62: appcommon/filelock P3 —— 隔离精度 / tmp 残留 / 非 dict JSON /
    filelock 五处脚枪。"""

    # (a) 隔离时间戳微秒精度：同秒两次损坏不互相覆盖
    def test_quarantine_same_second_no_overwrite(self):
        p = os.path.join(self.tmp, "state.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{bad1")
        bad1 = appcommon.quarantine_corrupt(p)
        with open(p, "w", encoding="utf-8") as f:
            f.write("{bad2")
        bad2 = appcommon.quarantine_corrupt(p)
        self.assertIsNotNone(bad1)
        self.assertIsNotNone(bad2)
        # 旧代码秒精度：同秒内两次隔离同名，第二次覆盖第一次的证据
        self.assertNotEqual(bad1, bad2)
        with open(bad1, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{bad1")
        with open(bad2, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{bad2")

    # (b) tmp 残留清理：过期残留被清，正在写的（新鲜）不动；覆盖多 tmp_kind
    def test_atomic_write_sweeps_stale_tmp(self):
        p = os.path.join(self.tmp, "state.json")
        stale = p + ".tmp12345-67890"
        with open(stale, "w", encoding="utf-8") as f:
            f.write("{}")
        stale2 = p + ".guisave111-222"  # 其它写方的 kind 也要清
        with open(stale2, "w", encoding="utf-8") as f:
            f.write("{}")
        old = time.time() - 7200
        os.utime(stale, (old, old))
        os.utime(stale2, (old, old))
        fresh = p + ".tmp99999-88888"  # 模拟另一进程正在写
        with open(fresh, "w", encoding="utf-8") as f:
            f.write("{}")
        notmp = p + ".tmp-backup"  # 用户自建文件：形态不对，不动
        with open(notmp, "w", encoding="utf-8") as f:
            f.write("keep")
        os.utime(notmp, (old, old))
        appcommon.atomic_write_json(p, {"a": 1})
        # 旧代码：残留 tmp 永不清理
        self.assertFalse(os.path.exists(stale))
        self.assertFalse(os.path.exists(stale2))
        self.assertTrue(os.path.exists(fresh))
        self.assertTrue(os.path.exists(notmp))
        with open(p, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"a": 1})

    # (c) 合法非 dict JSON 走"内容损坏"口径（隔离），不是"正常"
    def test_read_state_non_dict_is_corrupt(self):
        p = os.path.join(self.tmp, "state.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("[1, 2, 3]")
        state, err, _fp = appcommon.read_state_or_none(p)
        self.assertIsNone(state)
        # 旧代码：返回 ([1, 2, 3], None)；新口径：ValueError → 隔离（Task 40 划分）
        self.assertIsInstance(err, ValueError)
        self.assertNotIsInstance(err, OSError)

    def test_load_state_array_quarantines_not_crash(self):
        p = os.path.join(self.tmp, "state.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("[1, 2, 3]")
        e = make_engine(self.tmp)
        # 旧代码：_load_state 旧版迁移块 state.items() 抛 AttributeError
        state = e._load_state()
        self.assertEqual(state.get("tasks"), {})
        bads = [f for f in os.listdir(self.tmp)
                if f.startswith("state.json.bad-")]
        self.assertTrue(bads)

    # (d1) 线程锁超时生效：旧代码 _tlock.acquire() 无限阻塞，timeout 被静默忽略
    def test_thread_lock_timeout_effective(self):
        p = os.path.join(self.tmp, "x.lock")
        holder = filelock.FileLock(p, timeout=10)
        holder.acquire()
        try:
            errors = []

            def try_acquire():
                try:
                    filelock.FileLock(p, timeout=0.5).acquire()
                except TimeoutError as ex:
                    errors.append(ex)

            t = threading.Thread(target=try_acquire, daemon=True)
            t0 = time.monotonic()
            t.start()
            t.join(10)
            dt = time.monotonic() - t0
            # 旧代码：工作线程在 _tlock 上永久阻塞，join 超时
            self.assertFalse(t.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertLess(dt, 5)
        finally:
            holder.release()

    # (d2) deadline 用 monotonic：墙钟跳变不影响超时判定
    def test_deadline_uses_monotonic_not_wallclock(self):
        p = os.path.join(self.tmp, "y.lock")
        release_evt = threading.Event()
        ready_evt = threading.Event()

        def hold():
            with filelock.file_lock(p, timeout=10):
                ready_evt.set()
                release_evt.wait(10)

        t = threading.Thread(target=hold, daemon=True)
        t.start()
        try:
            self.assertTrue(ready_evt.wait(10))  # holder 已拿到锁

            def boom(*a, **k):
                raise AssertionError("acquire 不应再读墙钟 time.time()")

            # 旧代码：deadline = time.time() + timeout → 此处直接触发 boom
            with mock.patch.object(filelock.time, "time", boom):
                with self.assertRaises(TimeoutError):
                    with filelock.file_lock(p, timeout=0.5):
                        pass
        finally:
            release_evt.set()
            t.join(10)

    # (d3) 同线程嵌套重入被支持：旧代码内层空转 timeout 才 TimeoutError
    def test_same_thread_nested_reentrant(self):
        p = os.path.join(self.tmp, "z.lock")
        with filelock.file_lock(p, timeout=1):
            with filelock.file_lock(p, timeout=1):
                pass
        # 完全释放后可重新获取（无泄漏）
        with filelock.file_lock(p, timeout=1):
            pass

    # (d4) release 加 acquired 守卫：double-release 不抛 RuntimeError
    def test_release_without_acquire_no_raise(self):
        p = os.path.join(self.tmp, "w.lock")
        lk = filelock.FileLock(p)
        lk.release()  # 旧代码：RLock.release() 抛 RuntimeError
        lk.acquire()
        lk.release()
        lk.release()  # 旧代码：第二次抛 RuntimeError

    # (d5) _thread_lock 按绝对路径归一化：相对/绝对路径不再绕过互斥
    def test_thread_lock_key_normalized_abspath(self):
        p = os.path.join(self.tmp, "v.lock")
        rel = os.path.relpath(p, self.tmp)
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            a = filelock._thread_lock(rel)
            b = filelock._thread_lock(p)
            c = filelock._thread_lock("./" + rel)
        finally:
            os.chdir(cwd)
        # 旧代码：三种写法各一把锁，互斥被绕过
        self.assertIs(a, b)
        self.assertIs(a, c)



class TestPassengersP3(TempDirCase):
    """Task 63: passengers P3（静默降级 / 密钥生成竞态 / 损坏密钥无自愈）。

    本机未安装 cryptography：注入与真实 API 同形的最小 fake
    cryptography.fernet（generate_key / 构造校验 / encrypt / decrypt），
    不改变测试环境。DPAPI 的 Windows 真机行为不在本机验证范围。
    """

    @staticmethod
    def _install_fake_fernet():
        import base64, hashlib, hmac, types

        class InvalidToken(Exception):
            pass

        class Fernet:
            def __init__(self, key):
                if isinstance(key, str):
                    key = key.encode("ascii")
                try:
                    raw = base64.urlsafe_b64decode(key)
                except Exception:
                    raise ValueError("Fernet key must be 32 url-safe base64-encoded bytes.")
                if len(raw) != 32:
                    raise ValueError("Fernet key must be 32 url-safe base64-encoded bytes.")
                self._key = key

            @staticmethod
            def generate_key():
                return base64.urlsafe_b64encode(os.urandom(32))

            def encrypt(self, data):
                tag = hmac.new(self._key, data, hashlib.sha256).digest()
                return base64.urlsafe_b64encode(b"\x01" + tag + data)

            def decrypt(self, token):
                if isinstance(token, str):
                    token = token.encode("ascii")
                try:
                    raw = base64.urlsafe_b64decode(token)
                except Exception:
                    raise InvalidToken("bad token")
                if len(raw) < 33 or raw[0:1] != b"\x01":
                    raise InvalidToken("bad token")
                tag, data = raw[1:33], raw[33:]
                if not hmac.compare_digest(
                        tag, hmac.new(self._key, data, hashlib.sha256).digest()):
                    raise InvalidToken("Signature did not match digest.")
                return data

        fernet_mod = types.ModuleType("cryptography.fernet")
        fernet_mod.Fernet = Fernet
        fernet_mod.InvalidToken = InvalidToken
        crypto_mod = types.ModuleType("cryptography")
        crypto_mod.fernet = fernet_mod
        return crypto_mod, fernet_mod

    def setUp(self):
        super().setUp()
        crypto_mod, fernet_mod = self._install_fake_fernet()
        self._saved_crypto = sys.modules.get("cryptography")
        self._saved_fernet = sys.modules.get("cryptography.fernet")
        sys.modules["cryptography"] = crypto_mod
        sys.modules["cryptography.fernet"] = fernet_mod
        self.addCleanup(self._restore_fernet_modules)
        self.key_path = os.path.join(self.tmp, ".passengers_key")
        self._kp_patch = mock.patch.object(
            pax_mod, "_fernet_key_path", return_value=self.key_path, create=True)
        self._kp_patch.start()
        self.addCleanup(self._kp_patch.stop)

    def _restore_fernet_modules(self):
        for name, saved in (("cryptography", self._saved_crypto),
                            ("cryptography.fernet", self._saved_fernet)):
            if saved is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved

    # (a) DPAPI 失败降级不再静默
    def test_encrypt_dpapi_failure_logs_error_and_falls_back(self):
        with mock.patch.object(pax_mod, "_is_windows", return_value=True), \
             mock.patch.object(pax_mod, "_dpapi_protect",
                               side_effect=RuntimeError("DPAPI 坏了")), \
             self.assertLogs("monitor", level="ERROR") as cm:
            enc, data = pax_mod._encrypt('{"a": 1}')
        self.assertEqual(enc, "fernet")
        self.assertTrue(any("降级" in m for m in cm.output),
                        "DPAPI 降级必须记 error 日志，不再静默：%s" % cm.output)

    # (b) O_EXCL 原子创建：并发首跑只产生一个密钥
    def test_concurrent_key_generation_single_winner(self):
        results = []

        def worker():
            results.append(pax_mod._generate_fernet_key(self.key_path))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 8)
        self.assertEqual(len(set(results)), 1, "并发生成必须收敛到同一个密钥")
        with open(self.key_path, "rb") as f:
            self.assertEqual(f.read(), results[0])
        self.assertEqual(os.stat(self.key_path).st_mode & 0o077, 0,
                         "密钥文件不得组/其他可读")

    def test_generate_never_overwrites_existing_key(self):
        from cryptography.fernet import Fernet
        k1 = Fernet.generate_key()
        with open(self.key_path, "wb") as f:
            f.write(k1)
        self.assertEqual(pax_mod._generate_fernet_key(self.key_path), k1)
        with open(self.key_path, "rb") as f:
            self.assertEqual(f.read(), k1)

    # (b) 解密路径绝不生成密钥
    def test_decrypt_path_never_generates_key(self):
        self.assertFalse(os.path.exists(self.key_path))
        with self.assertRaises(RuntimeError) as cm:
            pax_mod._get_fernet(create=False)
        self.assertIn("缺失", str(cm.exception))
        self.assertFalse(os.path.exists(self.key_path), "解密路径绝不能生成密钥文件")

    # (c) 损坏密钥在加密路径自愈：备份 + 重建 + 明确日志
    def test_corrupt_key_heals_on_encrypt_path(self):
        with open(self.key_path, "wb") as f:
            f.write(b"this-is-not-a-valid-fernet-key")
        with self.assertLogs("monitor", level="ERROR") as cm:
            fernet, path = pax_mod._get_fernet(create=True)
        self.assertIsNotNone(fernet)
        bads = [n for n in os.listdir(self.tmp)
                if n.startswith(".passengers_key.bad-")]
        self.assertEqual(len(bads), 1, "损坏的旧密钥必须备份留证")
        from cryptography.fernet import Fernet
        with open(self.key_path, "rb") as f:
            Fernet(f.read())  # 新密钥必须合法，不抛异常
        self.assertTrue(any("备份" in m for m in cm.output))

    # (c) 解密路径损坏密钥：明确报错，不自愈
    def test_corrupt_key_decrypt_path_raises_without_healing(self):
        with open(self.key_path, "wb") as f:
            f.write(b"this-is-not-a-valid-fernet-key")
        with self.assertRaises(RuntimeError) as cm:
            pax_mod._get_fernet(create=False)
        self.assertIn("损坏", str(cm.exception))
        bads = [n for n in os.listdir(self.tmp) if ".bad-" in n]
        self.assertEqual(bads, [], "解密路径不得自愈/备份")

    # 端到端：密钥丢失后解密返回 [] 且不重新生成（旧代码会生成新密钥掩盖丢失）
    def test_key_lost_load_returns_empty_without_regenerating(self):
        p = os.path.join(self.tmp, "passengers.json")
        self.assertTrue(pax_mod.save_passengers([{"name": "张三", "id_no": "x"}], p))
        self.assertTrue(os.path.exists(self.key_path))
        os.remove(self.key_path)  # 模拟密钥丢失
        back = pax_mod.load_passengers(p)
        self.assertEqual(back, [])
        self.assertFalse(os.path.exists(self.key_path), "解密路径绝不能重新生成密钥")




class TestTicketP3(TempDirCase):
    """Task 64: ticket/车站 P3（数组响应崩 / "0" 当有票 / 席别码遍历 / 缓存非原子 / 待识别）。"""

    def test_query_tickets_array_response_no_crash(self):
        """(a) 端点返回 JSON 数组：旧代码 data.get("httpstatus") 抛 AttributeError 逃出 except。"""
        sess = mock.Mock()
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.side_effect = [
            ["not", "a", "dict"],
            {"httpstatus": 200, "data": {"result": ["row1"]}},
        ]
        sess.get.return_value = resp
        with mock.patch.object(ticket, "get_session", return_value=sess):
            self.assertEqual(
                ticket.query_tickets("VNP", "ZAF", "2026-10-10"), ["row1"])

    def test_query_tickets_all_array_raises_runtimeerror(self):
        """(a) 全部端点都回数组：应 RuntimeError（无可用端点），而非 AttributeError。"""
        sess = mock.Mock()
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.side_effect = [[1], [2], [3], [4]]
        sess.get.return_value = resp
        with mock.patch.object(ticket, "get_session", return_value=sess):
            with self.assertRaises(RuntimeError):
                ticket.query_tickets("VNP", "ZAF", "2026-10-10")

    def test_parse_row_zero_string_is_no_ticket(self):
        """(b) 余票 "0" 字符串：旧代码 truthy 且不在 no_ticket 中 → 被当有票。"""
        info = ticket.parse_row(synthetic_row(hard_seat="0"), {}, None)
        self.assertNotIn("硬座", info["available_seats"])

    def test_seat_names_all_multichar_code_parsing(self):
        """(c) 多字符码按码表 longest-match 解析：旧代码逐字符，"AB"→'A' 错位。"""
        with mock.patch.dict(ticket.SEAT_CODE_NAMES_ALL, {"AB": "\u6d4b\u8bd5\u5e2d"}):
            names = ticket.seat_names_all("AB")
        self.assertIn("\u6d4b\u8bd5\u5e2d", names)
        self.assertNotIn("高级动卧", names)

    def test_seat_names_all_unknown_code_warns(self):
        """(c) 未知席别码：记 warning 后跳过，不静默。"""
        with self.assertLogs("monitor", level="WARNING") as cm:
            ticket.seat_names_all("Q9")
        self.assertTrue(any("未知席别码" in m for m in cm.output),
                        "应有未知席别码告警: %s" % (cm.output,))

    def test_write_station_caches_atomic_on_dump_failure(self):
        """(d) 写缓存中途崩溃：旧代码 open("w") 已截断目标；新代码 tmp+replace 目标完好。"""
        map_path = os.path.join(self.tmp, "station_name.json")
        index_path = os.path.join(self.tmp, "station_index.json")
        old = '{"name2code": {"old": 1}}'
        with open(map_path, "w", encoding="utf-8") as f:
            f.write(old)
        with mock.patch.object(ticket.json, "dump",
                               side_effect=RuntimeError("模拟崩溃")):
            with self.assertRaises(RuntimeError):
                ticket._write_station_caches(map_path, index_path, {}, {}, [])
        with open(map_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), old)

    def test_station_db_rebuild_marks_unknown_kind_with_notice(self):
        """(e) rebuild：未知 kind 打印显式提示（不静默定死为"待识别"）。"""
        import station_db
        fake_idx = [
            {"name": "北京", "code": "VNP", "py": "beijing", "spy": "bj"},
            {"name": "新站", "code": "XXX", "py": "xinzhan", "spy": "xz"},
        ]
        fake_launcher = mock.Mock()
        fake_launcher.get_station_index.return_value = fake_idx
        fake_launcher.load_station_kinds.return_value = {"VNP": "G"}
        db_path = os.path.join(self.tmp, "stations_db.json")
        with mock.patch.dict(sys.modules, {"launcher": fake_launcher}), \
                mock.patch.object(station_db, "DB_PATH", db_path), \
                mock.patch("builtins.print") as mprint:
            db = station_db.rebuild()
        printed = "\n".join(
            str(c.args[0]) for c in mprint.call_args_list if c.args)
        self.assertIn("待识别", printed)
        kinds = {r["code"]: r["kind"] for r in db["stations"]}
        self.assertEqual(kinds["XXX"], "待识别")


class TestTask65InteractionLoginChain(TempDirCase):
    """Task 65: 交互与登录链 P3 bundle（a–i）。

    (a) probe 打印原始乘车人响应 → 姓名/证件号进控制台 → 脱敏打印
    (b) 二维码过期静默刷新 → 刷新前先问用户
    (c) capture_session 出口约定统一为退出码（文档注明）
    (d) 最终校验通过后才存 cookie、才打印成功
    (e) page.goto 加 try 给友好提示
    (f) gui warn 重登 http 路径与 Task 53 口径统一（等退出码、写回 UI）
    (g) 确认按钮类名精确匹配（防子串误点倒计时按钮）
    (h) WarmSession.close 跨线程锁泄漏 → 超时检测 + 强制恢复
    (i) launcher 繁忙/排队类软失败不计入 fail_streak
    """

    # ---- (a) PII 脱敏打印 ----

    def test_mask_pii_hides_passenger_fields(self):
        body = ('{"status":true,"data":{"datas":['
                '{"passenger_name":"张三","passenger_id_no":"110101199001011234",'
                '"mobile_no":"13800138000"}]}}')
        masked = probe_login._mask_pii(body)
        self.assertNotIn("张三", masked)
        self.assertNotIn("110101199001011234", masked)
        self.assertNotIn("13800138000", masked)
        # 诊断价值保留：字段结构还在
        self.assertIn("passenger_name", masked)
        self.assertIn("passenger_id_no", masked)

    def test_show_with_mask_pii_option(self):
        resp = mock.Mock()
        resp.status_code = 200
        resp.elapsed.total_seconds.return_value = 12.0
        resp.content = b"x"
        resp.headers = {"Content-Type": "application/json"}
        resp.text = '{"passenger_id_no":"110101199001011234"}'
        with mock.patch("builtins.print") as mprint:
            probe_login.show(resp, mask_pii=True)
        out = "\n".join(str(c.args[0]) for c in mprint.call_args_list if c.args)
        self.assertNotIn("110101199001011234", out)

    # ---- (b) 二维码过期刷新前确认 ----

    def test_confirm_qr_refresh_answers(self):
        with mock.patch("builtins.input", return_value="y"):
            self.assertTrue(probe_login._confirm_qr_refresh())
        with mock.patch("builtins.input", return_value="n"):
            self.assertFalse(probe_login._confirm_qr_refresh())
        with mock.patch("builtins.input", return_value=""):
            self.assertTrue(probe_login._confirm_qr_refresh())  # 回车=默认是
        with mock.patch("builtins.input", side_effect=EOFError):
            # 非交互环境（stdin 关闭）：回退到自动刷新（旧行为），不卡死
            self.assertTrue(probe_login._confirm_qr_refresh())

    def test_qr_expiry_asks_before_refresh(self):
        # code==3 时必须先问用户；用户拒绝 → 不刷新、直接返回 None
        r3 = mock.Mock()
        r3.json.return_value = {"result_code": 3, "result_message": "expired"}
        with mock.patch.object(probe_login.SESSION, "post", return_value=r3), \
             mock.patch.object(probe_login, "create_qr_raw",
                               return_value=None) as mkr, \
             mock.patch("builtins.input", return_value="n"):
            self.assertIsNone(probe_login.step4_poll_qr("uuid-old"))
        mkr.assert_not_called()  # 旧代码：静默刷新（mkr 会被调用）→ 本断言变红

    def test_qr_expiry_confirmed_then_refreshes(self):
        r3 = mock.Mock()
        r3.json.return_value = {"result_code": 3, "result_message": "expired"}
        with mock.patch.object(probe_login.SESSION, "post", return_value=r3), \
             mock.patch.object(probe_login, "create_qr_raw",
                               return_value=None) as mkr, \
             mock.patch("builtins.input", return_value="y"):
            self.assertIsNone(probe_login.step4_poll_qr("uuid-old"))
        mkr.assert_called_once()

    # ---- (c) 出口约定：退出码 ----

    def test_capture_session_main_returns_exit_code(self):
        import capture_session
        with mock.patch.object(capture_session, "_order_mode",
                               return_value="browser"), \
             mock.patch("browser_order.login", return_value=True):
            self.assertEqual(capture_session.main(), 0)
        with mock.patch.object(capture_session, "_order_mode",
                               return_value="browser"), \
             mock.patch("browser_order.login", return_value=False):
            self.assertEqual(capture_session.main(), 1)
        # 旧代码 main() 内 sys.exit → 这里抛 SystemExit → 变红
        self.assertIn("退出码", capture_session.main.__doc__)

    # ---- (d) 最终校验门控 ----

    def test_final_verify_predicate(self):
        import capture_session
        ok_page = mock.Mock()
        ok_page.content.return_value = '{"status":true,"data":{"user_name":"t"}}'
        self.assertTrue(capture_session._final_verify(ok_page))
        bad_page = mock.Mock()
        bad_page.content.return_value = "<html>请先登录</html>"
        self.assertFalse(capture_session._final_verify(bad_page))
        err_page = mock.Mock()
        err_page.goto.side_effect = RuntimeError("net down")
        self.assertFalse(capture_session._final_verify(err_page))

    # ---- (e) goto 友好提示 ----

    def test_goto_login_page_failure_friendly(self):
        import capture_session
        page = mock.Mock()
        page.goto.side_effect = RuntimeError("timeout")
        with mock.patch("builtins.print") as mprint:
            self.assertFalse(capture_session._goto_login_page(page))
        out = "\n".join(str(c.args[0]) for c in mprint.call_args_list if c.args)
        self.assertIn("登录页", out)
        self.assertTrue(capture_session._goto_login_page(mock.Mock()))

    # ---- (f) warn 重登 http 路径统一 ----

    def test_warn_relogin_http_waits_and_writes_back(self):
        app = object.__new__(gui.MonitorApp)
        app.root = mock.Mock()
        app._after_warn_relogin = mock.Mock()
        with mock.patch.object(gui, "_relogin_ok_via_script",
                               return_value=True) as mok, \
             mock.patch.object(gui.subprocess, "Popen",
                               side_effect=AssertionError("must not Popen")):
            app._warn_relogin_http()  # 旧代码无此方法 → AttributeError 变红
            root, cb, _a, _b = gui._RESULT_QUEUE.get(timeout=10)
        mok.assert_called_once()
        self.assertIn("capture_session.py", mok.call_args.args[0])
        cb(None, None)
        app._after_warn_relogin.assert_called_once_with(True)

    # ---- (g) 类名精确匹配 ----

    def test_cls_exact_match_btn92s(self):
        self.assertTrue(browser_order._cls_has("btn92s", "btn92s"))
        self.assertTrue(browser_order._cls_has("btn92s countdown", "btn92s"))
        # 子串陷阱：倒计时态类名含 btn92s 子串但不是独立 token
        self.assertFalse(browser_order._cls_has("btn92s-countdown", "btn92s"))
        self.assertFalse(browser_order._cls_has("xbtn92s", "btn92s"))
        self.assertFalse(browser_order._cls_has("btn92", "btn92s"))
        self.assertFalse(browser_order._cls_has("", "btn92s"))
        self.assertFalse(browser_order._cls_has(None, "btn92s"))

    # ---- (h) 跨线程锁泄漏恢复 ----

    def test_recover_browser_lock_dead_owner(self):
        lock_before = browser_order._BROWSER_LOCK

        def _hold_and_die():
            browser_order._BROWSER_LOCK.acquire()
            # 线程直接退出：RLock 永不可释放（旧 close() 只打警告，永久泄漏）

        t = threading.Thread(target=_hold_and_die)
        t.start()
        t.join()
        with mock.patch("builtins.print"):
            ok = browser_order._recover_browser_lock(t, probe_timeout=0.2)
        self.assertTrue(ok)
        self.assertIsNot(browser_order._BROWSER_LOCK, lock_before)
        # 新锁可用：泄漏已恢复
        self.assertTrue(browser_order._BROWSER_LOCK.acquire(blocking=False))
        browser_order._BROWSER_LOCK.release()

    def test_recover_browser_lock_live_owner_not_rebuilt(self):
        lock_before = browser_order._BROWSER_LOCK
        release_ev = threading.Event()
        held_ev = threading.Event()

        def _hold():
            browser_order._BROWSER_LOCK.acquire()
            held_ev.set()
            release_ev.wait(10)
            browser_order._BROWSER_LOCK.release()

        t = threading.Thread(target=_hold)
        t.start()
        self.assertTrue(held_ev.wait(5))
        try:
            with mock.patch("builtins.print"):
                ok = browser_order._recover_browser_lock(t, probe_timeout=0.2)
            # 持锁线程仍存活：不重建（会破坏其正常释放），如实返回 False
            self.assertFalse(ok)
            self.assertIs(browser_order._BROWSER_LOCK, lock_before)
        finally:
            release_ev.set()
            t.join()

    # ---- (i) 软失败不计入 fail_streak ----

    def test_is_soft_fail(self):
        self.assertTrue(launcher._is_soft_fail("系统繁忙，请稍后再试"))
        self.assertTrue(launcher._is_soft_fail("网络异常"))
        self.assertTrue(launcher._is_soft_fail("当前排队人数较多，请等待"))
        self.assertFalse(launcher._is_soft_fail("余票不足"))
        self.assertFalse(launcher._is_soft_fail(""))


class TestTask66SmtpPasswordEncryption(TempDirCase):
    """Task 66 (P1): menu_notify 不得明文落盘 SMTP 授权码；config.json 写盘 0600。"""

    def _run_menu_notify(self, config, read_seq, pw_input, protect_fake):
        import monitor as monitor_mod
        import notify as notify_mod
        saved = {}
        with mock.patch.object(monitor_mod, "load_config", return_value=config), \
             mock.patch.object(monitor_mod, "read", side_effect=read_seq), \
             mock.patch("getpass.getpass", return_value=pw_input), \
             mock.patch.object(monitor_mod, "save_config",
                               side_effect=lambda c, **k: saved.update(c)), \
             mock.patch.object(notify_mod, "protect_secret",
                               side_effect=protect_fake) as ps:
            monitor_mod.menu_notify()
        return saved, ps

    @staticmethod
    def _fake_encrypt(t):
        # 模拟 DPAPI 加密：已加密/空原样返回，明文加前缀（与 protect_secret 契约同形）
        if not t or t.startswith("dpapi1:"):
            return t
        return "dpapi1:FAKE:" + t

    def test_menu_notify_password_goes_through_protect_secret(self):
        # 旧代码 email["password"] = new_pw 原样明文保存：protect_secret 从未被调用
        saved, ps = self._run_menu_notify(
            {}, ["", "465", "u@x.com", "", "a@x.com", "n"],
            "newsecret", self._fake_encrypt)
        ps.assert_called_once_with("newsecret")
        self.assertEqual(saved["notify"]["email"]["password"],
                         "dpapi1:FAKE:newsecret")

    def test_menu_notify_migrates_old_plaintext_on_keep(self):
        # 用户回车保留旧明文密码：保存时也必须加密（encrypt-on-save 迁移），
        # 不能把旧明文继续落盘
        config = {"notify": {"email": {"enabled": True, "smtp_host": "smtp.qq.com",
                                       "smtp_port": 465, "username": "u@x.com",
                                       "password": "oldplain", "from": "u@x.com",
                                       "to": []}}}
        saved, ps = self._run_menu_notify(
            config, ["", "465", "", "", "", "n"], "", self._fake_encrypt)
        ps.assert_called_once_with("oldplain")
        self.assertEqual(saved["notify"]["email"]["password"],
                         "dpapi1:FAKE:oldplain")

    def test_menu_notify_keeps_already_encrypted(self):
        # 已加密的值不再二次包裹（protect_secret 幂等）；旧代码此处本就通过，作回归 pin
        config = {"notify": {"email": {"enabled": True, "smtp_host": "smtp.qq.com",
                                       "smtp_port": 465, "username": "u@x.com",
                                       "password": "dpapi1:REALCIPHERTEXT",
                                       "from": "u@x.com", "to": []}}}
        saved, ps = self._run_menu_notify(
            config, ["", "465", "", "", "", "n"], "", self._fake_encrypt)
        ps.assert_called_once_with("dpapi1:REALCIPHERTEXT")
        self.assertEqual(saved["notify"]["email"]["password"],
                         "dpapi1:REALCIPHERTEXT")

    def test_menu_notify_non_string_password_passthrough(self):
        # 手改配置把 password 写成非字符串：不崩，原样透传（旧代码在 len() 处
        # 直接 TypeError 崩；此处只求不崩，发送侧的校验是其它 Task 的范围）
        import monitor as monitor_mod
        import notify as notify_mod
        config = {"notify": {"email": {"password": 12345}}}
        saved = {}
        with mock.patch.object(monitor_mod, "load_config", return_value=config), \
             mock.patch.object(monitor_mod, "read",
                               side_effect=["", "465", "", "", "", "n"]), \
             mock.patch("getpass.getpass", return_value=""), \
             mock.patch.object(monitor_mod, "save_config",
                               side_effect=lambda c, **k: saved.update(c)), \
             mock.patch.object(notify_mod, "protect_secret",
                               side_effect=AssertionError("must not be called")):
            monitor_mod.menu_notify()
        self.assertEqual(saved["notify"]["email"]["password"], 12345)

    def test_save_config_sets_0600(self):
        # 旧代码 atomic_write_json 不设权限：config.json 落盘 0644，同组/备份可读
        import stat as stat_mod
        import monitor as monitor_mod
        cfg_path = os.path.join(self.tmp, "config.json")
        with mock.patch.object(monitor_mod, "CONFIG_PATH", cfg_path):
            monitor_mod.save_config({"notify": {"email": {"password": "x"}}})
        mode = stat_mod.S_IMODE(os.stat(cfg_path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_atomic_write_json_mode_param(self):
        # 旧代码无 mode 参数：敏感文件无法收紧权限
        import stat as stat_mod
        import appcommon
        p1 = os.path.join(self.tmp, "secret.json")
        appcommon.atomic_write_json(p1, {"x": 1}, mode=0o600)
        self.assertEqual(stat_mod.S_IMODE(os.stat(p1).st_mode), 0o600)
        # 不传 mode 时行为不变：与普通 open 写盘权限一致（不强制、不改动）
        ref = os.path.join(self.tmp, "ref.json")
        with open(ref, "w", encoding="utf-8") as f:
            f.write("{}")
        p2 = os.path.join(self.tmp, "plain.json")
        appcommon.atomic_write_json(p2, {"x": 1})
        self.assertEqual(stat_mod.S_IMODE(os.stat(p2).st_mode),
                         stat_mod.S_IMODE(os.stat(ref).st_mode))


class TestTask67MonitorMenuRobustness(TempDirCase):
    """Task 67 (P1/P3): (a) menu_task_list 缺键任务不崩菜单；
    (b) 添加乘车人姓名处 Ctrl+C/EOF 取消本次添加；
    (c) menu_history 非 list/条目非 dict 不崩。"""

    @staticmethod
    def _mock_engine():
        eng = mock.MagicMock()
        # 还原真实 task_status 的键访问语义：缺 name 即抛 KeyError
        eng.task_status.side_effect = lambda t: t["name"]
        eng.state = {"tasks": {}}
        eng.base_interval = 300
        return eng

    def _run_task_list(self, tasks):
        import monitor as monitor_mod
        with mock.patch.object(monitor_mod, "load_config",
                               return_value={"tasks": tasks}), \
             mock.patch.object(monitor_mod, "fresh_engine",
                               return_value=self._mock_engine()), \
             mock.patch("builtins.print") as mprint:
            monitor_mod.menu_task_list()  # 不得抛 KeyError
        return " ".join(str(c.args[0]) for c in mprint.call_args_list)

    def test_task_missing_name_no_crash(self):
        # 旧代码 eng.task_status(t) 内 task["name"] 抛 KeyError，菜单崩溃。
        printed = self._run_task_list(
            [{"from": "北京", "to": "上海", "dates": []}])
        self.assertIn("配置损坏", printed)
        self.assertNotIn("Traceback", printed)

    def test_task_missing_from_to_no_crash(self):
        # 旧代码 t["from"] 直接索引抛 KeyError。
        printed = self._run_task_list([{"name": "t1", "dates": []}])
        self.assertIn("配置损坏", printed)
        self.assertNotIn("Traceback", printed)

    def test_task_list_mixed_valid_and_damaged(self):
        # 混合：好任务正常显示，坏任务占位跳过，整体不崩。
        tasks = [
            {"name": "t1", "from": "北京", "to": "上海",
             "dates": ["2099-01-01"], "trains": [], "seat_types": [],
             "priority": 5},
            {"from": "北京", "to": "上海", "dates": []},
        ]
        printed = self._run_task_list(tasks)
        self.assertIn("2099-01-01", printed)
        self.assertIn("配置损坏", printed)

    def test_add_passenger_ctrl_c_cancels(self):
        # 旧代码：姓名处 Ctrl+C/EOF → read()→None → "姓名不能为空" → 无法取消添加。
        import monitor as monitor_mod
        import passengers as passengers_mod
        with mock.patch.object(passengers_mod, "load_passengers",
                               return_value=[]), \
             mock.patch.object(passengers_mod, "save_passengers") as msave, \
             mock.patch.object(monitor_mod, "read",
                               side_effect=["1", None, "0"]), \
             mock.patch("builtins.print") as mprint:
            monitor_mod.menu_passengers()  # 不得死循环
        printed = " ".join(str(c.args[0]) for c in mprint.call_args_list)
        self.assertIn("已取消", printed)
        self.assertNotIn("姓名不能为空", printed)
        msave.assert_not_called()

    def _run_history(self, payload):
        import monitor as monitor_mod
        path = os.path.join(self.tmp, "order_history.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        with mock.patch.object(monitor_mod, "load_config",
                               return_value={"history_file": path}), \
             mock.patch("builtins.print") as mprint:
            monitor_mod.menu_history()  # 不得抛 TypeError/AttributeError
        return " ".join(str(c.args[0]) for c in mprint.call_args_list)

    def test_history_non_list_no_crash(self):
        # 旧代码 history[-50:] 对 dict 抛 TypeError。
        printed = self._run_history({"a": 1})
        self.assertIn("警告", printed)
        self.assertNotIn("Traceback", printed)

    def test_history_non_dict_entry_no_crash(self):
        # 旧代码 r.get 对字符串条目抛 AttributeError。
        printed = self._run_history([
            "notadict",
            {"time": "t", "task": "t1", "date": "2099-01-01", "train": "G1",
             "from": "北京", "to": "上海", "seat": "二等座",
             "passengers": ["张三"]}])
        self.assertIn("警告", printed)
        self.assertIn("张三", printed)
        self.assertNotIn("Traceback", printed)


class TestTask68EngineConfigShapes(TempDirCase):
    """Task 68 (P1/P2): 非法配置形状不得崩引擎进程；trains 字符串不逐字符拆；日期跨度设上限。"""

    def _write_config(self, cfg):
        p = os.path.join(self.tmp, "config.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        return p

    def _engine_with(self, cfg):
        cfg = dict(cfg)
        cfg.setdefault("state_file", os.path.join(self.tmp, "state.json"))
        cfg.setdefault("history_file", os.path.join(self.tmp, "order_history.json"))
        p = self._write_config(cfg)
        with mock.patch.object(ticket, "load_station_map",
                               return_value=({}, {})):
            e = engine_mod.MonitorEngine(config_path=p, setup_logging=False)
        return e, p

    def _adaptive_engine(self, adaptive):
        e = make_engine(self.tmp)
        e.config = {"adaptive": adaptive}
        e.base_interval, e.min_interval = 45, 30
        return e

    # ---- (a) P1: 非法形状不崩进程 ----
    def test_peak_hours_single_element_no_crash(self):
        # peak_hours=[0]：0 <= hour 恒成立，旧代码必走到 peak[1] 抛 IndexError 崩进程
        e = self._adaptive_engine({"enabled": True, "peak_hours": [0]})
        t = task_of("t68a")
        iv = e.task_interval(t)  # 旧代码 peak[1] 抛 IndexError 崩进程
        self.assertIsInstance(iv, (int, float))

    def test_peak_hours_string_no_crash(self):
        e = self._adaptive_engine({"enabled": True, "peak_hours": "6-23"})
        t = task_of("t68a")
        iv = e.task_interval(t)  # 旧代码 peak[0] <= hour 抛 TypeError
        self.assertIsInstance(iv, (int, float))

    def test_peak_multiplier_string_no_crash(self):
        e = self._adaptive_engine({"enabled": True, "peak_hours": [0, 24],
                                   "peak_multiplier": "1.0",
                                   "rush_within_hours": 0})
        t = task_of("t68a")
        iv = e.task_interval(t)  # 旧代码 iv * mult 抛 TypeError
        self.assertIsInstance(iv, (int, float))

    def test_poll_interval_abc_init_no_crash(self):
        e, _ = self._engine_with({"tasks": [],
                                  "poll_interval_seconds": "abc"})
        # 旧代码 __init__ 里 int("abc") 抛 ValueError，引擎起不来
        self.assertEqual(e.base_interval, 45)

    def test_poll_interval_abc_sync_no_crash(self):
        e, p = self._engine_with({"tasks": [], "poll_interval_seconds": 45})
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"tasks": [], "poll_interval_seconds": "abc",
                       "state_file": e.config["state_file"],
                       "history_file": e.config["history_file"]}, f)
        os.utime(p, (time.time() + 5, time.time() + 5))  # 保证 mtime 变化
        e._sync_config()  # 旧代码 int("abc") 抛 ValueError
        self.assertEqual(e.base_interval, 45)

    def test_tasks_dict_sanitized(self):
        e, _ = self._engine_with({"tasks": {"t1": {}}})
        # 旧代码 enumerate(dict) 拿 key 调 .get 抛 AttributeError
        self.assertEqual(e.tasks, [])

    def test_tasks_non_dict_entry_skipped(self):
        e, _ = self._engine_with({"tasks": [{"name": "ok"}, "garbage", 42]})
        self.assertEqual([t["name"] for t in e.tasks], ["ok"])

    def test_legal_adaptive_still_applies(self):
        # 回归 pin：合法 adaptive 仍生效（全天高峰 ×2.0，优先级 5 → 90s）
        e = self._adaptive_engine({"enabled": True, "peak_hours": [0, 24],
                                   "peak_multiplier": 2.0,
                                   "rush_within_hours": 0})
        t = task_of("t68a", priority=5)
        self.assertAlmostEqual(e.task_interval(t), 90)

    # ---- (b) P2: trains 字符串不逐字符拆 ----
    def test_trains_string_single_train(self):
        got = engine_mod.normalize_trains({"name": "t68b", "trains": "G101"})
        # 旧代码逐字符拆成 ['G', '1', '0', '1']，静默漏单
        self.assertEqual(got, ["G101"])

    def test_trains_string_warns(self):
        with self.assertLogs("monitor", level="WARNING") as cm:
            engine_mod.normalize_trains({"name": "t68b", "trains": "G101"})
        self.assertTrue(any("trains" in m for m in cm.output),
                        "字符串 trains 未记 warning: %s" % cm.output)

    def test_trains_list_unchanged(self):
        # 回归 pin：合法列表行为不变
        self.assertEqual(
            engine_mod.normalize_trains({"trains": ["k225", " G101 "]}),
            ["K225", "G101"])

    # ---- (c) P2: 日期跨度上限 31 天 ----
    def test_date_range_365d_truncated(self):
        t = {"name": "t68c", "date_range": ["2026-01-01", "2026-12-31"]}
        with self.assertLogs("monitor", level="ERROR") as cm:
            got = engine_mod.expand_dates(t)
        self.assertEqual(len(got), 31)  # 旧代码返回 365 个日期
        self.assertEqual(got[0], "2026-01-01")
        self.assertEqual(got[-1], "2026-01-31")
        self.assertTrue(any("截断" in m for m in cm.output),
                        "截断未明确告知: %s" % cm.output)

    def test_dates_list_over_31_truncated(self):
        base = datetime.date(2026, 1, 1)
        dates = [(base + datetime.timedelta(days=i)).isoformat()
                 for i in range(40)]
        with self.assertLogs("monitor", level="ERROR") as cm:
            got = engine_mod.expand_dates({"name": "t68c", "dates": dates})
        self.assertEqual(len(got), 31)
        self.assertTrue(any("截断" in m for m in cm.output),
                        "截断未明确告知: %s" % cm.output)

    def test_valid_5d_range_untouched(self):
        # 回归 pin：合法短区间不受影响
        t = {"name": "t68c", "date_range": ["2026-10-01", "2026-10-05"]}
        self.assertEqual(len(engine_mod.expand_dates(t)), 5)


class TestTask69CaptureSession(TempDirCase):
    """Task 69: capture_session P2/P3 bundle（a–e）。

    (a) COOKIE_PATH 硬编码无视 config 的 session_cookies_file → 从配置读，缺省回退
    (b) browser 分支无 try/except → 友好失败（Task 65e 口径）
    (c) p.chromium.launch 无保护 → 友好失败
    (d) 登录轮询吞浏览器关闭 → is_connected() 早退
    (e) cookie_dict 以 name 为键，同名多 path 互相覆盖 → 按 (name,path,domain) 区分落盘
    """

    # ---- (a) COOKIE_PATH 读配置 ----

    def test_cookie_path_from_config(self):
        import capture_session
        cfg = os.path.join(self.tmp, "config.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"session_cookies_file": "my_cookies.json"}, f)
        self.assertEqual(capture_session._cookie_path(cfg),
                         os.path.join(capture_session.HERE, "my_cookies.json"))

    def test_cookie_path_fallback(self):
        import capture_session
        default = os.path.join(capture_session.HERE, "session_cookies.json")
        # 配置文件不存在 → 回退
        self.assertEqual(capture_session._cookie_path(os.path.join(self.tmp, "nope.json")),
                         default)
        # 配置存在但无该键 → 回退
        cfg = os.path.join(self.tmp, "config.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"order_mode": "http"}, f)
        self.assertEqual(capture_session._cookie_path(cfg), default)
        # 配置损坏 → 回退，不抛异常
        with open(cfg, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(capture_session._cookie_path(cfg), default)

    def test_cookie_path_non_string_fallback(self):
        import capture_session
        default = os.path.join(capture_session.HERE, "session_cookies.json")
        cfg = os.path.join(self.tmp, "config.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"session_cookies_file": 123}, f)
        # 非字符串值：回退默认，不抛 TypeError（旧代码此处永不崩）
        self.assertEqual(capture_session._cookie_path(cfg), default)

    def test_cookie_path_absolute_passthrough(self):
        import capture_session
        cfg = os.path.join(self.tmp, "config.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"session_cookies_file": "/tmp/abs_cookies.json"}, f)
        # 绝对路径：os.path.join(HERE, abs) == abs，与 monitor.py:684 同口径
        self.assertEqual(capture_session._cookie_path(cfg), "/tmp/abs_cookies.json")

    # ---- (b) browser 分支友好失败 ----

    def test_browser_mode_login_passthrough(self):
        import capture_session
        with mock.patch("browser_order.login", return_value=True):
            self.assertTrue(capture_session._browser_mode_login())
        with mock.patch("browser_order.login", return_value=False):
            self.assertFalse(capture_session._browser_mode_login())

    def test_browser_mode_login_exception_friendly(self):
        import capture_session
        with mock.patch("browser_order.login",
                        side_effect=RuntimeError("Edge not found")), \
             mock.patch("builtins.print") as mprint:
            self.assertFalse(capture_session._browser_mode_login())
        out = "\n".join(str(c.args[0]) for c in mprint.call_args_list if c.args)
        self.assertIn("[失败]", out)  # 友好提示，而不是把 traceback 抛给调用方

    # ---- (c) launch 友好失败 ----

    def test_launch_browser_success(self):
        import capture_session
        p = mock.Mock()
        self.assertIs(p.chromium.launch.return_value,
                      capture_session._launch_browser(p))

    def test_launch_browser_failure_friendly(self):
        import capture_session
        p = mock.Mock()
        p.chromium.launch.side_effect = RuntimeError("Executable doesn't exist")
        with mock.patch("builtins.print") as mprint:
            self.assertIsNone(capture_session._launch_browser(p))
        out = "\n".join(str(c.args[0]) for c in mprint.call_args_list if c.args)
        self.assertIn("Edge 启动失败", out)

    # ---- (d) 轮询早退 ----

    def test_wait_for_login_detects_tk(self):
        import capture_session
        browser = mock.Mock()
        browser.is_connected.return_value = True
        context = mock.Mock()
        context.cookies.return_value = [
            {"name": "JSESSIONID", "value": "x"},
            {"name": "tkabc", "value": "y"},
        ]
        with mock.patch("time.sleep"):
            st = capture_session._wait_for_login(browser, context, time.time() + 300)
        self.assertEqual(st, capture_session.LOGIN_OK)

    def test_wait_for_login_browser_closed_early_exit(self):
        import capture_session
        browser = mock.Mock()
        browser.is_connected.return_value = False
        context = mock.Mock()
        with mock.patch("builtins.print") as mprint:
            st = capture_session._wait_for_login(browser, context, time.time() + 300)
        self.assertEqual(st, capture_session.LOGIN_BROWSER_CLOSED)
        context.cookies.assert_not_called()  # 早退，不再轮询
        out = "\n".join(str(c.args[0]) for c in mprint.call_args_list if c.args)
        self.assertIn("已被关闭", out)

    def test_wait_for_login_timeout(self):
        import capture_session
        browser = mock.Mock()
        browser.is_connected.return_value = True
        context = mock.Mock()
        context.cookies.return_value = [{"name": "JSESSIONID", "value": "x"}]
        with mock.patch("time.sleep") as msleep:
            st = capture_session._wait_for_login(browser, context, time.time() - 1)
        self.assertEqual(st, capture_session.LOGIN_TIMEOUT)
        msleep.assert_not_called()  # 已超时，直接返回

    def test_wait_for_login_malformed_cookie_entry(self):
        import capture_session
        browser = mock.Mock()
        browser.is_connected.return_value = True
        context = mock.Mock()
        # 畸形条目（非 dict）：跳过不崩，下一轮继续（旧代码同语义）
        context.cookies.side_effect = [["not-a-dict"], [{"name": "tk", "value": "t"}]]
        with mock.patch("time.sleep"):
            st = capture_session._wait_for_login(browser, context, time.time() + 300)
        self.assertEqual(st, capture_session.LOGIN_OK)

    # ---- (e) 存储结构：按 (name, path, domain) 区分 ----

    def test_build_cookie_dict_keeps_multi_path(self):
        import capture_session
        cookies = [
            {"name": "JSESSIONID", "value": "AAA", "domain": ".12306.cn", "path": "/otn"},
            {"name": "JSESSIONID", "value": "BBB", "domain": ".12306.cn", "path": "/passport"},
            {"name": "tk", "value": "T", "domain": ".kyfw.12306.cn", "path": "/"},
            {"name": "other", "value": "x", "domain": "example.com", "path": "/"},
        ]
        d = capture_session._build_cookie_dict(cookies)
        # 第三方域被过滤；同名双 path 条目都保留 → 3 条
        self.assertEqual(len(d), 3)
        vals = sorted(v["value"] for v in d.values())
        self.assertEqual(vals, ["AAA", "BBB", "T"])

    def test_build_cookie_dict_single_keeps_plain_key(self):
        import capture_session
        d = capture_session._build_cookie_dict([
            {"name": "tk", "value": "T", "domain": ".kyfw.12306.cn", "path": "/"},
        ])
        # 单条目保持纯 name 键：旧文件/旧版本可读
        self.assertEqual(d, {"tk": {"value": "T", "domain": ".kyfw.12306.cn", "path": "/"}})

    def test_build_cookie_dict_dedup_exact_triple(self):
        import capture_session
        d = capture_session._build_cookie_dict([
            {"name": "tk", "value": "old", "domain": ".12306.cn", "path": "/"},
            {"name": "tk", "value": "new", "domain": ".12306.cn", "path": "/"},
        ])
        self.assertEqual(d["tk"]["value"], "new")

    def test_load_session_reads_composite_keys(self):
        path = os.path.join(self.tmp, "cookies.json")
        SEP = "\x1f"
        data = {
            "JSESSIONID" + SEP + "/otn" + SEP + ".12306.cn": {"value": "AAA", "domain": ".12306.cn", "path": "/otn"},
            "JSESSIONID" + SEP + "/passport" + SEP + ".12306.cn": {"value": "BBB", "domain": ".12306.cn", "path": "/passport"},
            "tk": {"value": "T", "domain": ".kyfw.12306.cn", "path": "/"},
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        s = order_mod.load_session(path)
        got = {}
        for c in s.cookies:
            got.setdefault(c.name, {})[c.value] = c.path
        # 两个同名 Cookie 都被还原，作用域各归其位
        self.assertEqual(got["JSESSIONID"], {"AAA": "/otn", "BBB": "/passport"})
        self.assertEqual(got["tk"], {"T": "/"})

    def test_load_session_old_format_unchanged(self):
        # 回归 pin：纯 name 键旧文件行为不变
        path = os.path.join(self.tmp, "cookies.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"tk": {"value": "T", "domain": ".kyfw.12306.cn", "path": "/"},
                       "plain": "v"}, f)
        s = order_mod.load_session(path)
        self.assertEqual(s.cookies.get("tk"), "T")
        self.assertEqual(s.cookies.get("plain"), "v")

    # ---- 端到端：main() 写到配置路径，落盘格式可读 ----

    def _run_main_with_fake_browser(self, cookies, cookie_path):
        import capture_session
        browser = mock.MagicMock()
        browser.is_connected.return_value = True
        context = mock.MagicMock()
        context.cookies.return_value = cookies
        page = mock.MagicMock()
        page.content.return_value = '{"status":true,"data":{"user_name":"u"}}'
        browser.new_context.return_value = context
        context.new_page.return_value = page
        p = mock.MagicMock()
        p.chromium.launch.return_value = browser
        cm = mock.MagicMock()
        cm.__enter__.return_value = p
        cm.__exit__.return_value = False
        fake_sync_api = mock.MagicMock()
        fake_sync_api.sync_playwright.return_value = cm
        mods = {"playwright": mock.MagicMock(), "playwright.sync_api": fake_sync_api}
        with mock.patch.dict(sys.modules, mods), \
             mock.patch.object(capture_session, "_order_mode", return_value="http"), \
             mock.patch.object(capture_session, "_cookie_path",
                               return_value=cookie_path), \
             mock.patch("time.sleep"), \
             mock.patch("builtins.print"):
            return capture_session.main()

    def test_main_writes_to_configured_path(self):
        import capture_session  # noqa: F401 保持引用一致
        cookie_path = os.path.join(self.tmp, "my_cookies.json")
        cookies = [
            {"name": "JSESSIONID", "value": "AAA", "domain": ".12306.cn", "path": "/otn"},
            {"name": "JSESSIONID", "value": "BBB", "domain": ".12306.cn", "path": "/passport"},
            {"name": "tk", "value": "T", "domain": ".kyfw.12306.cn", "path": "/"},
        ]
        rc = self._run_main_with_fake_browser(cookies, cookie_path)
        self.assertEqual(rc, 0)
        # (a) 写到了配置路径
        self.assertTrue(os.path.exists(cookie_path))
        # (e) 落盘的双 path 条目可被 load_session 读回
        s = order_mod.load_session(cookie_path)
        vals = sorted(c.value for c in s.cookies if c.name == "JSESSIONID")
        self.assertEqual(vals, ["AAA", "BBB"])


# ============================ Task 70 ============================

class TestTask70BrowserOrder(TempDirCase):
    """Task 70: browser_order P2/P3（锁重入捷径 / clicked 未定义 / dlg_seat 死代码）。

    (a) stale warm 的 close() 在 exclusive() 体内释放两把锁并清零 depth →
        重入 finally 恢复旧 depth 造成"持锁"假象 → 本线程后续 exclusive()
        永久走重入捷径，跨进程 profile 互斥静默失效（P2）。
    (b) _order_impl 外层 except 引用 clicked，但它在 try 内才赋值，
        步骤 1–6.5 抛异常时 UnboundLocalError 掩盖原始错误（P3）。
    (c) dlg_seat 初始化 "" 后从未被赋值 → 结果页警告与
        extra["seat_in_dialog"] 是死代码（P3）。
    """

    # ---------- (a) 锁重入捷径 ----------

    def _manual_lock_state(self):
        """模拟 warm_up 后的线程状态：手动持有两把锁，depth=1。"""
        self.assertTrue(browser_order._BROWSER_LOCK.acquire(timeout=5))
        self.addCleanup(self._safe_release_browser_lock)
        self.assertTrue(browser_order._PROFILE_LOCK.acquire(timeout=5))
        self.addCleanup(browser_order._PROFILE_LOCK.release)  # fd None 时是 no-op
        browser_order._LOCK_LOCAL.depth = 1
        self.addCleanup(setattr, browser_order._LOCK_LOCAL, "depth", 0)

    @staticmethod
    def _safe_release_browser_lock():
        # RLock 重复 release 会抛 RuntimeError：测试的 close() 复刻可能已释放
        try:
            browser_order._BROWSER_LOCK.release()
        except RuntimeError:
            pass

    class _StaleWarm:
        """复刻 WarmSession.close() 的同线程 stale 路径（锁语义逐字一致）。"""
        def __init__(self):
            self.effective_closes = 0
            self._closed = False

        def usable(self):
            return False

        def close(self):
            if self._closed:
                return
            self._closed = True
            self.effective_closes += 1
            if getattr(browser_order._LOCK_LOCAL, "depth", 0):
                browser_order._LOCK_LOCAL.depth = 0
            browser_order._PROFILE_LOCK.release()
            browser_order._BROWSER_LOCK.release()

    def test_exclusive_reentrant_finally_keeps_zero_depth(self):
        # 体内 close() 已清零 depth（锁已释放）：finally 不应"复活"旧 depth
        browser_order._LOCK_LOCAL.depth = 1
        self.addCleanup(setattr, browser_order._LOCK_LOCAL, "depth", 0)
        with browser_order.exclusive(timeout=5):   # 重入分支：depth 1→2
            browser_order._LOCK_LOCAL.depth = 0   # 模拟 warm.close()
        self.assertEqual(getattr(browser_order._LOCK_LOCAL, "depth", 0), 0,
                         "重入 exclusive 的 finally 把已清零的 depth 恢复为旧值，"
                         "会造成本线程'仍持有锁'的假象（P2）")

    def test_exclusive_reentrant_normal_restore_kept(self):
        # 回归 pin：正常路径的重入记账不受影响
        browser_order._LOCK_LOCAL.depth = 1
        self.addCleanup(setattr, browser_order._LOCK_LOCAL, "depth", 0)
        with browser_order.exclusive(timeout=5):
            self.assertEqual(browser_order._LOCK_LOCAL.depth, 2)
        self.assertEqual(browser_order._LOCK_LOCAL.depth, 1)

    def test_stale_warm_order_then_exclusive_really_locks(self):
        # P2 端到端：stale warm 下单后，本线程后续 exclusive() 必须真实加锁
        self._manual_lock_state()
        warm = self._StaleWarm()

        def fake_impl(*a, **k):
            # 逐字复刻旧 _order_impl 的 stale 分支（改前代码）
            w = k.get("warm")
            if w is not None and not w.usable():
                try:
                    w.close()
                except Exception:
                    pass
            return (False, "mock-cold", None)

        info = {"train_code": "G101", "from_name": "北京", "to_name": "上海",
                "from_code": "VNP", "to_code": "SHH"}
        with mock.patch.object(browser_order, "_order_impl", side_effect=fake_impl):
            ok, msg, extra = browser_order.order_via_browser(
                info, "二等座", "O", ["张三"], "2026-10-10", warm=warm)
        self.assertEqual(warm.effective_closes, 1, "stale warm 应被关闭一次")
        self.assertFalse(ok)
        # 核心断言 1：返回后线程不再"假装持锁"
        self.assertEqual(getattr(browser_order._LOCK_LOCAL, "depth", 0), 0,
                         "stale warm close 后 depth 仍为旧值："
                         "后续 exclusive() 会走重入捷径（P2）")
        # 核心断言 2：exclusive() 真实持有锁（另一线程拿不到），而非走捷径
        probe = []

        def other_thread():
            got = browser_order._BROWSER_LOCK.acquire(timeout=0.5)
            probe.append(got)
            if got:
                browser_order._BROWSER_LOCK.release()

        with browser_order.exclusive(timeout=5):
            t = threading.Thread(target=other_thread)
            t.start()
            t.join()
        self.assertEqual(probe, [False],
                         "exclusive() 未真实加锁：走了重入捷径（P2 复现）")

    # ---------- (b) clicked 未定义 ----------

    def _stub_playwright_cold(self):
        import sys
        import types as _types
        pw = _types.ModuleType("playwright")
        pw_sync = _types.ModuleType("playwright.sync_api")
        cm = mock.MagicMock()
        cm.__enter__.return_value = mock.MagicMock()
        pw_sync.sync_playwright = lambda: cm
        pw.sync_api = pw_sync
        mods = {"playwright": pw, "playwright.sync_api": pw_sync}
        for name, mod in mods.items():
            sys.modules[name] = mod
        self.addCleanup(lambda: [sys.modules.pop(n, None) for n in mods])

    def test_early_exception_not_masked_by_unbound_clicked(self):
        # 步骤 1–6.5（clicked = False 赋值点之前）抛异常：外层 except 必须读到
        # 已初始化的 clicked=False，走普通失败分支，而非 UnboundLocalError 掩盖原始错误
        self._stub_playwright_cold()
        page = mock.MagicMock()
        ctx = mock.MagicMock()
        ctx.pages = [page]
        info = {"train_code": "G101", "from_name": "北京", "to_name": "上海",
                "from_code": "VNP", "to_code": "SHH"}
        with mock.patch.object(browser_order, "launch", return_value=ctx), \
             mock.patch.object(browser_order, "session_ok",
                               return_value=(True, "mock")), \
             mock.patch.object(browser_order, "_goto_and_query",
                               side_effect=RuntimeError("query boom")):
            ok, msg, extra = browser_order._order_impl(
                info, "二等座", "O", ["张三"], "2026-10-10")
        self.assertFalse(ok)
        self.assertIn("query boom", msg)
        self.assertNotIn("UnboundLocalError", msg)

    # ---------- (c) dlg_seat 死代码 ----------

    def test_dialog_seat_words(self):
        self.assertEqual(browser_order._dialog_seat_words("车次 G101 二等座 1 张"),
                         ["二等座"])
        self.assertEqual(browser_order._dialog_seat_words("无座改签二等座"),
                         ["无座", "二等座"])
        self.assertEqual(browser_order._dialog_seat_words(""), [])
        self.assertEqual(browser_order._dialog_seat_words(None), [])

    def test_audit_dialog_seat(self):
        # 显示所选席别：取证记录，不中止
        self.assertEqual(
            browser_order._audit_dialog_seat("G101 二等座", "二等座", None),
            ("二等座", None))
        # 显示改判席别：取证记录为改判席别，不中止（结果页警告取证用）
        self.assertEqual(
            browser_order._audit_dialog_seat("G101 二等座", "无座", "二等座"),
            ("二等座", None))
        # 显示无关席别：中止
        self.assertEqual(
            browser_order._audit_dialog_seat("G101 硬座", "二等座", None),
            ("硬座", "硬座"))
        # 无席别词
        self.assertEqual(
            browser_order._audit_dialog_seat("请确认订单", "二等座", None),
            ("", None))


class TestTask72EngineState(TempDirCase):
    """Task 72: engine P2/P3 bundle —— 指纹 TOCTOU 回归 / _reload_state 静默 /
    mtime 消费 / 浅拷贝崩 / 墓碑膨胀 / 启动恢复子串误清。"""

    def _write_config(self, eng, tasks):
        cfg = {"poll_interval_seconds": 45, "min_interval_seconds": 30,
               "tasks": tasks}
        with open(eng.config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    # ---- (a) 指纹在读失败瞬间（持锁内）抓取 ----

    def test_read_state_or_none_returns_fingerprint(self):
        import appcommon
        p = os.path.join(self.tmp, "state.json")
        # 不存在 → ({} , None, None)
        self.assertEqual(appcommon.read_state_or_none(p), ({}, None, None))
        json.dump({"tasks": {}}, open(p, "w", encoding="utf-8"))
        st, err, fp = appcommon.read_state_or_none(p)
        self.assertIsNone(err)
        self.assertEqual(st, {"tasks": {}})
        self.assertEqual(fp, appcommon.stat_fingerprint(p))
        # 损坏 → (None, err, 读失败瞬间的指纹)
        open(p, "w", encoding="utf-8").write("{corrupt")
        fp_before = appcommon.stat_fingerprint(p)
        st2, err2, fp2 = appcommon.read_state_or_none(p)
        self.assertIsNone(st2)
        self.assertIsInstance(err2, ValueError)
        self.assertEqual(fp2, fp_before)

    def test_quarantine_uses_at_read_fingerprint(self):
        import appcommon
        p = os.path.join(self.tmp, "state.json")
        open(p, "w", encoding="utf-8").write("{corrupt")
        st, err, fp = appcommon.read_state_or_none(p)
        self.assertIsNotNone(err)
        # 模拟并发写方在"读失败→隔离决策"间隙写入健康文件
        json.dump({"dedup": {}, "tasks": {}, "retry": {}},
                  open(p, "w", encoding="utf-8"))
        # 读失败瞬间的指纹与新文件不一致 → 必须放弃隔离，健康文件不得被挪走
        self.assertIsNone(appcommon.quarantine_corrupt(p, fp))
        self.assertTrue(os.path.exists(p))
        self.assertEqual(json.load(open(p, encoding="utf-8"))["tasks"], {})

    # ---- (b) _reload_state 损坏不再静默吞 ----

    def test_reload_state_quarantines_corrupt(self):
        e = make_engine(self.tmp)
        e.state = {"dedup": {"k": "v"}, "tasks": {}, "retry": {}}
        open(e.state_path, "w", encoding="utf-8").write("{corrupt")
        with self.assertLogs("monitor", level="WARNING") as cm:
            result = e._reload_state()
        # 不再静默返回 {}：挪档留证 + 醒目告警 + 返回 None（调用方保留旧内存态）
        self.assertIsNone(result)
        bads = [f for f in os.listdir(self.tmp)
                if f.startswith("state.json.bad-")]
        self.assertEqual(len(bads), 1)
        self.assertEqual(e.state["dedup"], {"k": "v"})
        out = "\n".join(cm.output)
        self.assertIn("state.json", out)

    # ---- (c) _sync_config 解析失败不消费 mtime ----

    def test_sync_config_does_not_consume_mtime_on_failure(self):
        e = make_engine(self.tmp)
        open(e.config_path, "w", encoding="utf-8").write("{corrupt")
        mtime = os.path.getmtime(e.config_path)
        self.assertFalse(e._sync_config())
        # 原地修好配置但 mtime 不变（同秒内修复/utime 回拨）：必须能重试同步
        with open(e.config_path, "w", encoding="utf-8") as f:
            json.dump({"poll_interval_seconds": 45, "tasks": []}, f)
        os.utime(e.config_path, (mtime, mtime))
        self.assertTrue(e._sync_config())
        self.assertEqual(e.config["poll_interval_seconds"], 45)

    # ---- (d) _save_state 序列化竞态不逃出 ----

    def test_save_state_survives_serialize_race(self):
        import appcommon
        e = make_engine(self.tmp)
        e.state = {"dedup": {"k": "v"}, "tasks": {}, "retry": {}}
        with mock.patch.object(appcommon, "atomic_write_json",
                               side_effect=RuntimeError(
                                   "dictionary changed size during iteration")):
            # GUI 线程在序列化期间改嵌套 dict：绝不能逃出主循环
            result = e._save_state()
        self.assertEqual(result["dedup"], {"k": "v"})

    def test_save_state_survives_deepcopy_race(self):
        e = make_engine(self.tmp)
        e.state = {"dedup": {"k": "v"}, "tasks": {}, "retry": {}}
        with mock.patch.object(engine_mod.copy, "deepcopy",
                               side_effect=RuntimeError("changed size")):
            # 3 次重试耗尽 + 兜底浅拷贝同样撞上并发修改：仍不抛
            result = e._save_state()
        self.assertEqual(result["dedup"], {"k": "v"})

    # ---- (e) 孤儿 state 条目清理 + 墓碑上限 ----

    def test_sync_config_prunes_orphan_state_tasks(self):
        e = make_engine(self.tmp)
        e.state["tasks"]["ghost"] = {"status": "monitoring"}
        e.state["tasks"]["keep"] = {"status": "paused"}
        self._write_config(e, [task_of("keep")])
        self.assertTrue(e._sync_config())
        self.assertNotIn("ghost", e.state["tasks"])
        self.assertIn("keep", e.state["tasks"])

    def test_pruned_orphan_stays_dead_on_disk(self):
        # Task 72 rework (e)：内存清理后，磁盘上的孤儿条目不得被
        # _merge_state_for_save 的 union 合并复活——清理时必须落墓碑。
        e = make_engine(self.tmp)
        disk_state = {"dedup": {}, "retry": {},
                      "tasks": {"ghost": {"status": "monitoring"},
                                "keep": {"status": "paused"}}}
        with open(e.state_path, "w", encoding="utf-8") as f:
            json.dump(disk_state, f)
        # 模拟刚从磁盘加载的内存态
        e.state = {"dedup": {}, "retry": {},
                   "tasks": {"ghost": {"status": "monitoring"},
                             "keep": {"status": "paused"}}}
        self._write_config(e, [task_of("keep")])
        self.assertTrue(e._sync_config())
        # 墓碑必须保留（ghost 不在 current_names 内）
        self.assertIn("ghost", getattr(e, "_deleted_names", ()))
        e._save_state()
        with open(e.state_path, encoding="utf-8") as f:
            reloaded = json.load(f)
        self.assertNotIn("ghost", reloaded["tasks"],
                         "磁盘上的孤儿条目被 union 合并复活了")
        self.assertIn("keep", reloaded["tasks"])
        # 同名任务回到配置 → 墓碑被清除，任务可正常轮询
        self._write_config(e, [task_of("keep"), task_of("ghost")])
        e._config_mtime = None  # 强制重同步（mtime 粒度可能不够）
        self.assertTrue(e._sync_config())
        self.assertNotIn("ghost", getattr(e, "_deleted_names", ()))

    def test_tombstone_capped(self):
        e = make_engine(self.tmp)
        e._deleted_names = {"t%d" % i for i in range(600)}
        self._write_config(e, [])
        e._sync_config()
        self.assertLessEqual(len(e._deleted_names), 500)

    # ---- (f) 启动恢复精确键匹配 ----

    def test_cancelled_dedup_keys_exact_match(self):
        keys = ["BJ|SH|2026-10-10|G1|硬座|*",
                "BJ|SH|2026-10-10|G101|硬座|*",
                "BJ|SH|2026-10-11|G1|硬座|*"]
        got = engine_mod.MonitorEngine._cancelled_dedup_keys(
            {"date": "2026-10-10", "train": "G1"}, keys)
        # "G1" 绝不能误清同日 "G101"
        self.assertEqual(got, ["BJ|SH|2026-10-10|G1|硬座|*"])

    def test_cancelled_dedup_keys_missing_fields(self):
        keys = ["BJ|SH|2026-10-10|G1|硬座|*"]
        # 缺 date/train → 返回 [] 而不是 TypeError 中止整体恢复
        self.assertEqual(
            engine_mod.MonitorEngine._cancelled_dedup_keys(
                {"date": None, "train": "G1"}, keys), [])
        self.assertEqual(
            engine_mod.MonitorEngine._cancelled_dedup_keys({}, keys), [])


class TestTask73MonitorCreateCancel(TempDirCase):
    """Task 73 (P2, Task 28 回归): 建任务流程中车次选择、乘车人选择两处
    Ctrl+C/EOF（read()→None）不得被 `if trains_choice else []` / `if sel:`
    当作"回车默认"消化（静默建出"全部车次/默认乘车人"任务并落盘），
    应抛 KeyboardInterrupt 让 main_menu 接住（"已中断，返回主菜单。"），
    不落盘、不建任务。"""

    GOOD = {"name": "张三", "id_type_code": "1", "id_no": "110101199001011234",
            "mobile": "13800138000", "is_default": True, "is_adult": True}

    def _run_create_task(self, reads, passengers=None):
        import monitor as monitor_mod
        pm = mock.MagicMock()
        pm.load_passengers.return_value = [dict(p) for p in (passengers or [])]
        pm.default_names.return_value = ["张三"]
        saved_cfg = {}
        printed = []
        raised = None
        # reads 消费顺序：车次选择 / 乘车人选择 / 优先级 / 自动下单 /
        #                下单后停止 / 任务名 / 是否立即启动
        try:
            with mock.patch.object(monitor_mod, "passengers_mod", pm), \
                 mock.patch.object(monitor_mod, "pick_station",
                                   side_effect=["北京", "上海"]), \
                 mock.patch.object(monitor_mod, "input_dates",
                                   return_value=(["2026-10-09"], [])), \
                 mock.patch.object(monitor_mod, "ticket") as mock_ticket, \
                 mock.patch.object(monitor_mod, "pick_multi",
                                   return_value=["二等座"]), \
                 mock.patch.object(monitor_mod, "read",
                                   side_effect=list(reads)), \
                 mock.patch.object(monitor_mod, "load_config", return_value={}), \
                 mock.patch.object(monitor_mod, "update_config_locked",
                                   side_effect=lambda mut: saved_cfg.update(
                                       _capture_mut(mut))), \
                 mock.patch("builtins.print",
                            side_effect=lambda *a: printed.append(
                                " ".join(map(str, a)))):
                mock_ticket.load_station_map.return_value = (
                    {"北京": "BJP", "上海": "SHH"},
                    {"BJP": "北京", "SHH": "上海"})
                mock_ticket.query_tickets.side_effect = Exception("offline")
                monitor_mod.menu_create_task()
        except KeyboardInterrupt as e:
            raised = e
        return saved_cfg.get("tasks", []), printed, raised

    def test_trains_choice_ctrl_c_cancels(self):
        # 车次选择处 Ctrl+C：旧代码把 None 当"回车=全部车次"消化，静默建任务；
        # 新代码应抛 KeyboardInterrupt（Task 28 约定），不建任务不落盘
        tasks, printed, raised = self._run_create_task(
            [None, "", "5", "y", "y", "", "n"])
        self.assertIsInstance(raised, KeyboardInterrupt)
        self.assertEqual(tasks, [])

    def test_passenger_sel_ctrl_c_cancels(self):
        # 乘车人选择处 Ctrl+C：旧代码把 None 当"回车=默认乘车人"消化；
        # 新代码应抛 KeyboardInterrupt，不建任务不落盘
        tasks, printed, raised = self._run_create_task(
            ["", None, "5", "y", "y", "", "n"], passengers=[self.GOOD])
        self.assertIsInstance(raised, KeyboardInterrupt)
        self.assertEqual(tasks, [])

    def test_enter_still_means_default(self):
        # 回归 pin：真正的回车（""）仍表示"全部车次/默认乘车人"，正常建任务
        tasks, printed, raised = self._run_create_task(
            ["", "", "5", "y", "y", "", "n"], passengers=[self.GOOD])
        self.assertIsNone(raised)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["trains"], [])
        self.assertEqual(tasks[0]["passenger_names"], ["张三"])


class TestTask83MonitorMenuRobustness(TempDirCase):
    """Task 83 (P2/P3): monitor 菜单健壮性 bundle——
    (a) menu_passengers 顶部 op 提示处 stdin EOF 紧循环重打菜单；
    (b) 同菜单 op2/3/4 的 Ctrl+C/EOF raise 回主菜单，与 op1 的 continue
        回子菜单口径不一致；
    (c) menu_task_list 的 from/to 非字符串 → TypeError 崩菜单；
    (d) menu_create_task 任务名处 Ctrl+C/EOF → `or base_name` 静默落盘
        自动名任务（Task 67/73 同类 P2）。"""

    @staticmethod
    def _printed(mprint):
        return " ".join(str(c.args[0]) for c in mprint.call_args_list)

    def test_passengers_eof_at_op_prompt_exits_menu(self):
        # (a) 旧代码：read()→None 不匹配任何分支 → 紧循环重打菜单刷屏。
        import monitor as monitor_mod
        import passengers as passengers_mod
        with mock.patch.object(passengers_mod, "load_passengers",
                               return_value=[]), \
             mock.patch.object(monitor_mod, "read",
                               side_effect=[None, "0"]) as mread, \
             mock.patch("builtins.print") as mprint:
            monitor_mod.menu_passengers()  # 旧代码会循环第二次才退出
        printed = self._printed(mprint)
        self.assertIn("输入结束", printed)
        self.assertEqual(mread.call_count, 1)  # 只读一次：无紧循环

    def test_passengers_inner_ctrl_c_returns_to_submenu(self):
        # (b) 旧代码 op2/3/4 的 raw is None → raise KeyboardInterrupt
        #（回主菜单），与 op1 的 continue（回子菜单）口径不一致。
        import monitor as monitor_mod
        import passengers as passengers_mod
        for op in ("2", "3", "4"):
            raised = None
            with mock.patch.object(passengers_mod, "load_passengers",
                                   return_value=[]), \
                 mock.patch.object(monitor_mod, "read",
                                   side_effect=[op, None, "0"]), \
                 mock.patch("builtins.print") as mprint:
                try:
                    monitor_mod.menu_passengers()
                except KeyboardInterrupt:
                    raised = True
            self.assertIsNone(raised, "op=%s 不应抛回主菜单" % op)
            self.assertIn("已取消", self._printed(mprint))

    @staticmethod
    def _mock_engine():
        eng = mock.MagicMock()
        eng.task_status.side_effect = lambda t: t["name"]
        eng.state = {"tasks": {}}
        eng.base_interval = 300
        return eng

    def test_task_list_non_str_from_to_no_crash(self):
        # (c) 旧代码 (t["from"] + "-" + t["to"]) 对 int 抛 TypeError 崩菜单。
        import monitor as monitor_mod
        tasks = [{"name": "t1", "from": 123, "to": "上海", "dates": []}]
        with mock.patch.object(monitor_mod, "load_config",
                               return_value={"tasks": tasks}), \
             mock.patch.object(monitor_mod, "fresh_engine",
                               return_value=self._mock_engine()), \
             mock.patch("builtins.print") as mprint:
            monitor_mod.menu_task_list()  # 旧代码抛 TypeError
        printed = self._printed(mprint)
        self.assertIn("警告", printed)
        self.assertIn("配置损坏", printed)
        self.assertNotIn("Traceback", printed)

    GOOD = {"name": "张三", "id_type_code": "1", "id_no": "110101199001011234",
            "mobile": "13800138000", "is_default": True, "is_adult": True}

    def _run_create_task(self, reads, passengers=None):
        import monitor as monitor_mod
        pm = mock.MagicMock()
        pm.load_passengers.return_value = [dict(p) for p in (passengers or [])]
        pm.default_names.return_value = ["张三"]
        saved_cfg = {}
        printed = []
        raised = None
        # reads 消费顺序：车次选择 / 乘车人选择 / 优先级 / 自动下单 /
        #                下单后停止 / 任务名 / 是否立即启动
        try:
            with mock.patch.object(monitor_mod, "passengers_mod", pm), \
                 mock.patch.object(monitor_mod, "pick_station",
                                   side_effect=["北京", "上海"]), \
                 mock.patch.object(monitor_mod, "input_dates",
                                   return_value=(["2026-10-09"], [])), \
                 mock.patch.object(monitor_mod, "ticket") as mock_ticket, \
                 mock.patch.object(monitor_mod, "pick_multi",
                                   return_value=["二等座"]), \
                 mock.patch.object(monitor_mod, "read",
                                   side_effect=list(reads)), \
                 mock.patch.object(monitor_mod, "load_config",
                                   return_value={}), \
                 mock.patch.object(monitor_mod, "update_config_locked",
                                   side_effect=lambda mut: saved_cfg.update(
                                       _capture_mut(mut))), \
                 mock.patch("builtins.print",
                            side_effect=lambda *a: printed.append(
                                " ".join(map(str, a)))):
                mock_ticket.load_station_map.return_value = (
                    {"北京": "BJP", "上海": "SHH"},
                    {"BJP": "北京", "SHH": "上海"})
                mock_ticket.query_tickets.side_effect = Exception("offline")
                monitor_mod.menu_create_task()
        except KeyboardInterrupt as e:
            raised = e
        return saved_cfg.get("tasks", []), printed, raised

    def test_task_name_ctrl_c_cancels_no_silent_create(self):
        # (d) 旧代码 task_name = read(...) or base_name：None→base_name，
        # 用户取消却静默落盘自动名任务。
        tasks, printed, raised = self._run_create_task(
            ["", "", "5", "y", "y", None, "n"], passengers=[self.GOOD])
        self.assertIsNone(raised)
        self.assertEqual(tasks, [])
        self.assertIn("已取消", " ".join(printed))

    def test_task_name_enter_still_creates(self):
        # 回归 pin：真正的回车（""）仍用自动名正常建任务。
        tasks, printed, raised = self._run_create_task(
            ["", "", "5", "y", "y", "", "n"], passengers=[self.GOOD])
        self.assertIsNone(raised)
        self.assertEqual(len(tasks), 1)
        self.assertTrue(tasks[0]["name"])


class TestTask74Launcher(TempDirCase):
    """Task 74: launcher P2/P3 bundle（a–h）。无 Tk 真机，全部 mock/桩测试。"""

    def _var(self, value):
        v = mock.Mock()
        v.get.return_value = value
        return v

    def _make_app(self, **kw):
        app = object.__new__(launcher.LauncherApp)
        app.grabber = None
        app.lc = {"from": "", "to": "", "trains": [], "seat_types": [],
                  "date": "", "purpose_code": "ADULT", "passenger_names": []}
        app.from_ent = self._var(kw.get("from_", "北京"))
        app.to_ent = self._var(kw.get("to_", "上海"))
        app.trains_var = self._var(kw.get("trains", "G101"))
        app.seat_vars = {"二等座": self._var(True)}
        app.seat_pri_var = self._var("")
        app.date_var = self._var(kw.get("date", "2026-10-10"))
        app.date_to_var = self._var("")
        app.start_var = self._var("")
        app.remind_var = self._var(kw.get("remind", "10"))
        app.warm_var = self._var(kw.get("warm", "10"))
        app.pax_vars = kw.get("pax_vars", {})
        app.pax_purpose_vars = {}
        app._save_cfg = mock.Mock()
        app._set_status = mock.Mock()
        app._put_log = mock.Mock()
        app._top = mock.Mock()
        app.go_btn = mock.Mock()
        app.logq = mock.Mock()
        app._auto_vfail_last_msg = None
        return app

    # ---- (b) kind_rank 大小写 ----

    def test_search_stations_kind_rank_case_insensitive(self):
        snapshot = dict(launcher._station_kinds)
        old_path = launcher.STATION_KIND_PATH
        launcher.STATION_KIND_PATH = os.path.join(self.tmp, "station_kind.json")
        self.addCleanup(setattr, launcher, "STATION_KIND_PATH", old_path)

        def _restore():
            launcher._station_kinds.clear()
            launcher._station_kinds.update(snapshot)
        self.addCleanup(_restore)
        with open(launcher.STATION_KIND_PATH, "w", encoding="utf-8") as f:
            json.dump({"BJB": "普速", "VNP": "高铁"}, f)
        launcher._station_kinds.clear()
        stations = [
            {"name": "北京北站X", "spy": "bjb", "py": "beijingbei", "code": "BJB"},
            {"name": "北京南", "spy": "bjn", "py": "beijingnan", "code": "VNP"},
        ]
        with mock.patch.object(launcher, "get_station_index", return_value=stations):
            out = launcher.search_stations("bj")
        # 同分下普速站应排在高铁站前面（旧代码 kind 全 miss → 按站名长度排错）
        self.assertEqual(out[0]["name"], "北京北站X")

    # ---- (c) synced_trains 保留 ----

    def test_load_launcher_config_preserves_synced_trains(self):
        old = launcher.LAUNCHER_CFG_PATH
        p = os.path.join(self.tmp, "launcher_config.json")
        launcher.LAUNCHER_CFG_PATH = p
        self.addCleanup(setattr, launcher, "LAUNCHER_CFG_PATH", old)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"trains": ["G1"], "synced_trains": ["G2", "G3"],
                       "from": "北京"}, f)
        lc = launcher.load_launcher_config()
        self.assertEqual(lc["synced_trains"], ["G2", "G3"])
        self.assertEqual(lc["trains"], ["G1"])

    # ---- (d) 先校验后落盘 + 失败只记一条 warning ----

    def test_start_grab_no_disk_write_on_validate_fail(self):
        app = self._make_app()  # pax_vars={} → 自动开抢校验失败（未选乘车人）
        app.start_grab(auto=True)
        app._save_cfg.assert_not_called()  # 旧代码：_ui_to_lc 内已落盘

    def test_start_grab_saves_on_validate_pass(self):
        # 回归 pin：校验通过时仍落盘一次并启动线程（行为不变）
        app = self._make_app(pax_vars={"张三": self._var(True)})
        with mock.patch.object(launcher, "Grabber") as m_grabber:
            ok = app.start_grab(auto=False)
        self.assertTrue(ok)
        self.assertEqual(app._save_cfg.call_count, 1)
        m_grabber.assert_called_once()

    def test_validate_fail_warn_once(self):
        app = self._make_app()
        app._validate_fail("未选乘车人", "未勾选乘车人，自动开抢已跳过", True)
        app._validate_fail("未选乘车人", "未勾选乘车人，自动开抢已跳过", True)
        app._top.bell.assert_called_once()
        self.assertEqual(app._put_log.call_count, 1)  # 旧代码：每 500ms 记一条

    # ---- (e) Spinbox 非数字兜底 ----

    def test_ui_to_lc_spinbox_non_numeric(self):
        app = self._make_app(remind="abc", warm="xyz")
        app._ui_to_lc()  # 旧代码：int("abc") 抛 ValueError
        self.assertEqual(app.lc["remind_minutes"], 10)
        self.assertEqual(app.lc["warm_minutes"], 10)

    def test_tick_remind_non_numeric_no_crash(self):
        app = object.__new__(launcher.LauncherApp)
        app._row_widgets = {}
        app.grabber = None
        future = (datetime.datetime.now() + datetime.timedelta(hours=2)
                  ).strftime("%Y-%m-%d %H:%M:%S")
        app.start_var = self._var(future)
        app.warm_var = self._var("10")
        app.remind_var = self._var("abc")
        app.countdown_lbl = mock.Mock()
        app.after = mock.Mock()
        app.armed = False
        app.auto_refresh_var = self._var(False)  # _tick 先调 _auto_refresh_trains
        app._querying = False
        app._tick()  # 旧代码：ValueError 从 _tick 逃出（控制台 traceback）
        app.countdown_lbl.configure.assert_called()

    # ---- (f) 测试会话异常走实例 logq ----

    def test_test_session_error_to_instance_logq(self):
        app = object.__new__(launcher.LauncherApp)
        app.logq = mock.Mock()
        app._put_log = mock.Mock()

        def run_sync(target, daemon=True):
            target()
            m = mock.Mock()
            m.start = mock.Mock()
            return m

        with mock.patch.object(launcher.browser_order, "check_session",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(launcher, "LOGQ") as m_logq, \
             mock.patch("threading.Thread", side_effect=run_sync):
            app._test_session()
        app.logq.put.assert_called_once()
        self.assertIn("会话校验异常", app.logq.put.call_args[0][0])
        m_logq.put.assert_not_called()  # 旧代码：异常进了全局 LOGQ

    # ---- (g) 历史记录非 dict 跳过 ----

    def test_history_item_text_skips_non_dict(self):
        with self.assertLogs(launcher.LOG, level="WARNING"):
            self.assertIsNone(launcher._history_item_text("not-a-dict"))
        self.assertIsNone(launcher._history_item_text(None))
        self.assertEqual(
            launcher._history_item_text({"from": "北京", "to": "上海",
                                         "date": "2026-10-10"}),
            "北京 → 上海    2026-10-10")

    # ---- (a) 删任务停抢票线程 ----

    def test_delete_task_stops_grabber_thread(self):
        panel = object.__new__(launcher.TaskManagerPanel)
        task = {"id": "t1", "name": "任务一", "status": "running"}
        panel.tasks = [task]
        w = mock.Mock()
        w.winfo_exists.return_value = True
        w.destroy.side_effect = AssertionError("must not destroy directly")
        panel._windows = {"t1": w}
        panel._mp = mock.Mock()
        panel._find = lambda tid: task if tid == "t1" else None
        panel._refresh_list = mock.Mock()
        panel._put_log = mock.Mock()
        with mock.patch.object(launcher.messagebox, "askyesno",
                               return_value=True), \
             mock.patch.object(launcher, "save_grab_tasks") as m_save:
            panel._delete_task("t1")
        w._on_close.assert_called_once_with()  # 旧代码：直接 w.destroy()
        w.destroy.assert_not_called()
        self.assertEqual(panel.tasks, [])
        m_save.assert_called()

    # ---- (h) 关窗保留已抢到 ----

    def test_on_window_closed_keeps_ok(self):
        panel = object.__new__(launcher.TaskManagerPanel)
        panel.tasks = [{"id": "t1", "name": "n1", "status": "ok"},
                       {"id": "t2", "name": "n2", "status": "running"}]
        panel._windows = {}
        panel._refresh_list = mock.Mock()
        panel._put_log = mock.Mock()
        with mock.patch.object(launcher, "save_grab_tasks"):
            panel._on_window_closed("t1")
            panel._on_window_closed("t2")
        self.assertEqual(panel.tasks[0]["status"], "ok")  # 旧代码：被抹成 idle
        self.assertEqual(panel.tasks[1]["status"], "idle")

    # ---- (g) rework：构造时过滤，self.items 与列表框行号 1:1 ----

    def test_history_pick_aligned_with_junk_in_middle(self):
        import tkinter as tk
        from tkinter import ttk
        good1 = {"from": "北京", "to": "上海", "date": "2026-10-10"}
        good2 = {"from": "广州", "to": "深圳", "date": "2026-10-11"}
        picked = []
        lb = mock.Mock()
        lb.curselection.return_value = (1,)  # 用户双击显示的第 2 行
        with mock.patch.object(tk.Toplevel, "__init__", return_value=None), \
             mock.patch.object(launcher.QueryHistoryDialog, "title"), \
             mock.patch.object(launcher.QueryHistoryDialog, "geometry"), \
             mock.patch.object(launcher.QueryHistoryDialog, "transient"), \
             mock.patch.object(ttk, "Frame", return_value=mock.Mock()), \
             mock.patch.object(ttk, "Label", return_value=mock.Mock()), \
             mock.patch.object(ttk, "Scrollbar", return_value=mock.Mock()), \
             mock.patch.object(tk, "Listbox", return_value=lb):
            with self.assertLogs(launcher.LOG, level="WARNING"):
                dlg = launcher.QueryHistoryDialog(
                    mock.Mock(), [good1, "junk-string", good2], picked.append)
        dlg.destroy = mock.Mock()  # 无显示环境，_pick 的 destroy 用桩
        # junk 被过滤：列表框只插入 2 行
        self.assertEqual(lb.insert.call_count, 2)
        # 双击显示的第 2 行（索引 1）必须拿到 good2，而不是错位的 "junk-string"
        dlg._pick()
        self.assertEqual(picked, [good2])  # 旧代码：picked == ["junk-string"]

    # ---- (d) rework：warn 按失败原因去重，原因变化重新提示 ----

    def test_validate_fail_warn_per_reason(self):
        app = self._make_app()
        app._validate_fail("t", "原因A", True)
        app._validate_fail("t", "原因A", True)   # 同原因 → 不再提示
        app._validate_fail("t", "原因B", True)   # 原因变化 → 重新提示
        app._validate_fail("t", "原因B", True)
        self.assertEqual(app._top.bell.call_count, 2)  # 旧代码：1（原因B 静默）
        self.assertEqual(app._put_log.call_count, 2)

    def test_validate_fail_warn_resets_after_pass(self):
        app = self._make_app()
        app._validate_fail("t", "原因A", True)
        # 模拟 start_grab(auto=True) 校验通过后的重置：新一轮 armed 会话同原因再报
        app._auto_vfail_last_msg = None
        app._validate_fail("t", "原因A", True)
        self.assertEqual(app._put_log.call_count, 2)


class TestTask75Gui(TempDirCase):
    """Task 75: gui P2/P3 bundle（a–e）。无 Tk 真机，全部 mock tkinter。"""

    def _write_config(self, content):
        cfg = os.path.join(self.tmp, "config.json")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write(content)
        return cfg

    # ---- (a) 损坏配置：JSONDecodeError → 空配置 + 友好提示 ----

    def test_load_config_corrupt_returns_empty(self):
        cfg = self._write_config("{bad json,")
        with mock.patch.object(gui, "CONFIG_PATH", cfg):
            gui._CONFIG_CORRUPT_WARNED = False
            try:
                self.assertEqual(gui.load_config(), {})
            finally:
                gui._CONFIG_CORRUPT_WARNED = False

    def test_load_config_corrupt_warns_once(self):
        cfg = self._write_config("{bad json,")
        with mock.patch.object(gui, "CONFIG_PATH", cfg):
            gui._CONFIG_CORRUPT_WARNED = False
            try:
                with self.assertLogs(gui.LOG, level="WARNING") as cm:
                    gui.load_config()
                    gui.load_config()
            finally:
                gui._CONFIG_CORRUPT_WARNED = False
        warns = [r for r in cm.records if "解析失败" in r.getMessage()]
        self.assertEqual(len(warns), 1)

    def test_main_corrupt_config_friendly_prompt_and_starts(self):
        cfg = self._write_config("{bad json,")
        with mock.patch.object(gui, "CONFIG_PATH", cfg), \
             mock.patch.object(gui.tk, "Tk") as mock_tk, \
             mock.patch.object(gui, "messagebox") as mock_mb, \
             mock.patch.object(gui, "MonitorApp") as mock_app, \
             mock.patch.object(gui, "setup_logging"), \
             mock.patch.object(gui, "_ensure_stdio"):
            gui._CONFIG_CORRUPT_WARNED = False
            try:
                gui.main()
            finally:
                gui._CONFIG_CORRUPT_WARNED = False
        self.assertEqual(mock_mb.showerror.call_count, 1)
        self.assertIn("损坏", mock_mb.showerror.call_args[0][1])
        mock_app.assert_called_once()  # 仍以空配置启动，不直接退出
        mock_tk.return_value.mainloop.assert_called_once()

    # ---- (b) 编辑保存保留 seats_by_date ----

    def _edited(self, task, **kw):
        base = dict(from_name="北京", to_name="上海", dates=["2026-10-01"],
                    date_range=[], trains=["G101"], seat_types=["二等座"],
                    passengers=["张三"], auto_order=True, stop_after_order=False,
                    priority=5, purpose_code="ADULT")
        base.update(kw)
        return gui._build_edited_task(task, **base)

    def test_build_edited_task_preserves_seats_by_date(self):
        task = {"name": "t", "uid": "u1", "notify_channels": ["email"],
                "seats_by_date": {"2026-10-01": ["商务座"]}}
        new = self._edited(task)
        # 旧代码：写死 {}，按日席别被静默抹除
        self.assertEqual(new["seats_by_date"], {"2026-10-01": ["商务座"]})
        self.assertEqual(new["name"], "t")
        self.assertEqual(new["seat_types"], ["二等座"])

    def test_build_edited_task_no_seats_by_date(self):
        new = self._edited({"name": "t"})
        self.assertEqual(new["seats_by_date"], {})
        self.assertEqual(new["notify_channels"], ["email"])

    # ---- (c) refresh 缓存日期文本：非法日期只警告一次 ----

    def _make_task_page(self):
        page = gui.TaskPage.__new__(gui.TaskPage)
        page.app = mock.Mock()
        page.app.engine_thread = None
        page.tree = mock.Mock()
        page.tree.selection.return_value = ()
        page.tree.get_children.return_value = ()
        page.tree.yview.return_value = (0.0, 1.0)
        page.edit_btn = mock.Mock()
        page._task_by_iid = {}
        page._dates_text_cache = {}
        return page

    def test_refresh_caches_date_text_no_warning_spam(self):
        task = {"name": "t1", "from": "A", "to": "B",
                "dates": "20261001", "trains": [], "seat_types": []}
        page = self._make_task_page()
        with mock.patch.object(gui, "load_config",
                               return_value={"tasks": [task]}), \
             mock.patch.object(gui, "load_state",
                               return_value={"tasks": {}}):
            with self.assertLogs(gui.LOG, level="WARNING") as cm:
                page.refresh()
                page.refresh()
                page.refresh()
        warns = [r for r in cm.records if "非列表" in r.getMessage()]
        # 旧代码：每次 refresh 都调 expand_dates → 3 条 warning 刷屏
        self.assertEqual(len(warns), 1)

    def test_refresh_cache_invalidates_on_config_change(self):
        page = self._make_task_page()
        t1 = {"name": "t1", "dates": ["2026-10-01"]}
        t2 = {"name": "t1", "dates": ["2026-10-02"]}
        with mock.patch.object(gui, "load_config",
                               return_value={"tasks": [t1]}), \
             mock.patch.object(gui, "load_state",
                               return_value={"tasks": {}}):
            page.refresh()
            shown1 = page._dates_text_cache[("t1", repr(["2026-10-01"]), repr(None))]
        with mock.patch.object(gui, "load_config",
                               return_value={"tasks": [t2]}), \
             mock.patch.object(gui, "load_state",
                               return_value={"tasks": {}}):
            page.refresh()
        self.assertEqual(shown1, "2026-10-01")
        self.assertNotIn(("t1", repr(["2026-10-01"]), repr(None)),
                         page._dates_text_cache)

    # ---- (d) 非连续 dates 如实展示/回写 ----

    def test_date_display_text_non_contiguous(self):
        text, non_contig = gui._date_display_text(["2026-10-03", "2026-10-01"])
        self.assertTrue(non_contig)
        self.assertEqual(text, "2026-10-01、2026-10-03")

    def test_date_display_text_contiguous_single_empty(self):
        self.assertEqual(gui._date_display_text(["2026-10-01", "2026-10-02"]),
                         ("2026-10-01~2026-10-02", False))
        self.assertEqual(gui._date_display_text(["2026-10-01"]),
                         ("2026-10-01", False))
        self.assertEqual(gui._date_display_text([]), ("", False))

    def test_resolve_saved_dates_keeps_original_when_untouched(self):
        # 非连续展示且用户未改动 → 原样回写，不静默扩展为连续区间
        dates, dr = gui._resolve_saved_dates(
            "2026-10-01、2026-10-03", True,
            ["2026-10-01", "2026-10-03"], [],
            "2026-10-01、2026-10-03")
        self.assertEqual(dates, ["2026-10-01", "2026-10-03"])
        self.assertEqual(dr, [])

    def test_resolve_saved_dates_parses_edited(self):
        dates, dr = gui._resolve_saved_dates(
            "2026-10-01、2026-10-03", True,
            ["2026-10-01", "2026-10-03"], [],
            "2026-10-05~2026-10-06")
        self.assertEqual(dates, [])
        self.assertEqual(dr, ["2026-10-05", "2026-10-06"])

    def test_resolve_saved_dates_illegal_raises(self):
        with self.assertRaises(ValueError):
            gui._resolve_saved_dates("", False, [], [], "not-a-date")

    # ---- (e) 非 dict state 按 {} 处理 ----

    def test_update_state_locked_non_dict_state(self):
        cfg = self._write_config(json.dumps({"state_file": "state.json"}))
        def mutator(state):
            state.setdefault("tasks", {}).setdefault("t", {})["status"] = "paused"
        with mock.patch.object(gui, "CONFIG_PATH", cfg), \
             mock.patch.object(gui.engine_mod, "load_state_file",
                               return_value=[]), \
             mock.patch.object(gui.filelock, "file_lock") as mock_lock, \
             mock.patch.object(gui.appcommon, "write_state") as mock_write:
            mock_lock.return_value.__enter__.return_value = None
            with self.assertLogs(gui.LOG, level="WARNING") as cm:
                gui.update_state_locked(mutator)
        # 旧代码：state.setdefault 直接 AttributeError
        mock_write.assert_called_once()
        saved = mock_write.call_args[0][1]
        self.assertEqual(saved, {"tasks": {"t": {"status": "paused"}}})
        self.assertTrue(any("非 dict" in r.getMessage() for r in cm.records))


class TestTask76TicketSession(TempDirCase):
    """Task 76: ticket P2/P3（_SESSION 永不刷新 / A/I/J 席别未验证）。"""

    def setUp(self):
        super().setUp()
        self._old_session = ticket._SESSION
        self._old_streak = ticket._session_fail_streak
        self._old_warned = set(ticket._WARNED_UNORDERABLE_SKIPS)
        ticket._SESSION = None
        ticket._session_fail_streak = 0
        ticket._WARNED_UNORDERABLE_SKIPS.clear()

    def tearDown(self):
        ticket._SESSION = self._old_session
        ticket._session_fail_streak = self._old_streak
        ticket._WARNED_UNORDERABLE_SKIPS.clear()
        ticket._WARNED_UNORDERABLE_SKIPS.update(self._old_warned)
        super().tearDown()

    @staticmethod
    def _dead_session():
        s = mock.Mock()
        s.get.side_effect = ticket.requests.RequestException("conn reset")
        return s

    @staticmethod
    def _live_session(result=None):
        s = mock.Mock()
        r = mock.Mock()
        r.raise_for_status.return_value = None
        r.json.return_value = {"httpstatus": 200,
                               "data": {"result": result if result is not None else []}}
        s.get.return_value = r
        return s

    def _fail_rounds(self, n, sess):
        with mock.patch.object(ticket, "get_session", return_value=sess):
            for _ in range(n):
                with self.assertRaises(Exception):
                    ticket.query_tickets("VNP", "ZAF", "2026-10-10")

    def test_consecutive_failures_invalidate_session(self):
        """(a) 连续 5 次查询失败 → 旧会话被丢弃（懒重建）并记 warning。"""
        sentinel = self._dead_session()
        ticket._SESSION = sentinel
        with self.assertLogs(ticket.LOG, level="WARNING") as cm:
            self._fail_rounds(5, sentinel)
        self.assertIsNone(ticket._SESSION)
        self.assertTrue(any("重建" in r.getMessage() or "会话" in r.getMessage()
                            for r in cm.records),
                        "未记录会话重建 warning: %s" % [r.getMessage() for r in cm.records])

    def test_success_resets_streak(self):
        """(a) 成功一次即清零：4 失败→成功→4 失败，不触发重建。"""
        sentinel = self._dead_session()
        ticket._SESSION = sentinel
        self._fail_rounds(4, sentinel)
        self.assertEqual(ticket._session_fail_streak, 4)
        with mock.patch.object(ticket, "get_session",
                               return_value=self._live_session(["row1"])):
            self.assertEqual(ticket.query_tickets("VNP", "ZAF", "2026-10-10"), ["row1"])
        self.assertEqual(ticket._session_fail_streak, 0)
        self._fail_rounds(4, sentinel)
        self.assertIs(ticket._SESSION, sentinel)  # 未达 5 次，不丢弃

    def test_rebuild_recovers(self):
        """(a) 丢弃后下次 get_session 拿到新会话 → 查询恢复。"""
        ticket._SESSION = self._dead_session()
        self._fail_rounds(5, ticket._SESSION)
        self.assertIsNone(ticket._SESSION)
        live = self._live_session(["row9"])
        with mock.patch.object(ticket.requests, "Session", return_value=live):
            s = ticket.get_session()
            self.assertIs(s, live)
            rows = ticket.query_tickets("VNP", "ZAF", "2026-10-10")
        self.assertEqual(rows, ["row9"])
        self.assertEqual(ticket._session_fail_streak, 0)

    def test_unorderable_skip_logged(self):
        """(b) 不限席别遇到 A（高级动卧）有票：非静默，warning 说明跳过原因。"""
        avail = {"高级动卧": "有", "二等座": "有"}
        with self.assertLogs(ticket.LOG, level="WARNING") as cm:
            cand = ticket.seat_candidates_for("D123", [], [], avail)
        self.assertEqual(cand, ["二等座"])
        msgs = [r.getMessage() for r in cm.records]
        self.assertTrue(any("高级动卧" in m for m in msgs),
                        "跳过未明确告知: %s" % msgs)

    def test_unorderable_skip_warn_once(self):
        """(b) 同（车次，席别）只提示一次，不每轮刷屏。"""
        avail = {"高级动卧": "有", "二等座": "有"}
        with self.assertLogs(ticket.LOG, level="WARNING") as cm:
            ticket.seat_candidates_for("D123", [], [], avail)
            ticket.seat_candidates_for("D123", [], [], avail)
        n = sum(1 for r in cm.records if "高级动卧" in r.getMessage())
        self.assertEqual(n, 1)

    def test_aij_assumption_marked_unverified(self):
        """(b) 码表处明确标注 A/I/J 不可下单为未验证假设。"""
        import inspect
        src = inspect.getsource(ticket)
        self.assertIn("UNVERIFIED", src)


# ============================ Task 77 ============================

class TestTask77Order(unittest.TestCase):
    """Task 77: order P2/P3（date 回退误判 / 交集归因 / 改判前席别回归 /
    alias 丢失 / 时区窗口 / 非 JSON 不重试 / 双查限流 / order_no 脱敏）。"""

    # ---- (a) 缺 start_train_date_page 时 date 留空 ----

    def test_normalize_missing_start_date_leaves_date_empty(self):
        # 旧代码：date 回退为 order_date（今天）→ find_duplicate 按乘车日期
        # 永远 miss → 被误判为 blocked"其它行程"。新：留空（无 date 记录被跳过）。
        item = {"sequence_no": "E1", "train_code_page": "K225",
                "order_date": "2026-10-08 10:00:00",
                "passengerDTOList": [{"passenger_name": "张三"}]}
        got = order_mod._normalize_order_item(item, "未完成/未支付")
        self.assertEqual(got["date"], "")

    def test_normalize_present_start_date_unchanged(self):
        # 回归 pin：字段存在时行为不变
        item = {"sequence_no": "E1", "train_code_page": "K225",
                "start_train_date_page": "2026-10-10 00:00:00",
                "order_date": "2026-10-08 10:00:00",
                "passengerDTOList": [{"passenger_name": "张三"}]}
        got = order_mod._normalize_order_item(item, "未完成/未支付")
        self.assertEqual(got["date"], "2026-10-10")

    # ---- (b) find_recent_order 全员命中 ----

    def _ts(self, dt):
        return dt.replace(tzinfo=datetime.timezone.utc).timestamp()

    def test_find_recent_order_requires_all_pax(self):
        # 旧代码：任一交集即中 → 部分重叠的他人订单被归因成本次提交。
        # 新：与 Task 59 同口径（全员命中）。
        not_before = self._ts(datetime.datetime(2026, 10, 8, 10, 0, 0))
        orders = [{"train": "G101", "date": "2026-10-10",
                   "passengers": ["张三", "李四"], "order_no": "Epartial",
                   "order_ts": not_before + 60}]
        got = order_mod.find_recent_order(orders, "2026-10-10", "G101",
                                          ["张三", "王五"], not_before)
        self.assertIsNone(got)

    def test_find_recent_order_all_hit_still_matches(self):
        # 回归 pin：全员命中仍归因
        not_before = self._ts(datetime.datetime(2026, 10, 8, 10, 0, 0))
        orders = [{"train": "G101", "date": "2026-10-10",
                   "passengers": ["张三", "王五"], "order_no": "Emine",
                   "order_ts": not_before + 60}]
        got = order_mod.find_recent_order(orders, "2026-10-10", "G101",
                                          ["张三", "王五"], not_before)
        self.assertIsNotNone(got)
        self.assertEqual(got["order_no"], "Emine")

    # ---- (c) find_duplicate 用改判后席别比对 ----

    def test_find_duplicate_uses_post_regrade_seat(self):
        # 旧代码：调用方传"无座"（改判前），已存订单 seat 是"二等座"（改判后），
        # 双侧非空不等即跳过 → 同行程未支付单漏检。新：统一到提交时有效席别。
        orders = [{"date": "2026-10-10", "train": "G101",
                   "passengers": ["张三"], "order_no": "E1", "seat": "二等座"}]
        dup = order_mod.find_duplicate(orders, "2026-10-10", "G101", ["张三"],
                                       seat_name="无座")
        self.assertIsNotNone(dup)
        self.assertEqual(dup["order_no"], "E1")

    def test_find_duplicate_seat_mismatch_still_skipped(self):
        # 回归 pin：真不一致仍跳过
        orders = [{"date": "2026-10-10", "train": "G101",
                   "passengers": ["张三"], "order_no": "E1", "seat": "二等座"}]
        self.assertIsNone(order_mod.find_duplicate(
            orders, "2026-10-10", "G101", ["张三"], seat_name="一等座"))

    # ---- (d) HTTP 路径改判后写 alias_seat/selected_seat ----

    def test_order_ticket_http_writes_alias_seat(self):
        ticket = {"train_code": "G101", "query_date": "2026-10-10",
                  "from_name": "北京", "to_name": "上海",
                  "start_time": "08:00", "arrive_time": "12:00",
                  "train_location": "P3", "secret_str": "x"}
        task = {"passenger_names": ["张三"]}
        with mock.patch.object(order_mod, "load_session",
                               return_value=object()), \
             mock.patch.object(order_mod, "check_login",
                               return_value=(True, "u")), \
             mock.patch.object(order_mod, "submit_with_busy_retry",
                               return_value=(True, "")), \
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
            ok, msg, extra = order_mod.order_ticket({}, task, ticket, "无座")
        self.assertTrue(ok, msg)
        # 旧代码：extra 无 alias_seat/selected_seat，seat 显示改判前的"无座"
        self.assertEqual(extra.get("alias_seat"), "二等座")
        self.assertEqual(extra.get("selected_seat"), "无座")

    # ---- (e) 查询窗口用北京时间 ----

    def test_check_existing_orders_beijing_windows(self):
        # 机器 TZ=UTC、冻结在 2026-10-08 20:00 UTC（= 北京 2026-10-09 04:00）：
        # 旧代码窗口按机器本地算 → today="2026-10-08"；新：北京时间 → "2026-10-09"
        # 注：datetime.now() 的 C 实现不走 Python 层 time.time mock，
        # 故用 FakeDateTime 冻结 now()。
        real_dt = datetime.datetime
        frozen = real_dt(2026, 10, 8, 20, 0, 0,
                         tzinfo=datetime.timezone.utc)

        class FakeDateTime(real_dt):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return frozen.replace(tzinfo=None)
                return frozen.astimezone(tz)

        seen = {}

        def fake_post(url, data=None, timeout=None):
            # G/H 都 POST 到 queryMyOrder：按 query_where 区分
            seen[(data or {}).get("query_where")] = dict(data or {})
            r = mock.MagicMock()
            if "NoComplete" in url:
                r.json.return_value = {"data": {"orderDBList": []}}
            else:
                r.json.return_value = {"data": {"OrderDTODataList": []}}
            return r

        old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        try:
            try:
                import time as _time
                _time.tzset()
            except Exception:
                pass
            session = mock.MagicMock()
            session.post.side_effect = fake_post
            with mock.patch.object(order_mod.datetime, "datetime", FakeDateTime):
                order_mod.check_existing_orders(session, "2026-10-09")
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            try:
                import time as _time2
                _time2.tzset()
            except Exception:
                pass
        g = seen.get("G", {})
        self.assertEqual(g.get("queryEndDate"), "2026-10-09")
        self.assertEqual(g.get("queryStartDate"), "2026-08-10")

    # ---- (f) 非 JSON 响应重试 3 次 ----

    def _non_json_resp(self):
        r = mock.Mock()
        r.json.side_effect = ValueError("No JSON object could be decoded")
        r.text = "<html>waf intercept</html>"
        return r

    def test_submit_non_json_retries_then_succeeds(self):
        ok_resp = mock.Mock()
        ok_resp.json.return_value = {"status": True}
        session = mock.MagicMock()
        session.post.side_effect = [self._non_json_resp(),
                                    self._non_json_resp(), ok_resp]
        with mock.patch("time.sleep"):
            ok, msg = order_mod.submit_with_busy_retry(
                session, {"secret_str": "x"}, "O", "2026-10-10", 3, 0.01)
        # 旧代码：第 1 次非 JSON 即判失败（post 只调 1 次）
        self.assertTrue(ok, msg)
        self.assertEqual(session.post.call_count, 3)

    def test_submit_non_json_gives_up_after_3(self):
        session = mock.MagicMock()
        session.post.side_effect = [self._non_json_resp()] * 5
        with mock.patch("time.sleep"):
            ok, msg = order_mod.submit_with_busy_retry(
                session, {"secret_str": "x"}, "O", "2026-10-10", 5, 0.01)
        self.assertFalse(ok)
        self.assertEqual(session.post.call_count, 3)

    def test_submit_non_json_tries_one_still_retries_3(self):
        # 非 JSON 重试独立于 busy 的 tries：tries=1 时仍最多试 3 次，
        # 而不是只试 1 次就报"连续 1 次系统忙"。
        session = mock.MagicMock()
        session.post.side_effect = [self._non_json_resp()] * 5
        with mock.patch("time.sleep"):
            ok, msg = order_mod.submit_with_busy_retry(
                session, {"secret_str": "x"}, "O", "2026-10-10", 1, 0.01)
        self.assertFalse(ok)
        self.assertEqual(session.post.call_count, 3)
        # 失败原因是"非 JSON 响应"类（带原始响应片段），而非"连续 1 次系统忙"
        self.assertIn("waf intercept", msg)

    def test_confirm_non_json_retries_then_succeeds(self):
        ok_resp = mock.Mock()
        ok_resp.json.return_value = {"status": True,
                                     "data": {"submitStatus": True}}
        session = mock.MagicMock()
        session.post.side_effect = [self._non_json_resp(), ok_resp]
        with mock.patch("time.sleep"):
            ok, msg = order_mod.confirm_with_busy_retry(
                session, "tok", "left", "key", "P3", "pts", "old", 3, 0.01)
        # 旧代码：非 JSON 即判失败不重试
        self.assertTrue(ok, msg)
        self.assertEqual(session.post.call_count, 2)

    # ---- (g) classify_with_time 只查一次 ----

    def test_classify_with_time_single_query(self):
        not_before = self._ts(datetime.datetime(2026, 10, 8, 10, 0, 0))
        orders = [{"train": "G101", "date": "2026-10-10",
                   "passengers": ["张三"], "order_no": "Emine",
                   "order_ts": not_before + 60, "_no_complete": True,
                   "status": "未完成/未支付"}]
        with mock.patch.object(order_mod, "check_existing_orders",
                               return_value=orders) as m_q:
            cls, ono, raw, recent = order_mod.classify_with_time(
                "2026-10-10", "G101", ["张三"], not_before_ts=not_before,
                session=object())
        # 旧代码：classify 内查一次 + 归因又查一次 = 2 次
        self.assertEqual(m_q.call_count, 1)
        self.assertEqual(cls, "unpaid")
        self.assertIsNotNone(recent)
        self.assertEqual(recent["order_no"], "Emine")

    def test_classify_with_time_no_session_error_path(self):
        # session=None 且无浏览器 state 文件 → error，recent 为 None（旧行为保持）
        cls, ono, raw, recent = order_mod.classify_with_time(
            "2026-10-10", "G101", ["张三"], not_before_ts=12345, session=None)
        self.assertEqual(cls, "error")
        self.assertIsNone(recent)

    # ---- (h) 畸形订单警告脱敏 ----

    def test_malformed_order_warning_masks_order_no(self):
        orders = [{"date": "2026-10-10", "train": "K225",
                   "passengers": [], "order_no": "E1234567890"}]
        with self.assertLogs(order_mod.LOG, level="WARNING") as cm:
            order_mod.find_duplicate(orders, "2026-10-10", "K225", ["张三"])
        out = "\n".join(cm.output)
        # 旧代码：打印完整订单号原文
        self.assertNotIn("E1234567890", out)
        self.assertIn("E123****7890", out)


class TestTask80GuiPassengersSave(TempDirCase):
    """Task 80(b)：save_passengers 拒写（return False）必须被 GUI 处理。

    旧代码三处裸调：磁盘盒子不可解密时显示"完成"/刷新列表，但磁盘未写，
    重启后修改丢失。helper 失败时弹 error（与 LOG.error 口径一致）。"""

    def test_save_failure_shows_error_not_done(self):
        with mock.patch.object(gui.passengers_mod, "save_passengers",
                               return_value=False), \
             mock.patch.object(gui, "messagebox") as mb:
            ok = gui._save_passengers_or_warn([], parent=None)
        self.assertFalse(ok)
        mb.showerror.assert_called_once()
        # 绝不能弹"完成"
        mb.showinfo.assert_not_called()

    def test_save_success_no_popup(self):
        with mock.patch.object(gui.passengers_mod, "save_passengers",
                               return_value=True), \
             mock.patch.object(gui, "messagebox") as mb:
            ok = gui._save_passengers_or_warn([], parent=None)
        self.assertTrue(ok)
        mb.showerror.assert_not_called()
        mb.showinfo.assert_not_called()


class TestTask80PassengersCrypto(TempDirCase):
    """Task 80(c)(d)：passengers 加密回退与哨兵。"""

    # ---- (c) cryptography 缺失的明文回退必须记 error（三路可见），不再 print ----

    def test_encrypt_no_crypto_fallback_logs_error(self):
        # 旧代码此处 print（GUI 下直接消失），用户无感知地明文保存证件号/手机号
        with mock.patch.object(pax_mod, "_is_windows", return_value=False), \
             mock.patch.object(pax_mod, "_get_fernet",
                               return_value=(None, None)), \
             self.assertLogs("monitor", level="ERROR") as cm:
            enc, data = pax_mod._encrypt('{"a": 1}')
        self.assertEqual(enc, "none")
        self.assertEqual(data, '{"a": 1}')
        self.assertTrue(any("明文" in m for m in cm.output),
                        "明文回退必须记 error 日志（三路可见）：%s" % cm.output)

    # ---- (d) dpapi1: 哨兵冲突：字面以哨兵开头的明文必须被真正加密 ----

    def test_protect_secret_colliding_prefix_gets_encrypted(self):
        # 旧代码：text.startswith("dpapi1:") 直接原样返回，明文存盘后
        # unprotect 误判为密文 → SecretDecryptError。必须真正加密并可还原。
        fake_blob = lambda b: b"ENCRYPTED:" + b
        with mock.patch.object(pax_mod, "_dpapi_protect",
                               side_effect=fake_blob):
            enc = pax_mod.protect_secret("dpapi1:my-password")
        self.assertTrue(enc.startswith("dpapi1:"))
        self.assertNotEqual(enc, "dpapi1:my-password")  # 旧代码此处直接返回原文
        # 加密输出可被 unprotect 完整还原
        with mock.patch.object(pax_mod, "_dpapi_unprotect",
                               side_effect=lambda b: b[len(b"ENCRYPTED:"):]):
            self.assertEqual(pax_mod.unprotect_secret(enc), "dpapi1:my-password")

    def test_protect_secret_encrypted_value_passthrough(self):
        # 真密文（哨兵 + 合法 base64）必须原样返回，避免二次包裹
        blob = "dpapi1:" + __import__("base64").b64encode(b"ENCRYPTED:x").decode()
        with mock.patch.object(pax_mod, "_dpapi_protect",
                               side_effect=AssertionError("must not re-encrypt")):
            self.assertEqual(pax_mod.protect_secret(blob), blob)

    def test_protect_secret_base64_collision_residual(self):
        # 残留（已文档化）：字面 "dpapi1:"+严格 base64 的明文仍会被当成密文；
        # 读取失败必须诚实抛 SecretDecryptError，不静默错密。
        self.assertEqual(pax_mod.protect_secret("dpapi1:QUJD"), "dpapi1:QUJD")
        with mock.patch.object(pax_mod, "_dpapi_unprotect",
                               side_effect=RuntimeError("not our blob")):
            with self.assertRaises(pax_mod.SecretDecryptError):
                pax_mod.unprotect_secret("dpapi1:QUJD")


class TestTask81FilelockAppcommon(TempDirCase):
    """Task 81: filelock + appcommon P3 bundle（a–f）。"""

    def _bad_files(self, path):
        import glob
        return glob.glob(path + ".bad-*")

    # ---- (a) 首跑加锁不建空数据文件、不误隔离 ----
    def test_append_history_first_run_no_spurious_quarantine(self):
        p = os.path.join(self.tmp, "order_history.json")
        rec = {"train": "G1"}
        with self.assertNoLogs("monitor", level="ERROR"):
            appcommon.append_history(p, rec)
        self.assertEqual(self._bad_files(p), [],
                         "首跑加锁不应建出空数据文件并误隔离")
        with open(p, encoding="utf-8") as f:
            self.assertEqual(json.load(f), [rec])
        # sidecar 锁文件可以存在（与 engine/gui/monitor/launcher 的
        # file_lock(path + ".lock") 约定一致），但数据文件语义必须干净
        self.assertFalse(os.path.exists(p + ".bad-dummy"))

    # ---- (b) 挪移失败：跳过本次写入，证据保留原地 ----
    def _flaky_replace_once(self):
        real_replace = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("busy")  # 隔离挪移瞬间被占用
            return real_replace(src, dst)  # 之后句柄释放，写盘能成功
        return flaky

    def test_append_history_move_failed_skips_write_preserves_evidence(self):
        p = os.path.join(self.tmp, "order_history.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{CORRUPT")
        with mock.patch.object(appcommon.os, "replace",
                               self._flaky_replace_once()):
            with self.assertLogs("monitor", level="ERROR") as logs:
                appcommon.append_history(p, {"train": "G2"})
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{CORRUPT",
                             "证据必须保留原地，不得被单条记录覆写")
        self.assertIn("证据保留原地", "\n".join(logs.output))

    def test_upsert_order_move_failed_skips_write_preserves_evidence(self):
        p = os.path.join(self.tmp, "orders.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{CORRUPT")
        with mock.patch.object(appcommon.os, "replace",
                               self._flaky_replace_once()):
            with self.assertLogs("monitor", level="ERROR"):
                appcommon.upsert_order(p, "K1", {"order_no": "E1"})
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{CORRUPT",
                             "证据必须保留原地，不得被空库+新记录覆写")

    # ---- (c) 放弃隔离后重读仍失败：放弃本次追加，不覆写 ----
    def test_append_history_abandon_reread_failure_skips_not_overwrites(self):
        p = os.path.join(self.tmp, "order_history.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{CORRUPT")
        orig = appcommon.quarantine_corrupt

        def sneaky_delete(path, fp=None):
            os.remove(path)  # 读失败与隔离之间文件被删 → 放弃隔离 → 重读失败
            return orig(path, fp)

        with mock.patch.object(appcommon, "quarantine_corrupt", sneaky_delete):
            with self.assertLogs("monitor", level="ERROR") as logs:
                appcommon.append_history(p, {"train": "G9"})
        self.assertFalse(os.path.exists(p),
                         "重读失败不得用空数据+新记录覆写（可能存在的健康写入）")
        self.assertIn("重读", "\n".join(logs.output))

    def test_upsert_order_abandon_reread_failure_skips_not_overwrites(self):
        p = os.path.join(self.tmp, "orders.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{CORRUPT")
        orig = appcommon.quarantine_corrupt

        def sneaky_delete(path, fp=None):
            os.remove(path)
            return orig(path, fp)

        with mock.patch.object(appcommon, "quarantine_corrupt", sneaky_delete):
            with self.assertLogs("monitor", level="ERROR"):
                appcommon.upsert_order(p, "K1", {"order_no": "E1"})
        self.assertFalse(os.path.exists(p),
                         "重读失败不得用空库+新记录覆写")

    # ---- (d) upsert_order 用跨进程 file_lock ----
    def test_upsert_order_uses_cross_process_file_lock(self):
        p = os.path.join(self.tmp, "orders.json")
        seen = []
        orig = filelock.file_lock

        @contextlib.contextmanager
        def spy(path, timeout=10.0):
            seen.append(path)
            with orig(path, timeout=timeout) as lk:
                yield lk

        with mock.patch.object(filelock, "file_lock", spy):
            appcommon.upsert_order(p, "K1", {"order_no": "E1"})
        self.assertEqual(seen, [p + ".lock"],
                         "upsert_order 必须用跨进程 file_lock 包住读-改-写")
        with open(p, encoding="utf-8") as f:
            self.assertIn("K1", json.load(f)["orders"])

    def test_upsert_order_lock_timeout_skips_gracefully(self):
        p = os.path.join(self.tmp, "orders.json")

        def boom(path, timeout=10.0):
            raise TimeoutError("busy")

        with mock.patch.object(filelock, "file_lock", boom):
            with self.assertLogs("monitor", level="WARNING"):
                appcommon.upsert_order(p, "K1", {"order_no": "E1"})  # 不抛
        self.assertFalse(os.path.exists(p))

    # ---- (e) sweep 正则收紧：用户备份不误删，真 tmp 仍清理 ----
    def test_sweep_stale_tmp_keeps_user_bak_files(self):
        p = os.path.join(self.tmp, "state.json")
        bak = p + ".bak2024-01"  # 用户自建备份：字母+数字-数字形态
        with open(bak, "w", encoding="utf-8") as f:
            f.write("keep")
        stale_kinds = []
        for kind in ("tmp", "launcher", "guisave", "monsave"):
            q = p + ".%s12345-67890" % kind
            with open(q, "w", encoding="utf-8") as f:
                f.write("{}")
            stale_kinds.append(q)
        old = time.time() - 7200
        os.utime(bak, (old, old))
        for q in stale_kinds:
            os.utime(q, (old, old))
        appcommon.sweep_stale_tmp(p)
        self.assertTrue(os.path.exists(bak), "用户备份文件不得被误删")
        for q in stale_kinds:
            self.assertFalse(os.path.exists(q), "真正的 tmp 残留仍应清理: %s" % q)

    # ---- (f) parse_date_range 非 str 输入抛 ValueError ----
    def test_parse_date_range_non_str_raises_value_error(self):
        with self.assertRaises(ValueError):
            appcommon.parse_date_range(20261007)
        with self.assertRaises(ValueError):
            appcommon.parse_date_range("2026-10-01", 20261007)
        with self.assertRaises(ValueError):
            appcommon.parse_date_range(["2026-10-01"])
        # 合法输入不受影响；None 仍走原空值路径（ValueError）
        self.assertEqual(appcommon.parse_date_range("2026-10-01"),
                         (["2026-10-01"], []))
        with self.assertRaises(ValueError):
            appcommon.parse_date_range(None)


class TestTask82StationDbRebuildRefusesZero(TempDirCase):
    """Task 82 (P3)：get_station_index() 降级为空时 rebuild() 不得静默清零已有库。

    触发：离线/无缓存时跑 `python station_db.py rebuild`。旧行为：直接落盘
    count=0，覆盖 3404 站的 stations_db.json 且打印误导性"共 0 站"。
    """

    def setUp(self):
        super().setUp()
        self._real_db_path = station_db_mod.DB_PATH
        station_db_mod.DB_PATH = os.path.join(self.tmp, "stations_db.json")
        self.addCleanup(self._restore_db_path)

    def _restore_db_path(self):
        station_db_mod.DB_PATH = self._real_db_path

    def _seed_old_db(self, count=3404):
        with open(station_db_mod.DB_PATH, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "updated": "2026-01-01 00:00:00",
                       "count": count, "stations": []}, f)
        with open(station_db_mod.DB_PATH, encoding="utf-8") as f:
            return f.read()

    def _run_rebuild_empty_index(self, **kwargs):
        """空索引下跑 rebuild：返回 (ret, stdout)。"""
        import io
        with mock.patch.object(launcher, "get_station_index",
                               return_value=[]), \
             mock.patch.object(launcher, "load_station_kinds",
                               return_value={}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ret = station_db_mod.rebuild(**kwargs)
            return ret, buf.getvalue()

    def test_empty_index_refuses_write_and_keeps_old_db(self):
        before = self._seed_old_db(count=3404)
        ret, out = self._run_rebuild_empty_index()
        self.assertIsNone(ret, "空索引且无 --force 时应返回 None 表示失败")
        with open(station_db_mod.DB_PATH, encoding="utf-8") as f:
            self.assertEqual(f.read(), before, "旧库字节级原样保留，不得被清零")

    def test_empty_index_message_is_explicit_failure_not_zero_count(self):
        self._seed_old_db(count=3404)
        _, out = self._run_rebuild_empty_index()
        self.assertNotIn("共 0 站", out, "'共 0 站'是误导信息，不得再出现")
        self.assertIn("失败", out, "应给出明确的失败提示")
        self.assertIn("保留", out, "应告知旧库已保留")

    def test_empty_index_no_old_db_creates_nothing(self):
        self.assertFalse(os.path.exists(station_db_mod.DB_PATH))
        ret, _ = self._run_rebuild_empty_index()
        self.assertIsNone(ret)
        self.assertFalse(os.path.exists(station_db_mod.DB_PATH),
                         "无旧库时也不得凭空写出 count=0 的空库")

    def test_empty_index_with_force_writes_and_warns(self):
        self._seed_old_db(count=3404)
        ret, out = self._run_rebuild_empty_index(force=True)
        self.assertIsNotNone(ret)
        self.assertEqual(ret["count"], 0)
        with open(station_db_mod.DB_PATH, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["count"], 0)
        self.assertIn("--force", out, "--force 显式覆盖必须有警告/说明")
        self.assertNotIn("共 0 站（运营中）", out,
                         "即使强制写入，'共 0 站（运营中）'也是虚假表述")

    def test_normal_index_still_writes(self):
        import io
        idx = [{"name": "北京", "code": "BJP", "py": "beijing", "spy": "bj"},
               {"name": "上海", "code": "SHH", "py": "shanghai", "spy": "sh"}]
        with mock.patch.object(launcher, "get_station_index",
                               return_value=idx), \
             mock.patch.object(launcher, "load_station_kinds",
                               return_value={"BJP": "高铁"}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ret = station_db_mod.rebuild()
            out = buf.getvalue()
        self.assertEqual(ret["count"], 2)
        self.assertEqual(ret["stations"][0]["kind"], "高铁")
        self.assertEqual(ret["stations"][1]["kind"], "待识别")
        with open(station_db_mod.DB_PATH, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["count"], 2)
        self.assertIn("共 2 站", out, "正常重建的输出不得变化")


class TestTask84LauncherP3(TempDirCase):
    """Task 84: launcher P3 bundle（a–d）。无 Tk 真机，全部 mock/桩测试。"""

    def _var(self, value):
        v = mock.Mock()
        v.get.return_value = value
        return v

    # ---- (a) trains 字符串不得逐字符拆 ----

    def test_normalize_trains_string_single_with_warning(self):
        warned = []
        got = launcher._normalize_trains("G101", warned.append)
        self.assertEqual(got, ["G101"])
        self.assertEqual(len(warned), 1, "字符串 trains 必须记一次警告")

    def test_normalize_trains_list_unchanged(self):
        warned = []
        got = launcher._normalize_trains([" g101 ", "K2"], warned.append)
        self.assertEqual(got, ["G101", "K2"])
        self.assertEqual(warned, [])

    def test_normalize_trains_non_string_items_skipped(self):
        warned = []
        got = launcher._normalize_trains(["G101", 123, None], warned.append)
        self.assertEqual(got, ["G101"])

    def test_normalize_trains_illegal_shape_ignored_with_warning(self):
        warned = []
        got = launcher._normalize_trains(123, warned.append)
        self.assertEqual(got, [])
        self.assertEqual(len(warned), 1)

    def test_grabber_run_wires_normalize_trains(self):
        import queue as _queue
        th = object.__new__(launcher.Grabber)
        th.lc = {"from": "北京", "to": "上海", "date": "2026-10-10",
                 "trains": "G101", "seat_types": [], "seat_priority": "",
                 "passenger_names": []}
        th.logq = _queue.Queue()
        th.stop_event = threading.Event()
        th.result = None
        with mock.patch.object(launcher, "HERE", self.tmp), \
             mock.patch.object(launcher, "_normalize_trains",
                               wraps=launcher._normalize_trains) as sp:
            th._run()
        self.assertEqual(th.result, (False, "请至少勾选一种席别"))
        sp.assert_called_once()
        self.assertEqual(sp.call_args[0][0], "G101")

    def test_merge_trains_from_monitor_string_not_char_split(self):
        cfg = os.path.join(self.tmp, "config.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"tasks": [{"trains": "G101"}]}, f, ensure_ascii=False)
        lc = {"trains": []}
        logged = []
        with mock.patch.object(launcher, "HERE", self.tmp), \
             mock.patch.object(launcher, "log", logged.append):
            ret = launcher.merge_trains_from_monitor(lc, saver=lambda c: None)
        self.assertTrue(ret)
        self.assertEqual(lc["trains"], ["G101"])

    # ---- (b) update_config_locked 写路径损坏配置 ----

    def test_update_config_locked_damaged_config_friendly_abort(self):
        cfg = os.path.join(self.tmp, "config.json")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write("{损坏的 json,,,")
        with open(cfg, "rb") as f:
            before = f.read()
        with mock.patch.object(gui, "CONFIG_PATH", cfg), \
             mock.patch.object(gui, "messagebox") as mb:
            with self.assertRaises(Exception) as cm:
                gui.update_config_locked(lambda c: c)
        self.assertNotIsInstance(
            cm.exception, (json.JSONDecodeError, UnicodeDecodeError),
            "损坏配置不得抛出原始 JSON 解析异常（traceback）")
        self.assertIn("损坏", str(cm.exception))
        mb.showerror.assert_called_once()
        with open(cfg, "rb") as f:
            self.assertEqual(f.read(), before,
                             "损坏的旧文件不得被覆盖")

    # ---- (c) log 路径 PII 脱敏 ----

    def test_mask_pii_text_masks_order_no(self):
        masked = launcher._mask_pii_text(
            "订单已提交成功（未支付）：订单号 E123456789，下单时间 2026-10-08，请尽快去 12306 支付")
        self.assertNotIn("E123456789", masked)
        self.assertIn("订单号", masked)
        plain = "已提交订单（未支付）：https://kyfw.12306.cn/otn/payOrder/init"
        self.assertEqual(launcher._mask_pii_text(plain), plain)

    def test_mask_names(self):
        self.assertEqual(launcher._mask_names(["张三丰", "李"]), "张**、*")

    def test_ensure_passengers_log_masks_names(self):
        cfg = os.path.join(self.tmp, "config.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"tasks": [{"passenger_names": ["张三丰"]}]},
                      f, ensure_ascii=False)
        logged = []
        with mock.patch.object(launcher, "HERE", self.tmp), \
             mock.patch.object(launcher, "log", logged.append), \
             mock.patch.object(launcher.passengers_mod, "save_passengers",
                               return_value=True) as m_save:
            launcher.ensure_passengers()
        m_save.assert_called_once()
        text = "".join(logged)
        self.assertNotIn("张三丰", text)
        self.assertIn("张**", text)

    # ---- (d) save_passengers 拒写（False）不得显示成功 ----

    def _make_passenger_dialog(self):
        dlg = object.__new__(launcher.PassengerDialog)
        dlg.plist = []
        dlg.name_var = self._var("张三")
        dlg.id_var = self._var("110101199001011234")
        dlg.default_var = self._var(False)
        dlg.type_var = self._var("二代身份证")
        dlg.mob_var = self._var("13800000000")
        dlg.adult_var = self._var(True)
        dlg.pick = mock.Mock()
        dlg.on_saved = None
        return dlg

    def test_passenger_dialog_save_refusal_shows_error_not_success(self):
        dlg = self._make_passenger_dialog()
        with mock.patch.object(launcher.passengers_mod, "save_passengers",
                               return_value=False), \
             mock.patch.object(launcher, "messagebox") as mb:
            launcher.PassengerDialog._save(dlg)
        mb.showerror.assert_called_once()
        mb.showinfo.assert_not_called()
        dlg.pick.configure.assert_not_called()

    def test_passenger_dialog_delete_refusal_no_refresh(self):
        dlg = self._make_passenger_dialog()
        dlg.plist = [{"name": "张三"}]
        dlg.pick.get.return_value = "张三"
        with mock.patch.object(launcher.passengers_mod, "save_passengers",
                               return_value=False), \
             mock.patch.object(launcher, "messagebox") as mb:
            launcher.PassengerDialog._delete(dlg)
        mb.showerror.assert_called_once()
        dlg.pick.configure.assert_not_called()

    def test_ensure_passengers_refusal_no_false_success_log(self):
        cfg = os.path.join(self.tmp, "config.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"tasks": [{"passenger_names": ["张三"]}]},
                      f, ensure_ascii=False)
        logged = []
        with mock.patch.object(launcher, "HERE", self.tmp), \
             mock.patch.object(launcher, "log", logged.append), \
             mock.patch.object(launcher.passengers_mod, "save_passengers",
                               return_value=False):
            launcher.ensure_passengers()
        text = "".join(logged)
        self.assertNotIn("已从监控任务导入乘车人", text)
        self.assertIn("错误", text)


class TestTask85OrderBrowserOrder(TempDirCase):
    """Task 85: save_session 三元组语义 / 分隔符共享常量 / docstring /
    warm.close owner-aware。"""

    def test_save_session_preserves_same_name_multi_path(self):
        # (a) 同名多 path 的 Cookie 不再按 name last-wins 丢数据
        import requests
        jar = requests.cookies.RequestsCookieJar()
        jar.set("JSESSIONID", "AAA", domain=".12306.cn", path="/otn")
        jar.set("JSESSIONID", "BBB", domain=".12306.cn", path="/passport")
        sess = requests.Session()
        sess.cookies = jar
        p = os.path.join(self.tmp, "session_cookies.json")
        order_mod.save_session(sess, p)
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data), 2,
                         "同名多 path 应落盘 2 条目（三元组键），不是 last-wins 只剩 1 条")
        self.assertEqual(sorted(v["value"] for v in data.values()), ["AAA", "BBB"])
        # round-trip：load_session 必须能解析 save_session 写出的三元组键
        s2 = order_mod.load_session(p)
        got = {(c.name, c.path, c.value) for c in s2.cookies
               if c.name == "JSESSIONID"}
        self.assertEqual(got, {("JSESSIONID", "/otn", "AAA"),
                               ("JSESSIONID", "/passport", "BBB")})

    def test_cookie_key_sep_is_shared_constant(self):
        # (b) "\x1f" 字面收敛为共享常量，且与 capture_session 同一对象
        import capture_session
        self.assertEqual(order_mod.COOKIE_KEY_SEP, "\x1f")
        self.assertIs(order_mod.COOKIE_KEY_SEP, capture_session.COOKIE_KEY_SEP)

    def test_classify_docstring_matches_actual_semantics(self):
        # (c) docstring 不再写"乘车人交集"（实际是目标集 ⊆ 订单乘车人）
        doc = order_mod.classify_order_status.__doc__ or ""
        self.assertNotIn("乘车人交集", doc)
        self.assertIn("blocked", doc)

    def _bare_warm(self, owner_ident, owner_thread):
        ws = browser_order.WarmSession.__new__(browser_order.WarmSession)
        ws._closed = False
        ws._ctx = None
        ws._p = None
        ws._file_locked = False
        ws._local_locked = False
        ws._owner = owner_ident
        ws._owner_thread = owner_thread
        return ws

    def test_warm_close_clears_owner_depth_cross_thread(self):
        # (d) 非创建线程 close() 也要清理 owner 线程的 depth 记录，
        # 否则 owner 后续 exclusive() 走重入捷径跳过跨进程文件锁（P2）
        owner = threading.Thread(name="fake-warm-owner-85d")
        browser_order._set_depth(1, owner)
        self.addCleanup(browser_order._set_depth, 0, owner)
        ws = self._bare_warm(123456789, owner)  # owner ident ≠ 当前线程
        ws.close()
        self.assertEqual(browser_order._get_depth(owner), 0,
                         "跨线程 close() 未清理 owner 线程的 depth=1 残留")

    def test_warm_close_clears_own_depth_same_thread(self):
        # (d) 同线程 close() 语义不变：depth 清零
        me = threading.current_thread()
        browser_order._set_depth(1, me)
        self.addCleanup(browser_order._set_depth, 0, me)
        ws = self._bare_warm(threading.get_ident(), me)
        ws.close()
        self.assertEqual(browser_order._get_depth(me), 0)


class TestTask86ProbeLoginP2P3(unittest.TestCase):
    """Task 86: probe_login/ticket/monitor P2/P3 bundle。

    (a) step4_poll_qr 对非 dict JSON（数组/字符串/null）不再抛 AttributeError：
        记警告后跳过本轮继续轮询（Task 78d 同类口径）。
    (b) step5 的 username 打印脱敏 + 两处 show(r) 走 mask_pii=True
        —— Task 78c 已落实（含 Task 65 reviewer 的 minor follow-up），
        由 TestTask78 的 test_step5_show_calls_use_mask_pii /
        test_step5_username_print_is_masked 钉住，本处不加重复测试。
    (c) ticket.query_tickets docstring「返回字典」→ 实际返回 list，修正文档。
    (d) monitor.menu_notify 的授权码走 protect_secret 加密存储
        —— Task 66 已落实，本处加接线 pin 测试防回归。
    """

    # ---- (a) step4 轮询非 dict JSON ----

    def _run_step4_non_dict(self, body):
        fake = mock.Mock()
        fake.post.side_effect = [
            _FakeResp(body),          # r.json() 成功但返回非 dict
            KeyboardInterrupt(),      # 退出轮询（外层优雅捕获）
        ]
        return fake

    def test_step4_poll_qr_array_json_skipped_no_crash(self):
        # RED on old code: data.get → AttributeError 逃出 step4_poll_qr
        with mock.patch.object(probe_login, "SESSION",
                               self._run_step4_non_dict('["a","b"]')), \
             mock.patch("builtins.print") as mprint, \
             mock.patch("time.sleep"):
            result = probe_login.step4_poll_qr("uuid-x")
        self.assertIsNone(result)
        out = "\n".join(str(c.args[0]) for c in mprint.call_args_list if c.args)
        self.assertIn("非 JSON 对象", out)

    def test_step4_poll_qr_null_json_skipped_no_crash(self):
        # RED on old code: data=None → data.get 抛 AttributeError
        with mock.patch.object(probe_login, "SESSION",
                               self._run_step4_non_dict("null")), \
             mock.patch("builtins.print") as mprint, \
             mock.patch("time.sleep"):
            result = probe_login.step4_poll_qr("uuid-x")
        self.assertIsNone(result)
        out = "\n".join(str(c.args[0]) for c in mprint.call_args_list if c.args)
        self.assertIn("非 JSON 对象", out)

    # ---- (c) query_tickets docstring ----

    def test_query_tickets_docstring_says_list(self):
        # RED on old code: docstring 写「返回按车次分组的字典」，实际返回 list
        doc = ticket.query_tickets.__doc__ or ""
        self.assertNotIn("字典", doc)
        self.assertIn("列表", doc)

    def test_query_tickets_returns_list(self):
        # 行为 pin：query_tickets 实际返回 result 列表（与修正后的 docstring 一致）
        payload = {"httpstatus": 200, "data": {"result": ["G101|...|..."]}}
        fake_resp = mock.Mock()
        fake_resp.json.return_value = payload
        fake_resp.raise_for_status.return_value = None
        fake_sess = mock.Mock()
        fake_sess.get.return_value = fake_resp
        with mock.patch.object(ticket, "get_session", return_value=fake_sess):
            result = ticket.query_tickets("BJP", "SHH", "2026-10-09")
        self.assertIsInstance(result, list)
        self.assertEqual(result, ["G101|...|..."])

    # ---- (d) menu_notify 授权码加密接线 pin ----

    def test_menu_notify_routes_password_through_protect_secret(self):
        # pin（防回归）：menu_notify 存授权码必须经过 protect_secret。
        # RED on pre-Task-66 code: protect_secret 根本未被调用。
        import monitor as monitor_mod
        saved = {}
        with mock.patch.object(monitor_mod, "load_config", return_value={}), \
             mock.patch.object(monitor_mod, "read",
                               side_effect=["", "465", "", "", "", "n"]), \
             mock.patch("getpass.getpass", return_value="new-auth-code"), \
             mock.patch.object(monitor_mod, "save_config",
                               side_effect=lambda c, **k: saved.update(c)), \
             mock.patch.object(monitor_mod.notify_mod, "protect_secret",
                               wraps=monitor_mod.notify_mod.protect_secret) as ps:
            monitor_mod.menu_notify()
        ps.assert_called_once_with("new-auth-code")
        self.assertEqual(saved["notify"]["email"]["password"],
                         monitor_mod.notify_mod.protect_secret("new-auth-code"))


class TestTask87StaleReadDegradation(TempDirCase):
    """Task 87 (P3): 交互式菜单 load→输入→save 的 stale-read —— 乐观并发降级。

    写侧已有 Task 46 的跨进程锁，但用户思考期间他进程的写入会被本次 save
    静默覆写。改后：save_config(config, expect_stamp) 在锁内重读字节指纹，
    不一致 → 打印警告并返回 False（放弃本次保存，不覆写对方修改）。
    """

    def _patch_cfg(self, monitor_mod):
        cfg = os.path.join(self.tmp, "config.json")
        p = mock.patch.object(monitor_mod, "CONFIG_PATH", cfg)
        p.start()
        self.addCleanup(p.stop)
        return cfg

    def _write(self, cfg, obj):
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def _read(self, cfg):
        with open(cfg, encoding="utf-8") as f:
            return json.load(f)

    def _printed(self, mprint):
        return " ".join(str(c.args[0]) for c in mprint.call_args_list)

    def test_external_write_abandons_save(self):
        # RED on old code: save_config 无 expect_stamp 参数（TypeError）；
        # 且旧行为会静默覆写外部写入（数据丢失）。
        import monitor as monitor_mod
        cfg = self._patch_cfg(monitor_mod)
        self._write(cfg, {"tasks": []})
        config = monitor_mod.load_config()
        stamp = monitor_mod._config_stamp()
        # 用户思考期间：另一进程写入 config.json
        self._write(cfg, {"tasks": [{"name": "ext"}]})
        with mock.patch("builtins.print") as mprint:
            ok = monitor_mod.save_config({"tasks": []}, expect_stamp=stamp)
        self.assertFalse(ok)
        self.assertEqual(self._read(cfg), {"tasks": [{"name": "ext"}]})
        self.assertIn("已放弃", self._printed(mprint))

    def test_unchanged_writes_ok(self):
        # 回归 pin：无外部写入时正常落盘（旧代码即通过）。
        import monitor as monitor_mod
        cfg = self._patch_cfg(monitor_mod)
        self._write(cfg, {"tasks": []})
        stamp = monitor_mod._config_stamp()
        with mock.patch("builtins.print") as mprint:
            ok = monitor_mod.save_config({"tasks": [{"name": "t1"}]},
                                         expect_stamp=stamp)
        self.assertTrue(ok)
        self.assertEqual(self._read(cfg)["tasks"], [{"name": "t1"}])
        self.assertNotIn("已放弃", self._printed(mprint))

    def test_identical_rewrite_no_false_positive(self):
        # 他进程重写了完全相同的字节 → 指纹一致 → 不误报放弃。
        import monitor as monitor_mod
        cfg = self._patch_cfg(monitor_mod)
        raw = b'{"tasks": []}'
        with open(cfg, "wb") as f:
            f.write(raw)
        stamp = monitor_mod._config_stamp()
        with open(cfg, "wb") as f:
            f.write(raw)  # 外部相同内容重写
        ok = monitor_mod.save_config({"tasks": [{"name": "t1"}]},
                                     expect_stamp=stamp)
        self.assertTrue(ok)
        self.assertEqual(self._read(cfg)["tasks"], [{"name": "t1"}])

    def test_first_run_no_file_writes_ok(self):
        # 首跑无 config.json：stamp 为 None，保存应正常建文件。
        import monitor as monitor_mod
        cfg = self._patch_cfg(monitor_mod)
        with mock.patch("builtins.print"):
            config = monitor_mod.load_config()
        stamp = monitor_mod._config_stamp()
        self.assertIsNone(stamp)
        ok = monitor_mod.save_config({"tasks": []}, expect_stamp=stamp)
        self.assertTrue(ok)
        self.assertEqual(self._read(cfg), {"tasks": []})

    def test_external_create_abandons(self):
        # 首跑无文件，但思考期间他进程创建了 config.json → 放弃，不覆写。
        import monitor as monitor_mod
        cfg = self._patch_cfg(monitor_mod)
        with mock.patch("builtins.print"):
            config = monitor_mod.load_config()
        stamp = monitor_mod._config_stamp()
        self.assertIsNone(stamp)
        self._write(cfg, {"tasks": [{"name": "ext"}]})
        with mock.patch("builtins.print") as mprint:
            ok = monitor_mod.save_config({"tasks": []}, expect_stamp=stamp)
        self.assertFalse(ok)
        self.assertEqual(self._read(cfg)["tasks"], [{"name": "ext"}])
        self.assertIn("已放弃", self._printed(mprint))

    def test_legacy_call_still_writes(self):
        # 回归 pin：不传 expect_stamp 保持旧行为（直接写，旧代码即通过）。
        import monitor as monitor_mod
        cfg = self._patch_cfg(monitor_mod)
        self._write(cfg, {"tasks": [{"name": "ext"}]})
        ok = monitor_mod.save_config({"tasks": []})
        self.assertTrue(ok)
        self.assertEqual(self._read(cfg), {"tasks": []})

    def test_menu_task_ops_delete_abandons_on_stale(self):
        # 接线 pin：删除任务时思考期间有外部写入 → 放弃保存，
        # 外部任务不丢失，且不同步清 state（任务实际未删）。
        import monitor as monitor_mod
        cfg = self._patch_cfg(monitor_mod)
        self._write(cfg, {"tasks": [{"name": "t1", "from": "A", "to": "B",
                                     "dates": [], "trains": [],
                                     "seat_types": [], "priority": 5,
                                     "passenger_names": []}]})
        eng = mock.MagicMock()
        eng.task_status.return_value = "monitoring"
        eng.task_interval.return_value = 60
        eng.base_interval = 60
        reads = iter(["1", "5"])
        external = {}
        def fake_read(prompt, default=""):
            v = next(reads)
            if "选择操作" in prompt:
                # 用户思考期间：另一进程新增任务并落盘
                self._write(cfg, {"tasks": [{"name": "t1"},
                                            {"name": "t2-ext"}]})
                with open(cfg, "rb") as f:
                    external["bytes"] = f.read()
            return v
        with mock.patch.object(monitor_mod, "fresh_engine",
                               return_value=eng), \
             mock.patch.object(monitor_mod, "read",
                               side_effect=fake_read), \
             mock.patch.object(monitor_mod, "ask_yes_no",
                               return_value=True), \
             mock.patch.object(monitor_mod, "_clear_task_state") as mclear, \
             mock.patch("builtins.print") as mprint:
            monitor_mod.menu_task_ops()
        with open(cfg, "rb") as f:
            self.assertEqual(f.read(), external["bytes"])
        self.assertEqual([t["name"] for t in self._read(cfg)["tasks"]],
                         ["t1", "t2-ext"])
        mclear.assert_not_called()
        self.assertIn("已放弃", self._printed(mprint))

    def test_menu_notify_abandons_on_stale(self):
        # 接线 pin：通知设置交互期间有外部写入 → 放弃保存，不打印"已保存"。
        import monitor as monitor_mod
        cfg = self._patch_cfg(monitor_mod)
        self._write(cfg, {"notify": {"email": {"username": "ext@x.com"}}})
        # 第 6 个 read 供旧代码的"是否发送测试邮件"提示（新代码放弃保存后直接返回，用不到）
        reads = iter(["", "465", "", "", "", "n"])
        external = {}
        def fake_read(prompt, default=""):
            v = next(reads)
            if "收件人" in prompt:
                self._write(cfg, {"notify": {"email": {"username":
                                                       "changed@x.com"}}})
                with open(cfg, "rb") as f:
                    external["bytes"] = f.read()
            return v
        with mock.patch.object(monitor_mod, "read",
                               side_effect=fake_read), \
             mock.patch("getpass.getpass", return_value=""), \
             mock.patch("builtins.print") as mprint:
            monitor_mod.menu_notify()
        with open(cfg, "rb") as f:
            self.assertEqual(f.read(), external["bytes"])
        printed = self._printed(mprint)
        self.assertIn("已放弃", printed)
        self.assertNotIn("已保存", printed)


class TestTask88ConvergenceLeftovers(TempDirCase):
    """Task 88 (P3): 收敛残留 bundle——
    (a) menu_task_list 的 trains/seat_types 非 list（如 int）→ TypeError 崩菜单；
    (b) launcher 四处展示路径 ","/"、".join 对字符串 trains 逐字符拆（纯展示）；
    (c) launcher _put_log 的"[查询]"分支经 _ptxt 记全量乘车人姓名；
    (d) menu_passengers 四处 load→think→save 写侧连跨进程锁都没有，
        并发写入可被静默丢失（Task 87 同口径：filelock + 乐观并发检查）。
    """

    @staticmethod
    def _printed(mprint):
        return " ".join(str(c.args[0]) for c in mprint.call_args_list)

    @staticmethod
    def _mock_engine():
        eng = mock.MagicMock()
        eng.task_status.side_effect = lambda t: t["name"]
        eng.state = {"tasks": {}}
        eng.base_interval = 300
        return eng

    def test_task_list_non_list_trains_no_crash(self):
        # (a) 旧代码 "/".join(t.get("trains") or []) 对 int/"G101" 抛 TypeError
        # 或逐字符拆（"G/1/0/1"），崩菜单/误导。
        import monitor as monitor_mod
        tasks = [{"name": "t1", "from": "北京", "to": "上海", "dates": [],
                  "trains": 123, "seat_types": "G101"}]
        with mock.patch.object(monitor_mod, "load_config",
                               return_value={"tasks": tasks}), \
             mock.patch.object(monitor_mod, "fresh_engine",
                               return_value=self._mock_engine()), \
             mock.patch("builtins.print") as mprint:
            monitor_mod.menu_task_list()  # 旧代码 TypeError
        printed = self._printed(mprint)
        self.assertIn("配置损坏", printed)
        self.assertNotIn("Traceback", printed)

    def test_display_trains_string_not_char_split(self):
        # (b) 旧代码无此 helper（AttributeError）；各展示点对 "G101"
        # 会 join 成 "G,1,0,1" 逐字符拆。
        import launcher as launcher_mod
        self.assertEqual(launcher_mod._display_trains("G101"), ["G101"])
        self.assertEqual(",".join(launcher_mod._display_trains("G101")), "G101")
        self.assertEqual(launcher_mod._display_trains(["g1 ", ""]), ["G1"])
        self.assertEqual(launcher_mod._display_trains(123), [])
        self.assertEqual(launcher_mod._display_trains(None), [])

    def test_mask_one_name(self):
        # (c) 旧代码无此 helper（AttributeError）；_ptxt 记全名。
        import launcher as launcher_mod
        self.assertEqual(launcher_mod._mask_one_name("张三丰"), "张**")
        self.assertEqual(launcher_mod._mask_one_name("李"), "*")
        self.assertEqual(launcher_mod._mask_one_name(""), "")
        # _mask_names 复用同一口径（单源）
        self.assertEqual(launcher_mod._mask_names(["张三", "李四"]), "张*、李*")

    def _patch_pax(self, pm):
        p = os.path.join(self.tmp, "passengers.json")
        mp = mock.patch.object(pm, "DEFAULT_PATH", p)
        mp.start()
        self.addCleanup(mp.stop)
        return p

    def test_passengers_stamp_mismatch_abandons(self):
        # (d) 旧代码 save_passengers 无 expect_stamp 参数（TypeError）；
        # 且旧行为会静默覆写思考期间的外部写入（数据丢失）。
        import passengers as pm
        self._patch_pax(pm)
        pm.save_passengers([{"name": "A"}])
        stamp = pm.passengers_stamp()
        # 用户思考期间：另一进程（GUI/Launcher）写入 passengers.json
        pm.save_passengers([{"name": "EXT"}])
        with mock.patch("builtins.print") as mprint:
            ok = pm.save_passengers([{"name": "A2"}], expect_stamp=stamp)
        self.assertFalse(ok)
        names = [x["name"] for x in pm.load_passengers()]
        self.assertEqual(names, ["EXT"])  # 外部数据保留，未被覆写
        self.assertIn("已放弃", self._printed(mprint))

    def test_passengers_stamp_match_writes(self):
        # 回归 pin：无外部写入时正常落盘。
        import passengers as pm
        self._patch_pax(pm)
        pm.save_passengers([{"name": "A"}])
        stamp = pm.passengers_stamp()
        with mock.patch("builtins.print") as mprint:
            ok = pm.save_passengers([{"name": "A", "x": 1}], expect_stamp=stamp)
        self.assertTrue(ok)
        self.assertEqual(pm.load_passengers()[0]["x"], 1)
        self.assertNotIn("已放弃", self._printed(mprint))

    def test_passengers_first_run_stamp_none(self):
        # 首跑：文件不存在 → 指纹 None → 正常写入，不误报。
        import passengers as pm
        self._patch_pax(pm)
        stamp = pm.passengers_stamp()
        self.assertIsNone(stamp)
        with mock.patch("builtins.print") as mprint:
            ok = pm.save_passengers([{"name": "A"}], expect_stamp=stamp)
        self.assertTrue(ok)
        self.assertEqual([x["name"] for x in pm.load_passengers()], ["A"])
        self.assertNotIn("已放弃", self._printed(mprint))

    def test_passengers_stamp_identical_rewrite_no_false_positive(self):
        # 外部重写了完全相同的字节 → 指纹一致 → 不误报放弃。
        import passengers as pm
        p = self._patch_pax(pm)
        pm.save_passengers([{"name": "A"}])
        stamp = pm.passengers_stamp()
        with open(p, "rb") as f:
            raw = f.read()
        with open(p, "wb") as f:
            f.write(raw)  # 外部相同内容重写
        ok = pm.save_passengers([{"name": "A2"}], expect_stamp=stamp)
        self.assertTrue(ok)
        self.assertEqual([x["name"] for x in pm.load_passengers()], ["A2"])

    def test_menu_passengers_external_write_abandons_and_reloads(self):
        # (d) 端到端：op1 添加的思考期间（姓名输入时）外部写入；
        # 旧代码静默覆写（names==["A","B"]），新代码放弃并重载。
        import monitor as monitor_mod
        import passengers as pm
        self._patch_pax(pm)
        pm.save_passengers([{"name": "A"}])
        state = {"ops": 0}

        def fake_read(prompt="", default=""):
            if "选择操作" in prompt:
                state["ops"] += 1
                return "1" if state["ops"] == 1 else "0"
            if "姓名：" in prompt:
                # 用户思考期间：另一进程改了 passengers.json
                pm.save_passengers([{"name": "EXT"}])
                return "B"
            return default

        with mock.patch.object(monitor_mod, "read", side_effect=fake_read), \
             mock.patch("builtins.print") as mprint:
            monitor_mod.menu_passengers()
        names = [x["name"] for x in pm.load_passengers()]
        self.assertEqual(names, ["EXT"])  # 外部数据保留，未被 A+B 覆写
        printed = self._printed(mprint)
        self.assertIn("已放弃", printed)
        self.assertIn("重新载入", printed)
        self.assertNotIn("已添加并加密保存", printed)


class TestTask89GuiLeftovers(TempDirCase):
    """Task 89：gui 残留——编辑对话框写回污染 / 展示字符拆 / PassengerDialog 指纹缺口。

    (a) P2：TaskEditDialog 对字符串 trains（如 "G101"）逐字符拆展示，
    经逗号回写污染 config（"G101"→["G","1","0","1"]）→ 静默漏单。
    (b) P3：StartMonitorDialog/TaskPage 任务行展示字符拆（纯展示）。
    (c) P3：gui.PassengerDialog 无乐观并发指纹，思考窗口内被外部写入会静默覆写。
    """

    # ---- (a)(b) 展示用车次归一化 helper ----

    def test_display_trains_string_is_single_not_char_split(self):
        # 旧代码无此 helper（AttributeError）；且旧展示表达式 ",".join("G101")
        # 会产出 "G,1,0,1"。
        self.assertEqual(gui._display_trains("G101"), ["G101"])
        self.assertEqual(gui._display_trains("g101"), ["G101"])  # 与引擎口径一致转大写

    def test_display_trains_list_and_illegal_shapes(self):
        self.assertEqual(gui._display_trains(["G101", "K225"]), ["G101", "K225"])
        self.assertEqual(gui._display_trains(None), [])
        self.assertEqual(gui._display_trains(123), [])
        self.assertEqual(gui._display_trains(["G101", 123, "  "]), ["G101"])

    def test_trains_roundtrip_no_pollution(self):
        # 对话框载入→展示→用户未改→保存 的完整链条：字符串/列表/空输入
        # 都不得污染 config。
        for raw, expect in [("G101", ["G101"]), (["G101", "K225"], ["G101", "K225"]),
                            ([], []), (None, [])]:
            displayed = ",".join(gui._display_trains(raw))
            # 下行即 TaskEditDialog.save 的解析表达式（逐字）
            saved = [t.strip() for t in
                     displayed.replace("，", ",").split(",") if t.strip()]
            self.assertEqual(saved, expect, "raw=%r" % (raw,))
        # 旧代码链条：",".join("G101") → "G,1,0,1" → ["G","1","0","1"]（污染）
        self.assertEqual([t for t in "G,1,0,1".split(",") if t],
                         ["G", "1", "0", "1"])

    def test_edit_dialog_load_wiring_uses_normalized_trains(self):
        import inspect
        src = inspect.getsource(gui.TaskEditDialog.__init__)
        # 旧代码：",".join(task.get("trains") or []) —— 字符串被逐字符拆
        self.assertNotIn('",".join(task.get("trains") or [])', src)
        self.assertIn('_display_trains(task.get("trains"))', src)

    # ---- (b) 任务行展示 ----

    def test_task_row_display_wiring_uses_normalized_trains(self):
        import inspect
        src1 = inspect.getsource(gui.StartMonitorDialog.__init__)
        self.assertNotIn('"/".join(t.get("trains") or [])', src1)
        self.assertIn('_display_trains(t.get("trains"))', src1)
        src2 = inspect.getsource(gui.TaskPage.refresh)
        self.assertNotIn('"/".join(t.get("trains") or [])', src2)
        self.assertNotIn('"/".join(t.get("seat_types") or [])', src2)
        self.assertIn('_display_trains(t.get("trains"))', src2)
        self.assertIn('_display_trains(t.get("seat_types"))', src2)

    # ---- (c) PassengerDialog 乐观并发 ----

    def _make_passenger_dialog(self):
        dlg = object.__new__(gui.PassengerDialog)
        dlg.passengers = [{"name": "A"}]
        dlg._pax_stamp = "old-stamp"
        return dlg

    def test_passenger_dialog_save_abandons_on_external_write(self):
        dlg = self._make_passenger_dialog()
        with mock.patch.object(gui.passengers_mod, "save_passengers",
                               return_value=False) as m_save, \
             mock.patch.object(gui.passengers_mod, "passengers_stamp",
                               return_value="new-stamp"), \
             mock.patch.object(gui, "messagebox") as mb, \
             mock.patch.object(gui.PassengerDialog, "refresh") as m_refresh:
            ok, reason = dlg._save_passengers_or_refresh(None)
        # 指纹必须透传给共享函数（锁内检查用）
        m_save.assert_called_once_with(dlg.passengers, expect_stamp="old-stamp")
        self.assertFalse(ok)
        self.assertEqual(reason, "stale")
        mb.showwarning.assert_called_once()  # 明确警告，不静默
        mb.showerror.assert_not_called()
        m_refresh.assert_called_once()  # 重载最新数据，用户重做

    def test_passenger_dialog_save_refusal_shows_error_not_warning(self):
        dlg = self._make_passenger_dialog()
        with mock.patch.object(gui.passengers_mod, "save_passengers",
                               return_value=False), \
             mock.patch.object(gui.passengers_mod, "passengers_stamp",
                               return_value="old-stamp"), \
             mock.patch.object(gui, "messagebox") as mb, \
             mock.patch.object(gui.PassengerDialog, "refresh") as m_refresh:
            ok, reason = dlg._save_passengers_or_refresh(None)
        self.assertFalse(ok)
        self.assertEqual(reason, "refused")
        mb.showerror.assert_called_once()  # 不可解密盒子走 error（Task 80 口径）
        mb.showwarning.assert_not_called()
        m_refresh.assert_not_called()  # 拒写不重载，不丢内存数据

    def test_passenger_dialog_save_success(self):
        dlg = self._make_passenger_dialog()
        with mock.patch.object(gui.passengers_mod, "save_passengers",
                               return_value=True) as m_save, \
             mock.patch.object(gui, "messagebox") as mb:
            ok, reason = dlg._save_passengers_or_refresh(None)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        m_save.assert_called_once_with(dlg.passengers, expect_stamp="old-stamp")
        mb.showwarning.assert_not_called()
        mb.showerror.assert_not_called()

    def test_passenger_dialog_refresh_takes_stamp(self):
        dlg = object.__new__(gui.PassengerDialog)
        dlg.listbox = mock.Mock()
        with mock.patch.object(gui.passengers_mod, "load_passengers",
                               return_value=[{"name": "A"}]), \
             mock.patch.object(gui.passengers_mod, "passengers_stamp",
                               return_value="s1"):
            dlg.refresh()
        self.assertEqual(dlg.passengers, [{"name": "A"}])
        self.assertEqual(dlg._pax_stamp, "s1")


class TestTask90LauncherPassengerDialogStamp(TempDirCase):
    """Task 90：launcher.PassengerDialog 指纹缺口（Task 89 评审确认真实）。

    与 gui.PassengerDialog 的 Task 89c 同类：跨进程锁（Task 88d）已保证
    写原子性，但 load→弹窗思考→save 窗口内被外部写入会静默覆写。
    改后：保存加乐观并发检查（Task 87/88d/89c 同口径）——指纹变化→
    警告并放弃（重载最新数据，用户重做）；不可解密盒子/锁超时→error。
    """

    def _make_dialog(self):
        dlg = object.__new__(launcher.PassengerDialog)
        dlg.plist = [{"name": "A"}]
        dlg._pax_stamp = "old-stamp"
        dlg.pick = mock.Mock()
        return dlg

    def test_save_passengers_or_warn_accepts_expect_stamp(self):
        # 旧代码：_save_passengers_or_warn(passengers, parent) 无 expect_stamp
        # 参数（TypeError）；且指纹不透传 → 思考窗口内外部写入被静默覆写。
        with mock.patch.object(launcher.passengers_mod, "save_passengers",
                               return_value=True) as m_save, \
             mock.patch.object(launcher, "messagebox"):
            ok = launcher._save_passengers_or_warn([{"name": "A"}], None,
                                                   expect_stamp="s1")
        self.assertTrue(ok)
        m_save.assert_called_once_with([{"name": "A"}], expect_stamp="s1")

    def test_save_abandons_on_external_write(self):
        dlg = self._make_dialog()
        with mock.patch.object(launcher.passengers_mod, "save_passengers",
                               return_value=False) as m_save, \
             mock.patch.object(launcher.passengers_mod, "passengers_stamp",
                               return_value="new-stamp"), \
             mock.patch.object(launcher, "messagebox") as mb, \
             mock.patch.object(launcher.PassengerDialog,
                               "_refresh_plist") as m_refresh:
            ok, reason = dlg._save_passengers_or_refresh(None)
        # 指纹必须透传给共享函数（锁内检查用）
        m_save.assert_called_once_with(dlg.plist, expect_stamp="old-stamp")
        self.assertFalse(ok)
        self.assertEqual(reason, "stale")
        mb.showwarning.assert_called_once()  # 明确警告，不静默
        mb.showerror.assert_not_called()
        m_refresh.assert_called_once()  # 重载最新数据，用户重做

    def test_save_refusal_shows_error_not_warning(self):
        dlg = self._make_dialog()
        with mock.patch.object(launcher.passengers_mod, "save_passengers",
                               return_value=False), \
             mock.patch.object(launcher.passengers_mod, "passengers_stamp",
                               return_value="old-stamp"), \
             mock.patch.object(launcher, "messagebox") as mb, \
             mock.patch.object(launcher.PassengerDialog,
                               "_refresh_plist") as m_refresh:
            ok, reason = dlg._save_passengers_or_refresh(None)
        self.assertFalse(ok)
        self.assertEqual(reason, "refused")
        mb.showerror.assert_called_once()  # 不可解密盒子走 error（Task 80 口径）
        mb.showwarning.assert_not_called()
        m_refresh.assert_not_called()  # 拒写不重载，不丢内存数据

    def test_save_success(self):
        dlg = self._make_dialog()
        with mock.patch.object(launcher.passengers_mod, "save_passengers",
                               return_value=True) as m_save, \
             mock.patch.object(launcher, "messagebox") as mb:
            ok, reason = dlg._save_passengers_or_refresh(None)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        m_save.assert_called_once_with(dlg.plist, expect_stamp="old-stamp")
        mb.showwarning.assert_not_called()
        mb.showerror.assert_not_called()

    def test_init_takes_stamp_wiring(self):
        # 对话框打开时记下 load 的字节指纹（旧代码无此行）
        import inspect
        src = inspect.getsource(launcher.PassengerDialog.__init__)
        self.assertIn("self._pax_stamp = passengers_mod.passengers_stamp()", src)

    def test_delete_goes_through_stamp_check(self):
        # _delete 走指纹：外部写入→放弃删除，不静默覆写、不通知已保存
        dlg = self._make_dialog()
        dlg.pick.get.return_value = "A"
        with mock.patch.object(launcher, "messagebox") as mb, \
             mock.patch.object(launcher, "_save_passengers_or_warn",
                               return_value=True), \
             mock.patch.object(launcher.PassengerDialog,
                               "_save_passengers_or_refresh",
                               create=True,
                               return_value=(False, "stale")) as m_s, \
             mock.patch.object(launcher.PassengerDialog, "_refresh_plist",
                               create=True) as m_refresh, \
             mock.patch.object(dlg, "_notify_saved") as m_notify:
            mb.askyesno.return_value = True
            dlg._delete()
        m_s.assert_called_once_with(dlg)
        # _refresh_plist 的调用发生在真实的 _save_passengers_or_refresh 内部，
        # 已由 test_save_abandons_on_external_write 覆盖
        m_notify.assert_not_called()  # 未保存成功，不通知

    def test_save_wiring_abandons_on_stale(self):
        # _save 走指纹：外部写入→放弃保存，不显示"已保存"、不通知
        dlg = self._make_dialog()
        dlg.name_var = mock.Mock()
        dlg.name_var.get.return_value = "B"
        dlg.id_var = mock.Mock()
        dlg.id_var.get.return_value = "123"
        dlg.mob_var = mock.Mock()
        dlg.mob_var.get.return_value = ""
        dlg.type_var = mock.Mock()
        dlg.type_var.get.return_value = "二代身份证"
        dlg.adult_var = mock.Mock()
        dlg.adult_var.get.return_value = True
        dlg.default_var = mock.Mock()
        dlg.default_var.get.return_value = False
        with mock.patch.object(launcher, "messagebox") as mb, \
             mock.patch.object(launcher, "_save_passengers_or_warn",
                               return_value=True), \
             mock.patch.object(launcher.PassengerDialog,
                               "_save_passengers_or_refresh",
                               create=True,
                               return_value=(False, "stale")) as m_s, \
             mock.patch.object(launcher.PassengerDialog, "_refresh_plist",
                               create=True) as m_refresh, \
             mock.patch.object(dlg, "_notify_saved") as m_notify:
            dlg._save()
        m_s.assert_called_once_with(dlg)
        # _refresh_plist 的调用发生在真实的 _save_passengers_or_refresh 内部，
        # 已由 test_save_abandons_on_external_write 覆盖
        m_notify.assert_not_called()
        mb.showinfo.assert_not_called()  # 不显示"已保存"


class TestTask91TopLevelConfigShape(TempDirCase):
    """Task 91：config.json 合法但非对象（手改误删大括号成 []）时，
    __init__ 与 _sync_config 不得 AttributeError 崩进程/杀线程。
    """

    def _write_raw(self, text):
        p = os.path.join(self.tmp, "config.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def _write_config(self, obj):
        obj = dict(obj)
        obj["state_file"] = os.path.join(self.tmp, "state.json")
        obj["history_file"] = os.path.join(self.tmp, "order_history.json")
        return self._write_raw(json.dumps(obj, ensure_ascii=False))

    def _bump_mtime(self, p, delta=2.0):
        st = os.stat(p)
        os.utime(p, (st.st_atime, st.st_mtime + delta))

    def _make_engine(self, cfg_path):
        with mock.patch.object(ticket, "load_station_map",
                               return_value=({}, {})):
            return engine_mod.MonitorEngine(config_path=cfg_path,
                                            setup_logging=False)

    def test_init_with_list_config_does_not_crash(self):
        # RED on old code: self.config.get("state_file", ...) 抛 AttributeError
        p = self._write_raw("[]")
        with self.assertLogs("monitor", level="ERROR") as logs:
            e = self._make_engine(p)
        self.assertEqual(e.config, {})
        self.assertEqual(e.tasks, [])
        self.assertEqual(e.base_interval, 45)
        self.assertTrue(any("顶层不是对象" in m for m in logs.output),
                        "error 日志必须明确指出配置顶层形状错误（与缺文件可区分）")

    def test_init_with_null_config_does_not_crash(self):
        # "null" 也是合法 JSON 非对象
        p = self._write_raw("null")
        e = self._make_engine(p)
        self.assertEqual(e.config, {})

    def test_sync_config_with_list_config_keeps_old(self):
        # RED on old code: _sync_config 内 self.config.get 抛 AttributeError
        p = self._write_config({"tasks": [], "poll_interval_seconds": 45})
        e = self._make_engine(p)
        old_config = e.config
        self._write_raw("[]")
        self._bump_mtime(p)
        with self.assertLogs("monitor", level="ERROR") as logs:
            changed = e._sync_config()
        self.assertFalse(changed)
        self.assertIs(e.config, old_config)  # 保留旧配置
        self.assertTrue(any("顶层不是对象" in m for m in logs.output))
        # mtime 已消费：再次调用不再重复报错
        with self.assertNoLogs("monitor", level="ERROR"):
            self.assertFalse(e._sync_config())

    def test_sync_config_recovers_after_fix(self):
        # 修好文件后 mtime 变化 → 正常同步
        p = self._write_config({"tasks": [], "poll_interval_seconds": 45})
        e = self._make_engine(p)
        self._write_raw("[]")
        self._bump_mtime(p)
        e._sync_config()
        self._write_config({"tasks": [], "poll_interval_seconds": 60})
        self._bump_mtime(p)
        changed = e._sync_config()
        self.assertTrue(changed)
        self.assertEqual(e.base_interval, 60)
        self.assertEqual(e.config["poll_interval_seconds"], 60)


class TestTask102EngineMissingConfig(TempDirCase):
    """Task 102：engine __init__ 缺 config.json 文件时，不得抛裸
    FileNotFoundError 崩进程；记 error 后用安全默认值继续（Task 91 同口径）。
    """

    def _make_engine(self, cfg_path):
        with mock.patch.object(ticket, "load_station_map",
                               return_value=({}, {})):
            # state/history 路径随 config 走；缺 config 时回退到 HERE——测试
            # 中把 HERE 指到 tmp，避免 __init__ 的 _save_state 碰真实文件。
            with mock.patch.object(engine_mod, "HERE", self.tmp):
                return engine_mod.MonitorEngine(config_path=cfg_path,
                                                setup_logging=False)

    def _missing_path(self):
        return os.path.join(self.tmp, "no-such-config.json")

    def test_init_missing_config_does_not_crash(self):
        # RED on old code: open() 抛 FileNotFoundError 崩进程
        p = self._missing_path()
        with self.assertLogs("monitor", level="ERROR") as logs:
            e = self._make_engine(p)
        self.assertEqual(e.config, {})
        self.assertEqual(e.tasks, [])
        self.assertEqual(e.base_interval, 45)
        self.assertTrue(any("不存在" in m for m in logs.output),
                        "error 日志必须明确指出配置文件缺失（与 Task 91 的"
                        "“顶层不是对象”可区分），并带出完整路径")

    def test_init_missing_config_recovers_when_file_appears(self):
        # 缺文件启动后，用户补上 config.json → _sync_config 自动恢复
        p = self._missing_path()
        e = self._make_engine(p)
        self.assertEqual(e.tasks, [])
        cfg = {"tasks": [{"name": "T1"}],
               "poll_interval_seconds": 45,
               "state_file": os.path.join(self.tmp, "state.json"),
               "history_file": os.path.join(self.tmp, "order_history.json")}
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        changed = e._sync_config()
        self.assertTrue(changed)
        self.assertEqual([t["name"] for t in e.tasks], ["T1"])


class TestTask105EngineInvalidJsonConfig(TempDirCase):
    """Task 105 (P1)：engine __init__ 读到"文件存在但内容非法 JSON"
    时，不得抛裸 JSONDecodeError 崩进程；记 error 后用安全默认值继续
    （Task 91/102 同口径；_sync_config 已有 Task 72 降级，冷启动漏了）。
    """

    def _make_engine(self, cfg_path):
        with mock.patch.object(ticket, "load_station_map",
                               return_value=({}, {})):
            # state/history 路径随 config 走；HERE 指到 tmp，避免 __init__
            # 的 _save_state 碰真实文件（与 Task 102 测试同脚手架）。
            with mock.patch.object(engine_mod, "HERE", self.tmp):
                return engine_mod.MonitorEngine(config_path=cfg_path,
                                                setup_logging=False)

    def _invalid_path(self):
        p = os.path.join(self.tmp, "bad-config.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{not valid json!!!")
        return p

    def test_init_invalid_json_does_not_crash(self):
        # RED on old code: json.load 抛 JSONDecodeError 崩进程
        p = self._invalid_path()
        with self.assertLogs("monitor", level="ERROR") as logs:
            e = self._make_engine(p)
        self.assertEqual(e.config, {})
        self.assertEqual(e.tasks, [])
        self.assertEqual(e.base_interval, 45)
        self.assertTrue(any("合法 JSON" in m for m in logs.output),
                        "error 日志必须明确指出内容不是合法 JSON（与 Task 91 的"
                        "“顶层不是对象”、Task 102 的“不存在”可区分），并带出完整路径")

    def test_init_invalid_json_keeps_valid_mtime_for_recovery(self):
        # 文件存在 → _config_mtime 必须取到有效值；用户修好文件后
        # mtime 变化 → _sync_config 自动恢复（Task 91/102 同机制）
        p = self._invalid_path()
        e = self._make_engine(p)
        self.assertIsNotNone(e._config_mtime)
        cfg = {"tasks": [{"name": "T1"}],
               "poll_interval_seconds": 45,
               "state_file": os.path.join(self.tmp, "state.json"),
               "history_file": os.path.join(self.tmp, "order_history.json")}
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        # 确保 mtime 变化（文件系统时间粒度可能粗）
        st = os.stat(p)
        os.utime(p, (st.st_mtime + 5, st.st_mtime + 5))
        changed = e._sync_config()
        self.assertTrue(changed)
        self.assertEqual([t["name"] for t in e.tasks], ["T1"])

    def test_init_missing_config_still_works(self):
        # 回归 pin：Task 102 的缺文件行为不变（"不存在"文案 + 默认值）
        p = os.path.join(self.tmp, "no-such-config.json")
        with self.assertLogs("monitor", level="ERROR") as logs:
            e = self._make_engine(p)
        self.assertEqual(e.config, {})
        self.assertTrue(any("不存在" in m for m in logs.output))


class TestTask107EngineNonUtf8Config(TempDirCase):
    """Task 107 (P1)：engine __init__ 读到"文件存在但不是 UTF-8 编码"
    的 config 时，不得抛裸 UnicodeDecodeError 崩进程；记 error 后用
    安全默认值继续（Task 91/102/105 同口径；与 Task 105 同一 try 块
    的相邻形状）。"""

    def _make_engine(self, cfg_path):
        with mock.patch.object(ticket, "load_station_map",
                               return_value=({}, {})):
            # state/history 路径随 config 走；HERE 指到 tmp，避免 __init__
            # 的 _save_state 碰真实文件（与 Task 102/105 测试同脚手架）。
            with mock.patch.object(engine_mod, "HERE", self.tmp):
                return engine_mod.MonitorEngine(config_path=cfg_path,
                                                setup_logging=False)

    def _non_utf8_path(self):
        p = os.path.join(self.tmp, "gbk-config.json")
        # GBK 编码的中文——open(..., encoding="utf-8") 读到非 ASCII
        # 字节即抛 UnicodeDecodeError
        with open(p, "wb") as f:
            f.write('{"tasks": []'.encode("utf-8") + "，注释".encode("gbk"))
        return p

    def test_init_non_utf8_config_does_not_crash(self):
        # RED on old code: open(encoding="utf-8") 抛 UnicodeDecodeError 崩进程
        p = self._non_utf8_path()
        with self.assertLogs("monitor", level="ERROR") as logs:
            e = self._make_engine(p)
        self.assertEqual(e.config, {})
        self.assertEqual(e.tasks, [])
        self.assertEqual(e.base_interval, 45)
        self.assertTrue(any("UTF-8" in m for m in logs.output),
                        "error 日志必须明确指出不是 UTF-8 编码（与 Task 91 的"
                        "“顶层不是对象”、Task 102 的“不存在”、Task 105 的"
                        "“合法 JSON”可区分），并带出完整路径")

    def test_init_non_utf8_config_keeps_valid_mtime_for_recovery(self):
        # 文件存在 → _config_mtime 必须取到有效值；用户转码修复后
        # mtime 变化 → _sync_config 自动恢复（Task 91/102/105 同机制）
        p = self._non_utf8_path()
        e = self._make_engine(p)
        self.assertIsNotNone(e._config_mtime)
        cfg = {"tasks": [{"name": "T1"}],
               "poll_interval_seconds": 45,
               "state_file": os.path.join(self.tmp, "state.json"),
               "history_file": os.path.join(self.tmp, "order_history.json")}
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        # 确保 mtime 变化（文件系统时间粒度可能粗）
        st = os.stat(p)
        os.utime(p, (st.st_mtime + 5, st.st_mtime + 5))
        changed = e._sync_config()
        self.assertTrue(changed)
        self.assertEqual([t["name"] for t in e.tasks], ["T1"])

    def test_init_invalid_json_config_still_works(self):
        # 回归 pin：Task 105 的非法 JSON 行为不变（"合法 JSON"文案 + 默认值）
        p = os.path.join(self.tmp, "bad-config.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{not valid json!!!")
        with self.assertLogs("monitor", level="ERROR") as logs:
            e = self._make_engine(p)
        self.assertEqual(e.config, {})
        self.assertTrue(any("合法 JSON" in m for m in logs.output))


class TestTask92MonitorP2(TempDirCase):
    """Task 92 (P2): (a) 损坏的 config.json → load_config 友好降级，
    save/update 路径绝不覆写损坏文件；(b) menu_notify 字符串型 "to"
    按单收件人处理，不逐字符拆、不污染回写。"""

    def _mod(self):
        import monitor as monitor_mod
        return monitor_mod

    def _write_raw(self, data):
        p = os.path.join(self.tmp, "config.json")
        with open(p, "wb") as f:
            f.write(data)
        return p

    def _read_raw(self, p):
        with open(p, "rb") as f:
            return f.read()

    # ---- (a) 损坏配置 ----

    def test_load_config_illegal_json_returns_empty(self):
        m = self._mod()
        p = self._write_raw(b"{not json!!!")
        with mock.patch.object(m, "CONFIG_PATH", p):
            self.assertEqual(m.load_config(), {})  # 旧代码抛 JSONDecodeError

    def test_load_config_non_utf8_returns_empty(self):
        m = self._mod()
        p = self._write_raw(b"\xff\xfe\x00bad-bytes")
        with mock.patch.object(m, "CONFIG_PATH", p):
            self.assertEqual(m.load_config(), {})  # 旧代码抛 UnicodeDecodeError

    def test_load_config_non_dict_root_returns_empty(self):
        m = self._mod()
        p = self._write_raw(b'["not", "a", "dict"]')
        with mock.patch.object(m, "CONFIG_PATH", p):
            self.assertEqual(m.load_config(), {})  # 旧代码原样返回 list

    def test_damaged_config_save_refuses_and_preserves(self):
        m = self._mod()
        p = self._write_raw(b"{damaged")
        with mock.patch.object(m, "CONFIG_PATH", p):
            cfg = m.load_config()
            cfg["injected"] = True
            self.assertFalse(m.save_config(cfg))  # 旧代码返回 True 并覆写
            self.assertEqual(self._read_raw(p), b"{damaged")

    def test_damaged_config_update_locked_refuses_and_preserves(self):
        m = self._mod()
        p = self._write_raw(b"{damaged")
        with mock.patch.object(m, "CONFIG_PATH", p):
            ok = m.update_config_locked(lambda c: c.update(injected=True))
            self.assertIs(ok, False)  # 旧代码返回 None 并覆写
            self.assertEqual(self._read_raw(p), b"{damaged")

    def test_first_run_save_still_works(self):
        # 首跑（无文件）流程不受损坏降级影响：可创建并落盘（回归 pin）。
        m = self._mod()
        p = os.path.join(self.tmp, "config.json")
        with mock.patch.object(m, "CONFIG_PATH", p):
            self.assertEqual(m.load_config(), {})
            self.assertTrue(m.save_config({"tasks": []}))
            with open(p, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"tasks": []})

    # ---- (b) menu_notify 字符串 "to" ----

    def _run_menu_notify_enter_through(self, m, p, email_cfg):
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"notify": {"email": email_cfg}}, f)
        calls = []

        def fake_read(prompt, default=""):
            calls.append((prompt, default))
            return default

        with mock.patch.object(m, "CONFIG_PATH", p), \
             mock.patch.object(m, "read", side_effect=fake_read), \
             mock.patch("getpass.getpass", return_value=""), \
             mock.patch.object(m, "ask_yes_no", return_value=False):
            m.menu_notify()
        with open(p, encoding="utf-8") as f:
            saved = json.load(f)
        return saved["notify"]["email"], calls

    def test_menu_notify_string_to_single_recipient(self):
        m = self._mod()
        p = os.path.join(self.tmp, "config.json")
        email, calls = self._run_menu_notify_enter_through(
            m, p, {"to": "a@b.com"})
        to_prompts = [d for pr, d in calls if "收件人" in pr]
        self.assertEqual(to_prompts, ["a@b.com"])  # 旧代码此处为 "a,@,b,.,c,o,m"
        self.assertEqual(email["to"], ["a@b.com"])  # 旧代码回写 ['a','@','b','.','c','o','m']

    def test_menu_notify_list_to_roundtrip(self):
        m = self._mod()
        p = os.path.join(self.tmp, "config.json")
        email, _ = self._run_menu_notify_enter_through(
            m, p, {"to": ["a@b.com", "c@d.com"]})
        self.assertEqual(email["to"], ["a@b.com", "c@d.com"])  # 回归 pin

    def test_menu_notify_missing_to(self):
        m = self._mod()
        p = os.path.join(self.tmp, "config.json")
        email, _ = self._run_menu_notify_enter_through(m, p, {})
        self.assertEqual(email["to"], [])  # 回归 pin


class TestTask93EngineP2(TempDirCase):
    """Task 93: (a) state.json tasks 节非 dict → 启动崩 / 运行中 _sync_state
    后线程死亡；(b) seat_types 裸字符串被逐字符拆 → 永久静默漏单。"""

    def _write_config(self, state_obj, tasks=()):
        sp = os.path.join(self.tmp, "state.json")
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(state_obj, f, ensure_ascii=False)
        cfg = {"tasks": list(tasks),
               "state_file": sp,
               "history_file": os.path.join(self.tmp, "order_history.json")}
        p = os.path.join(self.tmp, "config.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        return p, sp

    def _make_engine(self, state_obj, tasks=()):
        cfg_path, sp = self._write_config(state_obj, tasks)
        with mock.patch.object(ticket, "load_station_map",
                               return_value=({}, {})):
            eng = engine_mod.MonitorEngine(config_path=cfg_path,
                                           setup_logging=False)
        return eng, sp

    # ---- (a) tasks 节非 dict ----

    def test_load_state_tasks_list_degrades_not_crash(self):
        # 旧代码：_resume_or_init_status 内 self.state["tasks"].setdefault
        # 直接 AttributeError，启动即崩（config 里有一个任务即触发）
        with self.assertLogs("monitor", level="ERROR"):
            eng, sp = self._make_engine({"tasks": [], "dedup": {"k": "v"},
                                         "retry": {}},
                                        tasks=[{"name": "T1"}])
        # 降级为空任务集后，_resume_or_init_status 正常补上 T1 的默认条目
        self.assertIsInstance(eng.state["tasks"], dict)
        self.assertEqual(eng.state["tasks"]["T1"]["status"], "paused")
        self.assertEqual(eng.state["dedup"], {"k": "v"})  # 其它节不受影响

    def test_reload_state_tasks_list_does_not_kill_thread(self):
        # 运行中外部把 tasks 节写坏：旧代码 _sync_state 重载后
        # self.state["tasks"] 为 []，下一次 task_status 即 AttributeError
        # （生产环境=轮询线程死亡）
        eng, sp = self._make_engine({"tasks": {}, "dedup": {}, "retry": {}})
        with open(sp, "w", encoding="utf-8") as f:
            json.dump({"tasks": [], "dedup": {}, "retry": {}}, f)
        eng._state_mtime = 0  # 强制 _sync_state 重载
        with self.assertLogs("monitor", level="ERROR"):
            eng._sync_state()
        self.assertIsInstance(eng.state["tasks"], dict)
        self.assertEqual(eng.task_status({"name": "x"}), "paused")

    # ---- (b) seat_types 裸字符串 ----

    def test_seat_types_string_is_single_not_char_split(self):
        seats = engine_mod.normalize_seat_types({"name": "T1",
                                                 "seat_types": "硬座"})
        self.assertEqual(seats, ["硬座"])  # 旧代码此处为 ["硬", "座"]

    def test_seat_types_string_warns_loudly(self):
        with self.assertLogs("monitor", level="WARNING") as cm:
            engine_mod.normalize_seat_types({"name": "T1",
                                             "seat_types": "硬座"})
        self.assertTrue(any("seat_types" in m for m in cm.output),
                        "warning 必须点名 seat_types 字段")

    def test_seat_types_string_survives_availability_intersection(self):
        # P2 本体：旧代码逐字符拆后与余票求交恒为空 → 任务永久静默漏单
        seats = engine_mod.normalize_seat_types({"name": "T1",
                                                 "seat_types": "硬座"})
        self.assertTrue(set(seats) & {"硬座", "二等座"})

    def test_seat_types_shapes(self):
        n = engine_mod.normalize_seat_types
        self.assertEqual(n({"name": "T", "seat_types": ["硬座", "二等座"]}),
                         ["硬座", "二等座"])  # 正常形状零变化
        self.assertEqual(n({"name": "T"}), [])
        self.assertEqual(n({"name": "T", "seat_types": ""}), [])
        self.assertEqual(n({"name": "T", "seat_types": ["硬座", 123]}),
                         ["硬座"])
        with self.assertLogs("monitor", level="WARNING"):
            self.assertEqual(n({"name": "T", "seat_types": 123}), [])


class TestTask94GuiStartupRobustness(TempDirCase):
    """Task 94: gui P2 — 非 UTF-8/非 dict config.json 与非 list 订单历史不再崩 GUI 启动。"""

    def _write_bytes(self, name, data):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def _write_text(self, name, text):
        return self._write_bytes(name, text.encode("utf-8"))

    def _reset_corrupt_flag(self):
        gui._CONFIG_CORRUPT_WARNED = False
        self.addCleanup(setattr, gui, "_CONFIG_CORRUPT_WARNED", False)

    # ---- (a) load_config：非 UTF-8 编码 ----

    def test_load_config_gbk_bytes_returns_empty(self):
        # GBK 编码的中文在 UTF-8 下非法：旧代码只抓 JSONDecodeError，
        # UnicodeDecodeError 逃出崩启动。
        cfg = self._write_bytes("config.json",
                                '{"station": "北京西"}'.encode("gbk"))
        self._reset_corrupt_flag()
        with mock.patch.object(gui, "CONFIG_PATH", cfg):
            with self.assertLogs(gui.LOG, level="ERROR") as cm:
                self.assertEqual(gui.load_config(), {})
        self.assertTrue(any("UTF-8" in r.getMessage() for r in cm.records),
                        "必须明确指出编码问题")

    def test_load_config_latin1_bytes_returns_empty(self):
        # 任意非 UTF-8 编码都不只限 GBK：latin-1 的 0xE9 单字节非法。
        cfg = self._write_bytes("config.json", '{"a": "\xe9"}'.encode("latin-1"))
        self._reset_corrupt_flag()
        with mock.patch.object(gui, "CONFIG_PATH", cfg):
            self.assertEqual(gui.load_config(), {})

    # ---- (a) load_config：合法但非 dict ----

    def test_load_config_non_dict_list_returns_empty(self):
        # 合法 JSON 但顶层是 list：旧代码 json.load 成功返回 []，
        # 下游 load_config().get(...) 抛 AttributeError 崩启动。
        cfg = self._write_text("config.json", "[]")
        self._reset_corrupt_flag()
        with mock.patch.object(gui, "CONFIG_PATH", cfg):
            with self.assertLogs(gui.LOG, level="ERROR") as cm:
                self.assertEqual(gui.load_config(), {})
        self.assertTrue(any("不是" in r.getMessage() and "dict" in r.getMessage()
                            or "对象" in r.getMessage() for r in cm.records),
                        "必须明确指出顶层不是对象")

    def test_load_config_valid_dict_unchanged(self):
        cfg = self._write_text("config.json", '{"a": 1}')
        self._reset_corrupt_flag()
        with mock.patch.object(gui, "CONFIG_PATH", cfg):
            self.assertEqual(gui.load_config(), {"a": 1})

    def test_config_json_corrupt_detects_gbk_and_non_dict(self):
        self._reset_corrupt_flag()
        bad_gbk = self._write_bytes("c1.json", '{"x": "中文"}'.encode("gbk"))
        bad_list = self._write_text("c2.json", "[]")
        bad_json = self._write_text("c3.json", "{bad json,")
        good = self._write_text("c4.json", '{"a": 1}')
        missing = os.path.join(self.tmp, "nope.json")
        with mock.patch.object(gui, "CONFIG_PATH", bad_gbk):
            self.assertTrue(gui._config_json_corrupt())
        with mock.patch.object(gui, "CONFIG_PATH", bad_list):
            self.assertTrue(gui._config_json_corrupt())
        with mock.patch.object(gui, "CONFIG_PATH", bad_json):
            self.assertTrue(gui._config_json_corrupt())
        with mock.patch.object(gui, "CONFIG_PATH", good):
            self.assertFalse(gui._config_json_corrupt())
        with mock.patch.object(gui, "CONFIG_PATH", missing):
            self.assertFalse(gui._config_json_corrupt())

    # ---- (b) read_history_records：切片在 try 块外 ----

    def _history_cfg(self, history_content):
        cfg = self._write_text("config.json", '{"history_file": "order_history.json"}')
        self._write_text("order_history.json", history_content)
        return cfg

    def test_read_history_records_non_list_dict_returns_empty(self):
        # 顶层是 dict：history[-limit:] 在 try 外抛 TypeError 崩启动
        # （HistoryPanel eager 构造）。
        cfg = self._history_cfg('{"a": 1}')
        self._reset_corrupt_flag()
        with mock.patch.object(gui, "CONFIG_PATH", cfg), \
             mock.patch.object(gui, "HERE", self.tmp):
            with self.assertLogs(gui.LOG, level="WARNING") as cm:
                self.assertEqual(gui.read_history_records(), [])
        self.assertTrue(any("列表" in r.getMessage() for r in cm.records),
                        "必须明确指出历史不是列表")

    def test_read_history_records_non_list_number_returns_empty(self):
        cfg = self._history_cfg('42')
        self._reset_corrupt_flag()
        with mock.patch.object(gui, "CONFIG_PATH", cfg), \
             mock.patch.object(gui, "HERE", self.tmp):
            self.assertEqual(gui.read_history_records(), [])

    def test_read_history_records_valid_list_unchanged(self):
        cfg = self._history_cfg('[{"t": 1}, {"t": 2}, {"t": 3}]')
        self._reset_corrupt_flag()
        with mock.patch.object(gui, "CONFIG_PATH", cfg), \
             mock.patch.object(gui, "HERE", self.tmp):
            recs = gui.read_history_records(limit=2)
        self.assertEqual([r["t"] for r in recs], [3, 2])

    def test_read_history_records_missing_file_returns_empty(self):
        cfg = self._write_text("config.json", '{"history_file": "order_history.json"}')
        self._reset_corrupt_flag()
        with mock.patch.object(gui, "CONFIG_PATH", cfg), \
             mock.patch.object(gui, "HERE", self.tmp):
            self.assertEqual(gui.read_history_records(), [])


class TestTask95WarmReuseSessionRevalidation(TempDirCase):
    """Task 95: browser_order P2（warm 复用永不复验会话 → 误导性超时 + 任务自停）。

    改前：warm 复用路径（browser_order.py:758-760）跳过 session_ok；
    预热等待期（默认 10 分钟）内会话被踢/过期后，每轮 warm.refresh() 在死
    会话上烧 ~20-30 秒抛误导性 TimeoutError，连续几次后任务被误判自停（漏单）。
    改后：order_via_browser 在复用前复验会话有效性；失效则关闭预热现场、
    改走冷启动路径（内含诚实的会话校验与"请先运行 login"指引）。
    """

    class _LiveWarm:
        """usable()=True 的假预热现场：记录 close()，暴露 ctx/page 供复验。"""
        def __init__(self):
            self.ctx = object()
            self.page = object()
            self.closes = 0

        def usable(self):
            return True

        def close(self):
            self.closes += 1

        def refresh(self, info):
            pass

    class _StaleWarm:
        def __init__(self):
            self.closes = 0

        def usable(self):
            return False

        def close(self):
            self.closes += 1

    def _run(self, warm, session_ok_result=None, session_ok_exc=None):
        """经 order_via_browser 跑一轮；_order_impl 与 session_ok 均 mock。"""
        info = {"train_code": "G101", "from_name": "北京", "to_name": "上海",
                "from_code": "VNP", "to_code": "SHH"}
        calls = {}

        def fake_impl(*a, **k):
            calls["warm"] = k.get("warm")
            return (False, "mock-impl", None)

        def fake_session_ok(ctx, page=None):
            calls["session_ok"] = (ctx, page)
            if session_ok_exc is not None:
                raise session_ok_exc
            return session_ok_result

        with mock.patch.object(browser_order, "_order_impl", side_effect=fake_impl), \
             mock.patch.object(browser_order, "session_ok", side_effect=fake_session_ok):
            ok, msg, extra = browser_order.order_via_browser(
                info, "二等座", "O", ["张三"], "2026-10-10", warm=warm)
        return ok, msg, extra, calls

    def test_dead_session_during_warm_wait_closes_warm_and_cold_starts(self):
        # P2 本体：预热等待期内会话被踢/过期 → 不得把死 warm 传给 _order_impl
        #（改前行为：warm 原样复用，refresh() 烧 ~30 秒抛误导性 TimeoutError，
        #  连续几次后任务被误判自停）。
        warm = self._LiveWarm()
        ok, msg, extra, calls = self._run(
            warm, session_ok_result=(False, "接口返回 status=false（会话已失效）"))
        self.assertIn("session_ok", calls, "warm 复用前必须复验会话有效性")
        self.assertIs(calls["session_ok"][0], warm.ctx)
        self.assertIs(calls["session_ok"][1], warm.page)
        self.assertEqual(warm.closes, 1, "失效会话的预热现场必须被关闭")
        self.assertIn("warm", calls, "_order_impl 必须被调用（冷启动路径）")
        self.assertIsNone(calls["warm"], "死会话不得复用：_order_impl 必须走冷启动（warm=None）")
        self.assertFalse(ok)

    def test_healthy_session_keeps_warm_reuse(self):
        # 会话健康时复用路径不变：复验通过 → 不关闭、不降级冷启动
        warm = self._LiveWarm()
        ok, msg, extra, calls = self._run(warm, session_ok_result=(True, "张三"))
        self.assertIn("session_ok", calls, "warm 复用前必须复验会话有效性")
        self.assertEqual(warm.closes, 0, "健康会话不应关闭预热现场")
        self.assertIs(calls["warm"], warm, "健康会话必须复用 warm（不得冷启动）")

    def test_session_check_exception_closes_warm_and_cold_starts(self):
        # 校验本身抛异常（如页面结构异常）：按"会话不可信"处理——关闭预热现场
        # 改走冷启动（冷启动会用新浏览器重新诚实校验），不崩、不复用可疑 warm
        warm = self._LiveWarm()
        ok, msg, extra, calls = self._run(warm, session_ok_exc=RuntimeError("boom"))
        self.assertIn("session_ok", calls)
        self.assertEqual(warm.closes, 1, "校验异常的 warm 必须被关闭")
        self.assertIsNone(calls["warm"], "校验异常时不得复用可疑 warm")
        self.assertFalse(ok)

    def test_stale_warm_keeps_old_path_without_session_check(self):
        # Task 70a 路径不变：已失效（页面被关/跨线程）的 warm 直接关闭，
        # 不应再浪费一次会话校验
        warm = self._StaleWarm()
        ok, msg, extra, calls = self._run(warm, session_ok_result=(True, "张三"))
        self.assertNotIn("session_ok", calls, "stale warm 不应触发会话复验")
        self.assertEqual(warm.closes, 1)
        self.assertIsNone(calls["warm"])

    def test_no_warm_skips_revalidation(self):
        # 冷启动路径不受影响：无 warm 时不做复验
        ok, msg, extra, calls = self._run(None, session_ok_result=(True, "张三"))
        self.assertNotIn("session_ok", calls, "无 warm 时不应调用会话复验")
        self.assertIsNone(calls["warm"])


class TestTask96LauncherP3(TempDirCase):
    """Task 96：launcher P3 bundle（a 回归 / b Spinbox / c 脏条目 / d 脏配置 / e 关窗同步）。

    无 Tk 真机，全部 mock/桩测试（沿 Task 74/84/90 口径）。
    """

    # ---- (a) Task 90 回归：成功保存后未刷新 _pax_stamp ----

    def _make_pax_dialog(self):
        dlg = object.__new__(launcher.PassengerDialog)
        dlg.plist = [{"name": "A"}]
        dlg._pax_stamp = "old-stamp"
        dlg.pick = mock.Mock()
        return dlg

    def test_second_save_no_false_alarm(self):
        # 回归（Task 90/commit 516f92e）：成功保存后未刷新 _pax_stamp，
        # 同一会话第二次保存必误报"被外部修改"并放弃。
        disk = {"stamp": "old-stamp"}

        def fake_save(passengers, expect_stamp):
            if expect_stamp is not launcher.passengers_mod._STAMP_UNSET \
                    and expect_stamp != disk["stamp"]:
                return False  # 指纹不匹配 → 锁内放弃（真实 save_passengers 语义）
            disk["stamp"] = "stamp-after-write"
            return True

        dlg = self._make_pax_dialog()
        with mock.patch.object(launcher.passengers_mod, "save_passengers",
                               side_effect=fake_save), \
             mock.patch.object(launcher.passengers_mod, "passengers_stamp",
                               side_effect=lambda: disk["stamp"]), \
             mock.patch.object(launcher, "messagebox"), \
             mock.patch.object(launcher.PassengerDialog, "_refresh_plist"):
            ok1, r1 = dlg._save_passengers_or_refresh(None)
            ok2, r2 = dlg._save_passengers_or_refresh(None)
        self.assertTrue(ok1)
        self.assertEqual(r1, "ok")
        self.assertTrue(ok2, "第二次保存不应误报外部修改（回归）")
        self.assertEqual(r2, "ok")

    # ---- (b) 新建监控任务"优先级" Spinbox 手输非数字 ----

    def _make_new_task_dialog(self):
        import tkinter as tk
        dlg = object.__new__(launcher.NewMonitorTaskDialog)

        def _v(value):
            v = mock.Mock()
            v.get.return_value = value
            return v

        dlg.from_cb = _v("北京")
        dlg.to_cb = _v("上海")
        dlg.name2code = {"北京": "BJP", "上海": "SHH"}
        dlg.date_var = _v("2026-10-10")
        sv = _v(True)
        dlg.seat_vars = {"二等座": sv}
        dlg.auto_var = _v(False)
        dlg.trains_var = _v("G101")
        dlg.pax_vars = {}
        dlg.seat_pri_var = _v("")
        dlg.purpose_var = _v("成人票")
        dlg.stop_var = _v(True)
        dlg.prio_var = mock.Mock()
        dlg.prio_var.get.side_effect = tk.TclError(
            'expected integer but got "abc"')
        dlg.app = mock.Mock()
        dlg.app.lc = {}
        dlg.app._put_log = mock.Mock()
        dlg.destroy = mock.Mock()
        dlg._parse_dates = mock.Mock(return_value=(["2026-10-10"], []))
        return dlg

    def test_prio_spinbox_non_numeric_uses_default(self):
        # Task 96b：Spinbox 手输非数字 → tk.IntVar.get() 抛 TclError（Task 74e 同类）。
        # 改后：友好提示 + 安全默认值 5，不抛。
        dlg = self._make_new_task_dialog()
        with mock.patch.object(launcher, "messagebox") as mb, \
             mock.patch.object(launcher, "append_monitor_task",
                               return_value="任务名") as m_append:
            dlg._create(False)
        mb.showwarning.assert_called_once()
        m_append.assert_called_once()
        task = m_append.call_args[0][0]
        self.assertEqual(task["priority"], 5)

    def test_prio_spinbox_numeric_still_works(self):
        # 数字输入不受影响：不弹警告，原值透传。
        dlg = self._make_new_task_dialog()
        dlg.prio_var.get.side_effect = None
        dlg.prio_var.get.return_value = 8
        with mock.patch.object(launcher, "messagebox") as mb, \
             mock.patch.object(launcher, "append_monitor_task",
                               return_value="任务名") as m_append:
            dlg._create(False)
        mb.showwarning.assert_not_called()
        task = m_append.call_args[0][0]
        self.assertEqual(task["priority"], 8)

    # ---- (c) grab_tasks.json 非 dict 条目 ----

    def test_add_row_skips_non_dict(self):
        # Task 96c：grab_tasks.json 含非 dict 条目 → _add_row 的 task.get 崩启动。
        # 改后：记 warning 后跳过展示，不崩。
        panel = object.__new__(launcher.TaskManagerPanel)
        panel._row_widgets = {}
        panel.list_box = mock.Mock()
        with mock.patch.object(launcher, "ttk") as m_ttk, \
             mock.patch.object(launcher, "LOG") as m_log:
            panel._add_row("junk-string")
            panel._add_row(123)
            panel._add_row(None)
        m_ttk.Frame.assert_not_called()
        self.assertEqual(panel._row_widgets, {})
        self.assertEqual(m_log.warning.call_count, 3)

    def test_add_row_dict_still_renders(self):
        # 正常 dict 条目不受影响：仍建行。
        panel = object.__new__(launcher.TaskManagerPanel)
        panel._row_widgets = {}
        panel.list_box = mock.Mock()
        task = {"id": "t1", "name": "京沪", "from": "北京", "to": "上海",
                "date": "2026-10-10"}
        with mock.patch.object(launcher, "ttk") as m_ttk, \
             mock.patch.object(launcher, "LOG") as m_log:
            panel._add_row(task)
        m_ttk.Frame.assert_called_once()
        self.assertIn("t1", panel._row_widgets)
        m_log.warning.assert_not_called()

    # ---- (d) launcher_config.json 脏值 ----

    def _make_app_for_sync(self, lc_extra):
        app = object.__new__(launcher.LauncherApp)
        lc = {"from": "", "to": "", "trains": [], "seat_types": [],
              "date": "", "date_to": "", "start_time": "",
              "station_history": [], "passenger_names": [],
              "pax_purpose": {}, "remind_minutes": 10, "warm_minutes": 10,
              "presets": []}
        lc.update(lc_extra)
        app.lc = lc
        app.from_ent = mock.Mock()
        app.to_ent = mock.Mock()
        app.trains_var = mock.Mock()
        app.date_var = mock.Mock()
        app.date_to_var = mock.Mock()
        app.seat_vars = {}
        app.seat_pri_var = mock.Mock()
        app.start_var = mock.Mock()
        app.remind_var = mock.Mock()
        app.warm_var = mock.Mock()
        app.preset_cb = mock.Mock()
        app._put_log = mock.Mock()
        app._refresh_pax = mock.Mock()
        app._save_cfg = mock.Mock()
        return app

    def test_sync_from_lc_dirty_values(self):
        # Task 96d：remind_minutes 非数字 / presets 非 dict 条目 → 不崩，
        # 脏值记 warning 后用安全默认值。
        app = self._make_app_for_sync({
            "remind_minutes": "abc", "warm_minutes": "xyz",
            "presets": ["junk", {"name": "p1"}]})
        with mock.patch.object(launcher, "merge_trains_from_monitor",
                               return_value=False), \
             mock.patch("os.path.getmtime", side_effect=OSError("no")):
            app._sync_from_lc()
        app.remind_var.set.assert_called_with(10)
        app.warm_var.set.assert_called_with(10)
        app.preset_cb.configure.assert_called_once_with(values=["p1"])
        warns = [c for c in app._put_log.call_args_list
                 if "警告" in str(c)]
        self.assertGreaterEqual(len(warns), 3)  # remind/warm/presets 各一条

    def test_sync_from_lc_clean_values_unchanged(self):
        # 干净配置不受影响：原值透传，无警告。
        app = self._make_app_for_sync({
            "remind_minutes": 15, "warm_minutes": 20,
            "presets": [{"name": "p1"}, {"name": "p2"}]})
        with mock.patch.object(launcher, "merge_trains_from_monitor",
                               return_value=False), \
             mock.patch("os.path.getmtime", side_effect=OSError("no")):
            app._sync_from_lc()
        app.remind_var.set.assert_called_with(15)
        app.warm_var.set.assert_called_with(20)
        app.preset_cb.configure.assert_called_once_with(values=["p1", "p2"])
        warns = [c for c in app._put_log.call_args_list
                 if "警告" in str(c)]
        self.assertEqual(len(warns), 0)

    # ---- (e) GrabTaskWindow._on_close 同步界面编辑 ----

    def test_grab_task_window_on_close_syncs_ui(self):
        # Task 96e：_on_close 未调 _ui_to_lc → 2 秒内关窗丢界面编辑。
        # 改后：关闭时同步界面编辑到配置（与独立模式一致）。
        win = object.__new__(launcher.GrabTaskWindow)
        win.app = mock.Mock()
        win.app.grabber = None
        win.app.lc = {"status": "running"}
        win.task = {"id": "t1", "name": "n"}
        win.manager = mock.Mock()
        win.destroy = mock.Mock()
        launcher.GrabTaskWindow._on_close(win)
        win.app._ui_to_lc.assert_called_once_with(save=False)
        # 状态修正 + 写回任务库仍走原流程
        self.assertEqual(win.app.lc["status"], "idle")
        win.manager._upsert_task.assert_called_once()
        win.manager._on_window_closed.assert_called_once_with("t1")
        win.destroy.assert_called_once()


class TestTask97MonitorP3(TempDirCase):
    """Task 97 (P3): monitor 菜单健壮性 bundle——
    (a) menu_task_ops 坏任务条目（非 dict / 缺 name）可选可崩；
    (b) 不可哈希的 name（如 list）绕过 Task 83 的展示硬化；
    (c) "tasks" 非 list 时建任务锁内 AttributeError；
    (d) menu_passengers 内 ask_yes_no 的 Ctrl+C 回主菜单（违背 Task 83b
        回子菜单裁决）；
    (e) menu_notify 部分 prompt 把 Ctrl+C 消化为"保留旧值"；
    (f) "回车=使用默认/全部成人"文案与 default_names 首个-only 回退不符；
    (g) menu_quick_check 日期处 Ctrl+C 触发真实网络查询而非取消；
    (h) menu_notify 的 to_default 对 "to":123 仍 TypeError（Task 92 评审
        确认真实）。"""

    @staticmethod
    def _printed(mprint):
        return " ".join(str(c.args[0]) for c in mprint.call_args_list)

    @staticmethod
    def _mock_engine():
        eng = mock.MagicMock()
        # 复刻 engine.MonitorEngine.task_status 的真实行为：
        # state["tasks"].get(task["name"])——不可哈希的 name 在此 TypeError。
        def _status(t):
            return eng.state["tasks"].get(t["name"], {}).get("status",
                                                             "paused")
        eng.task_status.side_effect = _status
        eng.state = {"tasks": {}}
        eng.base_interval = 300
        return eng

    # ---- (a) menu_task_ops 坏条目 ----

    def test_task_ops_nondict_entry_no_crash(self):
        # (a) 旧代码：选中 "not-a-dict" 后 eng.task_status(task) 内
        # task["name"] 抛 TypeError 崩菜单。
        import monitor as m
        tasks = [{"name": "good", "from": "北京", "to": "上海", "dates": []},
                 "not-a-dict"]
        with mock.patch.object(m, "load_config",
                               return_value={"tasks": tasks}), \
             mock.patch.object(m, "fresh_engine",
                               return_value=self._mock_engine()), \
             mock.patch.object(m, "read", return_value="2"), \
             mock.patch("builtins.print") as mprint:
            m.menu_task_ops()  # 旧代码抛 TypeError
        printed = self._printed(mprint)
        self.assertIn("警告", printed)
        self.assertNotIn("Traceback", printed)

    def test_task_ops_missing_name_entry_no_crash(self):
        # (a) 旧代码：缺 name 的 dict 在 task["name"] 处抛 KeyError 崩菜单。
        import monitor as m
        tasks = [{"name": "good", "from": "北京", "to": "上海", "dates": []},
                 {"from": "北京", "to": "上海"}]
        with mock.patch.object(m, "load_config",
                               return_value={"tasks": tasks}), \
             mock.patch.object(m, "fresh_engine",
                               return_value=self._mock_engine()), \
             mock.patch.object(m, "read", return_value="2"), \
             mock.patch("builtins.print") as mprint:
            m.menu_task_ops()  # 旧代码抛 KeyError
        printed = self._printed(mprint)
        self.assertIn("警告", printed)
        self.assertNotIn("Traceback", printed)

    # ---- (b) 不可哈希 name ----

    def test_task_list_unhashable_name_no_crash(self):
        # (b) 旧代码：eng.state["tasks"].get(["x"], {}) 抛 TypeError
        #（_task_keys_ok 只查键存在，挡不住不可哈希的 name）。
        import monitor as m
        tasks = [{"name": ["x"], "from": "北京", "to": "上海", "dates": []}]
        with mock.patch.object(m, "load_config",
                               return_value={"tasks": tasks}), \
             mock.patch.object(m, "fresh_engine",
                               return_value=self._mock_engine()), \
             mock.patch("builtins.print") as mprint:
            m.menu_task_list()  # 旧代码抛 TypeError
        printed = self._printed(mprint)
        self.assertIn("警告", printed)
        self.assertNotIn("Traceback", printed)

    # ---- (c) tasks 非 list ----

    def _run_create_task_with_cfg(self, cfg):
        import monitor as m
        pm = mock.MagicMock()
        pm.load_passengers.return_value = []
        pm.default_names.return_value = []
        printed = []

        def fake_update(mut):
            # 模拟 update_config_locked 的锁内行为：mutator 直接作用于 cfg。
            mut(cfg)
            return True

        try:
            with mock.patch.object(m, "passengers_mod", pm), \
                 mock.patch.object(m, "pick_station",
                                   side_effect=["北京", "上海"]), \
                 mock.patch.object(m, "input_dates",
                                   return_value=(["2026-10-09"], [])), \
                 mock.patch.object(m, "ticket") as mock_ticket, \
                 mock.patch.object(m, "pick_multi",
                                   return_value=["二等座"]), \
                 mock.patch.object(m, "read",
                                   side_effect=["", "5", "y", "y", "", "n"]), \
                 mock.patch.object(m, "load_config",
                                   return_value=cfg), \
                 mock.patch.object(m, "update_config_locked",
                                   side_effect=fake_update), \
                 mock.patch("builtins.print",
                            side_effect=lambda *a: printed.append(
                                " ".join(map(str, a)))):
                mock_ticket.load_station_map.return_value = (
                    {"北京": "BJP", "上海": "SHH"},
                    {"BJP": "北京", "SHH": "上海"})
                mock_ticket.query_tickets.side_effect = Exception("offline")
                m.menu_create_task()
        except KeyboardInterrupt:
            pass
        return cfg, printed

    def test_create_task_nonlist_tasks_no_crash(self):
        # (c) 旧代码：config.setdefault("tasks", []).append(task) 对 dict
        # 抛 AttributeError（锁内），菜单 traceback。
        cfg = {"tasks": {"bad": 1}}
        cfg, printed = self._run_create_task_with_cfg(cfg)
        self.assertEqual(cfg["tasks"], {"bad": 1})  # 脏数据原样保留，不覆写
        self.assertIn("警告", " ".join(printed))

    # ---- (d) menu_passengers 内 ask_yes_no 的 Ctrl+C ----

    GOOD = {"name": "张三", "id_type_code": "1",
            "id_no": "110101199001011234", "mobile": "13800138000",
            "is_default": True, "is_adult": True}

    def test_passengers_add_ask_yes_no_ctrl_c_returns_to_submenu(self):
        # (d) 旧代码：op1 添加流程中 ask_yes_no 处 Ctrl+C 抛 KeyboardInterrupt
        # 回主菜单，违背 Task 83(b) 回子菜单裁决。
        import monitor as m
        import passengers as pm
        reads = ["1", "张三", "1", "13800138000", "13800138000", None, "0"]
        raised = False
        with mock.patch.object(pm, "load_passengers",
                               return_value=[]), \
             mock.patch.object(m, "read", side_effect=reads), \
             mock.patch("builtins.print") as mprint:
            try:
                m.menu_passengers()  # 旧代码抛 KeyboardInterrupt
            except KeyboardInterrupt:
                raised = True
        self.assertFalse(raised, "Ctrl+C 应回到乘车人子菜单，不应抛回主菜单")
        self.assertIn("已取消", self._printed(mprint))

    def test_passengers_delete_confirm_ctrl_c_returns_to_submenu(self):
        # (d) 旧代码：op3 删除确认处 Ctrl+C 抛回主菜单。
        import monitor as m
        import passengers as pm
        raised = False
        with mock.patch.object(pm, "load_passengers",
                               return_value=[dict(self.GOOD)]), \
             mock.patch.object(m, "read",
                               side_effect=["3", "1", None, "0"]), \
             mock.patch("builtins.print") as mprint:
            try:
                m.menu_passengers()  # 旧代码抛 KeyboardInterrupt
            except KeyboardInterrupt:
                raised = True
        self.assertFalse(raised, "Ctrl+C 应回到乘车人子菜单，不应抛回主菜单")
        self.assertIn("已取消", self._printed(mprint))

    # ---- (e) menu_notify 的 Ctrl+C 消化 ----

    def test_notify_ctrl_c_cancels_edit(self):
        # (e) 旧代码：smtp_host 处 Ctrl+C → read()→None → `None or 旧值`
        # 静默保留旧值，用户无法区分"取消"与"保留"。
        import monitor as m
        cfg = {"notify": {"email": {"enabled": True, "smtp_host": "smtp.qq.com",
                                    "smtp_port": 465, "username": "u",
                                    "password": "", "from": "", "to": []}}}
        with mock.patch.object(m, "load_config",
                               return_value=cfg), \
             mock.patch.object(m, "read", return_value=None), \
             mock.patch("builtins.print") as mprint:
            with self.assertRaises(KeyboardInterrupt):
                m.menu_notify()  # 旧代码不抛异常
        printed = self._printed(mprint)
        self.assertIn("已取消修改", printed)
        self.assertNotIn("已保存", printed)

    # ---- (f) 文案 ----

    def test_create_task_passenger_prompt_text_matches_behavior(self):
        # (f) default_names() 返回"默认乘车人（未设默认则仅第一位）"，
        # 旧文案"回车=使用默认/全部成人"误导为"全部成人"。
        import monitor as m
        prompts = []
        pm = mock.MagicMock()
        pm.load_passengers.return_value = [dict(self.GOOD)]
        pm.default_names.return_value = ["张三"]
        try:
            with mock.patch.object(m, "passengers_mod", pm), \
                 mock.patch.object(m, "pick_station",
                                   side_effect=["北京", "上海"]), \
                 mock.patch.object(m, "input_dates",
                                   return_value=(["2026-10-09"], [])), \
                 mock.patch.object(m, "ticket") as mock_ticket, \
                 mock.patch.object(m, "pick_multi",
                                   return_value=["二等座"]), \
                 mock.patch.object(m, "read",
                                   side_effect=["", "", "5", "y", "y", "",
                                                "n"]) as mread, \
                 mock.patch.object(m, "load_config",
                                   return_value={}), \
                 mock.patch.object(m, "update_config_locked",
                                   return_value=True), \
                 mock.patch("builtins.print"):
                mock_ticket.load_station_map.return_value = (
                    {"北京": "BJP", "上海": "SHH"},
                    {"BJP": "北京", "SHH": "上海"})
                mock_ticket.query_tickets.side_effect = Exception("offline")
                m.menu_create_task()
        except KeyboardInterrupt:
            pass
        prompts = [c.args[0] for c in mread.call_args_list]
        pax_prompts = [p for p in prompts if "选择乘车人" in p]
        self.assertTrue(pax_prompts)
        self.assertNotIn("全部成人", pax_prompts[0])

    # ---- (g) menu_quick_check ----

    def test_quick_check_ctrl_c_no_network(self):
        # (g) 旧代码：日期处 Ctrl+C → read()→None → query_tickets(..., None)
        # 发起真实网络查询，而非取消。
        import monitor as m
        with mock.patch.object(m.ticket, "load_station_map",
                               return_value=({"北京": "BJP", "上海": "SHH"},
                                             {})), \
             mock.patch.object(m.ticket, "query_tickets") as mq, \
             mock.patch.object(m, "pick_station",
                               side_effect=["北京", "上海"]), \
             mock.patch.object(m, "read", return_value=None), \
             mock.patch("builtins.print"):
            m.menu_quick_check()  # 旧代码调用 query_tickets
        mq.assert_not_called()

    # ---- (h) menu_notify to_default ----

    def test_notify_to_int_no_crash(self):
        # (h) 旧代码：",".join(123 or []) 抛 TypeError 崩菜单
        #（Task 92 评审确认真实）。
        import monitor as m
        cfg = {"notify": {"email": {"enabled": True, "smtp_host": "h",
                                    "smtp_port": 465, "username": "u",
                                    "password": "", "from": "", "to": 123}}}

        def fake_read(prompt, default=""):
            return default

        with mock.patch.object(m, "load_config",
                               return_value=cfg), \
             mock.patch.object(m, "read", side_effect=fake_read), \
             mock.patch("getpass.getpass", return_value=""), \
             mock.patch.object(m, "ask_yes_no", return_value=False), \
             mock.patch.object(m, "save_config", return_value=True), \
             mock.patch.object(m, "_config_stamp", return_value=None), \
             mock.patch("builtins.print") as mprint:
            m.menu_notify()  # 旧代码抛 TypeError
        printed = self._printed(mprint)
        self.assertIn("警告", printed)
        self.assertNotIn("Traceback", printed)


class TestTask98ProbeLoginP3(unittest.TestCase):
    """Task 98: probe_login P3（uamtk 明文打印 / Cookie 明文打印 / probe_cookies.json 权限）。"""

    def _printed(self, mprint):
        return "\n".join(str(c.args[0]) for c in mprint.call_args_list if c.args)

    # ---- (a) step4 uamtk 脱敏 ----

    def test_step4_poll_qr_uamtk_is_masked(self):
        # RED on old code: uamtk 明文打印（bearer token 比 username 更敏感）
        fake = mock.Mock()
        fake.post.return_value = _FakeResp(
            '{"result_code": 2, "result_message": "已确认", "uamtk": "secret-uamtk-token-xyz"}')
        with mock.patch.object(probe_login, "SESSION", fake), \
             mock.patch("builtins.print") as mprint, \
             mock.patch("time.sleep"):
            result = probe_login.step4_poll_qr("uuid-x")
        self.assertEqual(result, "secret-uamtk-token-xyz")  # 返回值仍是真 token
        self.assertNotIn("secret-uamtk-token-xyz", self._printed(mprint))

    # ---- (b) step2 Cookie 值脱敏 ----

    def _run_step2(self):
        cookie = mock.Mock()
        cookie.name = "JSESSIONID"
        cookie.value = "secret-jsessionid-value-12345"
        fake = mock.Mock()
        fake.get.return_value = _FakeResp("{}")
        fake.cookies = [cookie]
        return fake

    def test_step2_bootstrap_cookies_values_masked(self):
        # RED on old code: Cookie 值明文打印（每次运行必触发）
        with mock.patch.object(probe_login, "SESSION", self._run_step2()), \
             mock.patch("builtins.print") as mprint:
            probe_login.step2_bootstrap_cookies()
        out = self._printed(mprint)
        self.assertNotIn("secret-jsessionid-value-12345", out)
        self.assertIn("JSESSIONID", out)  # 字段名保留，仍可判断是否拿到

    # ---- (c) probe_cookies.json 0o600 ----

    def _run_step7(self):
        cookie = mock.Mock()
        cookie.name = "JSESSIONID"
        cookie.value = "x"
        fake = mock.Mock()
        fake.cookies = [cookie]
        return fake

    def test_step7_save_cookies_file_is_0600(self):
        # RED on old code: 默认 umask 落盘，他用户可读
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "probe_cookies.json")
            with mock.patch.object(probe_login, "SESSION", self._run_step7()), \
                 mock.patch.object(probe_login, "PROBE_COOKIE_PATH", path), \
                 mock.patch("builtins.print"):
                probe_login.step7_save_cookies()
            mode = os.stat(path).st_mode & 0o777
            self.assertEqual(mode, 0o600)
            with open(path, encoding="utf-8") as f:  # 内容不受影响
                self.assertEqual(json.load(f), {"JSESSIONID": "x"})

    def test_step7_save_cookies_tightens_existing_0644(self):
        # RED on old code: 已存在的 0644 文件不会被收紧
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "probe_cookies.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{}")
            os.chmod(path, 0o644)
            with mock.patch.object(probe_login, "SESSION", self._run_step7()), \
                 mock.patch.object(probe_login, "PROBE_COOKIE_PATH", path), \
                 mock.patch("builtins.print"):
                probe_login.step7_save_cookies()
            mode = os.stat(path).st_mode & 0o777
            self.assertEqual(mode, 0o600)


class TestTask99CaptureSessionP3(unittest.TestCase):
    """Task 99: capture_session P3（gui 超时误判 / 轮询无 Ctrl+C / 相对路径 CWD 歧义 / 已关闭浏览器 defensive）。"""

    # ---- (a) gui 重登超时必须覆盖脚本实际总耗时 ----

    def test_relogin_script_timeout_covers_script_total(self):
        # RED on old code: timeout 硬编码 300 == MAX_WAIT_SEC(300)，
        # 没留启动开销+最终验证的时间 → 用户在等待末尾登录成功会被误判"运行超时"
        import capture_session
        proc = mock.Mock()
        proc.returncode = 0
        with mock.patch("gui.subprocess.run", return_value=proc) as mrun:
            self.assertTrue(gui._relogin_ok_via_script("capture_session.py"))
        timeout = mrun.call_args.kwargs["timeout"]
        self.assertGreaterEqual(timeout, capture_session.MAX_WAIT_SEC + 60)

    def test_relogin_script_timeout_still_returns_false(self):
        # 超时仍视为失败返回 False（行为不变，只是阈值对齐）
        import capture_session
        with mock.patch("gui.subprocess.run",
                        side_effect=subprocess.TimeoutExpired("cmd", 1)):
            self.assertFalse(gui._relogin_ok_via_script("capture_session.py"))

    # ---- (b) 300s 轮询 Ctrl+C 优雅退出 ----

    def test_wait_for_login_ctrl_c_cancels_gracefully(self):
        # RED on old code: Ctrl+C 穿透 time.sleep(2) 抛 traceback（Task 78g 同类）
        import capture_session
        browser = mock.Mock()
        browser.is_connected.return_value = True
        context = mock.Mock()
        context.cookies.return_value = []
        with mock.patch("time.sleep", side_effect=KeyboardInterrupt):
            status = capture_session._wait_for_login(browser, context,
                                                     time.time() + 300)
        self.assertEqual(status, capture_session.LOGIN_CANCELLED)

    # ---- (c) 相对路径按脚本目录解析 ----

    def test_load_session_relative_path_resolves_against_here(self):
        # RED on old code: 显式传相对路径时按 CWD 解析；
        # 写侧（capture_session）按 HERE 写 → CWD≠脚本目录时"未找到会话文件"误导
        with mock.patch("os.path.exists", return_value=False) as m_exists:
            with self.assertRaises(RuntimeError):
                order_mod.load_session("rel/cookies.json")
        checked = m_exists.call_args[0][0]
        self.assertEqual(checked, os.path.join(order_mod.HERE, "rel", "cookies.json"))

    def test_load_session_absolute_path_passthrough(self):
        # 绝对路径透传（pin：正常路径行为不变）
        with mock.patch("os.path.exists", return_value=False) as m_exists:
            with self.assertRaises(RuntimeError):
                order_mod.load_session("/tmp/abs_cookies.json")
        self.assertEqual(m_exists.call_args[0][0], "/tmp/abs_cookies.json")

    def test_save_session_relative_path_resolves_against_here(self):
        # 写侧也要统一口径（engine 回写轮换 Cookie 时传 config 相对路径）
        # RED on old code: 按 CWD 落盘，与读侧/抓取侧 HERE 口径不一致
        cookie = mock.Mock()
        cookie.name = "tk"
        cookie.value = "v"
        cookie.domain = "kyfw.12306.cn"
        cookie.path = "/otn"
        session = mock.Mock()
        session.cookies = [cookie]
        with mock.patch.object(order_mod.appcommon, "atomic_write_json") as m_write:
            order_mod.save_session(session, "rel/cookies.json")
        written = m_write.call_args[0][0]
        self.assertEqual(written, os.path.join(order_mod.HERE, "rel", "cookies.json"))

    # ---- (d) 已关闭浏览器 defensive close ----

    def _run_main(self, connected, wait_status=None, final_ok=False):
        import capture_session
        browser = mock.Mock()
        browser.is_connected.return_value = connected
        fake_mod = mock.MagicMock()  # playwright.sync_api：sync_playwright() 返回支持 with 的 MagicMock
        with mock.patch.object(capture_session, "_order_mode",
                               return_value="http"), \
             mock.patch.object(capture_session, "_launch_browser",
                               return_value=browser), \
             mock.patch.object(capture_session, "_goto_login_page",
                               return_value=True), \
             mock.patch.object(capture_session, "_wait_for_login",
                               return_value=(capture_session.LOGIN_OK
                                             if wait_status is None else wait_status)), \
             mock.patch.object(capture_session, "_final_verify",
                               return_value=final_ok), \
             mock.patch.dict("sys.modules",
                             {"playwright": mock.MagicMock(),
                              "playwright.sync_api": fake_mod}), \
             mock.patch("builtins.print"):
            rc = capture_session.main()
        return rc, browser

    def _run_main_final_verify_fail(self, connected):
        return self._run_main(connected, final_ok=False)

    def test_final_verify_fail_skips_close_on_disconnected_browser(self):
        # RED on old code: _final_verify 失败后无条件 browser.close()；
        # 用户在验证阶段关了窗口 → close 作用于已关闭浏览器
        rc, browser = self._run_main_final_verify_fail(connected=False)
        self.assertEqual(rc, 1)
        browser.close.assert_not_called()

    def test_final_verify_fail_closes_connected_browser(self):
        # 正常路径行为不变：浏览器还开着 → 照常关闭
        rc, browser = self._run_main_final_verify_fail(connected=True)
        self.assertEqual(rc, 1)
        browser.close.assert_called_once()

    def test_main_cancel_closes_browser_and_returns_1(self):
        # 取消路径 pin：_wait_for_login 返回 CANCELLED → 关浏览器 + 返回 1
        import capture_session
        rc, browser = self._run_main(True, wait_status=capture_session.LOGIN_CANCELLED)
        self.assertEqual(rc, 1)
        browser.close.assert_called_once()


class TestTask100NotifyPassengersP3(TempDirCase):
    """Task 100: notify/passengers P3（邮件头非 ASCII 崩 / notify.email 非 dict 崩 CLI /
    ensure_passengers 裸调无指纹 / print 警告不可见 / 非字符串 password 崩对话框）。"""

    # ---- (a) 非 ASCII 邮件头纳入 (ok,msg) ----

    def test_a_non_ascii_from_returns_false_not_raise(self):
        # RED on old code: formataddr 在 try 之外抛 UnicodeEncodeError 逃出 (ok,msg)
        import notify as notify_mod
        cfg = {"enabled": True, "smtp_host": "x", "smtp_port": 465,
               "username": "u@x.com", "password": "plain",
               "from": "测试@example.com", "to": ["a@b.com"]}
        ok, msg = notify_mod.send_email(cfg, "主题", "正文")
        self.assertFalse(ok)
        self.assertIn("邮件头", msg)

    def test_a_ascii_path_unchanged(self):
        # 回归 pin：ASCII 快路径行为不变（旧代码即通过）
        import notify as notify_mod
        cfg = {"enabled": True, "smtp_host": "h", "smtp_port": 465,
               "username": "u@x.com", "password": "plain",
               "from": "u@x.com", "to": ["a@b.com"]}
        s = mock.Mock()
        with mock.patch("smtplib.SMTP_SSL", return_value=s):
            ok, msg = notify_mod.send_email(cfg, "ASCII subject", "body")
        self.assertTrue(ok)
        self.assertIn("a@b.com", msg)
        s.login.assert_called_once_with("u@x.com", "plain")

    # ---- (b) notify.email 非 dict 友好降级 ----

    def test_b_email_non_dict_no_crash(self):
        # RED on old code: email.get 在字符串上抛 AttributeError 杀死 CLI
        import monitor as monitor_mod
        with mock.patch.object(monitor_mod, "load_config",
                               return_value={"notify": {"email": "dirty"}}), \
             mock.patch("builtins.print") as mprint:
            monitor_mod.menu_notify()
        printed = " ".join(str(c.args[0]) for c in mprint.call_args_list)
        self.assertIn("警告", printed)
        self.assertIn("notify.email", printed)

    def test_b_notify_non_dict_no_crash(self):
        # RED on old code: 非 dict 的 notify 上调 setdefault 抛 AttributeError
        import monitor as monitor_mod
        with mock.patch.object(monitor_mod, "load_config",
                               return_value={"notify": "dirty"}), \
             mock.patch("builtins.print") as mprint:
            monitor_mod.menu_notify()
        printed = " ".join(str(c.args[0]) for c in mprint.call_args_list)
        self.assertIn("警告", printed)

    def test_b_normal_config_flow_unchanged(self):
        # 回归 pin：正常 dict 配置走完整流程（旧代码即通过）
        import monitor as monitor_mod
        saved = {}
        with mock.patch.object(monitor_mod, "load_config", return_value={}), \
             mock.patch.object(monitor_mod, "read",
                               side_effect=["", "465", "", "", "", ""]), \
             mock.patch("getpass.getpass", return_value=""), \
             mock.patch.object(monitor_mod, "ask_yes_no", return_value=False), \
             mock.patch.object(monitor_mod, "save_config",
                               side_effect=lambda c, **k: saved.update(c) or True):
            monitor_mod.menu_notify()
        self.assertIsInstance(saved["notify"]["email"], dict)
        self.assertEqual(saved["notify"]["email"]["smtp_port"], 465)

    # ---- (c) ensure_passengers 传 expect_stamp=None ----

    def test_c_ensure_passengers_passes_expect_stamp_none(self):
        # RED on old code: 裸调 save_passengers 不传指纹，首跑双进程可静默覆写
        import launcher
        cfg = os.path.join(self.tmp, "config.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"tasks": [{"passenger_names": ["张三"]}]}, f)
        with mock.patch.object(launcher, "HERE", self.tmp), \
             mock.patch.object(launcher, "log"), \
             mock.patch.object(launcher.passengers_mod, "save_passengers",
                               return_value=True) as m_save:
            launcher.ensure_passengers()
        m_save.assert_called_once()
        self.assertIn("expect_stamp", m_save.call_args.kwargs)
        self.assertIsNone(m_save.call_args.kwargs["expect_stamp"])

    def test_c_expect_stamp_none_refuses_when_file_appears(self):
        # 首跑语义 pin：expect_stamp=None = "load 时文件不存在"；
        # 写入前文件已出现 → 指纹不匹配 → 放弃，不静默覆写
        import passengers as pax_mod
        p = os.path.join(self.tmp, "passengers.json")
        self.assertTrue(pax_mod.save_passengers([{"name": "甲"}], path=p,
                                                expect_stamp=None))
        self.assertFalse(pax_mod.save_passengers([{"name": "乙"}], path=p,
                                                 expect_stamp=None))
        self.assertEqual(pax_mod.load_passengers(p)[0]["name"], "甲")

    # ---- (d) load_passengers 警告走 LOG.error ----

    def test_d_load_read_fail_logs_error(self):
        # RED on old code: print 在 GUI 下不可见，assertLogs 抓不到 ERROR
        import passengers as pax_mod
        p = os.path.join(self.tmp, "passengers.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{not json")
        with self.assertLogs("monitor", level="ERROR") as cm:
            self.assertEqual(pax_mod.load_passengers(p), [])
        self.assertTrue(any("读取失败" in m for m in cm.output))

    def test_d_load_decrypt_fail_logs_error(self):
        # 第二处 print 同样转 LOG.error
        import passengers as pax_mod
        p = os.path.join(self.tmp, "passengers.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"enc": "none", "data": "not json"}, f)
        with self.assertLogs("monitor", level="ERROR") as cm:
            self.assertEqual(pax_mod.load_passengers(p), [])
        self.assertTrue(any("解密失败" in m for m in cm.output))

    # ---- (e) unprotect_secret 非字符串走 SecretDecryptError ----

    def test_e_unprotect_non_string_raises_secret_decrypt_error(self):
        # RED on old code: 12345.startwith 抛裸 AttributeError；
        # gui 邮箱设置对话框只捕 SecretDecryptError → 对话框打不开
        import passengers as pax_mod
        with self.assertRaises(pax_mod.SecretDecryptError) as cm:
            pax_mod.unprotect_secret(12345)
        self.assertIn("12345", str(cm.exception))

    def test_e_unprotect_legacy_paths_unchanged(self):
        # 回归 pin：字符串/空/None 路径行为不变（旧代码即通过）
        import passengers as pax_mod
        self.assertEqual(pax_mod.unprotect_secret("legacy"), "legacy")
        self.assertEqual(pax_mod.unprotect_secret(""), "")
        self.assertEqual(pax_mod.unprotect_secret(None), "")


class TestTask101CrossModuleP3(TempDirCase):
    """Task 101：跨模块 P3 bundle（8 子项）——每项改前精确失败、改后通过。"""

    # ---- (a) save_station_kinds 并发写：tmp 文件名必须带线程后缀 ----
    def test_a_tmp_unique_per_thread(self):
        import launcher
        real_path = launcher.STATION_KIND_PATH
        real_kinds = launcher._station_kinds
        launcher.STATION_KIND_PATH = os.path.join(self.tmp, "station_kind.json")
        launcher._station_kinds = {"AAA": "高铁"}
        self.addCleanup(setattr, launcher, "STATION_KIND_PATH", real_path)
        self.addCleanup(setattr, launcher, "_station_kinds", real_kinds)
        srcs = []
        with mock.patch("os.replace") as m_replace:
            m_replace.side_effect = lambda s, d: srcs.append(s)
            ts = [threading.Thread(target=launcher.save_station_kinds)
                  for _ in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(len(srcs), 2)
        self.assertEqual(
            len(set(srcs)), 2,
            "两线程复用同一 tmp 路径，并发写会交错损坏：%r" % (srcs,))

    # ---- (b) get_station_index：加载失败不永久缓存，下次重试；日志文案属实 ----
    def test_b_failed_load_not_cached(self):
        import launcher
        launcher._STATION_INDEX = None
        self.addCleanup(setattr, launcher, "_STATION_INDEX", None)
        with mock.patch.object(ticket, "load_station_index",
                               side_effect=RuntimeError("net down")) as m_load:
            with mock.patch.object(launcher, "log") as m_log:
                self.assertEqual(launcher.get_station_index(), [])
                self.assertEqual(launcher.get_station_index(), [])
        self.assertEqual(m_load.call_count, 2, "加载失败被永久缓存，网络恢复后仍无结果")
        self.assertIsNone(launcher._STATION_INDEX)
        logged = " ".join(str(c) for c in m_log.call_args_list)
        self.assertNotIn("只按站名匹配", logged, "失败时索引为空，'只按站名匹配'不属实")

    def test_b_success_still_cached(self):
        import launcher
        launcher._STATION_INDEX = None
        self.addCleanup(setattr, launcher, "_STATION_INDEX", None)
        st = [{"name": "北京", "code": "BJP", "py": "beijing", "spy": "bj"}]
        with mock.patch.object(ticket, "load_station_index",
                               return_value=st) as m_load:
            with mock.patch.object(launcher, "log"):
                self.assertEqual(launcher.get_station_index(), st)
                self.assertEqual(launcher.get_station_index(), st)
        self.assertEqual(m_load.call_count, 1)

    # ---- (d) 未知席别码 warn-once 去重 ----
    def _isolate_seen_codes(self):
        seen_before = set(ticket._SEEN_UNKNOWN_SEAT_CODES)

        def _restore():
            ticket._SEEN_UNKNOWN_SEAT_CODES.clear()
            ticket._SEEN_UNKNOWN_SEAT_CODES.update(seen_before)
        self.addCleanup(_restore)
        ticket._SEEN_UNKNOWN_SEAT_CODES.clear()

    def test_d_unknown_seat_code_warn_once(self):
        self._isolate_seen_codes()
        with mock.patch.object(ticket.LOG, "warning") as mw:
            ticket._split_seat_codes("Q")
        self.assertEqual(mw.call_count, 1)
        with mock.patch.object(ticket.LOG, "warning") as mw2:
            ticket._split_seat_codes("Q")
        self.assertEqual(mw2.call_count, 0, "未知席别码每轮重复打 warning，日志刷屏")

    def test_d_known_codes_no_warning(self):
        self._isolate_seen_codes()
        with mock.patch.object(ticket.LOG, "warning") as mw:
            names = ticket._split_seat_codes("OQ")
        mw.assert_called_once()  # 只有 Q 触发一次
        self.assertIn("二等座", names)

    # ---- (e) 点名乘车人零命中/部分命中：记 warning，不静默回退 ----
    def test_e_partial_match_warns_names_missing(self):
        allp = [{"name": "张三", "is_adult": True, "id_no": "1"},
                {"name": "李四", "is_adult": True, "id_no": "2"}]
        with mock.patch.object(order_mod.LOG, "warning") as mw:
            picked = order_mod.select_passengers(allp, ["张三", "王五"])
        self.assertEqual([p["name"] for p in picked], ["张三"])
        self.assertTrue(mw.called, "部分命中静默回退，未告警")
        self.assertIn("王五", str(mw.call_args), "警告必须点名未命中的乘车人")

    def test_e_zero_match_warns_and_falls_back(self):
        allp = [{"name": "张三", "is_adult": True, "id_no": "1"}]
        with mock.patch.object(order_mod.LOG, "warning") as mw:
            picked = order_mod.select_passengers(allp, ["王五"])
        self.assertEqual([p["name"] for p in picked], ["张三"])  # 回退全体成人
        self.assertTrue(mw.called, "零命中静默回退全体成人，未告警")
        self.assertIn("王五", str(mw.call_args))

    def test_e_full_match_no_warning(self):
        allp = [{"name": "张三", "is_adult": True, "id_no": "1"}]
        with mock.patch.object(order_mod.LOG, "warning") as mw:
            picked = order_mod.select_passengers(allp, ["张三"])
        self.assertEqual([p["name"] for p in picked], ["张三"])
        mw.assert_not_called()

    # ---- (f) gui 非 dict state：友好处理，不打不开对话框 ----
    def test_f_nondict_state_safe(self):
        with mock.patch.object(gui, "load_state", return_value=[]):
            with mock.patch.object(gui.LOG, "warning") as mw:
                self.assertEqual(gui._state_tasks_dict(), {})
        self.assertTrue(mw.called, "非 dict state 应记警告")

    def test_f_nondict_tasks_section_safe(self):
        with mock.patch.object(gui, "load_state",
                               return_value={"tasks": ["x"]}):
            with mock.patch.object(gui.LOG, "warning"):
                self.assertEqual(gui._state_tasks_dict(), {})

    def test_f_dict_state_passthrough(self):
        st = {"tasks": {"t1": {"status": "paused"}}}
        with mock.patch.object(gui, "load_state", return_value=st):
            self.assertEqual(gui._state_tasks_dict(), {"t1": {"status": "paused"}})

    # ---- (g) 锁内 stale-warm 关闭：不释放装饰器持有的锁 ----
    def _fake_warm(self):
        import browser_order as bo
        ws = bo.WarmSession.__new__(bo.WarmSession)
        ws._closed = False
        ws._ctx = mock.Mock()
        ws._p = mock.Mock()
        ws._file_locked = True
        ws._local_locked = True
        ws._owner = threading.get_ident()
        ws._owner_thread = threading.current_thread()
        return ws

    def test_g_close_keep_locks(self):
        import browser_order as bo
        ws = self._fake_warm()
        with mock.patch("browser_order._PROFILE_LOCK") as m_pl, \
                mock.patch("browser_order._BROWSER_LOCK") as m_bl:
            ws.close(release_locks=False)
        ws._ctx.close.assert_called_once()  # 浏览器资源照关
        ws._p.stop.assert_called_once()
        self.assertTrue(ws._file_locked, "锁标记不应被清除")
        self.assertTrue(ws._local_locked, "锁标记不应被清除")
        m_pl.release.assert_not_called()
        m_bl.release.assert_not_called()

    def test_g_full_close_still_releases(self):
        import browser_order as bo
        ws = self._fake_warm()
        with mock.patch("browser_order._PROFILE_LOCK") as m_pl, \
                mock.patch("browser_order._BROWSER_LOCK") as m_bl, \
                mock.patch("browser_order._get_depth", return_value=1), \
                mock.patch("browser_order._set_depth") as m_sd:
            ws.close()
        m_pl.release.assert_called_once()
        m_bl.release.assert_called_once()
        m_sd.assert_called_with(0, ws._owner_thread)
        self.assertFalse(ws._file_locked)
        self.assertFalse(ws._local_locked)

    # ---- (h) browser_order._log PII 脱敏 ----
    def test_h_mask_helpers(self):
        import browser_order as bo
        self.assertEqual(bo._mask_name("张三"), "张*")
        self.assertEqual(bo._mask_name("李"), "*")
        self.assertEqual(bo._mask_name(""), "")
        self.assertEqual(bo._mask_scalar("zhangsan"), "***")
        self.assertEqual(bo._mask_scalar(""), "")
        self.assertIsNone(bo._mask_scalar(None))

    def test_h_mask_ticket_names(self):
        import browser_order as bo
        tickets = [{"name": "张三", "seat": "O", "ticket_type": "1"},
                   {"name": "李四", "seat": "M", "ticket_type": "1"}]
        masked = bo._mask_ticket_names(tickets)
        self.assertEqual([t["name"] for t in masked], ["张*", "李*"])
        self.assertEqual([t["seat"] for t in masked], ["O", "M"])  # 非 PII 保留
        self.assertEqual(tickets[0]["name"], "张三")  # 不污染原数据

    def test_h_mask_text_names(self):
        import browser_order as bo
        out = bo._mask_text_names("乘车人：张三，李四", ["张三", "李四"])
        self.assertNotIn("张三", out)
        self.assertNotIn("李四", out)
        self.assertIn("张*", out)


class TestTask103LauncherGuiP3(TempDirCase):
    """Task 103：launcher/gui P2/P3（a preset 写回污染 / b 席别默认值展示 /
    c 成员检查 / d update_config_locked 写侧非 dict 拒绝）。

    无 Tk 真机，全部 mock/桩测试（沿 Task 96/101 口径）。
    """

    # ---- (a) _save_preset 写回前归一化（P2） ----

    def _make_preset_app(self, trains, seat_types):
        app = object.__new__(launcher.LauncherApp)
        app.lc = {"from": "北京", "to": "上海",
                  "trains": trains, "seat_types": seat_types,
                  "presets": []}
        # _ui_to_lc 置空：只测写回点本身（entry 组装 + _save_cfg），
        # 模拟 lc 在写回时仍为脏值（手改配置/任务模式嵌入等）。
        app._ui_to_lc = mock.Mock()
        app._save_cfg = mock.Mock()
        app._put_log = mock.Mock()
        app._mp = mock.Mock()
        app.preset_cb = mock.Mock()
        app.preset_cb.get.return_value = ""
        return app

    def _saved_preset_entry(self, app):
        saved_lc = app._save_cfg.call_args[0][0]
        self.assertEqual(len(saved_lc["presets"]), 1)
        return saved_lc["presets"][0]

    def test_a_save_preset_dirty_trains_single_not_char_split(self):
        # P2：lc["trains"] 为裸字符串 "G101" 时，旧代码 list(...) 逐字符拆成
        # ["G","1","0","1"] 并写回 preset → 配置污染（Task 89a 同类）。
        app = self._make_preset_app("G101", ["二等座"])
        with mock.patch.object(launcher.simpledialog, "askstring",
                               return_value="京沪测试"):
            app._save_preset()
        entry = self._saved_preset_entry(app)
        self.assertEqual(entry["trains"], ["G101"])

    def test_a_save_preset_dirty_seat_types_single_not_char_split(self):
        # P2：lc["seat_types"] 为裸字符串 "二等座" 时，旧代码逐字符拆成
        # ["二","等","座"] 并写回 preset → 配置污染。
        app = self._make_preset_app(["G101"], "二等座")
        with mock.patch.object(launcher.simpledialog, "askstring",
                               return_value="京沪测试"):
            app._save_preset()
        entry = self._saved_preset_entry(app)
        self.assertEqual(entry["seat_types"], ["二等座"])

    def test_a_save_preset_clean_lists_unchanged(self):
        # 正向对照：干净 list 形状写回前后一致（归一化幂等，不改变正常行为）。
        app = self._make_preset_app(["G101", "G103"], ["二等座", "一等座"])
        with mock.patch.object(launcher.simpledialog, "askstring",
                               return_value="京沪测试"):
            app._save_preset()
        entry = self._saved_preset_entry(app)
        self.assertEqual(entry["trains"], ["G101", "G103"])
        self.assertEqual(entry["seat_types"], ["二等座", "一等座"])

    # ---- (b) _rebuild_seats 席别默认值展示（P3） ----

    def test_b_display_seat_types_unit(self):
        # 裸字符串按单个席别，不逐字符拆；非法形状视为空。
        self.assertEqual(launcher._display_seat_types("二等座"), ["二等座"])
        self.assertEqual(launcher._display_seat_types(["一等座"]), ["一等座"])
        self.assertEqual(launcher._display_seat_types(None), [])
        self.assertEqual(launcher._display_seat_types(123), [])

    def test_b_rebuild_seats_dirty_seat_types_default_checked(self):
        # P3：lc["seat_types"] 为裸字符串 "二等座" 时，旧代码
        # set("二等座") → {"二","等","座"}，default 匹配恒失败 → 回退勾选
        # names[0]（展示错误）。改后应勾选"二等座"。
        app = object.__new__(launcher.LauncherApp)
        app.lc = {"seat_types": "二等座"}
        app.seat_vars = {}
        app.seat_box = mock.Mock()
        app.seat_box.winfo_children.return_value = []
        app.sf = mock.Mock()
        app._put_log = mock.Mock()
        bool_values = {}

        def fake_boolvar(value=False):
            m = mock.Mock()
            m.get.return_value = value
            bool_values.setdefault("last", []).append(value)
            return m

        with mock.patch.object(launcher.tk, "BooleanVar",
                               side_effect=fake_boolvar), \
             mock.patch.object(launcher.ttk, "Checkbutton"), \
             mock.patch.object(launcher.ttk, "Label"):
            app._rebuild_seats()
        self.assertIn("二等座", app.seat_vars)
        self.assertTrue(app.seat_vars["二等座"].get(),
                        "脏字符串席别应按单个席别勾选，而非回退到首个席别")

    # ---- (c) gui 任务对话框席别预选成员检查（P3） ----

    def test_c_display_seat_types_unit(self):
        # 裸字符串按单个席别，不逐字符拆；非法形状视为空。
        self.assertEqual(gui._display_seat_types("二等座"), ["二等座"])
        self.assertEqual(gui._display_seat_types(["一等座"]), ["一等座"])
        self.assertEqual(gui._display_seat_types(None), [])
        self.assertEqual(gui._display_seat_types(123), [])

    # ---- (d) update_config_locked 写侧拒绝非 dict（P3） ----

    def _patch_gui_config(self, content):
        cfg = os.path.join(self.tmp, "config.json")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write(content)
        p = mock.patch.object(gui, "CONFIG_PATH", cfg)
        p.start()
        self.addCleanup(p.stop)
        return cfg

    def test_d_update_config_locked_rejects_non_dict(self):
        # P3：config.json 内容为 []（合法 JSON 但非 dict）时，旧代码
        # mutator([]) 抛裸 TypeError（Tk console）。改后：warning + error
        # 弹窗 + 友好 RuntimeError，mutator 不执行，文件不被覆盖。
        cfg = self._patch_gui_config("[]")
        calls = []

        def mutator(config):
            calls.append(config)
            config["x"] = 1

        with mock.patch.object(gui, "messagebox") as mb:
            with self.assertRaises(RuntimeError):
                gui.update_config_locked(mutator)
        self.assertEqual(calls, [], "非 dict 时 mutator 不得执行")
        mb.showerror.assert_called_once()
        with open(cfg, encoding="utf-8") as f:
            self.assertEqual(f.read(), "[]", "旧文件不得被覆盖")

    def test_d_update_config_locked_dict_still_works(self):
        # 正向对照：dict 配置写侧流程不变。
        cfg = self._patch_gui_config('{"a": 1}')
        gui.update_config_locked(lambda c: c.update({"b": 2}))
        with open(cfg, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"a": 1, "b": 2})


class TestTask104LoadSanitize(TempDirCase):
    """Task 104：入口一次性 sanitize（load_grab_tasks / load_launcher_config）。

    Task 96(c)(d) 只修了展示侧；_find/_new_task/_delete_task/_upsert_task/
    _on_window_closed（及 _current_preset/_save_preset/删除预设）全对原始条目
    调 t.get(...)，脏条目仍崩；_on_window_closed 在 _on_close 的 finally 块内，
    抛异常则 destroy 被跳过→僵尸窗口。改后在加载入口剔除非 dict 条目。
    无 Tk 真机，全部 mock/桩测试（沿 Task 96 口径）。
    """

    def _write(self, name, obj):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        return path

    def _make_panel(self, tasks):
        panel = object.__new__(launcher.TaskManagerPanel)
        panel.tasks = tasks
        panel._windows = {}
        panel._refresh_list = mock.Mock()
        panel._put_log = mock.Mock()
        return panel

    # ---- (1) grab_tasks 入口 sanitize ----

    def test_grab_tasks_load_drops_non_dict(self):
        # 改前：load_grab_tasks 原样返回脏条目，后续 t.get(...) 全崩。
        path = self._write("grab_tasks.json", {"tasks": [
            {"id": "a", "name": "A"}, "junk", 123, None, {"id": "b"}]})
        with mock.patch.object(launcher, "GRAB_TASKS_PATH", path), \
             mock.patch.object(launcher, "log") as mlog:
            tasks = launcher.load_grab_tasks()
        self.assertEqual([t.get("id") for t in tasks], ["a", "b"])
        warns = [c for c in mlog.call_args_list if "[警告]" in str(c)]
        self.assertTrue(warns, "应记 warning")

    def test_find_safe_after_sanitized_load(self):
        # 改前：_find 遍历到 "junk" 时 t.get 抛 AttributeError。
        path = self._write("grab_tasks.json", {"tasks": [
            {"id": "a", "name": "A"}, "junk", None]})
        with mock.patch.object(launcher, "GRAB_TASKS_PATH", path), \
             mock.patch.object(launcher, "log"):
            panel = self._make_panel(launcher.load_grab_tasks())
        self.assertEqual(panel._find("a")["name"], "A")
        self.assertIsNone(panel._find("zzz"))  # 改前在此抛 AttributeError

    def test_on_window_closed_safe_and_destroy_reached(self):
        # 改前：_on_window_closed 在 finally 块内抛 AttributeError，
        # 则 _on_close 的 self.destroy() 被跳过→僵尸窗口。
        path = self._write("grab_tasks.json", {"tasks": [
            {"id": "a", "name": "A", "status": "idle"}, "junk", None]})
        with mock.patch.object(launcher, "GRAB_TASKS_PATH", path), \
             mock.patch.object(launcher, "log"), \
             mock.patch.object(launcher, "save_grab_tasks") as sg:
            panel = self._make_panel(launcher.load_grab_tasks())
            # 模拟 GrabTaskWindow._on_close 的 finally 段：
            destroyed = []
            try:
                pass
            finally:
                panel._on_window_closed("a")  # 改前在此抛 AttributeError
                destroyed.append(True)        # = self.destroy() 被执行
        self.assertEqual(destroyed, [True])
        sg.assert_called_once()

    # ---- (2) launcher_config presets 入口 sanitize（Task 103 评审扩展） ----

    def test_launcher_config_load_drops_non_dict_presets(self):
        # 改前：lc["presets"] 原样保留脏条目，_current_preset/_save_preset/
        # 删除路径调 p.get(...) 抛 AttributeError（Task 96d 只修了展示侧）。
        cfg = self._write("launcher_config.json",
                          {"presets": [{"name": "p1"}, "junk", 42, None]})
        with mock.patch.object(launcher, "LAUNCHER_CFG_PATH", cfg), \
             mock.patch.object(launcher, "log") as mlog:
            lc = launcher.load_launcher_config()
        self.assertEqual(lc["presets"], [{"name": "p1"}])
        warns = [c for c in mlog.call_args_list if "[警告]" in str(c)]
        self.assertTrue(warns, "应记 warning")

    def test_launcher_config_non_list_presets_becomes_empty(self):
        # presets 本体非 list（如手改成字符串）：_save_preset 的推导式
        # 同样会 p.get 崩；入口统一按空处理。
        cfg = self._write("launcher_config.json", {"presets": "oops"})
        with mock.patch.object(launcher, "LAUNCHER_CFG_PATH", cfg), \
             mock.patch.object(launcher, "log") as mlog:
            lc = launcher.load_launcher_config()
        self.assertEqual(lc["presets"], [])
        warns = [c for c in mlog.call_args_list if "[警告]" in str(c)]
        self.assertTrue(warns, "应记 warning")

    def test_current_preset_safe_after_sanitized_load(self):
        # 改前：_current_preset 遍历到 "junk" 时 p.get 抛 AttributeError。
        # （用不存在的名字，迫使遍历走完整个列表。）
        cfg = self._write("launcher_config.json",
                          {"presets": [{"name": "p1"}, "junk"]})
        with mock.patch.object(launcher, "LAUNCHER_CFG_PATH", cfg), \
             mock.patch.object(launcher, "log"):
            lc = launcher.load_launcher_config()
        app = object.__new__(launcher.LauncherApp)
        app.lc = lc
        cb = mock.Mock()
        cb.get.return_value = "zzz"  # 不存在，迫使遍历经过脏条目
        app.preset_cb = cb
        self.assertIsNone(app._current_preset())  # 改前在此抛 AttributeError

    def test_save_preset_safe_after_sanitized_load(self):
        # 改前：_save_preset 的去重推导式在 "junk".get 处抛 AttributeError。
        cfg = self._write("launcher_config.json",
                          {"presets": [{"name": "p1"}, "junk", 42]})
        with mock.patch.object(launcher, "LAUNCHER_CFG_PATH", cfg), \
             mock.patch.object(launcher, "log"):
            lc = launcher.load_launcher_config()
        app = object.__new__(launcher.LauncherApp)
        app.lc = lc
        app._ui_to_lc = mock.Mock()
        app._save_cfg = mock.Mock()
        app._put_log = mock.Mock()
        cb = mock.Mock()
        cb.get.return_value = "p1"
        app.preset_cb = cb
        app._mp = mock.Mock()
        with mock.patch.object(launcher.simpledialog, "askstring",
                               return_value="np"):
            app._save_preset()  # 改前在此抛 AttributeError
        saved = app._save_cfg.call_args[0][0]
        self.assertTrue(all(isinstance(p, dict) for p in saved["presets"]))

    # ---- 正向对照：干净文件不受影响 ----

    def test_clean_files_unchanged_no_warnings(self):
        gt = self._write("grab_tasks.json",
                         {"tasks": [{"id": "a", "name": "A"}]})
        cfg = self._write("launcher_config.json",
                          {"presets": [{"name": "p1"}]})
        with mock.patch.object(launcher, "GRAB_TASKS_PATH", gt), \
             mock.patch.object(launcher, "LAUNCHER_CFG_PATH", cfg), \
             mock.patch.object(launcher, "log") as mlog:
            tasks = launcher.load_grab_tasks()
            lc = launcher.load_launcher_config()
        self.assertEqual(tasks, [{"id": "a", "name": "A"}])
        self.assertEqual(lc["presets"], [{"name": "p1"}])
        warns = [c for c in mlog.call_args_list if "[警告]" in str(c)]
        self.assertEqual(warns, [])


class TestTask106BareStringHardening(TempDirCase):
    """Task 106：bare-string 相邻硬化（Task 103 评审确认，P3 bundle）。

    1. gui.py:2892 任务对话框 passenger_names 展示逐字符拆 → 预选静默为空
    2. launcher.py:2326/3145/3313/3374 `s in (X.get("seat_types") or [])`
       裸字符串变子串检查（"软卧" in "高级软卧" → True 误勾选）
    3. launcher.py:573-574 Grabber._run seats/names 迭代原始 lc.get（防御性）

    改后：全部用已建立的 _display_seat_types 做单行应用（Task 103 同口径）。
    无 Tk 真机：dialog 内联点（2892/3313）用调用点契约测试（沿 Task 103c 口径）；
    可直接调用的方法（_load_preset/_import_from_main/_sync_from_lc/_run）
    用 object.__new__ + mock 做真实方法测试。
    """

    def _seat_var_mocks(self, choices=("软卧", "高级软卧", "硬卧")):
        set_values = {}
        seat_vars = {}
        for s in choices:
            m = mock.Mock()
            m.set.side_effect = lambda v, s=s: set_values.__setitem__(s, v)
            seat_vars[s] = m
        return seat_vars, set_values

    # ---- (2a) launcher.py:3145 _load_preset（直接方法测试） ----

    def _load_preset_app(self, seat_types):
        app = object.__new__(launcher.LauncherApp)
        app.from_ent = mock.Mock()
        app.to_ent = mock.Mock()
        app.trains_var = mock.Mock()
        app.seat_pri_var = mock.Mock()
        app.date_var = mock.Mock()
        app._put_log = mock.Mock()
        app.seat_vars, set_values = self._seat_var_mocks()
        app._current_preset = mock.Mock(
            return_value={"name": "p1", "seat_types": seat_types})
        return app, set_values

    def test_3145_load_preset_dirty_string_no_substring(self):
        # P3：preset["seat_types"] 为裸字符串 "高级软卧" 时，旧代码
        # s in ("高级软卧" or []) 走子串检查 → "软卧" 被误勾选。
        app, set_values = self._load_preset_app("高级软卧")
        app._load_preset()
        self.assertFalse(set_values["软卧"],
                         "子串误判：'软卧' 不应被 '高级软卧' 误勾选")
        self.assertTrue(set_values["高级软卧"])
        self.assertFalse(set_values["硬卧"])

    def test_3145_load_preset_clean_list_unchanged(self):
        # 正向对照：干净 list 形状预选行为不变（精确匹配）。
        app, set_values = self._load_preset_app(["软卧"])
        app._load_preset()
        self.assertTrue(set_values["软卧"])
        self.assertFalse(set_values["高级软卧"])
        self.assertFalse(set_values["硬卧"])

    # ---- (2b) launcher.py:3374 _import_from_main（直接方法测试） ----

    def test_3374_import_from_main_dirty_string_no_substring(self):
        # P3：app.lc["seat_types"] 为裸字符串 "高级软卧" 时同上子串误判。
        dlg = object.__new__(launcher.NewMonitorTaskDialog)
        app = mock.Mock()
        app.lc = {"seat_types": "高级软卧"}
        app.from_ent.get.return_value = "北京"
        app.to_ent.get.return_value = "上海"
        app.trains_var.get.return_value = "G101"
        app.date_var.get.return_value = "2026-10-10"
        dlg.app = app
        dlg.from_cb = mock.Mock()
        dlg.to_cb = mock.Mock()
        dlg.trains_var = mock.Mock()
        dlg.date_var = mock.Mock()
        dlg.seat_vars, set_values = self._seat_var_mocks()
        dlg._import_from_main()
        self.assertFalse(set_values["软卧"],
                         "子串误判：'软卧' 不应被 '高级软卧' 误勾选")
        self.assertTrue(set_values["高级软卧"])
        self.assertFalse(set_values["硬卧"])

    # ---- (2c) launcher.py:2326 _sync_from_lc（直接方法测试） ----

    def test_2326_sync_from_lc_dirty_string_no_substring(self):
        # P3：lc["seat_types"] 为裸字符串 "高级软卧" 时同上子串误判。
        app = object.__new__(launcher.LauncherApp)
        app.lc = {"seat_types": "高级软卧"}
        app.from_ent = mock.Mock()
        app.to_ent = mock.Mock()
        for attr in ("trains_var", "date_var", "date_to_var", "seat_pri_var",
                     "start_var", "remind_var", "warm_var"):
            setattr(app, attr, mock.Mock())
        app.remind_var.get.return_value = "10"
        app.warm_var.get.return_value = "10"
        app.seat_vars, set_values = self._seat_var_mocks()
        app._put_log = mock.Mock()
        app.preset_cb = mock.Mock()
        app._refresh_pax = mock.Mock()
        app._save_cfg = mock.Mock()
        with mock.patch.object(launcher, "merge_trains_from_monitor",
                               return_value=False):
            app._sync_from_lc()
        self.assertFalse(set_values["软卧"],
                         "子串误判：'软卧' 不应被 '高级软卧' 误勾选")
        self.assertTrue(set_values["高级软卧"])
        self.assertFalse(set_values["硬卧"])

    # ---- (3) launcher.py:573-574 Grabber._run（直接方法测试） ----

    def test_grabber_run_dirty_seat_types_single_not_char_split(self):
        # P3（防御性）：lc["seat_types"] 为裸字符串 "二等座" 时，旧代码
        # [s for s in ("二等座" or [])] 逐字符拆成 ["二","等","座"] →
        # 全部无法下单 → (False, "勾选的席别都无法自动下单")。
        # 改后按单个席别处理，通过席别检查，继续走到车站校验
        # （此处 mock 断网，直接返回车站加载失败）。
        grabber = object.__new__(launcher.Grabber)
        grabber.lc = {"from": "北京", "to": "上海", "date": "2026-10-10",
                      "trains": ["G101"], "seat_types": "二等座",
                      "seat_priority": "", "passenger_names": ["张三"]}
        grabber._log = mock.Mock()
        grabber.result = None
        with mock.patch.object(launcher.ticket, "load_station_map",
                               side_effect=Exception("offline")):
            grabber._run()
        self.assertEqual(grabber.result,
                         (False, "车站数据加载失败（网络异常），请联网后重试"))

    def test_grabber_run_clean_seat_types_unchanged(self):
        # 正向对照：干净 list 形状行为不变（同样走到车站校验）。
        grabber = object.__new__(launcher.Grabber)
        grabber.lc = {"from": "北京", "to": "上海", "date": "2026-10-10",
                      "trains": ["G101"], "seat_types": ["二等座"],
                      "seat_priority": "", "passenger_names": ["张三"]}
        grabber._log = mock.Mock()
        grabber.result = None
        with mock.patch.object(launcher.ticket, "load_station_map",
                               side_effect=Exception("offline")):
            grabber._run()
        self.assertEqual(grabber.result,
                         (False, "车站数据加载失败（网络异常），请联网后重试"))

    # ---- (1) gui.py:2892 调用点契约（dialog 内联，沿 Task 103c 口径） ----

    def test_gui_2892_passenger_names_dirty_string_preselects(self):
        # P3：task["passenger_names"] 为裸字符串 "张三" 时，旧表达式
        # for n in ("张三" or []) 逐字符迭代 → "张" in psg_items 永假 →
        # 预选静默为空。调用点改后表达式应正确预选。
        task = {"passenger_names": "张三"}
        psg_items = ["张三", "李四"]
        selected = [n for n in gui._display_seat_types(task.get("passenger_names"))
                    if n in psg_items]
        self.assertEqual(selected, ["张三"])
        # 正向对照：干净 list 行为不变。
        task2 = {"passenger_names": ["张三", "李四"]}
        selected2 = [n for n in gui._display_seat_types(task2.get("passenger_names"))
                     if n in psg_items]
        self.assertEqual(selected2, ["张三", "李四"])

    def test_display_helper_passenger_names_contract(self):
        # 2892/576 调用点依赖的契约：_display_seat_types 用于 passenger_names
        # 时，裸字符串按单个姓名处理（中文名不 strip/upper，与席别同口径）。
        self.assertEqual(gui._display_seat_types("张三"), ["张三"])
        self.assertEqual(launcher._display_seat_types("张三"), ["张三"])
        self.assertEqual(launcher._display_seat_types(["张三", "李四"]),
                         ["张三", "李四"])
        self.assertEqual(launcher._display_seat_types(None), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)



