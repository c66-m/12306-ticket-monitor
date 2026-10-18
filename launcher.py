# -*- coding: utf-8 -*-
"""12306 抢票业务模块 —— 抢票引擎 / 任务库 / 车站搜索 / 与监控系统联动。

2026-10-13 界面重构：原「抢票启动器」独立界面已推翻，全部界面收敛进
gui.py 的单窗口面板（朴素 ttk，无主题/无图片/无动画）。本模块只保留
不依赖 tkinter 的业务能力：

    1. 抢票引擎：Grabber 线程（校验会话 → 循环查询 → 命中即下单）
    2. 任务库：grab_tasks.json 读写（每个任务 = 一份独立配置 + id/name/status）
    3. 配置：launcher_config.json 读写（预设/车站历史/查询历史等）
    4. 车站：全量索引惰性加载 + 模糊搜索 + 高铁/普速类型探测与缓存
    5. 联动：append_monitor_task（写入监控系统任务表）、merge_trains_from_monitor
    6. 其他：更新检查、日期区间解析、乘车人首跑导入

界面侧调用约定：gui.py 里所有对 launcher 的调用都走 `launcher.xxx` 限定名，
测试 patch 模块属性即可生效（与旧口径一致）。

（历史上本文件兼有 tkinter 界面，重构后界面全部删除；旧版备份在
_undo_backup/launcher.py.20261013-2230。）
"""

import json
import logging
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime, timedelta

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import appcommon
import browser_order
import filelock
import logutil
import notify
import order as order_mod
try:
    import orderflow  # 新链：阶段化下单管线（REBUILD_PLAN.md），缺失时回落旧链
except ImportError:
    orderflow = None
import passengers as passengers_mod
import ticket

import launcher_utils
from launcher_utils import (
    SEAT_OPTIONS, SEAT_ORDER, MONITOR_SEAT_CHOICES,
    FONT, _DEFAULT_LC, PURPOSE_LABELS, LABEL_TO_PURPOSE,
    _ORDER_NO_RE, _FW_TRANS,
    _is_soft_fail, purpose_of, purpose_map_of,
    new_grab_task,
    mask_id, mask_mobile,
    _mask_order_no, _mask_one_name, _mask_names, _mask_pii_text,
    _normalize_seat_types, _display_seat_types,
    parse_dt, fmt_countdown,
    is_emu, train_kind, fmt_date_tag,
    ip_locate,
    build_monitor_task,
    _norm_query,
)

__version__ = "1.0.0"
LAUNCHER_CFG_PATH = os.path.join(HERE, "launcher_config.json")
GRAB_TASKS_PATH = os.path.join(HERE, "grab_tasks.json")
LOG_DIR = os.path.join(HERE, "logs")

LOGQ = queue.Queue()


class _QHandler(logging.Handler):
    def emit(self, record):
        try:
            LOGQ.put(self.format(record))
        except Exception:
            pass


def _ensure_stdio():
    """pythonw 没有控制台，sys.stdout/stderr 是 None，Playwright 启动浏览器时
    调用 flush() 会抛 'NoneType' object has no attribute 'flush'，浏览器当场退出。

    与 gui._ensure_stdio 同名不同语义（那边接到 logging；这里落文件）。勿合并。
    """
    if sys.stdout is None or sys.stderr is None:
        if not os.path.isdir(LOG_DIR):
            os.makedirs(LOG_DIR, exist_ok=True)
        f = open(os.path.join(LOG_DIR, "launcher_stdio.log"), "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = f
        if sys.stderr is None:
            sys.stderr = f


_ensure_stdio()

LOG = logging.getLogger("launcher")
LOG.setLevel(logging.INFO)
if not LOG.handlers:
    if not os.path.isdir(LOG_DIR):
        os.makedirs(LOG_DIR, exist_ok=True)
    _fh = logutil.DayFileHandler(LOG_DIR, "launcher")  # 按天滚动，长跑跨天不丢日志
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    LOG.addHandler(_fh)
    _qh = _QHandler()
    _qh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    LOG.addHandler(_qh)


def log(msg):
    LOG.info(msg)


def load_launcher_config():
    lc = dict(_DEFAULT_LC)
    if os.path.exists(LAUNCHER_CFG_PATH):
        try:
            with open(LAUNCHER_CFG_PATH, "r", encoding="utf-8-sig") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                for k, v in _DEFAULT_LC.items():
                    if k in saved:
                        lc[k] = saved[k]
                # 保留未知键（如 merge_trains_from_monitor 写入的 synced_trains）：
                # 否则用户删掉的同步车次会在重启后被再次自动加回
                for k, v in saved.items():
                    if k not in _DEFAULT_LC:
                        lc[k] = v
        except Exception as e:
            log("[错误] launcher_config.json 读取失败：%s，使用默认配置" % e)
    # Task 104 扩展：presets 在加载入口一次性 sanitize（非 dict 条目记 warning
    # 后剔除；本体非 list 则按空处理）。Task 96d 只修了展示侧；_current_preset/
    # _save_preset/删除路径都对原始条目调 .get，脏条目 → AttributeError。
    # 后续 _save_preset/_delete_preset 写回的是已清理列表，文件自然被治愈。
    presets = lc.get("presets")
    if not isinstance(presets, list):
        log("[警告] launcher_config.json presets 非 list，已按空处理：%r" % (presets,))
        lc["presets"] = []
    else:
        clean = [p for p in presets if isinstance(p, dict)]
        if len(clean) != len(presets):
            log("[警告] launcher_config.json presets 存在非 dict 条目，已剔除 %d 个"
                % (len(presets) - len(clean)))
        lc["presets"] = clean
    return lc


def save_launcher_config(lc):
    # 原子写：写一半被杀会损坏全部配置（同文件 append_monitor_task 已是此范式）
    appcommon.atomic_write_json(LAUNCHER_CFG_PATH, lc)


def load_grab_tasks():
    """读取全部抢票任务（每个任务 = 一份独立配置 + id/name/status）。"""
    if os.path.exists(GRAB_TASKS_PATH):
        try:
            with open(GRAB_TASKS_PATH, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("tasks"), list):
                # Task 104：入口一次性 sanitize——非 dict 条目记 warning 后剔除。
                # （Task 96c 只修了展示侧 _add_row；_find/_new_task/_delete_task/
                # _upsert_task/_on_window_closed 全对原始条目调 t.get(...)，
                # 脏条目仍会崩；入口一次清理，后续写回的是已清理列表。）
                clean = []
                for t in data["tasks"]:
                    if isinstance(t, dict):
                        clean.append(t)
                    else:
                        log("[警告] grab_tasks.json 存在非 dict 条目，已剔除：%r" % (t,))
                return clean
        except Exception as e:
            log("[错误] grab_tasks.json 读取失败：%s" % e)
    return []


def save_grab_tasks(tasks):
    # 原子写：grab_tasks.json 是全部抢票任务的唯一存储，中途崩溃不能截断
    appcommon.atomic_write_json(GRAB_TASKS_PATH, {"tasks": tasks})


def _normalize_trains(raw, warn):
    """trains 字段归一化（Task 84a；与 engine.normalize_trains 同口径）：
    裸字符串（如漏写方括号的 "G101"）按单个车次处理并记警告，绝不逐字符拆
    （旧代码会拆成 ['G','1','0','1'] 静默漏单）；形状非法记警告后忽略；
    非字符串条目跳过。

    机械层已收敛到 appcommon.normalize_trains；本函数只留「警告文案」这层
    策略，调用方与测试看到的签名 (raw, warn) 不变。
    """
    return appcommon.normalize_trains(raw, warn,
                                     "[提醒] trains 为字符串，已按单个车次处理：%s",
                                     "[提醒] trains 形状非法，已忽略：%r")


_display_trains = appcommon.display_trains


# ----------------------------- 抢票引擎 -----------------------------

_SESSION_LOCK = threading.Lock()  # 多任务并发时，浏览器会话校验/登录串行化


class Grabber(threading.Thread):
    """后台抢票线程：校验会话 → 循环查询 → 命中即下单。

    result: 结束时写入 (ok, msg)。过程日志统一走 logging
    （launcher 日志队列 LOGQ 由界面汇总显示，落 logs/launcher_*.log）。
    logq：可选的额外队列（历史接口；默认 None 表示不加队列，避免同一行
    在界面上重复显示两份）。"""

    def __init__(self, lc, logq=None):
        super().__init__(daemon=True, name="grabber")
        self.lc = lc
        self.logq = logq
        self.stop_event = threading.Event()
        self.result = None

    def _log(self, msg):
        LOG.info(msg)  # 也落 logs/launcher_*.log：事后排查抢票过程全靠它
        if self.logq is not None:
            self.logq.put(msg)

    def stop(self):
        self.stop_event.set()

    def run(self):
        log = self._log
        try:
            self._run()
        except Exception as e:
            self.result = (False, "抢票线程异常：%s: %s" % (type(e).__name__, e))
            log("[错误] 抢票线程异常：%s: %s" % (type(e).__name__, e))

    # ---- 内部 ----

    def _run(self):
        log = self._log
        lc = self.lc
        # 历史文件路径与监控系统同源(config.history_file),别写错文件
        # 读失败（缺文件/坏档）按空配置走缺省名——appcommon 单源口径。
        # mon_cfg 留变量：下单分流要读 order_flow_mode / order_flow（新链开关）
        mon_cfg = appcommon.read_config_or_empty(os.path.join(HERE, "config.json"))
        hist_path = appcommon.history_path_of(mon_cfg, HERE)
        from_, to_ = (lc.get("from") or "").strip(), (lc.get("to") or "").strip()
        date = (lc.get("date") or "").strip()
        trains = _normalize_trains(lc.get("trains"), log)
        # Task 106：展示用归一化——裸字符串按单个席别（旧代码逐字符拆后
        # s in SEAT_NAME_TO_CODE 恒失败 → 明确失败；与 572 行 trains 同口径）。
        seats = [s for s in _display_seat_types(lc.get("seat_types")) if s]
        pri_raw = lc.get("seat_priority") or ""
        # Task 106：同上——裸字符串按单个姓名，绝不逐字符拆。
        names = [n for n in _display_seat_types(lc.get("passenger_names")) if n]

        if not (from_ and to_ and date):
            self.result = (False, "请先填好出发站 / 到达站 / 日期")
            return
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            self.result = (False, "日期格式应为 YYYY-MM-DD")
            return
        if not seats:
            self.result = (False, "请至少勾选一种席别")
            return
        orderable = [s for s in seats if s in ticket.SEAT_NAME_TO_CODE]
        if len(orderable) != len(seats):
            log("[提醒] 席别 %s 暂不支持自动下单，已跳过" % "、".join(
                s for s in seats if s not in orderable))
        seats = orderable
        if not seats:
            self.result = (False, "勾选的席别都无法自动下单，请改选其他席别")
            return
        parsed_pri = ticket.seat_rules_parse(pri_raw)
        if parsed_pri["rules"] or parsed_pri["bare"]:
            summary = ["%s=%s" % (tr, "/".join(ss))
                       for tr, ss in parsed_pri["rules"].items()]
            if parsed_pri["bare"]:
                summary.append("其余车次：%s" % "/".join(parsed_pri["bare"]))
            log("[席别] 席别规则：%s（每趟车按各自候选抢，车次顺序优先）" % "；".join(summary))
        for warn in parsed_pri["warnings"]:
            log("[提醒] 首选席别：%s" % warn)
        if not names:
            log("[提醒] 未选择乘车人，将尝试使用账号默认乘车人（可能失败）")

        try:
            name2code, code2name = ticket.load_station_map()
        except Exception as e:
            # 离线首跑：车站表下载失败。下单 worker 后续每一步都要联网，
            # 空表继续只会误报"车站无法识别"，故直接以明确失败收尾
            #（Task 26 round 2；引擎/监控侧走空表降级继续）。
            log("[网络] 车站数据加载失败：%s" % e)
            self.result = (False, "车站数据加载失败（网络异常），请联网后重试")
            return
        fc, tc = name2code.get(from_), name2code.get(to_)
        if not fc or not tc:
            # 新开车站可能不在本地缓存里：后台强制刷新一次，下次可查到
            ticket.note_station_missing(from_ if not fc else to_)
            self.result = (False, "车站无法识别：%s → %s" % (from_, to_))
            return

        # 1) 会话（多任务并发时串行校验，避免两个线程同时操作同一个浏览器）
        log("[会话] 正在校验登录状态…（复用本机登录信息，全程无浏览器窗口）")
        with _SESSION_LOCK:
            ok, who = False, ""
            for attempt in range(60):
                if self.stop_event.is_set():
                    break
                if browser_order.busy():
                    # 多任务并行：另一任务正占着浏览器（预热/下单/登录）。
                    # 抢锁超时 ≠ 未登录——等对方放锁再试，别把自己的任务误杀。
                    log("[会话] 浏览器被其他任务占用，15 秒后再试（已等 %d 次）" % (attempt + 1))
                    if self.stop_event.wait(15):
                        break
                    continue
                try:
                    ok, who = browser_order.check_session()
                    break
                except Exception as e:
                    if browser_order.busy():
                        # 竞态：busy() 探完到拿锁之间被其他任务抢先——回到等待
                        continue
                    log("[错误] 会话校验失败：%s" % e)
                    ok, who = False, str(e)
                    break
            if not self.stop_event.is_set() and not ok:
                log("[会话] 当前未登录（%s），尝试拉起登录窗口，请在浏览器里完成登录…" % who)
                try:
                    browser_order.login(timeout_sec=240, stop_event=self.stop_event,
                                        lock_timeout=120)
                    if not self.stop_event.is_set():
                        ok, who = browser_order.check_session()
                except Exception as e:
                    log("[错误] 登录过程异常：%s" % e)
        if not ok:
            msg = "已手动停止" if self.stop_event.is_set() else "登录失败或超时：%s" % who
            self.result = (False, msg)
            log("[错误] %s" % self.result[1])
            return
        log("[会话] 已登录：%s" % who)

        # 2) 预热：开抢前 lead 分钟登录并停在车票列表页（到点只做 预订→提交）
        st = parse_dt(lc.get("start_time"))
        warm = None
        lead = 0
        try:
            lead = max(3, int(lc.get("warm_minutes") or 10))  # 至少提前 3 分钟：登录+预热需要时间
        except Exception:
            pass
        if lead > 0 and st and datetime.now() < st - timedelta(minutes=lead):
            _wa = st - timedelta(minutes=lead)
            log("[预热] 距开抢较久，%s 开始登录预热" % _wa.strftime("%H:%M:%S"))
            if self.stop_event.wait((_wa - datetime.now()).total_seconds()):
                return
        if lead > 0 and st and datetime.now() < st:
            warm_info = {
                "train_code": trains[0] if trains else "?",
                "from_code": fc, "from_name": from_,
                "to_code": tc, "to_name": to_,
            }
            try:
                log("[预热] 开始预热（登录 + 打开车票列表页）…")
                warm = browser_order.warm_up(warm_info, date, headless=False,
                                             wait_code=(trains[0] if trains else None),
                                             stop_event=self.stop_event)
                log("[预热] 预热就绪，浏览器保持在线，等待 %s 到点立即下单" % st.strftime("%H:%M:%S"))
            except Exception as e:
                log("[预热] 预热失败（%s），到点改用普通下单流程" % e)
                warm = None
            if self.stop_event.wait((st - datetime.now()).total_seconds()):
                if warm is not None:
                    try:
                        warm.close()
                    except Exception:
                        pass
                return

        # 3) 抢票主循环
        poll = max(1.0, float(lc.get("poll_seconds") or 3))
        n = 0
        last_target = None   # (车次, 席别)：同一目标连续失败计数用
        fail_streak = 0
        busy_n = 0           # 系统繁忙连续次数，用于冷却退避
        bad = set()          # {(车次, 席别)}：网页端下单页不下发的组合，永久跳过
        ambiguous_retries = 0  # 提交后结果未知且官方查无订单的安全重试计数
        MAX_ORDER_FAILS = 5  # 同一目标连续失败这么多次就停，不再无限重复
        exc_key, exc_streak = None, 0  # 同一异常连续计数（Task 55a：防吞异常无限活锁）
        MAX_SAME_EXC = 3     # 同一异常连续这么多次 → 计入失败走正常停止逻辑

        def note_exc(e):
            """同一异常（类名+消息）连续计数；达到 MAX_SAME_EXC 返回 True（应停止）。"""
            nonlocal exc_key, exc_streak
            key = (type(e).__name__, str(e))
            if key == exc_key:
                exc_streak += 1
            else:
                exc_key, exc_streak = key, 1
            return exc_streak >= MAX_SAME_EXC

        try:
            while not self.stop_event.is_set():
                n += 1
                try:
                    rows = ticket.query_tickets(fc, tc, date, purpose=purpose_of(lc))
                except Exception as e:
                    log("[错误] 余票查询失败：%s（%s 秒后重试）" % (e, int(poll)))
                    if self.stop_event.wait(poll):
                        break
                    continue

                info, seat, seat_rank = None, None, None
                by_code = {}
                for row in rows:
                    p = ticket.parse_row(row, code2name, date)
                    by_code.setdefault(p.get("train_code"), p)
                for code in (trains or list(by_code)):   # 按点选顺序（留空=全部车次）
                    p = by_code.get(code)
                    if not p:
                        continue
                    avail = p.get("available_seats") or {}
                    # 每趟车各自的席别候选（唯一口径 ticket.seat_candidates_for）：
                    # 「车次=席别」专属规则 ∩ 勾选集，交集空=该车跳过；无规则的车
                    # 按全局偏好排序。候选永远与该车实际有票求交。
                    cand = ticket.seat_candidates_for(code, seats, pri_raw, avail)
                    for rank, s in enumerate(cand, 1):
                        if (code, s) not in bad:
                            info, seat, seat_rank = p, s, (rank, len(cand))
                            break
                    if info:
                        break

                if info:
                    log("[有票] %s %s %s %s→%s 余%s（%s发车）！开始下单…%s" % (
                        date, info["train_code"], seat,
                        info["from_name"], info["to_name"],
                        info["available_seats"].get(seat), info["start_time"],
                        "（候选 %d/%d）" % seat_rank if seat_rank else ""))
                    try:
                        attempt_ts = time.time()
                        sc = ticket.SEAT_NAME_TO_CODE[seat]
                        # 网页端下单页不下发「无座」：按同价席别改判（动车组→二等座，
                        # 普速→硬座，见 ticket.ORDER_SEAT_ALIAS / EMU_SEAT_ALIAS）
                        sc, _alias = ticket.order_seat_code(seat, sc, info.get("train_code"))
                        if _alias:
                            log("[席别] %s 网页端下单页不下发，按同价改判为 %s 下单" % (seat, _alias))
                        # 新链分流：order_flow_mode（默认 True）走阶段化管线
                        # orderflow；旧链保留到实跑验证成功后退役（双链并存）
                        if orderflow is not None and mon_cfg.get("order_flow_mode", True):
                            ok, msg, extra = orderflow.run_orderflow(
                                info, seat, sc, names, date, headless=False,
                                purpose=purpose_of(lc), purpose_map=purpose_map_of(lc),
                                warm=warm, alias_name=_alias,
                                should_stop=self.stop_event.is_set,
                                flow_cfg=mon_cfg.get("order_flow") or {}, logger=LOG)
                        else:
                            ok, msg, extra = browser_order.order_via_browser(
                                info, seat, sc, names, date, headless=False, verify_timeout=90,
                                purpose=purpose_of(lc), purpose_map=purpose_map_of(lc),
                                warm=warm, alias_name=_alias,
                                should_stop=self.stop_event.is_set)
                    except RuntimeError as e:
                        if note_exc(e):
                            self.result = (False, "下单异常连续 %d 次（%s: %s），已自动停止"
                                           % (MAX_SAME_EXC, type(e).__name__, e))
                            log("[错误] %s" % self.result[1])
                            return
                        log("[错误] %s（5 秒后重试）" % e)
                        if self.stop_event.wait(5):
                            break
                        continue
                    except Exception as e:
                        if note_exc(e):
                            self.result = (False, "下单异常连续 %d 次（%s: %s），已自动停止"
                                           % (MAX_SAME_EXC, type(e).__name__, e))
                            log("[错误] %s" % self.result[1])
                            return
                        log("[错误] 下单异常：%s: %s（5 秒后重试）" % (type(e).__name__, e))
                        if self.stop_event.wait(5):
                            break
                        continue
                    exc_key, exc_streak = None, 0  # 本次下单尝试正常返回：异常 streak 断开
                    if ok:
                        self.result = (True, msg)
                        # Task 84c：日志路径 PII 脱敏（订单号打码）；self.result /
                        # 历史记录 / 通知里的原文不动，用户仍能在界面看到完整订单号
                        log("[抢到] %s" % _mask_pii_text(msg))
                        try:
                            order_no = (extra or {}).get("order_no") or ""
                            appcommon.append_history(
                                hist_path,
                                {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                                 "task": lc.get("name") or "启动器抢票",
                                 "result": "success", "train": info["train_code"],
                                 "date": date, "from": info["from_name"],
                                 "to": info["to_name"], "seat": seat,
                                 "passengers": names, "order_no": order_no,
                                 "message": msg, "notify": "已通知(启动器)"})
                        except Exception as e:
                            log("[提醒] 购票历史写入失败：%s" % e)
                        self._notify_success(info, seat, msg)
                        return
                    extra = extra or {}
                    if browser_order.session_failure_is_permanent(msg):
                        # 下单中途登录态被踢：再点下去每轮都是同一句失败，
                        # 且会一路走到下面的 fail_streak 计数（要凑满 5 次才停）。
                        # 确认失效就直接收尾，提示用户点「重新登录」。
                        self.result = (False, "登录会话已失效（%s）。请点界面上的"
                                              "「重新登录」后重新开抢" % msg)
                        log("[错误] %s" % self.result[1])
                        return
                    if extra.get("reason") == "ambiguous":
                        # 提交确认后结果未知：先回读官方订单接口做时间戳归因。
                        # 订单已生成且下单时间对得上=本次成功；确认无订单=安全重试；
                        # 其余（查不到/更早旧单/其它行程挡路）=停下交人工，防重复下单。
                        cls, ono, raw, recent = order_mod.classify_with_time(
                            date, info["train_code"], names, not_before_ts=attempt_ts)
                        if cls == "unpaid" and recent:
                            order_no = ono or (recent.get("order_no") or "")
                            self.result = (True,
                                "订单已提交成功（未支付）：订单号 %s，下单时间 %s，请尽快去 12306 支付"
                                % (order_no, recent.get("order_time") or "未知"))
                            # Task 84c：日志里的订单号打码（与 Task 61 同口径）；
                            # self.result[1] 原文保留，用户界面仍显示完整订单号
                            log("[抢到] %s" % _mask_pii_text(self.result[1]))
                            try:
                                appcommon.append_history(
                                    hist_path,
                                    {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                                     "task": lc.get("name") or "启动器抢票",
                                     "result": "success", "train": info["train_code"],
                                     "date": date, "from": info["from_name"],
                                     "to": info["to_name"], "seat": seat,
                                     "passengers": names, "order_no": order_no,
                                     "message": self.result[1], "notify": "已通知(启动器)"})
                            except Exception as e:
                                log("[提醒] 购票历史写入失败：%s" % e)
                            self._notify_success(info, seat, self.result[1])
                            return
                        if cls in ("none", "cancelled"):
                            ambiguous_retries += 1
                            if ambiguous_retries >= 3:
                                self.result = (False,
                                    "连续多次提交后结果未知且官方查无订单，请人工核对后重新开抢")
                                log("[错误] %s" % self.result[1])
                                return
                            log("[提醒] 提交结果未知但官方确认无此订单（第 %d 次），继续重试" % ambiguous_retries)
                            if self.stop_event.wait(2):
                                break
                            continue
                        self.result = (False, "订单提交后结果未知——请先到 12306 查「未支付订单」："
                                              "有单就支付或取消，确认无单后再重新开抢")
                        log("[错误] %s（下单返回：%s；官方核验：%s）" % (self.result[1], msg, raw))
                        return
                    if extra.get("reason") == "dup":
                        if extra.get("dup_kind") == "行程冲突":
                            # 行程冲突 ≠ 本行程已有订单：别报"票已到手"误导去支付
                            self.result = (False, "12306 提示行程冲突——可能是其它行程的未支付订单挡路，"
                                                  "请到「未完成订单」查证处理后重新开抢")
                            log("[提醒] 下单返回：%s" % msg)
                            return
                        # 本行程已有订单：回读官方接口 + 下单时间归因，确认是不是本次提交的
                        cls, ono, raw, recent = order_mod.classify_with_time(
                            date, info["train_code"], names, not_before_ts=attempt_ts)
                        result_kind = "dup"
                        if cls == "paid":
                            self.result = (True, "该行程订单已支付，请查收")
                        elif cls == "unpaid" and recent:
                            result_kind = "success"
                            self.result = (True,
                                "本次已提交成功（未支付）：订单号 %s，下单时间 %s，请尽快去 12306 支付"
                                % (ono or (recent.get("order_no") or ""),
                                   recent.get("order_time") or "未知"))
                        elif cls == "unpaid":
                            self.result = (True,
                                "账号存在该行程更早的未支付订单（订单号 %s），非本次提交，请核对后尽快支付"
                                % (ono or "未知"))
                        else:
                            self.result = (True, "检测到该行程已有订单，请尽快去 12306 完成支付（官方核验：%s）" % raw)
                        log("[提示] 下单返回：%s" % msg)
                        try:
                            appcommon.append_history(
                                hist_path,
                                {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                                 "task": lc.get("name") or "启动器抢票",
                                 "result": result_kind, "train": info["train_code"],
                                 "date": date, "from": info["from_name"],
                                 "to": info["to_name"], "seat": seat,
                                 "passengers": names, "order_no": ono or "",
                                 "message": self.result[1],
                                 "notify": "已通知(启动器)" if result_kind == "success" else ""})
                        except Exception:
                            pass
                        self._notify_success(info, seat, self.result[1])
                        return
                    if extra.get("need_captcha"):
                        self.result = (False, "触发滑块验证：%s（脚本不自动过验证码，已停止）" % msg)
                        log("[错误] %s" % self.result[1])
                        return
                    if extra.get("alias_seat") and any(
                            k in msg for k in ("余票", "不足", "无票", "不下发", "改判")):
                        # 勾选无座、实际按同价硬座下单：硬座此刻没票/不可售属车次状态问题，
                        # 继续监控，不计入「同目标连续失败」
                        log("[提醒] %s（按 %s 监控中，继续等待）" % (msg, seat))
                        if self.stop_event.wait(2):
                            break
                        continue
                    if extra.get("reason") == "seat_unavailable" or "网页端不提供席别" in msg:
                        # 网页端下单页的可售席别由服务端下发（普速车常常没有「无座」）：
                        # 这个组合再点多少次都不会变，加入跳过名单，只在剩余席别里继续抢
                        bad.add((info["train_code"], seat))
                        log("[提醒] %s；已跳过 %s %s" % (msg, info["train_code"], seat))
                        remain = [(c, s) for c in (trains or list(by_code))
                                  for s in seats if (c, s) not in bad]
                        if not remain:
                            self.result = (False, "勾选的席别在 12306 网页端下单页都不可选，"
                                                  "已停止：%s" % msg)
                            log("[错误] %s" % self.result[1])
                            return
                        if self.stop_event.wait(2):
                            break
                        continue
                    if "页面上没有" in msg:
                        # 抢输竞速/席别已售罄：不是下单失败，继续监控即可
                        log("[提醒] %s（继续监控）" % msg)
                        if self.stop_event.wait(3):
                            break
                        continue
                    if _is_soft_fail(msg):
                        # Task 65i：瞬时繁忙/排队是软失败，不计入 fail_streak——
                        # 连续 5 次 busy（~45s）就触发"连续失败已自动停止"是 bug。
                        # busy_n 仍驱动退避（3s→30s 封顶）；持续繁忙一直重试，
                        # 直到用户停止（stop_event）。
                        busy_n += 1
                        wait_s = min(30, 3 * busy_n)
                        log("[错误] 下单遇系统繁忙：%s（%d 秒后重试，累计 %d 次）" % (msg, wait_s, busy_n))
                        if self.stop_event.wait(wait_s):
                            break
                        continue
                    target = (info["train_code"], seat)
                    if target == last_target:
                        fail_streak += 1
                    else:
                        last_target, fail_streak = target, 1
                    busy_n = 0  # 非繁忙类失败：退避计数复位，别一直卡在 30 秒档
                    log("[错误] 下单未成功：%s（同目标第 %d 次失败）" % (msg, fail_streak))
                    if fail_streak >= MAX_ORDER_FAILS:
                        self.result = (False, "同一车次席别连续 %d 次下单失败，已自动停止：%s" % (fail_streak, msg))
                        log("[错误] %s" % self.result[1])
                        return
                    if self.stop_event.wait(3):
                        break
                    continue

                busy_n = 0  # 本轮无下单繁忙：复位退避，恢复正常轮询节奏
                exc_key, exc_streak = None, 0  # 完整空轮询一轮：异常 streak 断开
                if n % 4 == 1 or n == 1:
                    log("[运行] 第 %d 轮无票（%s %s %s），%s 秒后再查" % (
                        n, date, "/".join(trains) or "全部车次", from_ + "→" + to_, int(poll)))
                if self.stop_event.wait(poll):
                    break
        finally:
            if warm is not None:
                try:
                    warm.close()
                except Exception:
                    pass

    def _notify_success(self, info, seat, msg):
        log = self._log
        try:
            with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
            nc = (cfg.get("notify") or {}).get("email") or {}
            if not nc.get("enabled"):
                log("[提醒] 邮件通知未启用，跳过")
                return
            ok, m = notify.send_email(nc, "[抢票成功] %s %s %s" % (
                info["train_code"], info["query_date"], seat),
                "抢到票了！\n\n车次：%s\n日期：%s\n区间：%s → %s\n席别：%s\n\n%s\n\n请在 10 分钟内完成支付。" % (
                    info["train_code"], info["query_date"], info["from_name"],
                    info["to_name"], seat, msg))
            log("[提醒] 通知：%s" % m)
        except Exception as e:
            log("[错误] 发送通知失败：%s" % e)


# ----------------------------- 与监控系统联动 -----------------------------

def merge_trains_from_monitor(lc, saver=None):
    """把监控系统（config.json tasks）里的车次合并进抢票车次列表。

    规则：监控里新出现的车次自动加入；被同步过的车次记录在
    synced_trains，用户在抢票侧删掉后不会再次自动加回。返回是否合并。"""
    try:
        with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except Exception as e:
        log("[提醒] 读取监控系统 config.json 失败，本轮不合并车次：%s" % e)
        return False
    mon = []
    for t in (cfg.get("tasks") or []):
        # Task 84a：字符串 trains 按单车次归一化，绝不逐字符拆
        for tr in _normalize_trains(t.get("trains"), log):
            if tr and tr not in mon:
                mon.append(tr)
    cur = _normalize_trains(lc.get("trains"), log)
    old_synced = set(lc.get("synced_trains") or [])
    synced = set(old_synced)
    added = []
    for tr in mon:
        if tr not in synced and tr not in cur:
            cur.append(tr)
            added.append(tr)
        synced.add(tr)
    if added or synced != set(lc.get("synced_trains") or []):
        lc["trains"] = cur
        lc["synced_trains"] = sorted(synced)
        (saver or save_launcher_config)(lc)
        if added:
            log("[运行] 监控系统新增车次已同步：%s" % "、".join(added))
        return True
    return False


# ----------------------------- 更新检查 -----------------------------

def check_update(lc):
    """拉取 update_url 指向的版本清单。清单格式：
    {"version": "1.1.0", "notes": "...", "download": "https://..."}
    版本号三段数字逐段比较。返回 (has_new, new_version, notes, download)。"""
    url = (lc.get("update_url") or "").strip()
    if not url:
        return False, None, None, None
    try:
        r = requests.get(url, timeout=8)
        d = r.json()
        remote = str(d.get("version") or "")
        if not re.match(r"^\d+(\.\d+){1,2}$", remote):
            return False, None, None, None
        cur = tuple(int(x) for x in __version__.split("."))
        rmt = tuple(int(x) for x in remote.split("."))
        if rmt > cur:
            return True, remote, d.get("notes", ""), d.get("download", "")
    except Exception as e:
        log("[更新] 检查失败：%s" % e)
    return False, None, None, None


# ----------------------------- 监控任务追加 -----------------------------

# 同进程对 config.json/state.json 的读-改-写串行锁：mtime 冲突检测在
# Windows 上有 ~15.6ms 系统时钟量化盲区，同进程并发必须靠锁兜住；
# 跨进程仍靠 mtime 比对（盲区可接受，见 RULES）。
_CFG_WRITE_LOCK = threading.Lock()


# Windows 占用退避重试的 os.replace（实现单点在 appcommon）
_atomic_replace = appcommon.replace_with_retry


def append_monitor_task(task, start_now=True):
    """把任务写入监控系统的 config.json 与 state.json，并返回任务名。
    监控引擎主循环每轮 _sync_config() 检测 mtime 变化后自动重建调度表，
    处于「监控中」状态的新任务会被自动捡起来，无需重启监控软件。"""
    with _CFG_WRITE_LOCK:  # 同进程多窗口并发串行化
        cfg_path = os.path.join(HERE, "config.json")
        # 临时名带线程标识：同进程多窗口并发追加时共用一个名字会互相踩
        # （Windows 下 os.replace 撞上别人打开的句柄直接 PermissionError）
        tmp = cfg_path + ".launcher%s" % threading.get_ident()
        # 读-改-写必须串行化：监控系统 GUI 是独立进程，也在改同一个 config.json。
        # 只比 mtime 是 TOCTOU——实测 3 个线程并发追加任务，最后只剩 1 条。
        # 锁文件用 filelock（进程被强杀时由系统释放）；mtime 比对留作第二道，
        # 能发现「锁外」的改动（手工编辑、其它工具）。
        try:
            with filelock.file_lock(cfg_path + ".lock"):
                for _ in range(3):
                    try:
                        before = os.path.getmtime(cfg_path)
                    except OSError:
                        before = None
                    with open(cfg_path, "r", encoding="utf-8-sig") as f:
                        cfg = json.load(f)
                    cfg.setdefault("tasks", []).append(task)
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(cfg, f, ensure_ascii=False, indent=2)
                    # Task 66: config.json 含 SMTP 授权码等密钥，落盘 0600
                    # （os.replace 继承 tmp 权限；Windows 下 chmod 仅影响只读位）
                    os.chmod(tmp, 0o600)
                    try:
                        after = os.path.getmtime(cfg_path)
                    except OSError:
                        after = None
                    if after == before:
                        _atomic_replace(tmp, cfg_path)
                        break
                    # 撞车：本轮作废，带着对方的新内容重来
                else:
                    _atomic_replace(tmp, cfg_path)  # 三次都撞车：以本方落盘收场（低概率，双方都是追加型写）
        except TimeoutError as e:
            # 锁争用超时：给用户明确提示，异常继续上抛给调用方的
            # messagebox.showerror（不崩）。任务未落盘，用户稍后重试。
            log("[错误] 文件被占用，稍后重试：%s" % cfg_path)
            raise TimeoutError("文件被占用，稍后重试") from e
        # state.json 条目（与 gui.mark_task_created 的无 app 分支保持一致）
        state_path = appcommon.state_path_of(cfg, HERE)
        try:
            with filelock.file_lock(state_path + ".lock"):
                if os.path.exists(state_path):
                    try:
                        with open(state_path, "r", encoding="utf-8-sig") as f:
                            state = json.load(f)
                    except Exception as e:
                        # state.json 损坏：挪档留证（带时间戳，反复损坏不互相覆盖），再按
                        # 只含本任务的新状态重建——保住「立即启动」语义，不让它静默降级成
                        # 未启动。其它任务的状态/防重记录在坏档里，引擎会按未启动重建。
                        bad = "%s.bad-%s" % (state_path, time.strftime("%Y%m%d-%H%M%S"))
                        try:
                            os.replace(state_path, bad)
                        except OSError:
                            # 挪不动（如杀毒软件占用）：保住坏档要紧，跳过状态写入，
                            # 任务将以「未启动」落库——这点必须让用户知道
                            log("[错误] 读取 state.json 失败且挪档失败（文件被占用？）：%s；"
                                "本次只写任务不写状态，任务「%s」将按未启动落库，"
                                "请人工处理坏档后再启动它" % (e, task["name"]))
                            return task["name"]
                        log("[错误] state.json 损坏（%s），已挪档为 %s 并按空状态重建。"
                            "其它任务的运行状态与防重记录都在坏档里——请尽快到 12306"
                            "「未支付订单」核对在途行程，避免重复下单" % (e, bad))
                        state = {}
                else:
                    state = {}  # 首次使用：还没有状态文件，从空状态开始是正常的
                entry = state.setdefault("tasks", {}).setdefault(task["name"], {})
                entry["status"] = "monitoring" if start_now else "paused"
                entry.setdefault("fail_streak", 0)
                entry.setdefault("last_poll", 0)
                entry["message"] = ("启动器创建，立即启动" if start_now
                                    else "启动器创建，未启动")
                appcommon.write_state(state_path, state, tmp_kind="launcher")
        except TimeoutError as e:
            # 锁争用超时：config 段已写、state 段未写。给用户明确提示，
            # 异常继续上抛给调用方的 messagebox.showerror（不崩）。
            # 引擎 _sync_config 下次会为该任务补建缺省状态条目。
            log("[错误] 文件被占用，稍后重试：%s" % state_path)
            raise TimeoutError("文件被占用，稍后重试") from e
        return task["name"]


# ----------------------------- 车站搜索 -----------------------------

_STATION_INDEX = None


def get_station_index():
    """惰性加载车站全量索引（首次从 station_name.js 下载后缓存到 station_index.json）。

    Task 101b：加载失败不永久缓存——失败时保持 _STATION_INDEX 为 None，下次调用
    重试（网络恢复后本会话车站搜索自动恢复）；失败期间返回 []，调用方可直接迭代。
    """
    global _STATION_INDEX
    if _STATION_INDEX is None:
        try:
            _STATION_INDEX = ticket.load_station_index()
            log("[车站] 车站索引就绪：%d 个车站" % len(_STATION_INDEX))
        except Exception as e:
            # 旧文案"只按站名匹配"不属实：索引为空时搜索无候选，并非降级为站名匹配
            log("[提醒] 车站索引加载失败（%s）：车站搜索暂不可用，可手打站名/电报码继续"
                % e)
    return _STATION_INDEX or []


def _query_variants(q):
    """站名查询的容错变体，解决"杭州市""杭州东站"这类带后缀的输入搜不到站。

    站库 3404 条实测没有任何站名含"站"字，故剥"站"恒安全；"市"是真实站名后缀
    （白山市/小市/龙市…共 16 条），所以原串整体优先，剥掉之后的只作次选。"""
    out = [q]
    t = q
    if t.endswith("站"):
        t = t[:-1]
        if t:
            out.append(t)
    if t.endswith("市") and len(t) > 1:
        out.append(t[:-1])
    return out


def search_stations(text, limit=12):
    """本地模糊搜索车站，按匹配度降序：精确站名 > 站名前缀 > 简拼/全拼/
    电报码前缀 > 站名包含。同档按站名长度短者优先（长葛排在长葛北前）。

    旧实现汉字查询只走"站名包含"档且按索引序截断，长葛这类普速小站常被
    挤出结果。返回 [{"name","code","py","spy"}, ...]，纯本地计算。"""
    q = _norm_query(text)
    if not q:
        return []
    qs = _query_variants(q)
    name2code_rev = {}
    scored = []
    for idx, st in enumerate(get_station_index()):
        name, spy, py = st["name"], st["spy"], st["py"]
        code = st["code"].lower()
        if name not in name2code_rev:
            name2code_rev[name] = code
        name2code_rev[name] = code
        # 多个变体各打一次分，取最匹配的那个（"杭州市"→"杭州"精确命中 0 档）
        score = None
        for qv in qs:
            if qv == name:
                s = 0                       # 精确站名：唯一首选项
            elif name.startswith(qv):
                s = 1
            elif spy == qv or py == qv or code == qv:
                s = 2
            elif spy.startswith(qv):
                s = 3
            elif py.startswith(qv):
                s = 4
            elif code.startswith(qv):
                s = 5
            elif qv in name:
                s = 6
            elif qv in py:
                s = 7
            else:
                continue
            if score is None or s < score:
                score = s
        if score is None:
            continue
        # 同分内普速站优先于高铁/动车站，再按站名长度（用户口径：普通车站靠前）
        # name2code_rev 存的是小写 code，load_station_kinds() 的键是大写：查之前转大写
        kind = load_station_kinds().get(name2code_rev.get(name, "").upper(), "")
        kind_rank = 0 if "普速" in kind else (1 if kind else 2)
        scored.append((score, kind_rank, len(name), idx, st))
    scored.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
    out = [x[4] for x in scored]
    return out if limit is None else out[:limit]


# --------------------------- 车站类型（高铁/普速） ---------------------------
#
# 12306 没有公开的「车站类型」字段，但余票接口对**同城车站做聚合**：查许昌东
# 或查许昌，返回的都是许昌市全部车站的车次，而每行的 p6/p7 是这趟车真实的
# 上/下车站码。于是「本站 ⇄ 大枢纽」查一轮、按 p6/p7 统计车次字母（G=高铁、
# D/C=动车、其余=普速），就能反推每个站到底是高铁站还是普速站。
# 实测：许昌东=高铁 许昌=普速 长葛=普速 长葛北=高铁 驻马店西=高铁。
# 结果缓存进 station_kind.json，只探一次。

STATION_KIND_PATH = os.path.join(HERE, "station_kind.json")
_KIND_HUBS = ["BJP", "SHH", "GZQ", "ZZF"]      # 北京 / 上海 / 广州 / 郑州
_KIND_ORDER = {"高铁": 0, "动车": 1, "普速": 2}
_KIND_COLOR = {"高铁": "#0969da", "动车": "#1a7f37", "普速": "#6e7781"}
_station_kinds = {}
_kind_lock = threading.Lock()
_kind_pending = set()


def load_station_kinds():
    """惰性读车站类型缓存：{电报码: "高铁"/"动车"/"普速"/"高铁+普速"}。"""
    global _station_kinds
    with _kind_lock:
        if not _station_kinds:
            try:
                with open(STATION_KIND_PATH, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    _station_kinds = {k.upper(): v for k, v in data.items() if v}
            except Exception:
                _station_kinds = {}
        return _station_kinds


def save_station_kinds():
    with _kind_lock:
        data = dict(_station_kinds)
    if not data:
        return
    # Task 101a：tmp 文件名带线程后缀（Task 62 口径）——主线程（查询后被动学习）
    # 与后台探测线程并发写同一 tmp 会交错损坏缓存文件；各自独立 tmp +
    # os.replace 原子换名，写坏只影响自己那份（快照拷贝自带锁内一致性）。
    tmp = "%s.tmp%d" % (STATION_KIND_PATH, threading.get_ident())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, STATION_KIND_PATH)
    except Exception as e:
        log("[提醒] 车站类型缓存写入失败：%s" % e)


def station_kind(code):
    """车站类型文案（没探测过返回空串）。"""
    return load_station_kinds().get((code or "").upper(), "")


def _merge_kind(code, kind):
    """把一次观测合并进缓存：既有高铁又有普速时拼成 "高铁+普速"。

    kind 可能是多值串（`probe_city_kinds` 一次返回 "高铁+动车+普速"），必须拆开逐个
    合并：整串当成一个 part 会写出 "高铁+普速+高铁+动车+普速" 这种重复值
    （station_kind.json 里已经出现过，缓存文件被这么写脏过）。"""
    code = (code or "").upper()
    if len(code) != 3 or not kind:
        return False
    new_parts = {p for p in str(kind).split("+") if p}
    if not new_parts:
        return False
    load_station_kinds()  # 确保已加载（内部自带锁）
    with _kind_lock:  # 探测线程与主线程都会调这里：变更必须锁内做，
        old = _station_kinds.get(code)  # 否则与 save_station_kinds 的拷贝撞并发修改
        parts = {p for p in (old or "").split("+") if p}
        if new_parts <= parts:
            return False
        parts |= new_parts
        _station_kinds[code] = "+".join(sorted(parts, key=lambda k: _KIND_ORDER.get(k, 9)))
        return True


def learn_station_kinds(infos):
    """被动学习：从刚查到的车次里顺手记下车站类型（不联网，只累加）。"""
    changed = False
    for info in infos or []:
        head = (info.get("train_code") or "").strip().upper()[:1]
        if not head:
            continue
        if head == "G":
            kind = "高铁"
        elif head in "DC":
            kind = "动车"
        elif head in "KTLZYS" or head.isdigit():
            kind = "普速"
        else:
            continue
        for st in (info.get("from_code"), info.get("to_code")):
            if _merge_kind(st, kind):
                changed = True
    if changed:
        save_station_kinds()


def probe_city_kinds(code):
    """联网探测：返回 {站码: 类型}（同城车站一次查询顺带全算出来）。

    依次查「本站 → 大枢纽」，本站自身有结论就短路，最多 4 次请求。"""
    date = (datetime.now() + timedelta(days=8)).strftime("%Y-%m-%d")
    code = (code or "").upper()
    kinds = {}
    for hub in _KIND_HUBS:
        if hub == code:
            continue
        try:
            rows = ticket.query_tickets(code, hub, date)
        except Exception as e:
            log("[提醒] 车站类型探测失败（%s→%s）：%s" % (code, hub, e))
            continue
        for row in rows:
            f = row.split("|")
            if len(f) < 8:
                continue
            head = (f[3] or "").strip().upper()[:1]
            if not head:
                continue
            kind = "高铁" if head == "G" else ("动车" if head in "DC" else "普速")
            for st in (f[6], f[7]):
                if st and len(st) == 3:
                    kinds.setdefault(st, set()).add(kind)
        if kinds.get(code):
            break
    return {s: "+".join(sorted(v, key=lambda k: _KIND_ORDER.get(k, 9))) for s, v in kinds.items()}


def request_station_kinds(codes, on_done=None):
    """后台探测若干车站类型，结果写缓存；有新数据时回调 on_done()（子线程）。

    已在缓存、正在探测的站自动跳过；同城车站共用一次查询。"""
    known = load_station_kinds()
    todo = []
    for c in codes or []:
        c = (c or "").upper()
        if len(c) == 3 and not known.get(c) and c not in _kind_pending:
            todo.append(c)
    if not todo:
        return False
    with _kind_lock:
        _kind_pending.update(todo)

    def work():
        changed = False
        try:
            for code in todo:
                try:
                    got = probe_city_kinds(code)
                except Exception:
                    got = {}
                for c, k in got.items():
                    if _merge_kind(c, k):
                        changed = True
        finally:
            with _kind_lock:
                for c in todo:
                    _kind_pending.discard(c)
        if changed:
            save_station_kinds()
            if on_done is not None:
                try:
                    on_done()
                except Exception:
                    pass

    threading.Thread(target=work, daemon=True, name="station-kind").start()
    return True


def _history_item_text(it):
    """历史查询记录条目的显示文本；非 dict 条目记 warning 返回 None（调用方跳过）。

    query_history 可能被手工改坏（如写成字符串数组），直接 it.get() 会
    AttributeError 炸掉整个对话框（Tasks 24/25/28 修过同类脏数据崩溃）。
    """
    if not isinstance(it, dict):
        LOG.warning("查询历史记录条目非 dict，已跳过：%r" % (it,))
        return None
    return "%s → %s    %s" % (it.get("from"), it.get("to"), it.get("date"))


# ----------------------------- 日期解析 -----------------------------

def resolve_dates(lc):
    """把「从 / 到」解析为待查日期列表（appcommon 单点口径：相差最多 5 天）。

    （原 LauncherApp._resolve_dates 静态方法；界面收敛后挪到模块级。）
    ValueError 转 RuntimeError 由调用方提示（保持旧契约）。"""
    try:
        dates, date_range = appcommon.parse_date_range(
            (lc.get("date") or "").strip(), (lc.get("date_to") or "").strip())
    except ValueError as e:
        raise RuntimeError(str(e))
    if date_range:  # launcher 契约：区间展开成逐日列表
        d0 = datetime.strptime(date_range[0], "%Y-%m-%d").date()
        d1 = datetime.strptime(date_range[1], "%Y-%m-%d").date()
        dates = [(d0 + timedelta(days=i)).isoformat()
                 for i in range((d1 - d0).days + 1)]
    return dates


# ----------------------------- 乘车人首跑导入 -----------------------------

def ensure_passengers():
    """首次运行：passengers.json 不存在时，把 config.json 任务里的乘车人名字
    迁入加密乘客库（证件号留空，待用户在「新增/编辑」里补全）。"""
    if os.path.exists(os.path.join(HERE, "passengers.json")):
        return
    names = []
    try:
        with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
        for t in (cfg.get("tasks") or []):
            for n in (t.get("passenger_names") or []):
                n = str(n).strip()
                if n and n not in names:
                    names.append(n)
    except Exception:
        pass
    if not names:
        return
    try:
        # Task 100c：首跑双进程竞速——exists 检查与写入之间对方可能新建
        # passengers.json；传 expect_stamp=None（"load 时文件不存在"语义），
        # 文件若已出现则 save 内指纹比对失败→放弃，不静默覆写对方数据。
        ok = passengers_mod.save_passengers([
            {"name": n, "id_type_code": "1", "id_no": "", "mobile": "",
             "is_default": i == 0, "is_adult": True}
            for i, n in enumerate(names)], expect_stamp=None)
    except Exception as e:
        log("[错误] 乘车人导入失败：%s" % e)
        return
    # Task 84d：拒写（False）不得记"已导入"成功日志；Task 84c：姓名打码
    if not ok:
        log("[错误] 乘车人导入被拒：磁盘已有数据在本机不可解密，已保护原数据（本次未导入）")
    else:
        log("[提醒] 已从监控任务导入乘车人：%s（证件号请点「新增/编辑」补全）"
            % _mask_names(names))
