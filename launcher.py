# -*- coding: utf-8 -*-
"""
12306 桌面抢票启动器 —— 一键抢票 / 定时开抢 / 加密存储 / 更新检查

与后台监控（monitor.py / 桌面版）的区别
    监控 = 常驻轮询，有票才动手；启动器 = 面向「开抢时刻」，倒计时到点自动出手，
    平时也能手动点「立即抢票」。两者共用同一套浏览器下单链路（browser_order.py）。

功能
    1. 乘车人信息：复用 passengers.py 加密存储（Windows DPAPI / Fernet），界面打码显示
    2. 车次管理：行程预设（添加 / 编辑 / 删除），车次、席别、日期随预设保存
    3. 位置信息：出发 / 到达站自由选择，一键 IP 定位显示当前城市
    4. 开抢时间：精确到秒，倒计时显示，到点自动开抢，提前 N 分钟提醒（可选邮件）
    5. 一键抢票：主按钮直接开抢（打开真实浏览器自动完成 查询→预订→乘车人→席别→提交）
    6. 数据安全：证件号 / 手机号只存加密文件，界面只显示掩码
    7. 界面：单窗紧凑布局，日志按事件分级着色
    8. 兼容性：Windows / macOS / Linux；浏览器自动探测 Edge → Chrome → Chromium
    9. 日志：logs/launcher_YYYYMMDD.log + 界面日志区
   10. 自动更新：启动时向 update_url 拉版本清单比对，有新版弹提示（默认关闭）

用法
    python launcher.py        # 打开启动器
    pythonw launcher.py       # Windows 无控制台运行（桌面快捷方式用它）
"""

import calendar
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import browser_order
import notify
import passengers as pax_mod
import ticket as tk_mod

__version__ = "1.0.0"
LAUNCHER_CFG_PATH = os.path.join(HERE, "launcher_config.json")
LOG_DIR = os.path.join(HERE, "logs")

SEAT_OPTIONS = ["商务座", "特等座", "优选一等座", "一等座", "二等座", "高级软卧",
               "软卧", "硬卧", "软座", "硬座", "无座"]
SEAT_ORDER = {v: i for i, v in enumerate(SEAT_OPTIONS)}
# 监控系统任务的完整席别表（与 gui.py 的 SEAT_CHOICES 一致）
MONITOR_SEAT_CHOICES = ["商务座", "特等座", "优选一等座", "一等座", "二等座",
                       "高级软卧", "软卧", "硬卧", "软座", "硬座", "无座"]
LOG_COLOR = {
    "[有票]": "#d97706", "[抢到]": "#0969da", "[错误]": "#cf222e",
    "[会话]": "#6e7781", "[运行]": "#57606a", "[提醒]": "#9a6700", "[更新]": "#8250df",
    "[自动]": "#cf222e", "[启动]": "#0961da", "[停止]": "#6e7781", "[提示]": "#8250df",
}

LOGQ = queue.Queue()


class _QHandler(logging.Handler):
    def emit(self, record):
        try:
            LOGQ.put(self.format(record))
        except Exception:
            pass


def _ensure_stdio():
    """pythonw 没有控制台，sys.stdout/stderr 是 None，Playwright 启动浏览器时
    调用 flush() 会抛 'NoneType' object has no attribute 'flush'，浏览器当场退出。"""
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
    _fh = logging.FileHandler(os.path.join(
        LOG_DIR, "launcher_%s.log" % datetime.now().strftime("%Y%m%d")), encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    LOG.addHandler(_fh)
    _qh = _QHandler()
    _qh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    LOG.addHandler(_qh)


def log(msg):
    LOG.info(msg)


# ----------------------------- 配置读写 -----------------------------

_DEFAULT_LC = {
    "version": 1,
    "passenger_names": [],
    "from": "长葛",
    "to": "确山",
    "trains": ["K225", "K1969", "K925"],
    "seat_types": ["硬座"],
    "date": "",
    "date_to": "",
    "start_time": "",
    "remind_minutes": 10,
    "warm_minutes": 10,
    "poll_seconds": 3,
    "presets": [],
    "update_url": "",
    "station_history": ["长葛", "确山"],
    "purpose_code": "ADULT",
    "query_history": [],
}


def load_launcher_config():
    lc = dict(_DEFAULT_LC)
    if os.path.exists(LAUNCHER_CFG_PATH):
        try:
            with open(LAUNCHER_CFG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                for k, v in _DEFAULT_LC.items():
                    if k in saved:
                        lc[k] = saved[k]
        except Exception as e:
            log("[错误] launcher_config.json 读取失败：%s，使用默认配置" % e)
    return lc


def save_launcher_config(lc):
    with open(LAUNCHER_CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(lc, f, ensure_ascii=False, indent=2)


# ----------------------------- 掩码 -----------------------------

def mask_id(no):
    no = str(no or "").strip()
    if len(no) <= 8:
        return no
    return no[:4] + "*" * (len(no) - 8) + no[-4:]


def mask_mobile(m):
    m = str(m or "").strip()
    if len(m) == 11:
        return m[:3] + "****" + m[-4:]
    return m


# ----------------------------- 时间工具 -----------------------------

def parse_dt(s):
    """解析开抢时间。支持 YYYY-MM-DD HH:MM[:SS] / MM-DD HH:MM / HH:MM[:SS]。"""
    s = (s or "").strip()
    if not s:
        return None
    now = datetime.now()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m-%d %H:%M:%S",
                "%m-%d %H:%M", "%H:%M:%S", "%H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            if fmt.startswith("%H"):
                dt = dt.replace(year=now.year, month=now.month, day=now.day)
                if dt < now - timedelta(hours=1):
                    dt += timedelta(days=1)
            return dt
        except ValueError:
            continue
    return None


def fmt_countdown(sec):
    sec = int(sec)
    if sec <= 0:
        return "00:00:00"
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return "%d天 %02d:%02d:%02d" % (d, h, m, s)
    return "%02d:%02d:%02d" % (h, m, s)


# ---------------------- 车次类型 / 日期标签 ----------------------

def is_emu(train_code):
    """动车组（高铁 G / 动车 D / 城际 C）判定。"""
    return bool(train_code) and str(train_code)[0].upper() in ("G", "D", "C")


def train_kind(train_code):
    """车次号首字母 → 中文类型。"""
    c = str(train_code or "")[:1].upper()
    return {"G": "高铁", "D": "动车", "C": "城际", "Z": "直达", "T": "特快",
            "K": "快速", "Y": "旅游", "S": "市郊", "L": "临客"}.get(c, "普速")


def fmt_date_tag(date_str):
    """2026-10-06 → 10-06 周二（卡片日期角标用）。"""
    try:
        d = datetime.strptime(str(date_str), "%Y-%m-%d")
    except Exception:
        return str(date_str or "")
    return "%s 周%s" % (d.strftime("%m-%d"), "一二三四五六日"[d.weekday()])


# ----------------------------- 位置定位 -----------------------------

def ip_locate(timeout=6):
    """百度 IP 定位（公开接口，免 key）。返回可读字符串或错误提示。"""
    try:
        r = requests.get("https://qifu-api.baidubce.com/ip/local/geo/v1/district",
                         timeout=timeout)
        d = r.json()
        if d.get("code") == "Success":
            g = d.get("data") or {}
            parts = [g.get("country", ""), g.get("prov", ""), g.get("city", "")]
            parts = [p for p in parts if p]
            isp = g.get("isp", "")
            return ("%s（%s）" % (" ".join(parts), isp)) if isp else " ".join(parts)
        return "定位失败：%s" % (d.get("message") or d)
    except Exception as e:
        return "定位失败：%s" % e


# ----------------------------- 抢票引擎 -----------------------------

class Grabber(threading.Thread):
    """后台抢票线程：校验会话 → 循环查询 → 命中即下单。

    result: 结束时写入 (ok, msg)。GUI 通过 LOGQ 收到过程日志。"""

    def __init__(self, lc):
        super().__init__(daemon=True, name="grabber")
        self.lc = lc
        self.stop_event = threading.Event()
        self.result = None

    def stop(self):
        self.stop_event.set()

    def run(self):
        try:
            self._run()
        except Exception as e:
            self.result = (False, "抢票线程异常：%s: %s" % (type(e).__name__, e))
            log("[错误] 抢票线程异常：%s: %s" % (type(e).__name__, e))

    # ---- 内部 ----

    def _run(self):
        lc = self.lc
        from_, to_ = (lc.get("from") or "").strip(), (lc.get("to") or "").strip()
        date = (lc.get("date") or "").strip()
        trains = [t.strip().upper() for t in (lc.get("trains") or []) if t and t.strip()]
        seats = [s for s in (lc.get("seat_types") or []) if s]
        names = [n for n in (lc.get("passenger_names") or []) if n]

        if not (from_ and to_ and date):
            self.result = (False, "请先填好出发站 / 到达站 / 日期")
            return
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            self.result = (False, "日期格式应为 YYYY-MM-DD")
            return
        if not seats:
            self.result = (False, "请至少勾选一种席别")
            return
        orderable = [s for s in seats if s in tk_mod.SEAT_NAME_TO_CODE]
        if len(orderable) != len(seats):
            log("[提醒] 席别 %s 暂不支持自动下单，已跳过" % "、".join(
                s for s in seats if s not in orderable))
        seats = orderable
        if not seats:
            self.result = (False, "勾选的席别都无法自动下单，请改选其他席别")
            return
        if not names:
            log("[提醒] 未选择乘车人，将尝试使用账号默认乘车人（可能失败）")

        name2code, code2name = tk_mod.load_station_map()
        fc, tc = name2code.get(from_), name2code.get(to_)
        if not fc or not tc:
            self.result = (False, "车站无法识别：%s → %s" % (from_, to_))
            return

        # 1) 会话
        log("[会话] 正在校验登录状态…（复用本机登录信息，全程无浏览器窗口）")
        try:
            ok, who = browser_order.check_session()
        except Exception as e:
            log("[错误] 会话校验失败：%s" % e)
            ok, who = False, str(e)
        if not ok:
            log("[会话] 当前未登录（%s），尝试拉起登录窗口，请在浏览器里完成登录…" % who)
            try:
                browser_order.login(timeout_sec=240)
                ok, who = browser_order.check_session()
            except Exception as e:
                log("[错误] 登录过程异常：%s" % e)
        if not ok:
            self.result = (False, "登录失败或超时：%s" % who)
            log("[错误] %s" % self.result[1])
            return
        log("[会话] 已登录：%s" % who)

        # 2) 预热：开抢前 lead 分钟登录并停在车票列表页（到点只做 预订→提交）
        st = parse_dt(lc.get("start_time"))
        warm = None
        lead = 0
        try:
            lead = max(0, int(lc.get("warm_minutes") or 10))
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
                                             wait_code=(trains[0] if trains else None))
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
        MAX_ORDER_FAILS = 5  # 同一目标连续失败这么多次就停，不再无限重复
        try:
            while not self.stop_event.is_set():
                n += 1
                try:
                    rows = tk_mod.query_tickets(fc, tc, date,
                                                 purpose=lc.get("purpose_code") or "ADULT")
                except Exception as e:
                    log("[错误] 余票查询失败：%s（%s 秒后重试）" % (e, int(poll)))
                    if self.stop_event.wait(poll):
                        break
                    continue
    
                info, seat = None, None
                by_code = {}
                for row in rows:
                    p = tk_mod.parse_row(row, code2name, date)
                    by_code.setdefault(p.get("train_code"), p)
                for code in trains:   # 按点选顺序：优先抢选中的第一个
                    p = by_code.get(code)
                    if not p:
                        continue
                    avail = p.get("available_seats") or {}
                    for s in sorted(seats, key=lambda x: SEAT_ORDER.get(x, 99)):
                        if s in avail:
                            info, seat = p, s
                            break
                    if info:
                        break
    
                if info:
                    log("[有票] %s %s %s %s→%s 余%s（%s发车）！开始下单…" % (
                        date, info["train_code"], seat, info["from_name"], info["to_name"],
                        info["available_seats"].get(seat), info["start_time"]))
                    try:
                        sc = tk_mod.SEAT_NAME_TO_CODE[seat]
                        ok, msg, extra = browser_order.order_via_browser(
                            info, seat, sc, names, date, headless=False, verify_timeout=90,
                            purpose=lc.get("purpose_code") or "ADULT", warm=warm)
                    except RuntimeError as e:
                        log("[错误] %s（5 秒后重试）" % e)
                        if self.stop_event.wait(5):
                            break
                        continue
                    except Exception as e:
                        log("[错误] 下单异常：%s: %s（5 秒后重试）" % (type(e).__name__, e))
                        if self.stop_event.wait(5):
                            break
                        continue
                    if ok:
                        self.result = (True, msg)
                        log("[抢到] %s" % msg)
                        self._notify_success(info, seat, msg)
                        return
                    extra = extra or {}
                    if extra.get("reason") == "dup":
                        # 账号已有该行程订单（多为未支付）→ 票已到手，继续重试只会被拒
                        self.result = (True, "检测到该行程已有订单（多为未支付），请尽快去 12306 完成支付")
                        log("[提示] 下单返回：%s" % msg)
                        self._notify_success(info, seat, self.result[1])
                        return
                    if extra.get("need_captcha"):
                        self.result = (False, "触发滑块验证：%s（脚本不自动过验证码，已停止）" % msg)
                        log("[错误] %s" % self.result[1])
                        return
                    if "页面上没有" in msg or "网页端不提供席别" in msg:
                        # 抢输竞速/席别已售罄：不是下单失败，继续监控即可
                        log("[提醒] %s（继续监控）" % msg)
                        if self.stop_event.wait(3):
                            break
                        continue
                    target = (info["train_code"], seat)
                    if target == last_target:
                        fail_streak += 1
                    else:
                        last_target, fail_streak = target, 1
                    if any(k in msg for k in ("系统繁忙", "网络异常", "排队")):
                        busy_n += 1
                        wait_s = min(30, 3 * busy_n)
                        log("[错误] 下单遇系统繁忙：%s（%d 秒后重试，累计 %d 次）" % (msg, wait_s, busy_n))
                        if self.stop_event.wait(wait_s):
                            break
                        continue
                    log("[错误] 下单未成功：%s（同目标第 %d 次失败）" % (msg, fail_streak))
                    if fail_streak >= MAX_ORDER_FAILS:
                        self.result = (False, "同一车次席别连续 %d 次下单失败，已自动停止：%s" % (fail_streak, msg))
                        log("[错误] %s" % self.result[1])
                        return
                    if self.stop_event.wait(3):
                        break
                    continue
    
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
        try:
            with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8") as f:
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

def merge_trains_from_monitor(lc):
    """把监控系统（config.json tasks）里的车次合并进启动器车次列表。

    规则：监控里新出现的车次自动加入启动器；被同步过的车次记录在
    synced_trains，用户在启动器里删掉后不会再次自动加回。返回是否合并。"""
    try:
        with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return False
    mon = []
    for t in (cfg.get("tasks") or []):
        for tr in (t.get("trains") or []):
            tr = str(tr).strip().upper()
            if tr and tr not in mon:
                mon.append(tr)
    cur = [str(t).strip().upper() for t in (lc.get("trains") or []) if str(t).strip()]
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
        save_launcher_config(lc)
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

# ----------------------------- GUI -----------------------------

def build_monitor_task(from_name, to_name, dates, date_range, trains, seats,
                       passengers, purpose_code, auto_order, stop_after_order,
                       priority):
    """按监控系统的任务结构组装任务字典（与 gui.py QuickMonitorDialog 一致）。"""
    name = "%s-%s %s %s" % (
        from_name, to_name, "/".join(trains) if trains else "全部车次",
        "/".join(seats))
    return {
        "name": name,
        "uid": uuid.uuid4().hex,
        "from": from_name,
        "to": to_name,
        "dates": dates,
        "date_range": date_range,
        "trains": trains,
        "seat_types": seats,
        "auto_order": bool(auto_order),
        "stop_after_order": bool(stop_after_order),
        "passenger_names": passengers,
        "priority": int(priority),
        "purpose_code": purpose_code,
        "notify_channels": ["email"],
    }


def append_monitor_task(task, start_now=True):
    """把任务写入监控系统的 config.json 与 state.json，并返回任务名。
    监控引擎主循环每轮 _sync_config() 检测 mtime 变化后自动重建调度表，
    处于「监控中」状态的新任务会被自动捡起来，无需重启监控软件。"""
    cfg_path = os.path.join(HERE, "config.json")
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("tasks", []).append(task)
    tmp = cfg_path + ".launcher"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, cfg_path)
    # state.json 条目（与 gui.mark_task_created 的无 app 分支保持一致）
    state_path = os.path.join(HERE, cfg.get("state_file", "state.json"))
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
    entry = state.setdefault("tasks", {}).setdefault(task["name"], {})
    entry["status"] = "monitoring" if start_now else "paused"
    entry.setdefault("fail_streak", 0)
    entry.setdefault("last_poll", 0)
    entry["message"] = ("启动器创建，立即启动" if start_now
                        else "启动器创建，未启动")
    tmp2 = state_path + ".launcher"
    with open(tmp2, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp2, state_path)
    return task["name"]


import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

FONT = ("Microsoft YaHei UI", 10) if sys.platform == "win32" else ("Helvetica", 11)

ID_TYPES = {v: k for k, v in pax_mod.ID_TYPE_NAMES.items()}


# ----------------------------- 车站搜索 -----------------------------

_STATION_INDEX = None


def get_station_index():
    """惰性加载车站全量索引（首次从 station_name.js 下载后缓存到 station_index.json）。"""
    global _STATION_INDEX
    if _STATION_INDEX is None:
        try:
            _STATION_INDEX = tk_mod.load_station_index()
            log("[车站] 车站索引就绪：%d 个车站" % len(_STATION_INDEX))
        except Exception as e:
            log("[提醒] 车站拼音索引加载失败（%s），本次只按站名匹配" % e)
            _STATION_INDEX = []
    return _STATION_INDEX


def search_stations(text, limit=12):
    """本地模糊搜索车站：简拼/全拼/代码前缀优先，站名包含其次。

    返回 [{"name","code","py","spy"}, ...]。纯本地计算，无需联网。"""
    q = (text or "").strip().lower()
    if not q:
        return []
    head, tail = [], []
    for st in get_station_index():
        if st["spy"].startswith(q) or st["py"].startswith(q) or st["code"].lower().startswith(q):
            head.append(st)
        elif q in st["name"] or q in st["py"]:
            tail.append(st)
        if len(head) >= limit:
            break
    return (head + tail)[:limit]


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
                with open(STATION_KIND_PATH, "r", encoding="utf-8") as f:
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
    try:
        tmp = STATION_KIND_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, STATION_KIND_PATH)
    except Exception as e:
        log("[提醒] 车站类型缓存写入失败：%s" % e)


def station_kind(code):
    """车站类型文案（没探测过返回空串）。"""
    return load_station_kinds().get((code or "").upper(), "")


def _merge_kind(code, kind):
    """把一次观测合并进缓存：既有高铁又有普速时拼成 "高铁+普速"。"""
    code = (code or "").upper()
    if len(code) != 3 or not kind:
        return False
    kinds = load_station_kinds()
    old = kinds.get(code)
    if old == kind:
        return False
    if not old:
        kinds[code] = kind
        return True
    parts = old.split("+")
    if kind in parts:
        return False
    parts.append(kind)
    kinds[code] = "+".join(sorted(set(parts), key=lambda k: _KIND_ORDER.get(k, 9)))
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
            rows = tk_mod.query_tickets(code, hub, date)
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


class StationEntry(ttk.Frame):
    """车站输入框：输入即实时模糊搜索，下拉列表可选（支持键盘）。

    匹配规则：拼音简拼（cq → 重庆）、全拼（chongqing）、汉字（重庆）、电报码（CQW）。
    焦点为空时下拉显示历史车站，回车/双击/单击选定。"""

    def __init__(self, master, width=14, history=None, **kw):
        super().__init__(master, **kw)
        self.var = tk.StringVar()
        self.entry = ttk.Entry(self, textvariable=self.var, width=width)
        self.entry.pack(fill="x")
        self.history = [h for h in (history or []) if h]
        self._items = []
        self._lb = None
        self.entry.bind("<KeyRelease>", self._on_key)
        self.entry.bind("<Down>", self._focus_down)
        self.entry.bind("<Return>", self._enter)
        self.entry.bind("<Escape>", lambda e: self.hide())
        self.entry.bind("<FocusIn>", self._on_focus)
        self.entry.bind("<FocusOut>", lambda e: self.after(180, self.hide))

    def get(self):
        return self.var.get().strip()

    def set(self, value):
        self.var.set(value or "")

    def set_history(self, items):
        self.history = [h for h in (items or []) if h]

    # ---- 下拉 ----

    def _ensure_lb(self):
        """下拉框挂在顶层窗口上，避免被父容器裁剪。"""
        if self._lb is None or not self._lb.winfo_exists():
            top = self.winfo_toplevel()
            self._lb = tk.Listbox(top, height=7, activestyle="none", exportselection=False,
                                  font=(FONT[0], 10), relief="solid", borderwidth=1,
                                  highlightthickness=0)
            self._lb.bind("<ButtonRelease-1>", self._pick_click)
            self._lb.bind("<Double-Button-1>", self._pick)
            self._lb.bind("<Return>", self._pick)
            self._lb.bind("<Escape>", lambda e: (self.hide(), self.entry.focus_set()))
            self._lb.bind("<FocusOut>", lambda e: self.after(180, self.hide))
        return self._lb

    def show(self, items):
        lb = self._ensure_lb()
        lb.delete(0, "end")
        self._items = list(items)
        for i, it in enumerate(self._items):
            if isinstance(it, dict):
                kind = station_kind(it.get("code"))
                text = "%s  %s" % (it["name"], it["code"])
                if kind:
                    text += " · " + kind
                lb.insert("end", text)
                color = _KIND_COLOR.get((kind or "").split("+")[0])
                if color:
                    lb.itemconfig(i, foreground=color)
            else:
                lb.insert("end", str(it))
        if not self._items:
            self.hide()
            return
        h = self.entry.winfo_height() or 24
        lb.place(in_=self.entry, x=0, y=h, width=max(self.entry.winfo_width(), 210))
        lb.lift()

    def hide(self):
        if self._lb is not None and self._lb.winfo_exists():
            self._lb.place_forget()

    # ---- 事件 ----

    def _on_focus(self, _e=None):
        if not self.var.get().strip() and self.history:
            self.show(self.history[:8])

    def _on_key(self, event):
        if event.keysym in ("Down", "Up", "Return", "Escape", "Tab", "Shift_L", "Shift_R"):
            return
        text = self.var.get().strip()
        if not text:
            self.show(self.history[:8])
            return
        items = search_stations(text)
        if not items:
            items = [{"name": h} for h in self.history if text in h][:8]
        self.show(items)
        codes = [it.get("code") for it in items if isinstance(it, dict)]
        if codes:
            request_station_kinds(codes, self._on_kind_ready)

    def _on_kind_ready(self):
        """探测线程回调（子线程）——切回主线程刷新下拉。"""
        try:
            self.after(0, self._refresh_drop)
        except Exception:
            pass

    def _refresh_drop(self):
        """下拉仍开着时按当前输入重建列表，让刚探测到的类型标注立刻出现。"""
        try:
            if self._lb is None or not self._lb.winfo_exists() or not self._lb.winfo_ismapped():
                return
            text = self.var.get().strip()
            items = search_stations(text) if text else self.history[:8]
            if items:
                self.show(items)
        except Exception:
            pass

    def _focus_down(self, _e=None):
        if self._lb is not None and self._lb.winfo_ismapped() and self._lb.size():
            self._lb.focus_set()
            self._lb.selection_clear(0, "end")
            self._lb.selection_set(0)
            self._lb.activate(0)
        return "break"

    def _enter(self, _e=None):
        if self._lb is not None and self._lb.winfo_ismapped() and self._lb.size():
            sel = self._lb.curselection()
            self._apply(sel[0] if sel else 0)
        return "break"

    def _pick_click(self, _e=None):
        sel = self._lb.curselection()
        if sel:
            self._apply(sel[0])

    def _pick(self, _e=None):
        sel = self._lb.curselection()
        if sel:
            self._apply(sel[0])
        return "break"

    def _apply(self, idx):
        if 0 <= idx < len(self._items):
            it = self._items[idx]
            self.var.set(it["name"] if isinstance(it, dict) else str(it))
        self.hide()
        self.entry.focus_set()
        self.entry.icursor("end")


class CalendarDialog(tk.Toplevel):
    """简易月历，选中日期写回 var（YYYY-MM-DD）。"""

    def __init__(self, master, var):
        super().__init__(master)
        self.var = var
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", (var.get() or "").strip())
        try:
            self.cur = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else datetime.now()
        except ValueError:
            self.cur = datetime.now()
        self.title("选择乘车日期")
        self.resizable(False, False)
        self.transient(master)
        self.body = ttk.Frame(self, padding=10)
        self.body.pack(fill="both", expand=True)
        self._render()
        self.grab_set()

    def _shift(self, delta):
        y, m = self.cur.year, self.cur.month + delta
        y += (m - 1) // 12
        m = (m - 1) % 12 + 1
        self.cur = self.cur.replace(year=y, month=m, day=1)
        self._render()

    def _sel_date(self):
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", (self.var.get() or "").strip())
        if not m:
            return None
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        except ValueError:
            return None

    def _render(self):
        for w in self.body.winfo_children():
            w.destroy()
        head = ttk.Frame(self.body)
        head.pack(fill="x", pady=(0, 8))
        ttk.Button(head, text="‹", width=3, command=lambda: self._shift(-1)).pack(side="left")
        ttk.Label(head, text="%d 年 %d 月" % (self.cur.year, self.cur.month),
                  font=(FONT[0], 11, "bold"), anchor="center").pack(side="left", expand=True)
        ttk.Button(head, text="›", width=3, command=lambda: self._shift(1)).pack(side="left")
        ttk.Button(head, text="今天", width=5, command=self._today).pack(side="left", padx=(8, 0))

        grid_w = ttk.Frame(self.body)
        grid_w.pack()
        for i, wd in enumerate("一二三四五六日"):
            ttk.Label(grid_w, text=wd, width=4, anchor="center",
                      foreground="#6e7781").grid(row=0, column=i, padx=1, pady=(0, 3))
        today = datetime.now().date()
        sel = self._sel_date()
        for r, week in enumerate(calendar.monthcalendar(self.cur.year, self.cur.month), start=1):
            for c, day in enumerate(week):
                if day == 0:
                    continue
                d = datetime(self.cur.year, self.cur.month, day).date()
                bg, fg = "#f6f8fa", "#1f2328"
                if d == today:
                    fg = "#0969da"
                if d == sel:
                    bg, fg = "#0969da", "#ffffff"
                tk.Button(grid_w, text=str(day), width=4, relief="flat", bg=bg, fg=fg,
                          activebackground="#dbeafe", cursor="hand2",
                          command=lambda dd=day: self._choose(dd)).grid(
                    row=r, column=c, padx=1, pady=1)

        tail = ttk.Frame(self.body)
        tail.pack(fill="x", pady=(10, 0))
        ttk.Button(tail, text="清除", command=self._clear).pack(side="left")
        ttk.Button(tail, text="关闭", command=self.destroy).pack(side="right")

    def _choose(self, day):
        self.var.set("%04d-%02d-%02d" % (self.cur.year, self.cur.month, day))
        self.destroy()

    def _today(self):
        self.var.set(datetime.now().strftime("%Y-%m-%d"))
        self.destroy()

    def _clear(self):
        self.var.set("")
        self.destroy()


class DateTimeDialog(tk.Toplevel):
    """表格选日期 + 时/分/秒，确定后写回 var（YYYY-MM-DD HH:MM:SS）。"""

    def __init__(self, master, var):
        super().__init__(master)
        self.var = var
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?$",
                     (var.get() or "").strip())
        now = datetime.now() + timedelta(minutes=5)
        try:
            self.sel = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                int(m.group(4) or 0), int(m.group(5) or 0),
                                int(m.group(6) or 0)) if m else now
        except ValueError:
            self.sel = now
        self.cur = self.sel.replace(day=1)
        self.h_var = tk.StringVar(value="%02d" % self.sel.hour)
        self.m_var = tk.StringVar(value="%02d" % self.sel.minute)
        self.s_var = tk.StringVar(value="%02d" % self.sel.second)
        self.title("选择开抢时间")
        self.resizable(False, False)
        self.transient(master)
        self.body = ttk.Frame(self, padding=10)
        self.body.pack(fill="both", expand=True)
        self._render()
        self.grab_set()

    def _shift(self, delta):
        y, m = self.cur.year, self.cur.month + delta
        y += (m - 1) // 12
        m = (m - 1) % 12 + 1
        self.cur = self.cur.replace(year=y, month=m, day=1)
        self._render()

    def _render(self):
        for w in self.body.winfo_children():
            w.destroy()
        head = ttk.Frame(self.body)
        head.pack(fill="x", pady=(0, 8))
        ttk.Button(head, text="‹", width=3, command=lambda: self._shift(-1)).pack(side="left")
        ttk.Label(head, text="%d 年 %d 月" % (self.cur.year, self.cur.month),
                  font=(FONT[0], 11, "bold"), anchor="center").pack(side="left", expand=True)
        ttk.Button(head, text="›", width=3, command=lambda: self._shift(1)).pack(side="left")
        ttk.Button(head, text="今天", width=5, command=self._today).pack(side="left", padx=(8, 0))

        grid_w = ttk.Frame(self.body)
        grid_w.pack()
        for i, wd in enumerate("一二三四五六日"):
            ttk.Label(grid_w, text=wd, width=4, anchor="center",
                      foreground="#6e7781").grid(row=0, column=i, padx=1, pady=(0, 3))
        today = datetime.now().date()
        for r, week in enumerate(calendar.monthcalendar(self.cur.year, self.cur.month), start=1):
            for c, day in enumerate(week):
                if day == 0:
                    continue
                d = datetime(self.cur.year, self.cur.month, day).date()
                bg, fg = "#f6f8fa", "#1f2328"
                if d == today:
                    fg = "#0969da"
                if d == self.sel.date():
                    bg, fg = "#0969da", "#ffffff"
                tk.Button(grid_w, text=str(day), width=4, relief="flat", bg=bg, fg=fg,
                          activebackground="#dbeafe", cursor="hand2",
                          command=lambda dd=day: self._choose_day(dd)).grid(
                    row=r, column=c, padx=1, pady=1)

        tw = ttk.Frame(self.body)
        tw.pack(fill="x", pady=(8, 0))
        ttk.Label(tw, text="时间", foreground="#57606a").pack(side="left", padx=(0, 6))
        ttk.Spinbox(tw, from_=0, to=23, width=3, textvariable=self.h_var,
                    format="%02.0f", wrap=True).pack(side="left")
        ttk.Label(tw, text="时").pack(side="left", padx=(2, 10))
        ttk.Spinbox(tw, from_=0, to=59, width=3, textvariable=self.m_var,
                    format="%02.0f", wrap=True).pack(side="left")
        ttk.Label(tw, text="分").pack(side="left", padx=(2, 10))
        ttk.Spinbox(tw, from_=0, to=59, width=3, textvariable=self.s_var,
                    format="%02.0f", wrap=True).pack(side="left")
        ttk.Label(tw, text="秒").pack(side="left", padx=(2, 0))

        qw = ttk.Frame(self.body)
        qw.pack(fill="x", pady=(6, 0))
        ttk.Label(qw, text="常用", foreground="#57606a").pack(side="left", padx=(0, 6))
        for hh, mm in ((8, 0), (10, 0), (12, 0), (17, 0), (19, 0), (20, 0)):
            ttk.Button(qw, text="%02d:%02d" % (hh, mm), width=6,
                       command=lambda h=hh, mm2=mm: self._quick_time(h, mm2)).pack(
                side="left", padx=(0, 4))

        tail = ttk.Frame(self.body)
        tail.pack(fill="x", pady=(10, 0))
        ttk.Label(tail, text="格式 %s" % "YYYY-MM-DD HH:MM:SS",
                  foreground="#8c959f").pack(side="left")
        ttk.Button(tail, text="确定", command=self._ok).pack(side="right")
        ttk.Button(tail, text="清除", command=self._clear).pack(side="right", padx=(0, 6))

    def _choose_day(self, day):
        try:
            self.sel = self.sel.replace(year=self.cur.year, month=self.cur.month, day=day)
        except ValueError:
            self.sel = datetime(self.cur.year, self.cur.month, day)
        self._render()

    def _today(self):
        n = datetime.now()
        self.sel = n.replace(microsecond=0)
        self.h_var.set("%02d" % n.hour)
        self.m_var.set("%02d" % n.minute)
        self.s_var.set("%02d" % n.second)
        self.cur = self.sel.replace(day=1)
        self._render()

    def _quick_time(self, hh, mm):
        self.h_var.set("%02d" % hh)
        self.m_var.set("%02d" % mm)
        self.s_var.set("00")

    def _ok(self):
        def _num(v, hi):
            try:
                return max(0, min(hi, int(str(v.get()).strip())))
            except ValueError:
                return 0
        self.var.set("%04d-%02d-%02d %02d:%02d:%02d" % (
            self.sel.year, self.sel.month, self.sel.day,
            _num(self.h_var, 23), _num(self.m_var, 59), _num(self.s_var, 59)))
        self.destroy()

    def _clear(self):
        self.var.set("")
        self.destroy()


class QueryHistoryDialog(tk.Toplevel):
    """历史查询记录：双击一条即填回行程。"""

    def __init__(self, master, items, on_pick):
        super().__init__(master)
        self.items = list(items)
        self.on_pick = on_pick
        self.title("历史查询记录")
        self.geometry("380x320")
        self.transient(master)
        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="双击一条记录填回行程：", foreground="#6e7781").pack(anchor="w")
        wrap = ttk.Frame(body)
        wrap.pack(fill="both", expand=True, pady=(6, 0))
        sb = ttk.Scrollbar(wrap, command=lambda *a: self.lb.yview(*a))
        self.lb = tk.Listbox(wrap, font=(FONT[0], 10), yscrollcommand=sb.set, activestyle="none")
        sb.pack(side="right", fill="y")
        self.lb.pack(side="left", fill="both", expand=True)
        for it in self.items:
            self.lb.insert("end", "%s → %s    %s" % (it.get("from"), it.get("to"), it.get("date")))
        self.lb.bind("<Double-Button-1>", self._pick)
        self.lb.bind("<Return>", self._pick)
        if self.items:
            self.lb.selection_set(0)
        self.lb.focus_set()

    def _pick(self, _e=None):
        sel = self.lb.curselection()
        if sel:
            self.on_pick(self.items[sel[0]])
        self.destroy()


# ------------------------- 车次卡片列表 -------------------------

class TrainCard(tk.Frame):
    """单趟车卡片：时间 — 车次 类型 — 时间 / 历时 / 区间 / 有票席别摘要，整卡可点选。"""

    def __init__(self, master, info, on_click, selected=False):
        super().__init__(master, bd=0, bg="#ffffff",
                         highlightthickness=1, highlightbackground="#d0d7de", cursor="hand2")
        self.info = info
        self.on_click = on_click
        self.key = "%s@%s" % (info.get("_date") or info.get("query_date") or "",
                              info.get("train_code") or "?")
        self.selected = False
        body = tk.Frame(self, bg="#ffffff")
        body.pack(fill="x", padx=8, pady=3)

        small = (FONT[0], 9)
        head = tk.Frame(body, bg="#ffffff")
        head.pack(fill="x")
        big = (FONT[0], 11, "bold")
        tk.Label(head, text=info.get("start_time") or "-", font=big, bg="#ffffff").pack(side="left")
        tk.Label(head, text="——", fg="#8c959f", bg="#ffffff").pack(side="left", padx=5)
        tk.Label(head, text=info.get("train_code") or "?", font=(FONT[0], 10, "bold"),
                 fg="#0969da", bg="#ffffff").pack(side="left")
        tk.Label(head, text=train_kind(info.get("train_code")), fg="#57606a",
                 bg="#ffffff", font=small).pack(side="left", padx=(4, 0))
        tk.Label(head, text="——", fg="#8c959f", bg="#ffffff").pack(side="left", padx=5)
        tk.Label(head, text=info.get("arrive_time") or "-", font=big, bg="#ffffff").pack(side="left")
        tk.Label(head, text="历时 " + (info.get("duration") or "-"), fg="#57606a",
                 bg="#ffffff", font=small).pack(side="right")

        sub = tk.Frame(body, bg="#ffffff")
        sub.pack(fill="x")
        tk.Label(sub, text="%s → %s" % (info.get("from_name") or "?",
                                        info.get("to_name") or "?"),
                 fg="#57606a", bg="#ffffff", font=small).pack(side="left")
        if info.get("_date"):
            tk.Label(sub, text="  " + fmt_date_tag(info["_date"]), fg="#8250df",
                     bg="#ffffff", font=small).pack(side="left")
        # 席别只摘有票的，一行放下；全无票时给一句灰字
        seats = info.get("available_seats") or {}
        names = list(SEAT_OPTIONS) + [k for k in seats if k not in SEAT_ORDER]
        avail = [(n, seats.get(n)) for n in names if seats.get(n)]
        if avail:
            parts = ["%s %s" % (n, v) for n, v in avail[:5]]
            if len(avail) > 5:
                parts.append("…另 %d 席" % (len(avail) - 5))
            tk.Label(sub, text="    " + " · ".join(parts), fg="#1a7f37",
                     bg="#ffffff", font=(FONT[0], 9, "bold")).pack(side="left")
        else:
            tk.Label(sub, text="    %d 种席别均无票" % len(names), fg="#8c959f",
                     bg="#ffffff", font=small).pack(side="left")

        self._bind_click_all(self)
        self.set_selected(selected)

    def _bind_click_all(self, w):
        w.bind("<Button-1>", self._clicked)
        for c in w.winfo_children():
            self._bind_click_all(c)

    def _clicked(self, _e=None):
        self.on_click(self)
        return "break"

    def _all_children(self, w):
        out = []
        for c in w.winfo_children():
            out.append(c)
            out.extend(self._all_children(c))
        return out

    def set_selected(self, on):
        self.selected = bool(on)
        bg = "#eff6ff" if self.selected else "#ffffff"
        self.configure(highlightbackground="#0969da" if self.selected else "#d0d7de",
                       highlightthickness=2 if self.selected else 1)
        for w in [self] + self._all_children(self):
            try:
                w.configure(bg=bg)
            except tk.TclError:
                pass


class TrainCardList(ttk.Frame):
    """可滚动的车次卡片列表，支持多选；选中的车次经 on_change 回调给主界面。"""

    def __init__(self, master, on_change, height=128):
        super().__init__(master)
        self.on_change = on_change
        self.selected = {}
        self.cards = []
        self.canvas = tk.Canvas(self, height=height, highlightthickness=0, bg="#ffffff")
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg="#ffffff")
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _wheel(self, event):
        self.canvas.yview_scroll(-1 * int(event.delta / 120), "units")

    def render(self, infos, empty_text="（没有符合条件的车次）"):
        for w in self.inner.winfo_children():
            w.destroy()
        self.cards = []
        keep = set(self.selected)
        self.selected = {}
        if not infos:
            tk.Label(self.inner, text=empty_text, fg="#6e7781", bg="#ffffff").pack(
                anchor="w", pady=8, padx=4)
            self.on_change([])
            return
        for info in infos:
            card = TrainCard(self.inner, info, self._clicked)
            card.pack(fill="x", pady=(0, 4), padx=(0, 2))
            self.cards.append((card, info))
            if card.key in keep:
                card.set_selected(True)
                self.selected[card.key] = info
        self.canvas.yview_moveto(0)
        self.on_change(list(self.selected.values()))

    def _clicked(self, card):
        if card.key in self.selected:
            self.selected.pop(card.key, None)
            card.set_selected(False)
        else:
            self.selected[card.key] = card.info
            card.set_selected(True)
        self.on_change(list(self.selected.values()))

    def selected_codes(self):
        codes = []
        for info in self.selected.values():
            c = info.get("train_code")
            if c and c not in codes:
                codes.append(c)
        return codes


class LauncherApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.lc = load_launcher_config()
        self.grabber = None
        self.armed = False
        self.auto_fired = False
        self.reminded = False
        self.pax_vars = {}
        self.seat_vars = {}
        self.update_info = None
        self._monitor_mtime = 0.0
        self._pax_mtime = 0.0
        self.dataq = queue.Queue()
        self._querying = False
        self._last_query_ts = 0.0
        self._train_rows = {}
        self._train_infos = []

        self.title("12306 抢票启动器 v%s" % __version__)
        self.minsize(780, 620)
        self._build()
        self._sync_from_lc()
        # 高度按内容实测，屏幕放不下时压缩到可视区内
        self.update_idletasks()
        need_h = self.winfo_reqheight()
        win_h = min(need_h, max(620, self.winfo_screenheight() - 70))
        win_x = max(0, (self.winfo_screenwidth() - 820) // 2)
        self.geometry("820x%d+%d+%d" % (win_h, win_x, 14))
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(500, self._tick)
        self.after(200, self._drain)
        self.after(1200, self._check_update_async)
        self.after(2500, self._warm_station_kinds)

    def _warm_station_kinds(self):
        """启动后预热常用车站的高铁/普速标注（历史站 + 当前出发/到达）。"""
        try:
            names = [self.from_ent.get(), self.to_ent.get()]
            names.extend(self.from_ent.history[:6])
            names.extend(self.to_ent.history[:6])
            n2c, _c2n = tk_mod.load_station_map()
            codes = []
            for n in names:
                c = n2c.get((n or "").strip())
                if c and c not in codes:
                    codes.append(c)
            if codes and request_station_kinds(codes):
                log("[车站] 正在后台识别常用车站类型（高铁 / 普速）")
        except Exception as e:
            log("[提醒] 常用车站类型预热失败：%s" % e)

    # ---- 界面构建 ----

    def _build(self):
        self.grid_columnconfigure(0, weight=1)

        top = ttk.Frame(self, padding=(12, 8))
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="12306 抢票启动器", font=(FONT[0], 15, "bold")).pack(side="left")
        ttk.Label(top, text="v" + __version__, foreground="#6e7781").pack(side="left", padx=(8, 0))
        self.update_lbl = ttk.Label(top, text="", foreground="#8250df", cursor="hand2")
        self.update_lbl.pack(side="right")

        # 乘车人
        pf = ttk.LabelFrame(self, text=" 乘车人（勾选参与抢票） ", padding=8)
        pf.grid(row=3, column=0, sticky="ew", padx=12, pady=(3, 0))
        self.pax_box = ttk.Frame(pf)
        self.pax_box.pack(side="left", fill="x", expand=True)
        ttk.Button(pf, text="新增/编辑", command=self._edit_pax).pack(side="right")

        # 行程
        tf = ttk.LabelFrame(self, text=" 行程（车站支持拼音简拼 / 全拼 / 汉字 / 代码实时搜索） ", padding=8)
        tf.grid(row=1, column=0, sticky="ew", padx=12, pady=(2, 0))
        r0 = ttk.Frame(tf)
        r0.pack(fill="x")
        ttk.Label(r0, text="常用行程").pack(side="left")
        self.preset_cb = ttk.Combobox(r0, state="readonly", width=28)
        self.preset_cb.pack(side="left", padx=6, fill="x", expand=True)
        self.preset_cb.bind("<<ComboboxSelected>>", self._load_preset)
        ttk.Button(r0, text="存为预设", command=self._save_preset).pack(side="left", padx=4)
        ttk.Button(r0, text="删除", command=self._delete_preset).pack(side="left")

        r1 = ttk.Frame(tf)
        r1.pack(fill="x", pady=(8, 0))
        ttk.Label(r1, text="出发").pack(side="left")
        self.from_ent = StationEntry(r1, width=13, history=self.lc.get("station_history"))
        self.from_ent.pack(side="left", padx=(4, 2))
        ttk.Button(r1, text="⇄", width=3, command=self._swap_stations).pack(side="left", padx=2)
        ttk.Label(r1, text="到达").pack(side="left", padx=(6, 0))
        self.to_ent = StationEntry(r1, width=13, history=self.lc.get("station_history"))
        self.to_ent.pack(side="left", padx=(4, 6))
        ttk.Button(r1, text="定位", width=5, command=self._locate).pack(side="left")
        ttk.Label(r1, text="车次留空=全部；日期「到」留空=只查那一天（区间最多 5 天）",
                  foreground="#6e7781").pack(side="left", padx=(10, 0))

        r2 = ttk.Frame(tf)
        r2.pack(fill="x", pady=(8, 0))
        ttk.Label(r2, text="乘车日期").pack(side="left")
        ttk.Label(r2, text="从", foreground="#57606a").pack(side="left", padx=(6, 2))
        self.date_var = tk.StringVar()
        ttk.Entry(r2, textvariable=self.date_var, width=11).pack(side="left")
        ttk.Button(r2, text="📅", width=3,
                   command=lambda: self._open_calendar(self.date_var)).pack(side="left", padx=(2, 8))
        ttk.Label(r2, text="到", foreground="#57606a").pack(side="left")
        self.date_to_var = tk.StringVar()
        ttk.Entry(r2, textvariable=self.date_to_var, width=11).pack(side="left", padx=(4, 2))
        ttk.Button(r2, text="📅", width=3,
                   command=lambda: self._open_calendar(self.date_to_var)).pack(side="left")
        ttk.Label(r2, text="车次").pack(side="left", padx=(12, 0))
        self.trains_var = tk.StringVar()
        ttk.Entry(r2, textvariable=self.trains_var, width=16).pack(side="left", padx=(4, 0))

        # 车次列表（第 2 段：卡片式，点卡片即选车次）
        qf = ttk.LabelFrame(self, text=" 车次列表（查询实时余票，点卡片选中车次） ", padding=8)
        qf.grid(row=2, column=0, sticky="ew", padx=12, pady=(3, 0))
        q0 = ttk.Frame(qf)
        q0.pack(fill="x")
        self.query_btn = ttk.Button(q0, text="查询车次 (F5)", command=lambda: self.query_trains())
        self.query_btn.pack(side="left")
        self.query_state = ttk.Label(q0, text="", foreground="#6e7781")
        self.query_state.pack(side="left", padx=8)
        ttk.Button(q0, text="清空列表", command=self._clear_train_rows).pack(side="right")
        ttk.Label(q0, text="秒自动刷新", foreground="#6e7781").pack(side="right")
        self.refresh_sec_var = tk.IntVar(value=60)
        ttk.Spinbox(q0, from_=20, to=600, increment=10, textvariable=self.refresh_sec_var,
                    width=4).pack(side="right", padx=(0, 2))
        self.auto_refresh_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(q0, text="余票自动刷新", variable=self.auto_refresh_var).pack(side="right", padx=(10, 4))

        q1 = ttk.Frame(qf)
        q1.pack(fill="x", pady=(6, 0))
        self.filter_emu_var = tk.BooleanVar(value=False)
        self.filter_putong_var = tk.BooleanVar(value=False)
        self.filter_avail_var = tk.BooleanVar(value=False)
        self.sort_var = tk.StringVar(value="dep")
        for _txt, _var in (("只看高铁动车", self.filter_emu_var),
                           ("只看普速", self.filter_putong_var),
                           ("只看有票", self.filter_avail_var)):
            ttk.Checkbutton(q1, text=_txt, variable=_var,
                            command=self._refresh_train_view).pack(side="left", padx=(0, 10))
        ttk.Radiobutton(q1, text="历时最短", value="dur", variable=self.sort_var,
                        command=self._refresh_train_view).pack(side="right")
        ttk.Radiobutton(q1, text="发时最早", value="dep", variable=self.sort_var,
                        command=self._refresh_train_view).pack(side="right", padx=(12, 0))
        self.sel_state = ttk.Label(q1, text="已选 0 趟车 · 0 种席别", foreground="#0969da")
        self.sel_state.pack(side="left", padx=(16, 0))

        self.cardlist = TrainCardList(qf, self._on_cards_changed)
        self.cardlist.pack(fill="x", pady=(6, 0))
        self.cardlist.render([])

        # 票种与席别（第 4 段）
        self.sf = ttk.LabelFrame(
            self, text=" 票种与席别（点车次卡片可按该车实际余票刷新） ", padding=8)
        sf = self.sf
        sf.grid(row=4, column=0, sticky="ew", padx=12, pady=(3, 0))
        s0 = ttk.Frame(sf)
        s0.pack(fill="x")
        ttk.Label(s0, text="票种").pack(side="left")
        self.purpose_var = tk.StringVar(value="ADULT")
        ttk.Radiobutton(s0, text="成人票", value="ADULT", variable=self.purpose_var,
                        command=self._on_purpose_change).pack(side="left", padx=(6, 0))
        ttk.Radiobutton(s0, text="学生票", value="0X00", variable=self.purpose_var,
                        command=self._on_purpose_change).pack(side="left", padx=(6, 0))
        self.seat_box = ttk.Frame(s0)
        self.seat_box.pack(side="left", fill="x", expand=True)
        self._rebuild_seats()

        # 开抢时间
        ef = ttk.LabelFrame(self, text=" 开抢时间 ", padding=8)
        ef.grid(row=5, column=0, sticky="ew", padx=12, pady=(3, 0))
        r0e = ttk.Frame(ef)
        r0e.pack(fill="x")
        ttk.Label(r0e, text="到点自动开抢").pack(side="left")
        self.start_var = tk.StringVar()
        self.start_var.trace_add("write", self._on_start_change)
        ttk.Entry(r0e, textvariable=self.start_var, width=19).pack(side="left", padx=(6, 2))
        ttk.Button(r0e, text="📅", width=3,
                   command=self._open_datetime).pack(side="left", padx=(0, 6))
        ttk.Label(r0e, text="提前").pack(side="left")
        self.remind_var = tk.IntVar(value=10)
        ttk.Spinbox(r0e, from_=0, to=120, textvariable=self.remind_var, width=4).pack(side="left", padx=4)
        ttk.Label(r0e, text="分钟提醒（格式如 2026-10-09 17:00:00）").pack(side="left")
        self.countdown_lbl = ttk.Label(r0e, font=(FONT[0], 11, "bold"), foreground="#cf222e")
        self.countdown_lbl.pack(side="left", padx=(10, 0))
        r1e = ttk.Frame(ef)
        r1e.pack(fill="x", pady=(4, 0))
        ttk.Label(r1e, text="开抢前").pack(side="left")
        self.warm_var = tk.IntVar(value=10)
        ttk.Spinbox(r1e, from_=0, to=30, textvariable=self.warm_var, width=4).pack(side="left", padx=4)
        ttk.Label(r1e, text="分钟登录预热（到点立即填单下单，0=不预热）").pack(side="left")

        # 主按钮 + 状态
        bf = ttk.Frame(self)
        bf.grid(row=6, column=0, sticky="ew", padx=12, pady=(10, 0))
        self.go_btn = tk.Button(bf, text="立 即 抢 票", font=(FONT[0], 14, "bold"),
                                bg="#cf222e", fg="white", activebackground="#a40e26",
                                activeforeground="white", relief="flat", padx=30, pady=6,
                                cursor="hand2", command=self.toggle_grab)
        self.go_btn.pack(side="left", fill="x", expand=True)
        ttk.Button(bf, text="新建监控任务", command=self.open_new_monitor_task).pack(side="right", padx=(8, 0))
        ttk.Button(bf, text="测试会话", command=self._test_session).pack(side="right", padx=(8, 0))

        stf = ttk.Frame(self)
        stf.grid(row=7, column=0, sticky="ew", padx=12, pady=(8, 0))
        self.dot = tk.Canvas(stf, width=14, height=14, highlightthickness=0)
        self.dot.pack(side="left")
        self._dot_id = self.dot.create_oval(3, 3, 11, 11, fill="#6e7781", outline="")
        self.status_lbl = ttk.Label(stf, text="就绪")
        self.status_lbl.pack(side="left", padx=6)

        # 日志
        lg = ttk.LabelFrame(self, text=" 日志 ", padding=4)
        lg.grid(row=8, column=0, sticky="nsew", padx=12, pady=(8, 12))
        self.grid_rowconfigure(8, weight=1)
        txt = tk.Text(lg, height=3, wrap="word", state="disabled", font=(FONT[0], 9))
        sb = ttk.Scrollbar(lg, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)
        self.log_text = txt
        for tag, color in LOG_COLOR.items():
            txt.tag_configure(tag, foreground=color)

        # 快捷键：F5 查车次 / Ctrl+Enter 开抢或停止 / Ctrl+L 定位
        self.bind("<F5>", lambda e: self.query_trains())
        self.bind("<Control-Return>", lambda e: self.toggle_grab())
        self.bind("<Control-l>", lambda e: self._locate())

    # ---- 数据同步 ----

    def _sync_from_lc(self):
        lc = self.lc
        hist = lc.get("station_history") or []
        self.from_ent.set_history(hist)
        self.to_ent.set_history(hist)
        self.from_ent.set(lc.get("from") or "")
        self.to_ent.set(lc.get("to") or "")
        self.trains_var.set(",".join(lc.get("trains") or []))
        self.date_var.set(lc.get("date") or (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"))
        self.date_to_var.set(lc.get("date_to") or "")
        self.purpose_var.set(lc.get("purpose_code") or "ADULT")
        for s, v in self.seat_vars.items():
            v.set(s in (lc.get("seat_types") or []))
        self.start_var.set(lc.get("start_time") or "")
        self.remind_var.set(int(lc.get("remind_minutes") or 10))
        self.warm_var.set(max(0, min(30, int(lc.get("warm_minutes") or 10))))
        names = [p.get("name") or "" for p in (lc.get("presets") or [])]
        self.preset_cb.configure(values=names)
        if names:
            self.preset_cb.current(0)
        self._refresh_pax()
        if merge_trains_from_monitor(self.lc):
            self.trains_var.set(",".join(self.lc["trains"]))
        try:
            self._monitor_mtime = os.path.getmtime(os.path.join(HERE, "config.json"))
        except OSError:
            pass
        try:
            self._pax_mtime = os.path.getmtime(os.path.join(HERE, "passengers.json"))
        except OSError:
            pass
        st = parse_dt(self.lc.get("start_time") or "")
        self.armed = bool(st and st > datetime.now())
        self.auto_fired = False
        self.reminded = False

    def _ui_to_lc(self):
        lc = self.lc
        lc["from"] = self.from_ent.get()
        lc["to"] = self.to_ent.get()
        lc["purpose_code"] = self.purpose_var.get() or "ADULT"
        lc["trains"] = [t.strip().upper() for t in re.split(r"[,，\s]+", self.trains_var.get()) if t.strip()]
        lc["seat_types"] = [s for s, v in self.seat_vars.items() if v.get()]
        lc["date"] = self.date_var.get().strip()
        lc["date_to"] = self.date_to_var.get().strip()
        lc["start_time"] = self.start_var.get().strip()
        lc["remind_minutes"] = max(0, min(120, int(self.remind_var.get() or 10)))
        lc["warm_minutes"] = max(0, min(30, int(self.warm_var.get() or 10)))
        lc["passenger_names"] = [n for n, v in self.pax_vars.items() if v.get()]
        hist = list(lc.get("station_history") or [])
        for stn in (lc["from"], lc["to"]):
            if stn and stn not in hist:
                hist.insert(0, stn)
        lc["station_history"] = hist[:20]
        save_launcher_config(lc)

    def _refresh_pax(self):
        cur_sel = {n for n, v in self.pax_vars.items() if v.get()}
        for w in self.pax_box.winfo_children():
            w.destroy()
        self.pax_vars.clear()
        try:
            plist = pax_mod.load_passengers()
        except Exception as e:
            plist = []
            self._put_log("[错误] 乘车人读取失败：%s" % e)
        if not plist:
            ttk.Label(self.pax_box, text="（未添加乘车人，点右侧按钮添加）",
                      foreground="#6e7781").pack(side="left")
            return
        sel = cur_sel or self.lc.get("passenger_names") or pax_mod.default_names() or [plist[0]["name"]]
        for p in plist:
            v = tk.BooleanVar(value=p["name"] in sel)
            self.pax_vars[p["name"]] = v
            label = "%s · %s" % (p["name"], mask_id(p.get("id_no")))
            if p.get("mobile"):
                label += " · " + mask_mobile(p.get("mobile"))
            ttk.Checkbutton(self.pax_box, text=label, variable=v).pack(side="left", padx=(0, 12))

    # ---- 车次查询与席别联动 ----

    def _rebuild_seats(self, available=None):
        """重建席别勾选区。

        available=None：显示全量可选席别（SEAT_OPTIONS）。
        available={席别: 余票}：按该车次实际可购席别显示并带余票数。
        勾选策略：优先保留原勾选；原勾选与新列表无交集时自动勾第一个可购席别。"""
        keep = {s for s, v in self.seat_vars.items() if v.get()}
        for w in self.seat_box.winfo_children():
            w.destroy()
        self.seat_vars = {}
        names = [s for s in SEAT_OPTIONS if available is None or s in available]
        names += sorted((s for s in (available or {}) if s not in SEAT_OPTIONS),
                        key=lambda x: SEAT_ORDER.get(x, 99))
        if available is not None:
            self.sf.configure(text=" 票种与席别（已按所选车次刷新：%d 种可购席别） " % len(names))
        else:
            self.sf.configure(text=" 票种与席别（点车次卡片可按该车实际余票刷新，票价以官方下单页为准） ")
        if not names:
            ttk.Label(self.seat_box, text="（该车次暂无可购席别）",
                      foreground="#6e7781").grid(row=0, column=0, sticky="w")
            return
        default = set(self.lc.get("seat_types") or [])
        chosen = [s for s in names if s in keep] or [s for s in names if s in default] or [names[0]]
        for i, s in enumerate(names):
            v = tk.BooleanVar(value=s in chosen)
            self.seat_vars[s] = v
            val = (available or {}).get(s)
            label = "%s · %s" % (s, val) if val else s
            ttk.Checkbutton(self.seat_box, text=label, variable=v).grid(
                row=i // 6, column=i % 6, sticky="w", padx=(0, 6), pady=1)
        if available is not None and not (keep & set(names)):
            self._put_log("[席别] 该车次可购席别与原勾选无交集，已自动勾选 %s" % chosen[0])

    def _sync_inputs(self):
        """把界面行程输入落到 lc 并做基本校验（查询车次用，不弹窗）。"""
        self._ui_to_lc()
        lc = self.lc
        if not lc.get("from") or not lc.get("to"):
            return False
        return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", lc.get("date") or ""))

    def query_trains(self, silent=False):
        """拉取实时余票填充车次列表（后台线程，界面不卡）。"""
        if self._querying:
            if not silent:
                self._put_log("[提醒] 上一次查询还在进行中…")
            return
        if not self._sync_inputs():
            if silent:
                self.auto_refresh_var.set(False)
                self._put_log("[提醒] 行程信息不完整，已关闭余票自动刷新")
            else:
                messagebox.showwarning("参数不完整",
                                       "请填写出发站 / 到达站，日期格式 YYYY-MM-DD", parent=self)
            return
        self._querying = True
        self._last_query_ts = time.time()
        self.query_btn.configure(state="disabled")
        self.query_state.configure(text="查询中…", foreground="#d97706")
        lc = dict(self.lc)
        self._put_log("[查询] %s → %s %s（%s）…" % (
            lc.get("from"), lc.get("to"), lc.get("date"),
            "学生票" if lc.get("purpose_code") == "0X00" else "成人票"))
        threading.Thread(target=self._query_work, args=(lc,), daemon=True,
                         name="train-query").start()

    @staticmethod
    def _resolve_dates(lc):
        """把「从 / 到」解析为待查日期列表（最多 5 天）。"""
        d0s = (lc.get("date") or "").strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", d0s):
            raise RuntimeError("日期格式应为 YYYY-MM-DD")
        dates = [d0s]
        d1s = (lc.get("date_to") or "").strip()
        if d1s and d1s != d0s:
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", d1s):
                raise RuntimeError("「到」日期格式应为 YYYY-MM-DD")
            d0 = datetime.strptime(d0s, "%Y-%m-%d").date()
            d1 = datetime.strptime(d1s, "%Y-%m-%d").date()
            if d1 < d0:
                raise RuntimeError("「到」不能早于「从」")
            if (d1 - d0).days > 4:
                raise RuntimeError("日期区间最多相差 5 天")
            dates = [(d0 + timedelta(days=i)).strftime("%Y-%m-%d")
                     for i in range((d1 - d0).days + 1)]
        return dates

    def _query_work(self, lc):
        try:
            name2code, code2name = tk_mod.load_station_map()
            fc, tc = name2code.get(lc.get("from")), name2code.get(lc.get("to"))
            if not fc or not tc:
                raise RuntimeError("车站无法识别：%s / %s" % (lc.get("from"), lc.get("to")))
            infos = []
            for dt in self._resolve_dates(lc):
                rows = tk_mod.query_tickets(fc, tc, dt,
                                            purpose=lc.get("purpose_code") or "ADULT")
                for r in rows:
                    info = tk_mod.parse_row(r, code2name, dt)
                    info["_date"] = dt
                    infos.append(info)
            self.dataq.put(("trains", infos))
        except Exception as e:
            self.dataq.put(("trains_err", "%s: %s" % (type(e).__name__, e)))

    def _apply_train_rows(self, infos):
        self._querying = False
        self.query_btn.configure(state="normal")
        self._train_infos = list(infos)
        self._refresh_train_view(log=True)
        self._remember_query()
        learn_station_kinds(infos)

    def _refresh_train_view(self, log=False):
        """按筛选 / 排序条件重建车次卡片列表。"""
        infos = list(getattr(self, "_train_infos", []))
        only_emu = self.filter_emu_var.get()
        only_putong = self.filter_putong_var.get()
        if only_emu and not only_putong:
            infos = [i for i in infos if is_emu(i.get("train_code"))]
        elif only_putong and not only_emu:
            infos = [i for i in infos if not is_emu(i.get("train_code"))]
        if self.filter_avail_var.get():
            infos = [i for i in infos if i.get("available_seats")]
        if self.sort_var.get() == "dur":
            infos.sort(key=lambda x: ((x.get("_date") or ""), x.get("duration") or "99:99:99"))
        else:
            infos.sort(key=lambda x: ((x.get("_date") or ""), x.get("start_time") or "99:99"))
        total_all = len(getattr(self, "_train_infos", []))
        empty = "（没有符合筛选条件的车次）" if total_all else "（还没有车次，点「查询车次 (F5)」拉取实时余票）"
        self.cardlist.render(infos, empty)
        hit = [i for i in infos if i.get("available_seats")]
        if infos and len(infos) != total_all:
            state_txt = "%d/%d 趟 · 有票 %d 趟" % (len(infos), total_all, len(hit))
        else:
            state_txt = "%d 趟 · 有票 %d 趟" % (len(infos), len(hit))
        self.query_state.configure(text=state_txt,
                                   foreground="#1a7f37" if hit else "#6e7781")
        if log:
            if hit:
                self._put_log("[查询] %d 趟车里 %d 趟有票：%s" % (
                    len(infos), len(hit), " ".join(i["train_code"] for i in hit[:10])))
            else:
                self._put_log("[查询] 共 %d 趟，暂无可购余票" % len(infos))

    def _on_cards_changed(self, infos):
        """卡片选中变化：把选中车次写回车次框，并按并集余票刷新席别区。"""
        if not hasattr(self, "seat_box"):
            return          # _build 尚未建好席别区
        codes = self.cardlist.selected_codes()
        merged = {}
        for i in infos:
            for k, v in (i.get("available_seats") or {}).items():
                merged.setdefault(k, v)
        self.trains_var.set(",".join(codes))
        self._rebuild_seats(merged if merged else None)
        self.sel_state.configure(text="已选 %d 趟车 · %d 种席别" % (len(codes), len(merged)))
        if codes:
            self._put_log("[选择] 已选 %d 趟车（%s），可购席别 %d 种" % (
                len(codes), ",".join(codes), len(merged)))

    def _apply_train_error(self, err):
        self._querying = False
        self.query_btn.configure(state="normal")
        self.query_state.configure(text="查询失败", foreground="#cf222e")
        self._put_log("[错误] 车次查询失败：%s" % err)

    def _on_purpose_change(self):
        self.lc["purpose_code"] = self.purpose_var.get() or "ADULT"
        self._put_log("[票种] 已切换为%s（余票口径随之变化，建议重新查询）" % (
            "学生票" if self.purpose_var.get() == "0X00" else "成人票"))

    def _clear_train_rows(self):
        self._train_infos = []
        self._train_rows = {}
        self.cardlist.render([])
        self.query_state.configure(text="", foreground="#6e7781")
        self.sel_state.configure(text="已选 0 趟车 · 0 种席别")
        self._rebuild_seats()

    def _swap_stations(self):
        a, b = self.from_ent.get(), self.to_ent.get()
        self.from_ent.set(b)
        self.to_ent.set(a)
        if a or b:
            self._put_log("[行程] 已交换出发/到达：%s → %s" % (b or "?", a or "?"))

    def _open_datetime(self):
        DateTimeDialog(self, self.start_var)

    def _open_calendar(self, var=None):
        CalendarDialog(self, var if var is not None else self.date_var)

    def _remember_query(self):
        """记录本次查询到历史（去重、最多 10 条）。"""
        lc = self.lc
        item = {"from": lc.get("from") or "", "to": lc.get("to") or "",
                "date": lc.get("date") or ""}
        if not (item["from"] and item["to"]):
            return
        hist = [h for h in (lc.get("query_history") or [])
                if not (h.get("from") == item["from"] and h.get("to") == item["to"]
                        and h.get("date") == item["date"])]
        hist.insert(0, item)
        lc["query_history"] = hist[:10]
        save_launcher_config(lc)

    def _show_query_history(self):
        hist = self.lc.get("query_history") or []
        if not hist:
            messagebox.showinfo("历史查询记录", "还没有查询记录。", parent=self)
            return
        QueryHistoryDialog(self, hist, self._apply_history)

    def _apply_history(self, item):
        self.from_ent.set(item.get("from") or "")
        self.to_ent.set(item.get("to") or "")
        self.date_var.set(item.get("date") or "")
        self.date_to_var.set("")
        self._put_log("[历史] 已填入 %s → %s %s" % (
            item.get("from"), item.get("to"), item.get("date")))

    def _auto_refresh_trains(self):
        """余票自动刷新（在 _tick 里按间隔触发）。"""
        if not self.auto_refresh_var.get() or self._querying or self.grabber:
            return
        try:
            gap = max(20, int(self.refresh_sec_var.get() or 60))
        except (tk.TclError, ValueError):
            gap = 60
        if time.time() - self._last_query_ts >= gap:
            self.query_trains(silent=True)

    # ---- 定时器 ----

    def _tick(self):
        try:
            self._sync_monitor()
            self._sync_pax()
            self._auto_refresh_trains()
            now = datetime.now()
            g = self.grabber
            if g and not g.is_alive():
                self.grabber = None
                ok, msg = g.result if g.result else (False, "已停止")
                self.go_btn.configure(text="立 即 抢 票", bg="#cf222e", state="normal")
                if ok:
                    self.auto_fired = True
                    self._set_status("ok", "抢票成功！")
                    self.bell()
                else:
                    self._set_status("idle", msg or "已停止")
            if not self.grabber:
                st = parse_dt(self.start_var.get().strip())
                if st:
                    diff = (st - now).total_seconds()
                    lead = 0
                    try:
                        lead = max(0, int(self.warm_var.get() or 10)) * 60
                    except Exception:
                        pass
                    if diff > lead:
                        _cd = "距开抢 %s" % fmt_countdown(diff)
                        _cd = ("⏰ " + _cd) if self.armed else ("⚠ 自动抢未启用 · " + _cd)
                        self.countdown_lbl.configure(text=_cd)
                        remind = int(self.remind_var.get() or 0)
                        if self.armed and not self.reminded and remind > 0 and diff <= remind * 60:
                            self.reminded = True
                            self._put_log("[提醒] 距离开抢不到 %d 分钟！" % remind)
                            self.bell()
                            messagebox.showinfo("开抢提醒", "距离开抢不到 %d 分钟！\n开抢时间：%s" % (
                                remind, st.strftime("%Y-%m-%d %H:%M:%S")), parent=self)
                    else:
                        if self.armed and not self.auto_fired and (now - st).total_seconds() <= max(90, lead):
                            self.auto_fired = True
                            self.countdown_lbl.configure(text="已到点，自动开抢！")
                            if lead > 0:
                                self._put_log("[自动] 进入预热窗口（开抢前 %d 分钟）：先登录并保持浏览器，到点立即下单" % int(lead / 60))
                            else:
                                self._put_log("[自动] 已到点，自动开始抢票（%s）" % st.strftime("%H:%M:%S"))
                            self._put_log("[提示] 抢票在后台运行，浏览器无窗口，请勿关闭本窗口")
                            self.bell()
                            self.start_grab(auto=True)
                        elif self.armed and not self.auto_fired:
                            self.armed = False
                            self.countdown_lbl.configure(text="开抢时间已过，请手动开抢")
                        else:
                            self.countdown_lbl.configure(text="开抢时间已过")
                else:
                    self.countdown_lbl.configure(text="未设置开抢时间")
        finally:
            self.after(500, self._tick)

    # ---- 与监控系统实时联动 ----

    def _sync_monitor(self):
        """config.json 有变化时，把监控系统新加的车次合并进来。"""
        path = os.path.join(HERE, "config.json")
        try:
            mt = os.path.getmtime(path)
        except OSError:
            return
        if mt == self._monitor_mtime:
            return
        self._monitor_mtime = mt
        if merge_trains_from_monitor(self.lc):
            self.trains_var.set(",".join(self.lc["trains"]))

    def _sync_pax(self):
        """passengers.json 有变化（监控系统增删改乘车人）时刷新勾选区。"""
        path = os.path.join(HERE, "passengers.json")
        try:
            mt = os.path.getmtime(path)
        except OSError:
            return
        if mt == self._pax_mtime:
            return
        self._pax_mtime = mt
        self._refresh_pax()

    def _drain(self):
        try:
            while True:
                self._put_log(LOGQ.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                kind, payload = self.dataq.get_nowait()
                if kind == "trains":
                    self._apply_train_rows(payload)
                elif kind == "trains_err":
                    self._apply_train_error(payload)
        except queue.Empty:
            pass
        self.after(200, self._drain)

    def _put_log(self, line):
        txt = self.log_text
        txt.configure(state="normal")
        txt.insert("end", line + "\n")
        for tag in LOG_COLOR:
            if tag in line:
                txt.tag_add(tag, "end-2l", "end-1l")
                break
        txt.configure(state="disabled")
        txt.see("end")

    def _set_status(self, color_key, text):
        colors = {"running": "#d97706", "ok": "#1a7f37", "idle": "#6e7781"}
        self.dot.itemconfigure(self._dot_id, fill=colors.get(color_key, "#6e7781"))
        self.status_lbl.configure(text=text)

    # ---- 抢票控制 ----

    def _validate(self):
        lc = self.lc
        if not lc.get("from") or not lc.get("to"):
            messagebox.showwarning("参数不完整", "请填写出发站和到达站", parent=self)
            return False
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", lc.get("date") or ""):
            messagebox.showwarning("参数不完整", "日期格式应为 YYYY-MM-DD", parent=self)
            return False
        if not lc.get("seat_types"):
            messagebox.showwarning("参数不完整", "请至少勾选一种席别", parent=self)
            return False
        if not lc.get("passenger_names"):
            if not messagebox.askyesno("未选乘车人", "未勾选乘车人，将使用账号默认乘车人，继续？", parent=self):
                return False
        return True

    def toggle_grab(self):
        if self.grabber and self.grabber.is_alive():
            self.stop_grab()
        else:
            self.start_grab()

    def start_grab(self, auto=False):
        if self.grabber and self.grabber.is_alive():
            return
        if auto:
            self.auto_fired = True
        self._ui_to_lc()
        if not self._validate():
            return
        self.grabber = Grabber(dict(self.lc))
        self.grabber.start()
        self._set_status("running", "抢票中…（后台运行，无窗口）")
        self.go_btn.configure(text="停 止", bg="#57606a")
        self._put_log("[启动] 开始抢票：%s → %s %s，车次 %s" % (
            self.lc.get("from"), self.lc.get("to"), self.lc.get("date"),
            "、".join(self.lc.get("trains") or []) or "全部"))

    def stop_grab(self):
        if self.grabber:
            self.grabber.stop()
            self._put_log("[停止] 已请求停止抢票")
            self._set_status("idle", "已请求停止，等待当前操作结束…")
            self.go_btn.configure(state="disabled")

    # ---- 会话 / 定位 / 更新 ----

    def open_new_monitor_task(self):
        """打开「新建监控任务」对话框（任务写入监控系统的任务表）。"""
        NewMonitorTaskDialog(self)

    def _test_session(self):
        self._put_log("[会话] 正在校验登录状态…")

        def worker():
            try:
                ok, who = browser_order.check_session()
                LOGQ.put("[会话] 校验结果：%s（%s）" % ("已登录" if ok else "未登录", who))
            except Exception as e:
                LOGQ.put("[错误] 会话校验异常：%s" % e)
        threading.Thread(target=worker, daemon=True).start()

    def _locate(self):
        self._put_log("[运行] 正在定位当前 IP 位置…")

        def worker():
            LOGQ.put("[提醒] 当前位置：%s" % ip_locate())
        threading.Thread(target=worker, daemon=True).start()

    def _check_update_async(self):
        def worker():
            try:
                res = check_update(self.lc)
            except Exception:
                return
            self.update_info = (res[1], res[2], res[3]) if res[0] else None
            try:
                self.after(0, self._show_update)
            except RuntimeError:
                pass  # 窗口已关闭，忽略
        threading.Thread(target=worker, daemon=True).start()

    def _show_update(self):
        if self.update_info:
            ver, notes, url = self.update_info
            self.update_lbl.configure(text="新版本 v%s 可用，点击查看" % ver)
            self.update_lbl.bind("<Button-1>", lambda e: messagebox.showinfo(
                "版本更新", "新版本：v%s\n\n%s\n\n下载地址：%s" % (ver, notes or "（无说明）", url or "（未提供）"), parent=self))

    # ---- 预设 ----

    def _current_preset(self):
        sel = self.preset_cb.get().strip()
        for p in self.lc.get("presets") or []:
            if (p.get("name") or "") == sel:
                return p
        return None

    def _load_preset(self, _event=None):
        p = self._current_preset()
        if not p:
            return
        self.from_ent.set(p.get("from") or "")
        self.to_ent.set(p.get("to") or "")
        self.trains_var.set(",".join(p.get("trains") or []))
        for s, v in self.seat_vars.items():
            v.set(s in (p.get("seat_types") or []))
        self.date_var.set(p.get("date") or "")
        self._put_log("[运行] 已载入常用行程「%s」" % p.get("name"))

    def _save_preset(self):
        self._ui_to_lc()
        name = simpledialog.askstring("存为预设", "给这个常用行程起个名字：", parent=self,
                                      initialvalue=self.preset_cb.get())
        if not name:
            return
        entry = {
            "name": name.strip(),
            "from": self.lc.get("from"),
            "to": self.lc.get("to"),
            "trains": list(self.lc.get("trains") or []),
            "seat_types": list(self.lc.get("seat_types") or []),
            "date": self.lc.get("date") or "",
        }
        presets = [p for p in (self.lc.get("presets") or []) if (p.get("name") or "") != entry["name"]]
        presets.append(entry)
        self.lc["presets"] = presets
        save_launcher_config(self.lc)
        self.preset_cb.configure(values=[p["name"] for p in presets])
        self.preset_cb.set(entry["name"])
        self._put_log("[运行] 常用行程「%s」已保存" % entry["name"])

    def _delete_preset(self):
        p = self._current_preset()
        if not p:
            messagebox.showinfo("删除预设", "请先在下拉框里选中要删除的行程", parent=self)
            return
        if messagebox.askyesno("删除预设", "确定删除常用行程「%s」？" % p.get("name"), parent=self):
            self.lc["presets"] = [x for x in (self.lc.get("presets") or []) if x is not p]
            save_launcher_config(self.lc)
            self.preset_cb.configure(values=[x.get("name") or "" for x in self.lc["presets"]])
            self.preset_cb.set("")
            self._put_log("[运行] 常用行程「%s」已删除" % p.get("name"))

    # ---- 乘车人编辑 ----

    def _edit_pax(self):
        dlg = PassengerDialog(self, on_saved=self._pax_saved_by_dialog)

    def _pax_saved_by_dialog(self):
        self._refresh_pax()
        try:
            self._pax_mtime = os.path.getmtime(os.path.join(HERE, "passengers.json"))
        except OSError:
            pass

    # ---- 收尾 ----

    def _on_start_change(self, *_):
        st = parse_dt(self.start_var.get())
        self.armed = bool(st and st > datetime.now())
        self.auto_fired = False
        self.reminded = False

    def _on_close(self):
        if self.grabber:
            self.grabber.stop()
        try:
            self._ui_to_lc()
        except Exception:
            pass
        self.destroy()


class NewMonitorTaskDialog(tk.Toplevel):
    """新建监控任务（从监控系统搬来的「直接监视」模块）：
    填 出发/到达/日期/车次/席别/乘车人 → 写入监控系统的任务表。
    监控软件运行中每 2 秒刷新任务列表；引擎运行时新任务自动纳入调度。"""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("新建监控任务")
        self.geometry("500x780")
        self.resizable(False, False)
        self.seat_vars = {}
        self.pax_vars = {}
        try:
            self.name2code, self.code2name = tk_mod.load_station_map()
        except Exception as e:
            messagebox.showerror("错误", "车站代码表加载失败：%s" % e, parent=self)
            self.destroy()
            return
        self._build()
        self.transient(app)
        self.grab_set()

    def _build(self):
        # 行程
        tf = ttk.LabelFrame(self, text=" 行程 ", padding=8)
        tf.pack(fill="x", padx=10, pady=(10, 4))
        r0 = ttk.Frame(tf)
        r0.pack(fill="x")
        ttk.Label(r0, text="出发").pack(side="left")
        hist = self.app.lc.get("station_history") or []
        self.from_cb = ttk.Combobox(r0, values=hist, width=12)
        self.from_cb.pack(side="left", padx=4)
        self.from_cb.set(self.app.from_ent.get())
        ttk.Label(r0, text="到达").pack(side="left", padx=(12, 0))
        self.to_cb = ttk.Combobox(r0, values=hist, width=12)
        self.to_cb.pack(side="left", padx=4)
        self.to_cb.set(self.app.to_ent.get())
        ttk.Button(r0, text="从主界面带入", command=self._import_from_main).pack(side="right")

        r1 = ttk.Frame(tf)
        r1.pack(fill="x", pady=(8, 0))
        ttk.Label(r1, text="日期").pack(side="left")
        self.date_var = tk.StringVar(value=self.app.date_var.get())
        ttk.Entry(r1, textvariable=self.date_var, width=24).pack(side="left", padx=4)
        ttk.Label(r1, text="车次").pack(side="left", padx=(10, 0))
        self.trains_var = tk.StringVar(value=self.app.trains_var.get())
        ttk.Entry(r1, textvariable=self.trains_var).pack(side="left", padx=4, fill="x", expand=True)
        ttk.Label(tf, text="日期：单日 2026-10-06；范围 2026-10-06~2026-10-08（跨度≤5天）。车次逗号分隔，留空=全部车次",
                  foreground="#6e7781").pack(anchor="w", pady=(4, 0))

        # 票种
        pf = ttk.LabelFrame(self, text=" 票种 ", padding=8)
        pf.pack(fill="x", padx=10, pady=4)
        self.purpose_var = tk.StringVar(value="ADULT")
        ttk.Radiobutton(pf, text="成人票", value="ADULT",
                        variable=self.purpose_var).pack(side="left", padx=(0, 16))
        ttk.Radiobutton(pf, text="学生票", value="0X00",
                        variable=self.purpose_var).pack(side="left")

        # 席别
        sf = ttk.LabelFrame(self, text=" 监控席别（至少选一个） ", padding=8)
        sf.pack(fill="x", padx=10, pady=4)
        for i, s in enumerate(MONITOR_SEAT_CHOICES):
            v = tk.BooleanVar(value=s in (self.app.lc.get("seat_types") or []))
            self.seat_vars[s] = v
            ttk.Checkbutton(sf, text=s, variable=v).grid(
                row=i // 3, column=i % 3, sticky="w", padx=(0, 8))

        # 乘车人
        gf = ttk.LabelFrame(self, text=" 乘车人（不选=下单时用默认乘车人） ", padding=8)
        gf.pack(fill="x", padx=10, pady=4)
        sel = {n for n, v in self.app.pax_vars.items() if v.get()}
        try:
            plist = pax_mod.load_passengers()
        except Exception:
            plist = []
        self.pax_frame = ttk.Frame(gf)
        self.pax_frame.pack(fill="x")
        if not plist:
            ttk.Label(self.pax_frame, text="（乘客库为空，可在启动器主界面添加）",
                      foreground="#6e7781").pack(side="left")
        for p in plist:
            name = p.get("name") or ""
            v = tk.BooleanVar(value=name in sel)
            self.pax_vars[name] = v
            ttk.Checkbutton(self.pax_frame, text=name, variable=v).pack(
                side="left", padx=(0, 12))

        # 选项
        of = ttk.LabelFrame(self, text=" 选项 ", padding=8)
        of.pack(fill="x", padx=10, pady=4)
        r2 = ttk.Frame(of)
        r2.pack(fill="x")
        ttk.Label(r2, text="优先级").pack(side="left")
        self.prio_var = tk.IntVar(value=5)
        ttk.Spinbox(r2, from_=1, to=10, textvariable=self.prio_var,
                    width=5).pack(side="left", padx=4)
        ttk.Label(r2, text="（1-10，数字越大越先抢）",
                  foreground="#6e7781").pack(side="left")
        self.auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(of, text="余票命中后自动下单（不支付）",
                        variable=self.auto_var).pack(anchor="w")
        self.stop_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(of, text="一次下单成功后自动停止本任务",
                        variable=self.stop_var).pack(anchor="w")

        # 按钮
        bf = ttk.Frame(self)
        bf.pack(fill="x", padx=10, pady=(8, 10))
        ttk.Button(bf, text="创建并开始监视",
                   command=lambda: self._create(True)).pack(
            side="left", fill="x", expand=True, padx=(0, 4))
        ttk.Button(bf, text="仅创建任务",
                   command=lambda: self._create(False)).pack(
            side="left", fill="x", expand=True, padx=(4, 0))

    def _import_from_main(self):
        """把启动器主界面的行程/车次/日期/席别带入对话框。"""
        self.from_cb.set(self.app.from_ent.get())
        self.to_cb.set(self.app.to_ent.get())
        self.trains_var.set(self.app.trains_var.get())
        self.date_var.set(self.app.date_var.get())
        for s, v in self.seat_vars.items():
            v.set(s in (self.app.lc.get("seat_types") or []))

    def _parse_dates(self):
        raw = self.date_var.get().strip()
        if "~" in raw:
            a, b = [x.strip() for x in raw.split("~", 1)]
            d0 = datetime.strptime(a, "%Y-%m-%d")
            d1 = datetime.strptime(b, "%Y-%m-%d")
            if d1 < d0:
                raise ValueError("结束日期不能早于开始日期")
            if (d1 - d0).days > 5:
                raise ValueError("日期跨度最多相差 5 天")
            return [], [d0.strftime("%Y-%m-%d"), d1.strftime("%Y-%m-%d")]
        return [datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")], []

    def _create(self, start_now):
        from_name, to_name = self.from_cb.get().strip(), self.to_cb.get().strip()
        if from_name not in self.name2code or to_name not in self.name2code:
            messagebox.showwarning("提示", "出发站/到达站无效，请选择下拉里的有效站名", parent=self)
            return
        if from_name == to_name:
            messagebox.showwarning("提示", "出发站与到达站不能相同", parent=self)
            return
        try:
            dates, date_range = self._parse_dates()
        except ValueError as e:
            messagebox.showwarning("提示", "日期格式错误：%s" % e, parent=self)
            return
        seats = [s for s, v in self.seat_vars.items() if v.get()]
        if not seats:
            messagebox.showwarning("提示", "请至少选择一个席别", parent=self)
            return
        trains = [t.strip().upper() for t in
                  re.split(r"[,，\s]+", self.trains_var.get()) if t.strip()]
        passengers = [n for n, v in self.pax_vars.items() if v.get()]
        task = build_monitor_task(from_name, to_name, dates, date_range, trains,
                                  seats, passengers, self.purpose_var.get(),
                                  self.auto_var.get(), self.stop_var.get(),
                                  self.prio_var.get())
        try:
            name = append_monitor_task(task, start_now)
        except Exception as e:
            messagebox.showerror("创建失败", "写入监控系统配置失败：%s" % e, parent=self)
            return
        self.app._put_log("[运行] 已创建监控任务「%s」%s" % (
            name, "，监控中" if start_now else "，未启动"))
        self.destroy()
        if start_now:
            messagebox.showinfo(
                "完成",
                "任务「%s」已创建并开始监视。\n\n监控软件运行中会自动加载"
                "（约 2 秒），引擎运行时立即自动开抢；\n若监控软件未开启，"
                "下次启动后该任务已在监控中。" % name, parent=self.app)
        else:
            messagebox.showinfo(
                "完成",
                "任务「%s」已创建。\n\n在监控软件里勾选「启动」后开始监视。" % name,
                parent=self.app)


class PassengerDialog(tk.Toplevel):
    """乘车人新增/编辑。证件号/手机号经 passengers.py 加密落盘，界面只回显明文输入。"""

    def __init__(self, master, on_saved=None):
        super().__init__(master)
        self.master = master
        self.on_saved = on_saved
        self.title("乘车人信息")
        self.geometry("380x330")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        try:
            self.plist = pax_mod.load_passengers()
        except Exception as e:
            self.plist = []
            messagebox.showwarning("读取失败", "乘车人数据读取失败：%s" % e, parent=self)

        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)

        r0 = ttk.Frame(f)
        r0.pack(fill="x")
        ttk.Label(r0, text="选择").pack(side="left")
        self.pick = ttk.Combobox(r0, state="readonly", width=26,
                                 values=[p.get("name") or "" for p in self.plist])
        self.pick.pack(side="left", padx=6)
        self.pick.bind("<<ComboboxSelected>>", self._load)
        ttk.Button(r0, text="删除此人", command=self._delete).pack(side="left", padx=6)

        self.name_var = tk.StringVar()
        self.id_var = tk.StringVar()
        self.mob_var = tk.StringVar()
        self.type_var = tk.StringVar(value="二代身份证")
        self.adult_var = tk.BooleanVar(value=True)
        self.default_var = tk.BooleanVar(value=False)

        # 表单区单独一个容器：f 里已经 pack 了 r0，同一个父容器不能再混用 grid
        # （否则报 "cannot use geometry manager grid inside ... already has slaves
        #   managed by pack"，整个弹窗直接崩掉）。
        g = ttk.Frame(f)
        g.pack(fill="both", expand=True)
        grid = [
            ("姓名", ttk.Entry(g, textvariable=self.name_var, width=26)),
            ("证件类型", ttk.Combobox(g, textvariable=self.type_var, width=23,
                                      values=list(ID_TYPES.keys()), state="readonly")),
            ("证件号", ttk.Entry(g, textvariable=self.id_var, width=26, show="*")),
            ("手机号", ttk.Entry(g, textvariable=self.mob_var, width=26)),
        ]
        for i, (lab, w) in enumerate(grid):
            ttk.Label(g, text=lab).grid(row=i + 1, column=0, sticky="e", pady=4)
            w.grid(row=i + 1, column=1, sticky="w", padx=6, pady=4)
        ttk.Checkbutton(g, text="成人票", variable=self.adult_var).grid(row=5, column=1, sticky="w", pady=2)
        ttk.Checkbutton(g, text="设为默认乘车人", variable=self.default_var).grid(row=6, column=1, sticky="w")

        bf = ttk.Frame(g)
        bf.grid(row=7, column=0, columnspan=2, pady=(10, 0))
        ttk.Button(bf, text="保 存", command=self._save).pack(side="left", padx=6)
        ttk.Button(bf, text="关 闭", command=self.destroy).pack(side="left", padx=6)

        if self.plist:
            self.pick.current(0)
            self._load()

    def _load(self, _event=None):
        name = self.pick.get().strip()
        for p in self.plist:
            if (p.get("name") or "") == name:
                self.name_var.set(p.get("name") or "")
                self.id_var.set(p.get("id_no") or "")
                self.mob_var.set(p.get("mobile") or "")
                for lab, code in ID_TYPES.items():
                    if code == str(p.get("id_type_code")):
                        self.type_var.set(lab)
                self.adult_var.set(bool(p.get("is_adult", True)))
                self.default_var.set(bool(p.get("is_default")))
                return

    def _delete(self):
        name = self.pick.get().strip()
        if not name:
            return
        if not messagebox.askyesno("删除乘车人", "确定删除「%s」？" % name, parent=self):
            return
        self.plist = [p for p in self.plist if (p.get("name") or "") != name]
        try:
            pax_mod.save_passengers(self.plist)
        except Exception as e:
            messagebox.showerror("保存失败", str(e), parent=self)
            return
        self.pick.configure(values=[p.get("name") or "" for p in self.plist])
        self.pick.set("")
        self._notify_saved()

    def _save(self):
        name = self.name_var.get().strip()
        id_no = self.id_var.get().strip()
        if not name or not id_no:
            messagebox.showwarning("信息不完整", "姓名和证件号不能为空", parent=self)
            return
        if self.default_var.get():
            for p in self.plist:
                p["is_default"] = False
        rec = {
            "name": name,
            "id_type_code": ID_TYPES.get(self.type_var.get(), "1"),
            "id_no": id_no,
            "mobile": self.mob_var.get().strip(),
            "is_default": bool(self.default_var.get()),
            "is_adult": bool(self.adult_var.get()),
        }
        replaced = False
        for i, p in enumerate(self.plist):
            if (p.get("name") or "") == name:
                self.plist[i] = rec
                replaced = True
                break
        if not replaced:
            self.plist.append(rec)
        try:
            pax_mod.save_passengers(self.plist)
        except Exception as e:
            messagebox.showerror("保存失败", "加密保存失败：%s" % e, parent=self)
            return
        self.pick.configure(values=[p.get("name") or "" for p in self.plist])
        self.pick.set(name)
        self._notify_saved()
        messagebox.showinfo("已保存", "乘车人「%s」已加密保存" % name, parent=self)

    def _notify_saved(self):
        if self.on_saved:
            try:
                self.on_saved()
            except Exception:
                pass


def ensure_passengers():
    """首次运行：passengers.json 不存在时，把 config.json 任务里的乘车人名字
    迁入加密乘客库（证件号留空，待用户在「新增/编辑」里补全）。"""
    if os.path.exists(os.path.join(HERE, "passengers.json")):
        return
    names = []
    try:
        with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8") as f:
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
        pax_mod.save_passengers([
            {"name": n, "id_type_code": "1", "id_no": "", "mobile": "",
             "is_default": i == 0, "is_adult": True}
            for i, n in enumerate(names)])
        log("[提醒] 已从监控任务导入乘车人：%s（证件号请点「新增/编辑」补全）" % "、".join(names))
    except Exception as e:
        log("[错误] 乘车人导入失败：%s" % e)


def main():
    ensure_passengers()
    app = LauncherApp()
    app.mainloop()


if __name__ == "__main__":
    main()

