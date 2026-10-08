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
import math
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

# 无有效监控日期时的兜底轮询间隔（秒）：任务的 dates/date_range 非法或为空时，
# 用长间隔并告警，避免无效高频轮询（_run_task 会把该任务标为失败并停止）
NO_DATES_FALLBACK_INTERVAL = 300

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


def _mask_order_no(ono):
    """订单号脱敏（日志用）：保留前后各 4 位，中间打码；过短则全打码。"""
    s = str(ono or "")
    if len(s) <= 8:
        return "****"
    return s[:4] + "****" + s[-4:]


def _mask_names(names):
    """乘车人姓名打码（日志用）：保留首字其余打码；['张三','李四'] -> '张*、李*'。"""
    out = []
    for n in names or []:
        n = (n or "").strip()
        if not n:
            continue
        out.append(n[0] + "*" * (len(n) - 1) if len(n) > 1 else "*")
    return "、".join(out)

_WARNED_PRIORITIES = set()


def _safe_priority(task):
    """priority 安全取值：非法值记一次警告并回默认值 5。

    供 task_interval 与调度排序共用，避免两处各写一套转换逻辑。
    """
    raw = task.get("priority") or 5
    try:
        return int(raw)
    except (TypeError, ValueError):
        key = repr(raw)
        if key not in _WARNED_PRIORITIES:
            _WARNED_PRIORITIES.add(key)
            LOG.warning("任务 %s 的 priority=%r 非法，已按默认值 5 处理",
                        task.get("name"), raw)
        return 5


# 日期展开跨度上限（天）：手改配置配出超长区间/超长日期列表时截断并明确报错，
# 防止每轮数百次查询触发 12306 限流/封 IP（Task 68c；GUI 交互 picker 另有 5 天上限）
MAX_EXPAND_DAYS = 31

_WARNED_CONFIG = set()


def _safe_config_int(raw, default, what):
    """配置整数安全取值：非法形状记一次警告后回默认值（Task 68a）。

    供 poll_interval_seconds / min_interval_seconds 共用：手改配置写成
    "abc" 时旧代码 int() 抛 ValueError 崩掉整个引擎进程。
    """
    try:
        return int(raw)
    except (TypeError, ValueError):
        key = ("int", what, repr(raw))
        if key not in _WARNED_CONFIG:
            _WARNED_CONFIG.add(key)
            LOG.warning("[配置] %s=%r 非法，已按默认值 %r 处理",
                        what, raw, default)
        return default


def _safe_config_float(raw, default, what):
    """配置浮点数安全取值：非法形状/非有限值记一次警告后回默认值（Task 68a）。

    供 adaptive 的各类 multiplier 共用：配成字符串时旧代码 iv*mult 抛
    TypeError 崩进程。
    """
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = None
    if v is None or not math.isfinite(v):
        key = ("float", what, repr(raw))
        if key not in _WARNED_CONFIG:
            _WARNED_CONFIG.add(key)
            LOG.warning("[配置] %s=%r 非法，已按默认值 %r 处理",
                        what, raw, default)
        return default
    return v


def _sanitize_tasks(raw):
    """tasks 字段形状校验（Task 68a）：非列表→记 error 后视为空；非 dict 条目→
    记 error 后跳过该条。绝不让手改配置的形状手误崩掉引擎进程。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        LOG.error("[配置] tasks 字段非列表，已忽略全部任务：%r", raw)
        return []
    out = []
    for t in raw:
        if isinstance(t, dict):
            out.append(t)
        else:
            LOG.error("[配置] tasks 含非法条目已跳过：%r", t)
    return out


def normalize_trains(task):
    """trains 字段归一化（Task 68b）：裸字符串（如漏写方括号的 "G101"）按单个
    车次处理并记警告，绝不逐字符拆（旧代码会拆成 ['G','1','0','1'] 静默漏单）。
    非字符串条目跳过（旧代码直接 AttributeError）。"""
    raw = task.get("trains") or []
    if isinstance(raw, str):
        LOG.warning("[配置] 任务「%s」的 trains 为字符串，已按单个车次处理：%r",
                    task.get("name"), raw)
        raw = [raw]
    elif not isinstance(raw, (list, tuple)):
        LOG.warning("[配置] 任务「%s」的 trains 形状非法，已忽略：%r",
                    task.get("name"), raw)
        raw = []
    return [t.strip().upper() for t in raw
            if isinstance(t, str) and t.strip()]


def normalize_seat_types(task):
    """seat_types 字段归一化（Task 93b）：裸字符串（如漏写方括号的 "硬座"）
    按单个席别处理并记警告，绝不逐字符拆（旧代码会拆成 ['硬','座']，
    ticket.seat_candidates_for 照单返回后与余票求交恒为空 → 任务永久
    静默漏单）。与 Task 68b 的 trains 口径一致。非字符串条目跳过。"""
    raw = task.get("seat_types") or []
    if isinstance(raw, str):
        LOG.warning("[配置] 任务「%s」的 seat_types 为字符串，已按单个席别处理：%r",
                    task.get("name"), raw)
        raw = [raw]
    elif not isinstance(raw, (list, tuple)):
        LOG.warning("[配置] 任务「%s」的 seat_types 形状非法，已忽略：%r",
                    task.get("name"), raw)
        raw = []
    return [s for s in raw if isinstance(s, str)]


def expand_dates(task):
    """把 dates + date_range 展开成日期列表（去重保序）。"""
    result = []
    dates = task.get("dates") or []
    if not isinstance(dates, list):
        # 整字段非列表（如漏写方括号的 "dates": 20261009）：记警告后视为空，
        # 绝不让单个任务的手误崩掉整个引擎进程
        LOG.warning("[配置] 任务「%s」的 dates 字段非列表，已忽略：%r",
                    task.get("name"), dates)
        dates = []
    for d in dates:
        if isinstance(d, str):
            result.append(d)
        else:
            # 非字符串条目（如未加引号的整数日期）：记警告后跳过，
            # 绝不让单个任务的手误崩掉整个引擎进程
            LOG.warning("[配置] 任务「%s」的 dates 含非法条目已跳过：%r",
                        task.get("name"), d)
    dr = task.get("date_range")
    if not dr:
        # 未配置（或空）date_range：正常情况，静默跳过（保持旧行为）
        pass
    elif (isinstance(dr, (list, tuple)) and len(dr) == 2
            and isinstance(dr[0], str) and isinstance(dr[1], str)):
        try:
            d0 = datetime.date.fromisoformat(dr[0])
            d1 = datetime.date.fromisoformat(dr[1])
        except (ValueError, TypeError):
            # 非法日期区间：记警告后跳过，绝不让单个任务的手误崩掉整个引擎进程
            LOG.warning("[配置] 任务「%s」的 date_range 非法，已跳过：%r",
                        task.get("name"), dr)
        else:
            if d1 >= d0:
                span = (d1 - d0).days + 1
                if span > MAX_EXPAND_DAYS:
                    cut = d0 + datetime.timedelta(days=MAX_EXPAND_DAYS - 1)
                    LOG.error(
                        "[配置] 任务「%s」的 date_range 跨度 %d 天超过上限 %d 天，"
                        "已截断为 %s~%s；超长区间会导致每轮数百次查询、"
                        "触发 12306 限流/封 IP",
                        task.get("name"), span, MAX_EXPAND_DAYS,
                        d0.isoformat(), cut.isoformat())
                    d1 = cut
                d = d0
                while d <= d1:
                    result.append(d.isoformat())
                    d += datetime.timedelta(days=1)
    else:
        # 病态形状（dict、单元素、嵌套、非字符串元素、整数等）：
        # 记警告后跳过，绝不让单个任务的手误崩掉整个引擎进程
        LOG.warning("[配置] 任务「%s」的 date_range 形状非法，已跳过：%r",
                    task.get("name"), dr)
    seen, uniq = set(), []
    for x in result:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    if len(uniq) > MAX_EXPAND_DAYS:
        LOG.error(
            "[配置] 任务「%s」的监控日期共 %d 个超过上限 %d 天，已截断为前 %d 个；"
            "超长日期列表会导致每轮数百次查询、触发 12306 限流/封 IP",
            task.get("name"), len(uniq), MAX_EXPAND_DAYS, MAX_EXPAND_DAYS)
        uniq = uniq[:MAX_EXPAND_DAYS]
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
        except (ValueError, TypeError):
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
        try:
            with open(self.config_path, encoding="utf-8") as f:
                self.config = json.load(f)
        except FileNotFoundError:
            # Task 102：缺 config 文件（python engine.py 裸跑且无配置文件）——
            # 旧代码抛裸 FileNotFoundError 崩进程；记 error 后用空配置继续
            # （Task 91 同口径：进程不崩；修好文件后 _sync_config 自动恢复，
            #  mtime 从 None 变为有效值即触发同步）。
            LOG.error("[配置] %s 不存在，已按默认配置继续",
                      self.config_path)
            self.config = {}
        except json.JSONDecodeError:
            # Task 105：文件存在但内容非法 JSON——旧代码抛裸 JSONDecodeError
            # 崩进程（Task 91 同 P1 类；第三轮重扫和 Task 91 两次漏网，只覆盖
            # "合法 JSON 但非对象"）；记 error 后用空配置继续。文件存在故
            # _config_mtime 取有效值，修好文件后 mtime 变化 → _sync_config
            # 自动恢复（Task 91/102 同机制；_sync_config 已有 Task 72 降级）。
            LOG.error("[配置] %s 内容不是合法 JSON，已按默认配置继续",
                      self.config_path)
            self.config = {}
        if not isinstance(self.config, dict):
            # Task 91：顶层非 dict（手改误删大括号成 [] 等）——旧代码后续
            # self.config.get(...) 抛 AttributeError 崩进程；记 error 后用
            # 空配置继续（Task 68 口径：非法形状用安全默认值，进程不崩）。
            LOG.error("[配置] %s 顶层不是对象（%s），已按默认配置继续",
                      self.config_path, type(self.config).__name__)
            self.config = {}
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

        try:
            self.name2code, self.code2name = ticket.load_station_map()
        except Exception as e:
            # 离线首跑：车站表下载失败不得崩监控，空表降级运行；
            # 联网后下次启动自动恢复
            LOG.error("[网络] 车站数据加载失败，将以空表降级运行：%s", e)
            self.name2code, self.code2name = {}, {}
        self.tasks = _sanitize_tasks(self.config.get("tasks"))
        # 已删除任务名的墓碑（内存态）：删任务与在途轮询竞速时，拦住
        # _note_failure / 轮询后 setdefault 把已清掉的 state 条目复活。
        # _sync_config 见到同名任务重建即清除，不影响新任务。
        self._deleted_names = set()

        self.base_interval = _safe_config_int(
            self.config.get("poll_interval_seconds", 45), 45,
            "poll_interval_seconds")
        self.min_interval = max(MIN_INTERVAL_FLOOR,
                                _safe_config_int(
                                    self.config.get("min_interval_seconds", 30),
                                    30, "min_interval_seconds"))
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
        # 读与写侧持同一把 file_lock：_save_state 的 fallback_direct 直写是
        # 非原子的（open "w" 截断后再写），不持锁读可能撞上写一半的撕裂文件
        # → json 误判损坏 → 健康 state.json 被隔离。只包住读本身，
        # quarantine / _save_state 留在锁外（顺序持锁，不嵌套）。
        lock_timeout = False
        try:
            with filelock.file_lock(self.state_path + ".lock"):
                state, err, read_fp = appcommon.read_state_or_none(
                    self.state_path)
        except TimeoutError as e:
            # 锁争用超时（另一进程长时间持有写锁）：与瞬时占用（Task 40）同口径——
            # 本次跳过加载，不挪档、不重建、不落盘；_sync_state 会在文件变化后重载。
            # 绝不能把健康文件当坏档隔离（旧行为丢防重记录），也不能落盘空状态
            # 覆盖对方正在写的文件。
            LOG.warning("state.json 锁争用超时，本次跳过加载，稍后重试: %s", e)
            lock_timeout = True
            old = getattr(self, "state", None)
            # Task 72 rework：与正常路径同为 3-tuple 解包（read_fp 本路径恒为
            # None——锁都没拿到，不可能有"读失败瞬间"指纹；下游 quarantine
            # 分支因 lock_timeout 不会执行到）。
            state, err, read_fp = (old if isinstance(old, dict) else {}), None, None
        state_ok = err is None and not lock_timeout
        if err is not None:
            if isinstance(err, OSError):
                # 瞬时占用（另一进程正在写 state.json，读句柄撞车）：本次跳过
                # 加载，不挪档、不重建、不落盘；_sync_state 会在文件变化后重载。
                # 绝不能把健康文件当坏档隔离（旧行为丢防重记录）。
                LOG.warning("state.json 被占用，本次跳过加载，稍后重试: %s", err)
                old = getattr(self, "state", None)
                state = old if isinstance(old, dict) else {}
            else:
                LOG.warning("state.json 读取失败: %s", err)
                # 读不出来 ≠ 空状态：挪档留证（时间戳名，见 appcommon.quarantine_corrupt），
                # 再从空状态重建。丢 state 就是丢防重（dedup）记录，理论上会
                # 重复下单——必须醒目提示去核对在途行程。
                # 传入读失败瞬间（持锁内，由 read_state_or_none 在读取结束时抓取）
                # 的指纹：若另一进程在此期间已写入健康文件，quarantine 会放弃
                # 隔离，避免误伤（Task 33 TOCTOU 守卫；Task 72 修复"释放锁后现
                # 抓指纹"架空守卫的回归）。
                bad = appcommon.quarantine_corrupt(self.state_path, read_fp)
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
        state = self._sanitize_state_tasks(state)
        if state_ok:
            self._save_state(state)
        return state

    def _sanitize_state_tasks(self, state):
        """state.json 节级形状校验（Task 93a）：tasks 节被手改成非 dict
        （如 []）时，setdefault 只补缺键、损坏形状原样透过 → 后续
        self.state["tasks"].setdefault(...) 直接 AttributeError：启动崩
        （__init__→_resume_or_init_status）或运行中 _sync_state 重载后
        线程死亡。记 error 并按空任务集安全降级，线程不死。
        _load_state 与 _reload_state 共用（两条独立漏斗）。"""
        if not isinstance(state.get("tasks"), dict):
            LOG.error("[状态] %s 的 tasks 节不是对象（%s），已按空任务集降级；"
                      "请检查文件是否被手改损坏",
                      self.state_path, type(state.get("tasks")).__name__)
            state["tasks"] = {}
        return state

    def _save_state(self, state=None):
        if state is None:
            state = self.state
        self.state = state
        lock = getattr(self, "_save_lock", None)
        if lock is None:  # 兼容未初始化锁的旧实例
            lock = self._save_lock = threading.Lock()
        with lock:  # 同一实例内跨线程（GUI/引擎）串行化写入
            # Task 57：读-改-写全程持 file_lock。旧代码在拿锁前已从内存
            # deepcopy 出快照 → 写回时覆盖 launcher/gui 并发写入的变更
            # （丢任务/状态）。现锁内重读文件 → 合并内存变更 → 写回。
            try:
                with filelock.file_lock(self.state_path + ".lock"):
                    # 锁内重读：读到的一定是其它持锁写方已落盘的最新内容
                    disk_state = load_state_file(self.state_path)
                    snapshot = None
                    for _ in range(3):
                        try:
                            # 用副本序列化：避免跨线程（GUI 操作与引擎线程）
                            # 修改同一字典导致异常；tombstoned 的 set() 拷贝
                            # 同样可能撞上 note_task_deleted 的并发 add，
                            # 一并放在重试循环内
                            tombstoned = set(
                                getattr(self, "_deleted_names", None) or ())
                            snapshot = copy.deepcopy(
                                _merge_state_for_save(
                                    disk_state, state, tombstoned))
                            break
                        except RuntimeError:  # 字典/集合并发修改导致的迭代异常
                            time.sleep(0.02)
                    if snapshot is None:
                        # Task 72：兜底浅拷贝仍可能撞上并发修改 → 捕获后告警
                        # 并跳过本次落盘（内存态保留完好，下次 _save_state 重试），
                        # 绝不能让 RuntimeError 逃出主循环杀死引擎。
                        try:
                            snapshot = copy.deepcopy(dict(state))
                        except RuntimeError as e:
                            LOG.error("state.json 快照失败（并发修改），"
                                      "本次跳过落盘: %s", e)
                            return self.state
                    # 原子写入：先写临时文件再替换，避免两线程同时写坏 state.json
                    # 临时名/重试/直写兜底语义由 appcommon 参数化保留；
                    # 跨进程锁与 launcher.append_monitor_task 的 state 段互斥
                    try:
                        appcommon.atomic_write_json(self.state_path, snapshot,
                                                    fallback_direct=True)
                    except RuntimeError as e:
                        # Task 72：序列化期间被并发修改（防御性：快照本已是独立
                        # 深拷贝，正常到不了；若到，同样跳过落盘不逃出主循环）
                        LOG.error("state.json 序列化期间被并发修改，"
                                  "本次跳过落盘: %s", e)
                        return self.state
            except TimeoutError as e:
                # 锁争用超时：本次跳过落盘，内存态保留完好，下次 _save_state
                # 重试。绝不能让异常杀死引擎监控线程（→ 漏单）。
                LOG.warning("state.json 锁争用超时，本次跳过落盘，稍后重试: %s",
                            e)
                return self.state
        try:
            self._state_mtime = os.path.getmtime(self.state_path)
        except OSError:
            pass
        return self.state

    def _reload_state(self):
        """仅重新读取 state.json（供运行中的引擎同步外部修改，不触发写入）。

        锁争用超时返回 None：调用方保留旧状态、稍后重试；绝不能回退成 {}
        覆盖内存里的防重记录。
        内容损坏同样返回 None（Task 72）：先挪档留证 + 醒目告警（与 _load_state
        同级），绝不静默吞成 {}——否则 _sync_state 会用空状态覆盖内存态，
        随后 _save_state 经合并把空 dedup 写回磁盘，永久丢失防重记录。"""
        if not os.path.exists(self.state_path):
            state = {}
        else:
            try:
                # 与 _load_state 同理：读与写侧持同一把 file_lock，
                # 避免撞上 _save_state fallback_direct 直写的撕裂文件
                # → json 误判损坏 → self.state = {} → 空状态被落盘丢防重。
                with filelock.file_lock(self.state_path + ".lock"):
                    state, err, read_fp = appcommon.read_state_or_none(
                        self.state_path)
            except TimeoutError as e:
                LOG.warning("state.json 锁争用超时，本次跳过重载，稍后重试: %s",
                            e)
                return None
            if err is not None:
                if isinstance(err, OSError):
                    # 瞬时占用（另一进程正在写）：本次跳过重载，不挪档；
                    # _sync_state 会在文件变化后重试（Task 40 口径）。
                    LOG.warning("state.json 被占用，本次跳过重载，稍后重试: %s",
                                err)
                    return None
                LOG.warning("state.json 重载失败: %s", err)
                # 读不出来 ≠ 空状态：挪档留证（时间戳名），保留旧内存态。
                # 传入读失败瞬间（持锁内）的指纹，避免误伤健康文件。
                bad = appcommon.quarantine_corrupt(self.state_path, read_fp)
                if bad is None:
                    LOG.warning("坏档挪移失败（文件被占用？），保留旧内存态以保防重记录")
                else:
                    LOG.warning(
                        "坏档已挪为 %s，保留旧内存态。其它任务的运行状态与"
                        "防重记录都在坏档里——请尽快到 12306「未支付订单」"
                        "核对在途行程，避免重复下单", bad)
                return None
        state.setdefault("dedup", {})
        state.setdefault("tasks", {})
        state.setdefault("retry", {})
        state = self._sanitize_state_tasks(state)
        return state

    def _sync_state(self):
        """检测 state.json 是否被外部（GUI/其他进程）修改，有则重新加载。"""
        try:
            m = os.path.getmtime(self.state_path)
        except OSError:
            return
        if m != self._state_mtime:
            new_state = self._reload_state()
            if new_state is None:
                # 锁争用超时：保留旧内存态且不推进 mtime，下轮继续尝试重载
                return
            self.state = new_state
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
        try:
            with open(self.config_path, encoding="utf-8") as f:
                new_config = json.load(f)
        except Exception as e:
            # Task 72：解析失败不消费 mtime——否则本次配置变更永久被忽略，
            # 需再改一次文件才重同步。下次 _sync_config 会重试本次变更。
            LOG.warning("[配置] config.json 重新读取失败：%s", e)
            return False
        if not isinstance(new_config, dict):
            # Task 91：热更新读到顶层非 dict——记 error，保留旧配置继续；
            # 消费 mtime 避免每次轮询重复报错（修好文件后 mtime 变化会再次同步）。
            LOG.error("[配置] %s 顶层不是对象（%s），已忽略本次变更",
                      self.config_path, type(new_config).__name__)
            self._config_mtime = m
            return False
        self._config_mtime = m
        self.config = new_config
        self.base_interval = _safe_config_int(
            self.config.get("poll_interval_seconds", 45), 45,
            "poll_interval_seconds")
        self.min_interval = max(MIN_INTERVAL_FLOOR,
                                _safe_config_int(
                                    self.config.get("min_interval_seconds", 30),
                                    30, "min_interval_seconds"))
        raw_tasks = self.config.get("tasks")
        self.tasks = _sanitize_tasks(raw_tasks)
        current_names = {t.get("name") or "" for t in self.tasks}
        # Task 72：清孤儿 state 条目——config 已无此任务，state 残留不再需要，
        # 否则 state.json 缓慢膨胀（删任务只清内存条目时亦然）。
        # 形状损坏的 tasks（如手误写成 dict）不触发清理：_sanitize_tasks 已记
        # error 并视为空，此时清条目会误删"修好配置后还会回来"的任务状态。
        state_tasks = (self.state.get("tasks")
                       if isinstance(self.state, dict) else None)
        if isinstance(state_tasks, dict) and (
                raw_tasks is None or isinstance(raw_tasks, list)):
            pruned = [n for n in state_tasks if n not in current_names]
            for n in pruned:
                del state_tasks[n]
            if pruned:
                # Task 72 rework：清理出的孤儿名一并落墓碑，否则
                # _merge_state_for_save 的 union 合并会在下次 _save_state
                # 把磁盘残留条目复活（内存已删、磁盘复活）。
                # 墓碑清除步在后：pruned 不在 current_names 内，墓碑被保留；
                # 同名任务回到配置时清除步会清掉该墓碑（语义不变）。
                deleted = getattr(self, "_deleted_names", None)
                if deleted is None:
                    deleted = self._deleted_names = set()
                deleted.update(pruned)
        # 同名任务重建后清墓碑：删任务时记的墓碑只拦"已删除"的在途写回，
        # 新任务必须正常轮询
        deleted = getattr(self, "_deleted_names", None)
        if deleted:
            self._deleted_names = {n for n in deleted
                                   if n not in current_names}
            # Task 72：墓碑上限防膨胀。只在极端情况下截断（反复删建大量任务）；
            # 被截掉的墓碑最坏导致一条在途写回短暂复活孤儿条目，下次同步即清。
            if len(self._deleted_names) > 500:
                self._deleted_names = set(
                    list(self._deleted_names)[:500])
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
        # 墓碑守卫（Task 53 返工）：已删除任务的在途轮询写回不得复活 state
        # 条目——setdefault 会无声复活；GUI 显式动作（恢复/重置/新建）经
        # force=True 放行（删→同名重建流程不受影响）。
        if not force and name in getattr(self, "_deleted_names", ()):
            LOG.info("[状态] 任务 %s 已删除，忽略状态写入 %s %s",
                     name, STATUS_LABELS.get(status, status), message)
            return
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

    def note_task_deleted(self, name):
        """GUI 删除任务时调用：立即从内存状态中清除该任务条目并落盘。

        不等下一次 _sync_state 的 mtime 检查——否则删任务后、引擎下次同步前
        （≤一个轮询间隔）该任务仍会被调度（幽灵监控），且内存里的旧条目可能
        被后续 _save_state 写回文件、复活成孤儿 state。同名重建任务也不会再
        继承陈旧状态。

        另记墓碑：删任务若恰撞上该任务的在途轮询，_note_failure、轮询后的
        setdefault 与 set_task_status 都会把条目复活——墓碑拦住这三处写回
        （_sync_config 见到同名重建即清墓碑）。
        """
        tasks = self.state.get("tasks")
        if isinstance(tasks, dict):
            tasks.pop(name, None)
        if name:
            # 兼容绕过 __init__ 构造的旧实例（测试的 make_engine 等）
            deleted = getattr(self, "_deleted_names", None)
            if deleted is None:
                deleted = self._deleted_names = set()
            deleted.add(name)
        self._save_state()

    # ----------------------------- 自适应频率 -----------------------------

    @staticmethod
    def _soonest_date(task):
        dates = expand_dates(task)
        return min(dates) if dates else None

    def task_interval(self, task):
        """计算任务的有效轮询间隔（秒）。"""
        ad = self.config.get("adaptive") or {}
        if not isinstance(ad, dict):
            LOG.warning("[配置] adaptive 字段非字典，已禁用自适应频率：%r", ad)
            ad = {}
        iv = float(self.base_interval)
        if ad.get("enabled", True):
            soon = self._soonest_date(task)
            if soon is None:
                # 无有效监控日期：日期配置非法或为空，用兜底长间隔并告警，
                # 避免无效高频轮询（_run_task 会把该任务标为失败并停止）
                LOG.warning("[配置] 任务「%s」没有有效监控日期（date_range/dates 非法或为空），"
                            "使用兜底间隔 %ss", task.get("name"), NO_DATES_FALLBACK_INTERVAL)
                return max(self.base_interval, NO_DATES_FALLBACK_INTERVAL)
            hour = datetime.datetime.now().hour
            peak = ad.get("peak_hours") or [6, 23]
            if (isinstance(peak, (list, tuple)) and len(peak) == 2
                    and all(isinstance(x, (int, float)) for x in peak)):
                in_peak = peak[0] <= hour < peak[1]
            else:
                # 非法形状（单元素列表/字符串等）：记警告后禁用自适应频率，
                # 绝不让手改配置崩掉引擎进程（Task 68a）
                LOG.warning("[配置] adaptive.peak_hours 形状非法 %r，已禁用自适应频率",
                            peak)
                return max(self.min_interval, iv)
            mult = (_safe_config_float(ad.get("peak_multiplier", 1.0), 1.0,
                                       "adaptive.peak_multiplier")
                    if in_peak else
                    _safe_config_float(ad.get("offpeak_multiplier", 1.6), 1.6,
                                       "adaptive.offpeak_multiplier"))
            rush_hours = _safe_config_float(ad.get("rush_within_hours", 24), 24,
                                            "adaptive.rush_within_hours")
            if rush_hours:
                try:
                    delta_h = (datetime.datetime.fromisoformat(soon)
                               - datetime.datetime.now()).total_seconds() / 3600.0
                    if 0 <= delta_h <= rush_hours:
                        mult = min(mult, _safe_config_float(
                            ad.get("rush_multiplier", 0.75), 0.75,
                            "adaptive.rush_multiplier"))
                except (ValueError, TypeError):
                    pass
            prio = _safe_priority(task)
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

    # ----------------------------- 单任务扫描 -----------------------------

    def _run_task(self, task):
        """执行一个任务的一轮扫描。返回 (退出循环标志, 是否发生可恢复异常)。"""
        if (task.get("name") or "") in getattr(self, "_deleted_names", ()):
            # 任务已被 GUI 删除：本轮不再执行（立即停），也不重建 state 条目
            return True, False
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

        trains = normalize_trains(task)
        seats_want_all = normalize_seat_types(task)
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
                        try:
                            notify_results = self._notify(task, subject, body)
                        except Exception as e:
                            LOG.error("[通知异常] 发送通知时发生未捕获异常，已忽略：%s", e)
                            notify_results = {}
                        notify_txt = "; ".join("{0}:{1}".format(k, "成功" if nok else msg)
                                               for k, (nok, msg) in notify_results.items()) or "无通知渠道"
                        LOG.info("[有票] 任务「%s」%s 有余票，已通知：%s", name, seat_name, notify_txt)
                        appcommon.append_history(self.history_path, {
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
                    # 自动下单（记录本次发起时刻，供提交后结果未知/防重复时做时间戳归因）
                    attempt_ts = time.time()
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
                        cls, ono, raw, recent = order_mod.classify_with_time(
                            date, train_code, task.get("passenger_names") or [],
                            not_before_ts=attempt_ts)
                        okey = "%s|%s" % (date, train_code)
                        LOG.info("[防重核查] 任务「%s」%s %s 官方查询结果=%s %s",
                                 name, date, train_code, cls,
                                 ("订单号 %s,官方状态「%s」" % (_mask_order_no(ono), raw)) if ono else raw)
                        decision = {"unpaid": "待支付:任务停止,请尽快支付",
                                    "paid": "已支付:判定为已购得,任务停止",
                                    "cancelled": "已取消:清除本地防重记录,允许重新下单",
                                    "none": "官方无此订单:清除本地记录,允许重新下单",
                                    "blocked": "存在其它行程未支付订单挡路:请到 12306「未完成订单」支付或取消后恢复任务",
                                    "unknown": "官方状态不明确:保留本地记录,下轮再核",
                                    "error": "官方查询失败:保留本地记录,下轮再核"}[cls]
                        appcommon.upsert_order(
                            os.path.join(HERE, "orders.json"), okey,
                            {"order_no": ono, "train": train_code, "date": date,
                             "from": info["from_name"], "to": info["to_name"],
                             "seat": seat_name, "passengers": p_names,
                             "official_status": raw, "classify": cls,
                             "decision": decision, "source": "engine"})
                        appcommon.append_history(self.history_path, {
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
                            if stop_after:
                                self.set_task_status(task, "success",
                                                     "官方确认已支付,订单 %s" % (ono or "未知"))
                                return True, False
                            if self.task_status(task) == "retrying":
                                self.set_task_status(task, "monitoring",
                                    "官方确认已支付（订单 %s），继续监控其它日期" % (ono or "未知"))
                            return True, False  # 本轮不再继续（避免同轮重复下单）；任务保持 monitoring
                        if cls == "unpaid":
                            if recent:
                                # 下单时间落在本次提交窗口内 → 本次已提交成功
                                self.state["dedup"][key] = "SUBMITTED"
                                self._save_state()
                                if bool(task.get("stop_after_order", True)):
                                    self.set_task_status(task, "success",
                                        "本次已提交订单（未支付），下单时间 %s——请尽快支付"
                                        % (recent.get("order_time") or "未知"))
                                    return True, False
                            else:
                                # 有该行程未支付订单但下单时间更早 → 更早遗留订单
                                self.state["dedup"][key] = "ACCOUNT_DUP"
                                self._save_state()
                                if bool(task.get("stop_after_order", True)):
                                    self.set_task_status(task, "success",
                                        "账号存在该行程更早的未支付订单 %s——请核对后尽快支付"
                                        % (ono or "未知"))
                                    return True, False
                            continue
                        if cls in ("cancelled", "none"):
                            self.state["dedup"].pop(key, None)
                            self._save_state()
                            continue
                        if cls == "blocked":
                            self.set_task_status(task, "failed", "%s——处理后恢复本任务" % raw)
                            return True, False
                        continue  # unknown/error:保留本地记录,下轮再核
                    else:
                        msg = (extra or {}).get("msg", "")
                        if (extra or {}).get("reason") == "ambiguous":
                            # 提交确认后结果未知：先回读官方订单接口做时间戳归因。
                            # 订单已生成且下单时间对得上=本次成功；确认无订单=安全重试；
                            # 其余（查不到/更早旧单/其它行程挡路）=保守停任务，防重复下单。
                            cls, ono, raw, recent = order_mod.classify_with_time(
                                date, train_code, task.get("passenger_names") or [],
                                not_before_ts=attempt_ts)
                            if cls == "unpaid" and recent:
                                self.state["dedup"][key] = "SUBMITTED"
                                self._save_state()
                                self._record_success(
                                    task, info, date, seat_name,
                                    {"passengers": "、".join(p_names), "order_no": ono})
                                LOG.info("[结果回读] 任务「%s」%s 官方已生成订单 %s（下单时间 %s），判定本次成功",
                                         name, date, _mask_order_no(ono), recent.get("order_time") or "未知")
                                if stop_after:
                                    self.set_task_status(
                                        task, "success",
                                        "本次已提交订单（未支付），下单时间 %s" % (
                                            recent.get("order_time") or "未知"))
                                    return True, False
                                if self.task_status(task) == "retrying":
                                    self.set_task_status(task, "monitoring", "下单成功，恢复正常监控")
                                return True, False  # 本轮不再继续（避免同轮重复下单）
                            if cls in ("none", "cancelled"):
                                LOG.info("[结果回读] 任务「%s」%s 官方确认无此订单（%s），安全重试",
                                         name, date, cls)
                                continue
                            # unpaid 但时间对不上 / blocked / error / unknown：保守停
                            self.set_task_status(task, "failed",
                                "订单提交后结果未知——请先到 12306「未支付订单」核对："
                                "有单就支付/取消，确认无单后再恢复本任务")
                            LOG.error("[警告] 任务「%s」%s %s 提交后结果未知，已停止自动重试",
                                      name, date, train_code)
                            appcommon.append_history(self.history_path, {
                                "time": self._now(), "task": name, "result": "ambiguous",
                                "train": train_code, "date": date, "from": info["from_name"],
                                "to": info["to_name"], "seat": seat_name,
                                "passengers": p_names, "order_no": ono,
                                "message": msg, "notify": "",
                            })
                            return True, False
                        if (extra or {}).get("reason") == "seat_unavailable":
                            # 确认页可售席别由 12306 服务端下发（普速车常常没有「无座」），
                            # 换时间点重试也不会有：记入 dedup 永久跳过，别无限重试刷日志
                            self.state["dedup"][key] = "SEAT_UNAVAILABLE"
                            self._save_state()
                            appcommon.append_history(self.history_path, {
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
                        appcommon.append_history(self.history_path, {
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
        try:
            notify_results = self._notify(task, subject, body)
        except Exception as e:
            LOG.error("[通知异常] 发送通知时发生未捕获异常，已忽略：%s", e)
            notify_results = {}
        notify_txt = "; ".join("{0}:{1}".format(k, "成功" if ok else msg)
                               for k, (ok, msg) in notify_results.items()) or "无通知渠道"
        appcommon.append_history(self.history_path, {
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
                 _mask_names((extra.get("passengers", "") or "").split("、")),
                 _mask_order_no(order_no) if order_no else "未知")

    def _note_failure(self, task, message):
        name = task["name"]
        if (name or "") in getattr(self, "_deleted_names", ()):
            # 任务在本轮询中途被 GUI 删除：在途失败不再写回 state，
            # 不复活已清掉的条目（删任务后 state 无残留）
            return
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
        临时故障（网络异常/系统繁忙页/浏览器校验异常）只告警不杀任务，稍后自动重试。
        浏览器分支中，「校验异常」开头的校验结果视为临时故障，不标记任务失败。"""
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
                # session_ok 对 goto 超时等任何异常都返回 (False, "校验异常: ...")：
                # 这是 12306 偶发卡顿类的瞬时故障，只走冷却重试；只有明确的会话失效
                # 才把任务标记为失败。
                permanent = not ok and not str(who or "").startswith("校验异常")
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

    @staticmethod
    def _cancelled_dedup_keys(rec, dedup_keys):
        """已取消订单清本地防重：按 dedup 键的日期/车次字段精确匹配。

        键格式为 from|to|date|train|seat|names（见 dedup_key）：取第 3、4 字段
        与 rec 的 date/train 精确相等比较。Task 72 修复旧的子串匹配——车次
        "G1" 会误清同日 "G101" 等其它车次的防重记录。
        rec 缺 date/train 时返回 []（调用方记 warning 跳过该条），绝不抛
        TypeError 中止整个启动恢复。"""
        date, train = rec.get("date"), rec.get("train")
        if not date or not train:
            return []
        out = []
        for k in dedup_keys:
            parts = k.split("|")
            if len(parts) >= 4 and parts[2] == date and parts[3] == train:
                out.append(k)
        return out

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
                         rec.get("train"), rec.get("date"), rec.get("seat"),
                         _mask_order_no(rec.get("order_no") or ""))
                cls, ono, raw = order_mod.classify_order_status(
                    rec.get("date"), rec.get("train"), rec.get("passengers"))
                rec.update({"classify": cls, "official_status": raw,
                            "order_no": ono or rec.get("order_no"),
                            "decision": "启动复核:%s" % cls})
                appcommon.upsert_order(op, okey, rec)
                LOG.info("[订单恢复] %s 官方状态=%s(%s)", okey, cls, raw)
                if cls == "cancelled":
                    # Task 72：精确键匹配清本地防重（旧子串匹配会误清 "G101"）；
                    # rec 缺 date/train 时记 warning 跳过该条，不中止整体恢复。
                    if not rec.get("date") or not rec.get("train"):
                        LOG.warning("[订单恢复] %s 缺少 date/train，跳过本地防重清理",
                                    okey)
                    else:
                        for dk in self._cancelled_dedup_keys(
                                rec, self.state.get("dedup", {})):
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
                due.sort(key=lambda x: -_safe_priority(x[1]))
                for i, task in due:
                    if stop_event is not None and stop_event.is_set():
                        break
                    try:
                        interval = self.task_interval(task)
                    except Exception as e:
                        # 兜底：task_interval 已做形状校验，此处只防未知异常崩进程
                        LOG.error("[配置] 任务「%s」计算轮询间隔异常，"
                                  "已用兜底间隔 %ss：%s",
                                  task.get("name"), self.base_interval, e)
                        interval = self.base_interval
                    try:
                        broke, recoverable = self._run_task(task)
                    except Exception as e:
                        LOG.error("[错误] 任务「%s」内部异常: %s", task["name"], e)
                        self._note_failure(task, "内部异常: {0}".format(e))
                        broke, recoverable = True, True
                    if (task.get("name") or "") in getattr(self, "_deleted_names", ()):
                        # 任务在本轮询中途被 GUI 删除：在途结果不再写回 state，
                        # 不重建条目；丢弃本轮调度记录（删任务后 state 无残留）
                        next_due.pop(i, None)
                        continue
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


def _merge_state_for_save(disk_state, mem_state, tombstoned):
    """Task 57 锁内合并：磁盘为底、内存覆盖、墓碑剔除。返回新 dict。

    背景：_save_state 旧代码在拿 file_lock 之前已从内存 deepcopy 出快照，
    写回时覆盖其它进程（launcher/gui）在竞态窗口内并发写入的变更 →
    丢任务/状态。改为锁内重读文件后按本规则合并再写回。

    - tasks：并发写方只增删任务条目 → 取并集；同一任务两边不一致时
      内存（引擎最新一轮的调度结果）获胜；tombstoned 中的名字一律剔除
      （内存已删，磁盘残留不得复活——Task 53 墓碑语义）。
    - dedup/retry：只有引擎写这两节 → 内存为准（empty_task_dedup 的
      显式清空必须被尊重，不能用磁盘旧值复活）。

    返回的是新 dict，但 tasks 条目值仍引用 mem/disk 的 live 对象——
    调用方必须 deepcopy 后再序列化（见 _save_state 的重试循环）。
    """
    disk_state = disk_state if isinstance(disk_state, dict) else {}
    mem_state = mem_state if isinstance(mem_state, dict) else {}
    tombstoned = tombstoned or ()
    disk_tasks = disk_state.get("tasks")
    if not isinstance(disk_tasks, dict):
        disk_tasks = {}
    mem_tasks = mem_state.get("tasks")
    if not isinstance(mem_tasks, dict):
        mem_tasks = {}
    merged_tasks = dict(disk_tasks)
    merged_tasks.update(mem_tasks)
    for name in tombstoned:
        merged_tasks.pop(name, None)
    mem_dedup = mem_state.get("dedup")
    mem_retry = mem_state.get("retry")
    return {
        "dedup": dict(mem_dedup) if isinstance(mem_dedup, dict) else {},
        "retry": dict(mem_retry) if isinstance(mem_retry, dict) else {},
        "tasks": merged_tasks,
    }


if __name__ == "__main__":
    eng = MonitorEngine()
    eng.run()