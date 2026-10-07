# -*- coding: utf-8 -*-
"""
监控引擎模块：多任务调度、优先级、自适应频率、防重复下单、状态持久化、日志与历史记录。

设计要点
    1. 任务调度：所有"监控中"状态的任务按各自频率独立轮询，每轮按优先级从高到低执行
    2. 轮询频率：adaptive 关闭时直接用 poll_interval_seconds；开启时按
       基准 × 时段系数(高峰/非高峰) × 优先级系数，且不低于 min_interval_seconds 下限
    3. 防重复下单（三层）：
       a. 本地 state.json 记录 区间|日期|车次|席别|乘车人组合
       b. 下单前查询账号未完成订单 + 目标日期已完成订单（order.py）
       c. 下单成功或判定为账号重复后，任务按 stop_after_order 停止，避免反复购买
    4. 断点续连：状态、已下单记录、历史均落盘，重启后监控中的任务直接恢复
    5. 稳定性：查询网络异常自动重试+指数退避，连续失败自动拉长轮询间隔，恢复后自动还原；
       登录会话定期体检，失效时相关任务标记"已失败"并提示重新登录
    6. 日志：控制台 + logs/ 下按天滚动文件，记录所有轮询、命中、下单尝试与状态变化

不做什么
    - 不支付、不绕过验证码（滑块出现时中止并在日志/控制台明确提示）

用法
    一般不直接运行；由 monitor.py 的交互菜单或 `python monitor.py run` 调用。
"""

import copy
import datetime
import json
import logging
import os
import sys
import time
import threading

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import appcommon
import filelock
import notify as notify_mod
import order as order_mod
import ticket
import logutil

HERE = os.path.dirname(os.path.abspath(__file__))

# 轮询间隔硬底线：低于该值会被提升，避免配置成 0 时无间隔空转打爆请求
MIN_INTERVAL_FLOOR = 1

STATUS_LABELS = {
    "monitoring": "监控中",
    "retrying": "等待重试",
    "paused": "已暂停",
    "cancelled": "已取消",
    "success": "已成功",
    "failed": "已失败",
}
STATUS_COLORS = {}  # 预留；控制台不引入颜色库

ACTIVE_STATUSES = ("monitoring", "retrying")

DEDUPE_VALUES = {
    "SUBMITTED": "已提交订单(未支付)",
    "ACCOUNT_DUP": "账号已有订单(防重跳过)",
    "NO_ORDER": "已记录(不自动下单)",
    "MANUAL": "人工确认",
    # 网页端下单页不下发该席别（如普速车的「无座」）：重试无用，永久跳过
    "SEAT_UNAVAILABLE": "网页端不提供该席别(已跳过)",
}

LOG = logging.getLogger("monitor")


def expand_dates(task):
    """把 dates + date_range 展开成日期列表（去重保序）。"""
    result = list(task.get("dates") or [])
    dr = task.get("date_range")
    if dr and len(dr) == 2:
        d0 = datetime.date.fromisoformat(dr[0])
        d1 = datetime.date.fromisoformat(dr[1])
        if d1 >= d0:
            d = d0
            while d <= d1:
                result.append(d.isoformat())
                d += datetime.timedelta(days=1)
    seen, uniq = set(), []
    for x in result:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def future_dates(task, today=None):
    """只保留今天及以后的日期；返回 (未来日期列表, 已过期数量)。"""
    today = today or datetime.date.today()
    dates = expand_dates(task)
    kept, expired = [], 0
    for d in dates:
        try:
            if datetime.date.fromisoformat(d) >= today:
                kept.append(d)
            else:
                expired += 1
        except ValueError:
            expired += 1
    return kept, expired


def dedup_key(task, date, train_code, seat_name, passenger_names):
    """防重复唯一键：区间|日期|车次|席别|乘车人组合（排序后拼接）。"""
    names = "|".join(sorted(passenger_names)) if passenger_names else "*"
    return "{0}|{1}|{2}|{3}|{4}|{5}".format(
        task["from"], task["to"], date, train_code, seat_name, names)


class MonitorEngine(object):
    def __init__(self, config_path=None, setup_logging=True):
        """setup_logging=False 时由调用方（如 GUI）自行接管日志处理器。"""
        self.config_path = config_path or os.path.join(HERE, "config.json")
        with open(self.config_path, encoding="utf-8") as f:
            self.config = json.load(f)
        try:
            self._config_mtime = os.path.getmtime(self.config_path)
        except OSError:
            self._config_mtime = None

        self.state_path = os.path.join(HERE, self.config.get("state_file", "state.json"))
        self.history_path = os.path.join(HERE, self.config.get("history_file", "order_history.json"))
        self.state = self._load_state()
        try:
            self._state_mtime = os.path.getmtime(self.state_path)
        except OSError:
            self._state_mtime = None

        self.name2code, self.code2name = ticket.load_station_map()
        self.tasks = self.config.get("tasks") or []

        self.base_interval = int(self.config.get("poll_interval_seconds", 45))
        self.min_interval = max(MIN_INTERVAL_FLOOR,
                                int(self.config.get("min_interval_seconds", 30)))
        if self.min_interval < 15:
            LOG.info("min_interval_seconds=%s：轮询较激进，注意 12306 限流/封 IP 风险",
                     self.min_interval)

        if setup_logging:
            self._setup_logging()
        self._ensure_task_names()
        self._resume_or_init_status()

    # ----------------------------- 日志 -----------------------------

    def _setup_logging(self):
        log_dir = os.path.join(HERE, self.config.get("log_dir", "logs"))
        os.makedirs(log_dir, exist_ok=True)
        # 按天滚动：长跑跨天后日志自动切到新日期的文件
        fh = logutil.DayFileHandler(log_dir, "monitor")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        LOG.setLevel(logging.INFO)
        for h in list(LOG.handlers):
            LOG.removeHandler(h)
        LOG.addHandler(fh)
        LOG.addHandler(sh)
        LOG.propagate = False

    # ----------------------------- 状态持久化 -----------------------------

    def _load_state(self):
        state, err = appcommon.read_state_or_none(self.state_path)
        state_ok = err is None
        if err is not None:
            LOG.warning("state.json 读取失败: %s", err)
            # 读不出来 ≠ 空状态：挪档留证（时间戳名，见 appcommon.quarantine_corrupt），
            # 再从空状态重建。丢 state 就是丢防重（dedup）记录，理论上会
            # 重复下单——必须醒目提示去核对在途行程。
            bad = appcommon.quarantine_corrupt(self.state_path)
            if bad is None:
                # 挪不动（如杀毒软件占用）：本次不落盘，免得下面的
                # _save_state 把仅存的坏档覆盖掉
                LOG.warning("坏档挪移失败（文件被占用？），本次不落盘以保留证据")
            else:
                LOG.warning(
                    "坏档已挪为 %s，从空状态重建。其它任务的运行状态与"
                    "防重记录都在坏档里——请尽快到 12306「未支付订单」"
                    "核对在途行程，避免重复下单", bad)
            state = {}
        # 兼容旧版：把平铺的 "区间|日期|车次|席别" 键迁到 dedup 下
        if "dedup" not in state:
            dedup, tasks = {}, {}
            for k, v in state.items():
                if isinstance(k, str) and "|" in k:
                    dedup[k] = v
                else:
                    tasks[k] = v
            state = {"dedup": dedup, "tasks": tasks}
        state.setdefault("dedup", {})
        state.setdefault("tasks", {})
        state.setdefault("retry", {})
        if state_ok:
            self._save_state(state)
        return state

    def _save_state(self, state=None):
        if state is None:
            state = self.state
        self.state = state
        lock = getattr(self, "_save_lock", None)
        if lock is None:  # 兼容未初始化锁的旧实例
            lock = self._save_lock = threading.Lock()
        with lock:  # 同一实例内跨线程（GUI/引擎）串行化写入
            # 用副本序列化：避免跨线程（GUI 操作与引擎线程）修改同一字典导致异常
            snapshot = None
            for _ in range(3):
                try:
                    snapshot = copy.deepcopy(state)
                    break
                except RuntimeError:  # 字典并发修改导致的迭代异常
                    time.sleep(0.02)
            if snapshot is None:
                snapshot = copy.deepcopy(dict(state))
            # 原子写入：先写临时文件再替换，避免两线程同时写坏 state.json
            # 临时名/重试/直写兜底语义由 appcommon 参数化保留；
            # 跨进程锁与 launcher.append_monitor_task 的 state 段互斥
            with filelock.file_lock(self.state_path + ".lock"):
                appcommon.atomic_write_json(self.state_path, snapshot,
                                            fallback_direct=True)
        try:
            self._state_mtime = os.path.getmtime(self.state_path)
        except OSError:
            pass
        return self.state

    def _reload_state(self):
        """仅重新读取 state.json（供运行中的引擎同步外部修改，不触发写入）。"""
        state = {}
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, encoding="utf-8") as f:
                    state = json.load(f)
            except Exception:
                state = {}
        state.setdefault("dedup", {})
        state.setdefault("tasks", {})
        state.setdefault("retry", {})
        return state

    def _sync_state(self):
        """检测 state.json 是否被外部（GUI/其他进程）修改，有则重新加载。"""
        try:
            m = os.path.getmtime(self.state_path)
        except OSError:
            return
        if m != self._state_mtime:
            self.state = self._reload_state()
            self._state_mtime = m

    def _sync_config(self):
        """检测 config.json 是否被修改（GUI 增删任务）：有则热更新任务列表。
        返回 True 表示任务列表可能变化，主循环需重建调度表。"""
        try:
            m = os.path.getmtime(self.config_path)
        except OSError:
            return False
        if m == getattr(self, "_config_mtime", None):
            return False
        self._config_mtime = m
        try:
            with open(self.config_path, encoding="utf-8") as f:
                self.config = json.load(f)
        except Exception as e:
            LOG.warning("[配置] config.json 重新读取失败：%s", e)
            return False
        self.base_interval = int(self.config.get("poll_interval_seconds", 45))
        self.min_interval = max(MIN_INTERVAL_FLOOR,
                                int(self.config.get("min_interval_seconds", 30)))
        self.tasks = self.config.get("tasks") or []
        self._ensure_task_names()
        self._resume_or_init_status()  # 仅补缺省状态，不覆盖已有状态
        LOG.info("[配置] 已同步任务列表，共 %d 个任务", len(self.tasks))
        return True

    def _prune_retry_map(self, retry_map, max_entries=200):
        """冷却表防膨胀：清除过期条目；仍过多则裁掉最旧的。"""
        if len(retry_map) <= max_entries:
            return
        now = time.time()
        for k in [k for k, v in retry_map.items() if v < now - 3600]:
            del retry_map[k]
        if len(retry_map) > max_entries:
            for k in sorted(retry_map, key=retry_map.get)[:len(retry_map) - max_entries]:
                del retry_map[k]

    def _ensure_task_names(self):
        for i, t in enumerate(self.tasks):
            if not t.get("name"):
                t["name"] = "任务{0}".format(i + 1)

    def _resume_or_init_status(self):
        """重启后：保留上次状态；新任务默认"已暂停"（未启动）。
        只有用户在界面勾选启动/恢复后才进入监控，避免"未启动却还在监控"。"""
        for t in self.tasks:
            name = t["name"]
            entry = self.state["tasks"].setdefault(name, {})
            if "status" not in entry:
                entry["status"] = "paused"
                entry["fail_streak"] = 0
                entry["last_poll"] = 0
                entry["message"] = "新建任务，未启动"
        self._save_state()

    # ----------------------------- 状态读写（供 CLI 使用） -----------------------------

    def task_status(self, task):
        name = task["name"]
        return self.state["tasks"].get(name, {}).get("status", "paused")

    def set_task_status(self, task, status, message="", force=False):
        name = task["name"]
        entry = self.state["tasks"].setdefault(name, {})
        cur = entry.get("status", "monitoring")
        # 用户主动暂停/取消的状态不允许被引擎例行写入覆盖：
        # 暂停可能在耗时 API 调用期间发生，调用返回后引擎如果把状态改回
        # retrying/monitoring 会导致"已暂停但仍在监控"且无法编辑。
        # GUI 的恢复/重置/取消等明确操作通过 force=True 显式覆盖。
        if not force and cur in ("paused", "cancelled") \
                and status not in ("paused", "cancelled"):
            LOG.info("[状态] 任务 %s 已 %s，忽略覆盖为 %s %s",
                     name, STATUS_LABELS.get(cur, cur),
                     STATUS_LABELS.get(status, status), message)
            return
        entry["status"] = status
        if message:
            entry["message"] = message
        self._save_state()
        LOG.info("[状态] 任务 %s -> %s %s", name, STATUS_LABELS.get(status, status), message)

    def empty_task_dedup(self, task):
        """清空某任务的本地防重记录（状态重置后重新抢同一张票时用）。"""
        prefix = "{0}|{1}|".format(task["from"], task["to"])
        for k in list(self.state["dedup"].keys()):
            if k.startswith(prefix):
                del self.state["dedup"][k]
        self._save_state()

    # ----------------------------- 自适应频率 -----------------------------

    @staticmethod
    def _soonest_date(task):
        dates = expand_dates(task)
        return min(dates) if dates else None

    def task_interval(self, task):
        """计算任务的有效轮询间隔（秒）。"""
        ad = self.config.get("adaptive") or {}
        iv = float(self.base_interval)
        if ad.get("enabled", True):
            hour = datetime.datetime.now().hour
            peak = ad.get("peak_hours") or [6, 23]
            mult = ad.get("peak_multiplier", 1.0) if peak[0] <= hour < peak[1] \
                else ad.get("offpeak_multiplier", 1.6)
            rush_hours = ad.get("rush_within_hours", 24)
            if rush_hours:
                soon = self._soonest_date(task)
                if soon:
                    try:
                        delta_h = (datetime.datetime.fromisoformat(soon)
                                   - datetime.datetime.now()).total_seconds() / 3600.0
                        if 0 <= delta_h <= rush_hours:
                            mult = min(mult, ad.get("rush_multiplier", 0.75))
                    except ValueError:
                        pass
            try:
                prio = int(task.get("priority") or 5)
            except (TypeError, ValueError):
                prio = 5
            prio = min(max(prio, 1), 10)
            prio_factor = 1.15 - 0.03 * prio  # 优先级越高因子越小 => 轮询越勤
            iv = iv * mult * prio_factor
        return max(self.min_interval, iv)

    # ----------------------------- 通知与历史 -----------------------------

    def _notify(self, task, subject, body):
        channels = task.get("notify_channels") or ["email"]
        results = {}
        if "email" in channels:
            email_cfg = (self.config.get("notify") or {}).get("email")
            if email_cfg and email_cfg.get("enabled"):
                ok, msg = notify_mod.send_email(email_cfg, subject, body)
                results["email"] = (ok, msg)
                if not ok:
                    LOG.error("[通知失败] %s", msg)
        return results

    def _append_history(self, record):
        # 加锁串行化 + 原子写：与 _save_state 同一范式，防止并发追加丢记录、
        # 写一半崩溃截断 order_history.json
        lock = getattr(self, "_history_lock", None)
        if lock is None:  # 兼容未初始化锁的旧实例
            lock = self._history_lock = threading.Lock()
        with lock:
            history = []
            if os.path.exists(self.history_path):
                try:
                    with open(self.history_path, encoding="utf-8") as f:
                        history = json.load(f)
                except Exception:
                    history = []
            history.append(record)
            appcommon.atomic_write_json(self.history_path, history[-500:])

    # ----------------------------- 单任务扫描 -----------------------------

    def _run_task(self, task):
        """执行一个任务的一轮扫描。返回 (退出循环标志, 是否发生可恢复异常)。"""
        name = task["name"]
        entry = self.state["tasks"].setdefault(name, {})
        entry["fail_streak"] = entry.get("fail_streak", 0)
        entry["last_poll"] = time.time()
        self._save_state()

        if self.task_status(task) not in ACTIVE_STATUSES:
            return False, False

        from_code = self.name2code.get(task["from"])
        to_code = self.name2code.get(task["to"])
        if not from_code or not to_code:
            self.set_task_status(task, "failed", "车站名称无法识别: %s -> %s" % (task["from"], task["to"]))
            return False, False

        dates, expired = future_dates(task)
        if not dates:
            self.set_task_status(task, "failed", "监控日期已全部过期，任务自动停止")
            return False, False

        trains = [t.strip() for t in (task.get("trains") or []) if t.strip()]
        seats_want_all = [s for s in (task.get("seat_types") or [])]
        seats_by_date = task.get("seats_by_date") or {}
        auto_order = bool(task.get("auto_order", True))
        stop_after = bool(task.get("stop_after_order", True))

        hit_any = False
        for date in dates:
            # 按日独立席别：seats_by_date 里有的日期用当天的列表；没有的用全局 seat_types
            seats_want = (seats_by_date[date] if date in seats_by_date
                          and isinstance(seats_by_date.get(date), list)
                          else seats_want_all)
            if date in seats_by_date and not seats_want:
                # 该日未勾选任何席别：跳过（不查询不下单）
                continue
            try:
                rows = self._query_with_retry(from_code, to_code, date,
                                              task.get("purpose_code") or "ADULT")
            except Exception as e:
                self._note_failure(task, "查询异常(%s): %s" % (date, e))
                return True, True  # 网络类异常：本任务退避，实现自动恢复
            for row in rows:
                info = ticket.parse_row(row, self.code2name, date)
                train_code = info["train_code"]
                if trains and train_code not in trains:
                    continue
                avail = info["available_seats"]
                if not avail:
                    continue
                # 每趟车各自的席别候选（唯一口径 ticket.seat_candidates_for）：
                # 「车次=席别」专属规则 ∩ 当日勾选集，交集空=该车跳过（不回退
                # 全局）；无规则的车 = 首选在前 + 勾选顺序；两者皆空 = 不限席别。
                # 候选永远与该车实际有票求交。
                cand = ticket.seat_candidates_for(
                    train_code, seats_want, task.get("seat_priority") or [])
                hit_seats = [s for s in cand if s in avail]
                if not hit_seats:
                    continue
                for seat_name in hit_seats:
                    hit_any = True
                    LOG.info("[有票] 任务「%s」%s %s %s->%s %s 有余票(码%s)",
                             name, date, train_code, info["from_name"], info["to_name"],
                             seat_name, avail.get(seat_name))
                    # 先不查乘车人，直接用任务配置的乘车人组合做历史查重键
                    p_names = sorted(task.get("passenger_names") or []) or ["(账号默认)"]
                    key = dedup_key(task, date, train_code, seat_name,
                                    task.get("passenger_names") or [])
                    if key in self.state["dedup"]:
                        LOG.info("[跳过] 任务「%s」已有记录：%s", name,
                                 DEDUPE_VALUES.get(self.state["dedup"][key], self.state["dedup"][key]))
                        continue
                    if not auto_order:
                        # 未开启自动下单：只报警不下单，供人工在 App/网页快速购买
                        self.state["dedup"][key] = "NO_ORDER"
                        self._save_state()
                        subject = "[有票] {0} {1} {2}->{3} {4}".format(
                            train_code, date, info["from_name"], info["to_name"], seat_name)
                        body = (
                            "监控到余票（本任务未开启自动下单，请人工尽快购买）：\n\n"
                            "车次：{train}\n日期：{date}\n区间：{_from} -> {_to}\n"
                            "发车：{start}  到达：{arrive}\n席别：{seat}  余票：{num}\n\n"
                            "余票随时可能被抢走，请立即在 12306 App / 网页下单。\n"
                        ).format(train=train_code, date=date, _from=info["from_name"],
                                 _to=info["to_name"], start=info["start_time"],
                                 arrive=info["arrive_time"], seat=seat_name,
                                 num=avail.get(seat_name, ""))
                        notify_results = self._notify(task, subject, body)
                        notify_txt = "; ".join("{0}:{1}".format(k, "成功" if nok else msg)
                                               for k, (nok, msg) in notify_results.items()) or "无通知渠道"
                        LOG.info("[有票] 任务「%s」%s 有余票，已通知：%s", name, seat_name, notify_txt)
                        self._append_history({
                            "time": self._now(), "task": name, "result": "hit_no_order",
                            "train": train_code, "date": date, "from": info["from_name"],
                            "to": info["to_name"], "seat": seat_name,
                            "passengers": p_names, "order_no": "",
                            "message": "余票命中（任务未开启自动下单）", "notify": notify_txt,
                        })
                        continue
                    # 系统忙冷却：避免对同一目标过于频繁地下单请求（防止 IP 被限）
                    retry_map = self.state.setdefault("retry", {})
                    due_at = retry_map.get(key, 0)
                    if due_at and time.time() < due_at:
                        LOG.info("[冷却] 任务「%s」上次下单未成需等待（系统忙/改判席别此刻不可售），%d 秒后自动重试",
                                 name, int(due_at - time.time()))
                        continue
                    if due_at:
                        # 冷却到期：恢复为监控中，本轮正常尝试下单
                        retry_map.pop(key, None)
                        if self.task_status(task) == "retrying":
                            self.set_task_status(task, "monitoring", "冷却结束，恢复下单尝试")
                    # 自动下单
                    result_msg, extra = self._order_once(task, info, date, seat_name)
                    if result_msg == "ok":
                        retry_map.pop(key, None)
                        self.state["dedup"][key] = "SUBMITTED"
                        self._save_state()
                        self._record_success(task, info, date, seat_name, extra)
                        if stop_after:
                            self.set_task_status(task, "success",
                                "已成功下单 %s %s %s %s" % (date, train_code, seat_name,
                                extra.get("passengers") if extra else ""))
                            return True, False
                        if self.task_status(task) == "retrying":
                            self.set_task_status(task, "monitoring", "下单成功，恢复正常监控")
                        return True, False  # 本轮不再继续（避免同轮重复下单）
                    elif result_msg == "dup":
                        # 本地记录仅作线索:以 12306 官方接口的订单状态为准决策
                        cls, ono, raw = order_mod.classify_order_status(
                            date, train_code, task.get("passenger_names") or [])
                        okey = "%s|%s" % (date, train_code)
                        LOG.info("[防重核查] 任务「%s」%s %s 官方查询结果=%s %s",
                                 name, date, train_code, cls,
                                 ("订单号 %s,官方状态「%s」" % (ono, raw)) if ono else raw)
                        decision = {"unpaid": "待支付:任务停止,请尽快支付",
                                    "paid": "已支付:判定为已购得,任务停止",
                                    "cancelled": "已取消:清除本地防重记录,允许重新下单",
                                    "none": "官方无此订单:清除本地记录,允许重新下单",
                                    "unknown": "官方状态不明确:保留本地记录,下轮再核",
                                    "error": "官方查询失败:保留本地记录,下轮再核"}[cls]
                        appcommon.upsert_order(
                            os.path.join(HERE, "orders.json"), okey,
                            {"order_no": ono, "train": train_code, "date": date,
                             "from": info["from_name"], "to": info["to_name"],
                             "seat": seat_name, "passengers": p_names,
                             "official_status": raw, "classify": cls,
                             "decision": decision, "source": "engine"})
                        self._append_history({
                            "time": self._now(), "task": name, "result": "dup",
                            "train": train_code, "date": date, "from": info["from_name"],
                            "to": info["to_name"], "seat": seat_name,
                            "passengers": p_names, "order_no": ono,
                            "message": "官方核验:%s(%s)。决策:%s" % (raw or cls, cls, decision),
                            "notify": "未通知",
                        })
                        LOG.info("[防重决策] 任务「%s」%s", name, decision)
                        if cls == "paid":
                            self.state["dedup"][key] = "SUBMITTED"
                            self._save_state()
                            self.set_task_status(task, "success",
                                                 "官方确认已支付,订单 %s" % (ono or "未知"))
                            return True, False
                        if cls == "unpaid":
                            self.state["dedup"][key] = "ACCOUNT_DUP"
                            self._save_state()
                            if bool(task.get("stop_after_order", True)):
                                self.set_task_status(task, "success",
                                    "存在待支付订单 %s——请尽快支付" % (ono or "未知"))
                                return True, False
                            continue
                        if cls in ("cancelled", "none"):
                            self.state["dedup"].pop(key, None)
                            self._save_state()
                            continue
                        continue  # unknown/error:保留本地记录,下轮再核
                    else:
                        msg = (extra or {}).get("msg", "")
                        if (extra or {}).get("reason") == "ambiguous":
                            # 提交确认后结果未知：订单可能已在服务端生成，继续自动
                            # 重试有重复下单风险——停任务交人工核对
                            self.set_task_status(task, "failed",
                                "订单提交后结果未知——请先到 12306「未支付订单」核对："
                                "有单就支付/取消，确认无单后再恢复本任务")
                            LOG.error("[警告] 任务「%s」%s %s 提交后结果未知，已停止自动重试",
                                      name, date, train_code)
                            self._append_history({
                                "time": self._now(), "task": name, "result": "ambiguous",
                                "train": train_code, "date": date, "from": info["from_name"],
                                "to": info["to_name"], "seat": seat_name,
                                "passengers": p_names, "order_no": "",
                                "message": msg, "notify": "",
                            })
                            return True, False
                        if (extra or {}).get("reason") == "seat_unavailable":
                            # 确认页可售席别由 12306 服务端下发（普速车常常没有「无座」），
                            # 换时间点重试也不会有：记入 dedup 永久跳过，别无限重试刷日志
                            self.state["dedup"][key] = "SEAT_UNAVAILABLE"
                            self._save_state()
                            self._append_history({
                                "time": self._now(), "task": name, "result": "seat_unavailable",
                                "train": train_code, "date": date, "from": info["from_name"],
                                "to": info["to_name"], "seat": seat_name,
                                "passengers": p_names, "order_no": "",
                                "message": msg, "notify": "",
                            })
                            that = (extra or {}).get("seat_options") or "未知"
                            entry["message"] = "席别不可选：%s %s（服务端只下发 %s）" % (
                                train_code, seat_name, that)
                            LOG.warning("[跳过] 任务「%s」%s %s %s 在网页端下单页不可选"
                                        "（服务端只下发：%s），已加入跳过名单；"
                                        "如需该席位请改勾其他席别或换车次",
                                        name, date, train_code, seat_name, that)
                            continue
                        if (extra or {}).get("reason") == "alias_no_stock":
                            # 勾选无座→同价改判硬座，但硬座此刻不可售：属车次状态问题，
                            # 冷确一段时间再试，别按普通失败每轮刷提交请求
                            try:
                                cooldown = max(5, int(self.config.get(
                                    "order_retry_cooldown_seconds", 7)))
                            except (TypeError, ValueError):
                                cooldown = 7
                            retry_map[key] = time.time() + cooldown
                            self._prune_retry_map(retry_map)
                            LOG.info("[等待] 任务「%s」%s %s 勾选 %s、改判 %s 此刻不可售，%d 秒后重试",
                                     name, date, train_code, seat_name,
                                     (extra or {}).get("alias_seat") or "?", cooldown)
                            continue
                        if "会话已失效" in msg or "未找到会话文件" in msg:
                            self.set_task_status(task, "failed",
                                "登录会话不可用，请重新运行 capture_session.py 登录后恢复任务")
                            LOG.error("[错误] 任务「%s」抢票失败：%s", name, msg)
                            return True, False
                        if order_mod.is_busy_error(msg):
                            # 系统忙：进入"等待重试"状态 + 冷却退避，避免刷请求与刷日志
                            try:
                                cooldown = max(3, int(self.config.get("order_retry_cooldown_seconds", 7)))
                            except (TypeError, ValueError):
                                cooldown = 7
                            retry_map[key] = time.time() + cooldown
                            entry["busy_count"] = entry.get("busy_count", 0) + 1
                            self._prune_retry_map(retry_map)
                            self.set_task_status(
                                task, "retrying",
                                "下单遇系统忙（累计 %d 次），%d 秒后自动重试"
                                % (entry["busy_count"], cooldown))
                            continue
                        LOG.error("[错误] 任务「%s」抢票失败：%s", name, msg)
                        self._append_history({
                            "time": self._now(), "task": name, "result": "failed",
                            "train": train_code, "date": date, "from": info["from_name"],
                            "to": info["to_name"], "seat": seat_name,
                            "passengers": p_names, "order_no": "",
                            "message": msg, "notify": "",
                        })
                        continue  # 临时性失败（无票/排队失败），下轮继续

        # 本轮扫完：正常则清零失败计数
        if not hit_any:
            entry["fail_streak"] = 0
            entry["message"] = "上次查询 {0}：无余票".format(time.strftime("%H:%M:%S"))
            self._save_state()
        return False, False

    def _order_once(self, task, info, date, seat_name):
        """调用 order_ticket，把返回统一成 (结果标识, extra)。"""
        try:
            ok, msg, extra = order_mod.order_ticket(self.config, task, info, seat_name)
        except Exception as e:
            return "fail", {"msg": "下单异常: {0}".format(e)}
        if ok:
            return "ok", extra or {}
        if extra and extra.get("reason") == "dup":
            return "dup", extra
        # 保留下单层返回的机器可读信息（reason / seat_codes / seat_options / need_captcha…），
        # 否则这些线索在上层被丢掉，只剩一句人类文案，无法做分类处理
        packed = dict(extra) if isinstance(extra, dict) else {}
        packed["msg"] = msg
        return "fail", packed

    def _record_success(self, task, info, date, seat_name, extra):
        name = task["name"]
        order_no = extra.get("order_no") or ""
        alias_seat = extra.get("alias_seat") or ""
        # 勾选无座→网页端同价按硬座出票：通知与历史里要如实写清楚
        seat_display = seat_name + ("（勾选 %s，网页端同价按 %s 出票）" % (
            extra.get("selected_seat") or seat_name, alias_seat) if alias_seat else "")
        subject = "[购票成功] {0} {1} 有余票且已提交订单".format(info["train_code"], date)
        body = (
            "已自动提交订单（未支付，请在 12306 45 分钟内完成支付）：\n\n"
            "车次：{train}\n日期：{date}\n区间：{_from} -> {_to}\n"
            "发车：{start}  到达：{arrive}\n席别/座位类型：{seat}\n（座位号以出票后为准）\n"
            "乘车人：{passengers}\n"
        ).format(train=info["train_code"], date=date, _from=info["from_name"],
                 _to=info["to_name"], start=info["start_time"],
                 arrive=info["arrive_time"], seat=seat_display,
                 passengers=extra.get("passengers", ""))
        if order_no:
            body += "订单号：{0}\n".format(order_no)
        body += "\n（本条由监控系统自动发送）"
        notify_results = self._notify(task, subject, body)
        notify_txt = "; ".join("{0}:{1}".format(k, "成功" if ok else msg)
                               for k, (ok, msg) in notify_results.items()) or "无通知渠道"
        self._append_history({
            "time": self._now(), "task": name, "result": "success",
            "train": info["train_code"], "date": date, "from": info["from_name"],
            "to": info["to_name"], "seat": seat_name,
            "passengers": (extra.get("passengers") or "").split("、") if extra else [],
            "order_no": order_no,
            "message": "订单提交成功（未支付）" + ("，勾选 %s 同价按 %s 出票" % (
                extra.get("selected_seat") or seat_name, alias_seat) if alias_seat else ""),
            "notify": notify_txt,
        })
        LOG.info("[抢到] 任务「%s」已提交订单：%s %s %s 乘车人:%s 订单号:%s",
                 name, date, info["train_code"], seat_display,
                 extra.get("passengers", ""), order_no or "未知")

    def _note_failure(self, task, message):
        name = task["name"]
        entry = self.state["tasks"].setdefault(name, {})
        entry["fail_streak"] = entry.get("fail_streak", 0) + 1
        entry["message"] = "{0}（连续失败 {1} 次，将自动退避重试）".format(message, entry["fail_streak"])
        self._save_state()
        LOG.warning("[错误] 任务「%s」%s", name, entry["message"])

    def _query_with_retry(self, from_code, to_code, date, purpose="ADULT",
                          tries=3, backoff=2.0):
        """查询余票，网络异常自动重试（指数退避），全部失败抛异常。"""
        last = None
        for i in range(tries):
            try:
                return ticket.query_tickets(from_code, to_code, date, purpose)
            except Exception as e:
                last = e
                if i < tries - 1:
                    wait = backoff * (i + 1)
                    LOG.warning("    [查询重试 %d/%d] %s，%ss 后重试", i + 1, tries - 1, e, wait)
                    time.sleep(wait)
        raise last

    @staticmethod
    def _now():
        return time.strftime("%Y-%m-%d %H:%M:%S")

    # ----------------------------- 会话体检 -----------------------------

    def check_session_if_needed(self):
        """定期检查登录会话；确认失效时把开启自动下单的监控中任务标记为失败。
        临时故障（网络异常/系统繁忙页）只告警不杀任务，稍后自动重试。"""
        now = time.time()
        mark = getattr(self, "_last_session_check", 0)
        if now - mark < 1200:  # 20 分钟一次
            return
        self._last_session_check = now
        auto_tasks = [t for t in self.tasks if t.get("auto_order", True)
                      and self.task_status(t) in ACTIVE_STATUSES]
        if not auto_tasks:
            return
        browser_mode = (self.config.get("order_mode") or "http") == "browser"
        if browser_mode:
            # 浏览器模式下会话由 .browser_profile 承载，必须用真实页面校验；
            # 继续验 session_cookies.json 只会得到已失效的旧结果。
            try:
                import browser_order
                if browser_order.busy():
                    # 登录窗口/下单正在跑：这一轮不抢锁，1 分钟后再体检
                    self._last_session_check = now - 1140
                    return
                ok, who = browser_order.check_session(timeout=6)
                permanent = not ok
            except Exception as e:
                ok, who, permanent = False, "浏览器会话校验异常: %s" % str(e)[:100], False
        else:
            cookie_path = self.config.get("session_cookies_file", "session_cookies.json")
            try:
                s = order_mod.load_session(cookie_path)
                ok, who, permanent = order_mod.verify_session(s)
            except Exception as e:
                ok, who, permanent = False, str(e), False
        if ok:
            if not browser_mode:
                # 浏览器模式的会话在 profile 里，没有 cookie 需要回写
                try:
                    order_mod.save_session(s, cookie_path)  # 回写轮换后的 Cookie，延长有效期
                except Exception:
                    pass
            LOG.info("[会话] 登录状态正常：%s", who)
            # 会话恢复：自动把因会话失效而失败的任务重新拉起
            for t in self.tasks:
                st = self.state["tasks"].get(t["name"], {})
                if st.get("status") == "failed" and "登录会话失效" in st.get("message", ""):
                    self.set_task_status(t, "monitoring", "会话已恢复，自动重新监控")
        elif permanent:
            LOG.error("[错误] 登录会话已失效：%s", who)
            LOG.error("[错误] 以下任务的抢票已停止：%s",
                      "、".join("「%s」" % t["name"] for t in auto_tasks))
            LOG.error("[错误] 请重新登录后再恢复任务")
            for t in auto_tasks:
                self.set_task_status(t, "failed", "登录会话失效，请重新登录后恢复任务")
        else:
            # 临时故障：不标记失败，5 分钟后再校验，监控继续
            self._last_session_check = now - 900
            LOG.warning("[会话] 登录校验临时失败（非登录失效）：%s；监控继续，5 分钟后自动重试" % who)

    # ----------------------------- 主循环 -----------------------------

    def run(self, stop_event=None):
        """主循环。stop_event: threading.Event，置位后本轮结束即优雅退出（GUI 内嵌用）。"""
        LOG.info("=" * 64)
        LOG.info("监控引擎启动")
        # 启动恢复:从 orders.json 载入未完成订单上下文,并逐一用官方接口复核
        try:
            op = os.path.join(HERE, "orders.json")
            odb = appcommon.load_orders(op)
            for okey, rec in (odb.get("orders") or {}).items():
                if rec.get("classify") != "unpaid":
                    continue
                LOG.info("[订单恢复] 待支付订单 %s %s %s(订单号 %s)——重新核验官方状态",
                         rec.get("train"), rec.get("date"), rec.get("seat"), rec.get("order_no"))
                cls, ono, raw = order_mod.classify_order_status(
                    rec.get("date"), rec.get("train"), rec.get("passengers"))
                rec.update({"classify": cls, "official_status": raw,
                            "order_no": ono or rec.get("order_no"),
                            "decision": "启动复核:%s" % cls})
                appcommon.upsert_order(op, okey, rec)
                LOG.info("[订单恢复] %s 官方状态=%s(%s)", okey, cls, raw)
                if cls == "cancelled":
                    for dk in [k for k in self.state.get("dedup", {})
                               if rec.get("date") in k and rec.get("train") in k]:
                        self.state["dedup"].pop(dk, None)
                    self._save_state()
                    LOG.info("[订单恢复] 官方已取消,已清除 %s 的本地防重记录", okey)
        except Exception as e:
            LOG.warning("[订单恢复] 未完成订单上下文恢复失败(不影响监控): %s", e)
        active_tasks = [t for t in self.tasks
                        if self.task_status(t) in ACTIVE_STATUSES]
        for t in self.tasks:
            LOG.info("[运行] 任务「%s」%s %s", t["name"],
                     STATUS_LABELS.get(self.task_status(t), self.task_status(t)),
                     "启用（优先级 %s，间隔约 %.0fs）" % (t.get("priority", 5), self.task_interval(t))
                     if t in active_tasks else "")
        if not active_tasks:
            LOG.warning("没有处于「监控中/等待重试」状态的任务，引擎将空转（请在界面选择要启动的任务）")
        LOG.info("基准间隔 %ss / 最小间隔 %ss / 日志目录 %s",
                 self.base_interval, self.min_interval, self.config.get("log_dir", "logs"))
        LOG.info("按 Ctrl+C 停止监控")
        self.check_session_if_needed()

        next_due = {}
        for i, t in enumerate(self.tasks):
            if self.task_status(t) in ACTIVE_STATUSES:
                next_due[i] = time.time()  # 重启后立即恢复轮询

        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    break
                self._sync_state()  # 同步外部（GUI）对状态文件的修改
                if self._sync_config():
                    # 任务列表变化（增/删）：按最新列表重建调度表，避免旧索引错位
                    next_due = {i: time.time()
                                for i, t in enumerate(self.tasks)
                                if self.task_status(t) in ACTIVE_STATUSES}
                self.check_session_if_needed()
                now = time.time()
                # 运行中新增/恢复的任务自动纳入调度
                for i, t in enumerate(self.tasks):
                    if i not in next_due and self.task_status(t) in ACTIVE_STATUSES:
                        next_due[i] = now
                due = [(i, self.tasks[i]) for i in next_due
                       if self.task_status(self.tasks[i]) in ACTIVE_STATUSES
                       and now >= next_due[i]]
                due.sort(key=lambda x: -int(x[1].get("priority") or 5))
                for i, task in due:
                    if stop_event is not None and stop_event.is_set():
                        break
                    interval = self.task_interval(task)
                    try:
                        broke, recoverable = self._run_task(task)
                    except Exception as e:
                        LOG.error("[错误] 任务「%s」内部异常: %s", task["name"], e)
                        self._note_failure(task, "内部异常: {0}".format(e))
                        broke, recoverable = True, True
                    entry = self.state["tasks"].setdefault(task["name"], {})
                    streak = entry.get("fail_streak", 0)
                    if recoverable and streak:
                        # 连续失败退避：间隔按失败次数拉长（上限 5 分钟），实现自动恢复
                        backoff_iv = min(max(interval, interval * min(streak, 10) * 0.5), 300)
                        LOG.info("[运行] 任务「%s」连续失败 %s 次，下次轮询退避到 %.0fs 后",
                                 task["name"], streak, backoff_iv)
                        next_due[i] = time.time() + backoff_iv
                    else:
                        next_due[i] = time.time() + interval
                    if broke and self.task_status(task) not in ACTIVE_STATUSES:
                        next_due.pop(i, None)
                if stop_event is not None:
                    if stop_event.wait(1):
                        break
                else:
                    time.sleep(1)
        except KeyboardInterrupt:
            pass
        LOG.info("监控已停止。状态已保存到 %s", self.state_path)


def load_state_file(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


if __name__ == "__main__":
    eng = MonitorEngine()
    eng.run()