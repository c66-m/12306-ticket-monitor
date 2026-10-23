# -*- coding: utf-8 -*-
"""
跨界面共享的基础工具：日期区间解析、原子文件 IO、PII 脱敏口径、北京时间。

只收编「无策略差异」的机械层。涉及锁（filelock / _CFG_WRITE_LOCK）、坏档
时间戳挪档、mtime 二次检测、deepcopy 快照与直写兜底等防护语义全部留在
调用方（参数化保留差异，不是抹平差异）——见 RULES.md。
"""

import datetime
import filelock
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time

# 时钟单源（infra 层）：beijing_tz 转发，既有调用点零改动
from infra.clock import beijing_tz, now_ts as clock_now_ts, now_tz as clock_now_tz  # noqa: F401
# 双写镜像（P0：JSON 为主、SQLite 为副；镜像失败只告警不阻断；仅正规数据路径）
from adapters.store import dualwrite  # noqa: F401

LOG = logging.getLogger("monitor")

HERE = os.path.dirname(os.path.abspath(__file__))
# 运行时数据 / 静态数据统一收纳目录。代码文件留在根目录，数据与代码分离；
# config.json 与 launcher_config.json 属于「用户可手改的配置」，仍留在根目录。
DATA_DIR = os.path.join(HERE, "data")


def ensure_utf8_stdio() -> None:
    """把 stdout / stderr 切到 UTF-8 —— 中文 Windows 默认 GBK，中文输出会乱码。

    各 CLI 入口原先各写一份 5 行 try/except（9 处复制粘贴），统一到这里。
    幂等；流不支持 reconfigure（测试的 StringIO、被替换的管道）时静默跳过——
    这是能力探测而非业务错误，故吞掉三类预期异常。

    注意：api.py 不用本函数。它的 stdio 是 Electron 的 JSON-RPC 管道，reconfigure
    失败必须回报主进程（走自己的 _warn），语义不同、不能拉平。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError, ValueError):
            pass

MAX_DATE_SPAN_DAYS = 5   # 乘车日期区间最多相差天数（= 6 个日期）

# 购票历史 result → 中文标签（前端 / GUI / 终端共用一份，别再各写各的）。
# `failed` 的语义是「本轮未成功，下轮继续重试」（无票/排队/下单页状态变化都走
# 这条），不是「下单失败」——写成「下单失败」会让人以为任务终止、单子没了，
# 而真实情况常常是 12306 已经生成了未支付订单（拒绝重复下单的原文是
# 「包含未付款订单」）。改文案前先读 engine.py 里各 result 的产出点。
HISTORY_RESULT_LABEL = {
    "success": "下单成功",
    "dup": "防重跳过",
    "notified": "已通知",
    "hit_no_order": "有余票未下单",
    "ambiguous": "结果待核对",
    "browser_dead": "浏览器中断",
    "seat_unavailable": "席别不可售",
    "failed": "本轮未成功",
    "error": "异常",
    # 与 12306 官方订单对账后的改判结论（api.rpc_reconcile_history 写入）
    "confirmed_unpaid": "已对账·有未付单",
    "confirmed_paid": "已对账·已支付",
}


# ----------------------------- PII 脱敏 -----------------------------
# 脱敏口径的单源：engine / order / launcher_utils / browser_order / gui /
# monitor 曾各持一份同口径实现。口径漂移等于日志泄露，收敛到这里，原模块
# 保留同名别名，调用点不变。

def mask_order_no(ono):
    """订单号脱敏（日志用）：保留前后各 4 位，中间打码；过短则全打码。"""
    s = str(ono or "")
    if len(s) <= 8:
        return "****"
    return s[:4] + "****" + s[-4:]


def mask_one_name(n):
    """单个姓名打码（日志用）：保留首字其余打码。"""
    n = (n or "").strip()
    if not n:
        return ""
    return n[0] + "*" * (len(n) - 1) if len(n) > 1 else "*"


def mask_names(names):
    """乘车人姓名打码（日志用）：保留首字其余打码；['张三','李四'] -> '张*、李*'。"""
    out = []
    for n in names or []:
        m = mask_one_name(n)
        if m:
            out.append(m)
    return "、".join(out)


def mask_id_partial(id_no):
    """身份证脱敏（界面/日志显示用）：保留前 3 位与后 4 位。

    与 grabber_utils.mask_id 是**两种口径**：这里对长度 < 8 的输入原样返回
    且不 strip；mask_id 对长度 <= 8 原样返回但先 strip。界面路径用本函数，
    账号路径用 mask_id，勿互换。
    """
    if not id_no or len(id_no) < 8:
        return id_no or ""
    return id_no[:3] + "*" * (len(id_no) - 7) + id_no[-4:]


# ----------------------------- 北京时间 -----------------------------
# 单源在 infra/clock.beijing_tz（模块顶部已转发导入）。坐标说明：
# order._beijing_tz / logutil._beijing_tz 原先各自复制实现，均已转发到
# appcommon.beijing_tz 再向前转发，链路末端统一落在 infra/clock。


# ----------------------------- 展示归一化 -----------------------------
# trains / seat_types 字段的机械层单源：launcher / launcher_utils / gui 曾各持
# 一份同口径实现（裸字符串不逐字符拆，是防漏单的关键）。这里只收机械层，
# 「警告文案」这层策略由调用方以 str_warn / shape_warn 传入——展示路径传
# lambda *a: None（不打日志，防高频刷新刷屏）。

def normalize_trains(raw, warn, str_warn, shape_warn):
    """trains 归一化：裸字符串按单个车次处理，绝不逐字符拆；非法形状忽略；
    非字符串条目跳过。字符串条目 strip().upper()；形状异常时 warn 一次。"""
    raw = raw or []
    if isinstance(raw, str):
        warn(str_warn % raw)
        raw = [raw]
    elif not isinstance(raw, (list, tuple)):
        warn(shape_warn % (raw,))
        raw = []
    return [t.strip().upper() for t in raw
            if isinstance(t, str) and t.strip()]


def display_trains(raw):
    """展示用 trains 归一化：与 normalize_trains 同口径，不记日志。"""
    return normalize_trains(raw, lambda *a: None, "%s", "%r")


def normalize_seat_types(raw, warn, str_warn, shape_warn):
    """seat_types 归一化：裸字符串按单个席别处理，绝不逐字符拆；非法形状
    忽略；非字符串条目跳过。席别是中文名，不做 strip/upper。"""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        warn(str_warn % raw)
        return [raw]
    if not isinstance(raw, (list, tuple)):
        warn(shape_warn % (raw,))
        return []
    return [s for s in raw if isinstance(s, str)]


def display_seat_types(raw):
    """展示用 seat_types 归一化：与 normalize_seat_types 同口径，不记日志。"""
    return normalize_seat_types(raw, lambda *a: None, "%s", "%r")


# ----------------------------- 日期区间 -----------------------------

def parse_date_range(raw_from, raw_to=None, max_span_days=MAX_DATE_SPAN_DAYS):
    """把乘车日期输入解析为 (dates, date_range)，非法输入抛 ValueError。

    两种输入形态：
      - raw_to 给出（两变量形态）：raw_to 空或与 raw_from 相同 = 单日；
      - raw_to 为 None：raw_from 内允许「a~b」连写，无 ~ 即单日。
    返回 ISO 字符串；date_range 为 [起, 止] 或空列表。
    """
    for v in (raw_from, raw_to):
        if v is not None and not isinstance(v, str):
            raise ValueError("日期格式应为 YYYY-MM-DD（如 2026-10-07）")
    if raw_to is None:
        raw = (raw_from or "").strip()
        if "~" in raw:
            a, b = [x.strip() for x in raw.split("~", 1)]
        else:
            a, b = raw, ""
    else:
        a, b = (raw_from or "").strip(), (raw_to or "").strip()
        if b == a:
            b = ""
    for part in (a, b):
        # 严格 YYYY-MM-DD：fromisoformat 也接受 20261007 之类紧凑写法，不放行
        if part and not re.match(r"^\d{4}-\d{2}-\d{2}$", part):
            raise ValueError("日期格式应为 YYYY-MM-DD（如 2026-10-07）")
    if not a:
        raise ValueError("日期格式应为 YYYY-MM-DD（如 2026-10-07）")
    d0 = datetime.date.fromisoformat(a)
    if b:
        d1 = datetime.date.fromisoformat(b)
        if d1 < d0:
            raise ValueError("结束日期不能早于开始日期")
        if (d1 - d0).days > max_span_days:
            raise ValueError("日期跨度最多相差 %d 天" % max_span_days)
        return [], [d0.isoformat(), d1.isoformat()]
    return [d0.isoformat()], []


# ----------------------------- 原子文件 IO -----------------------------

def replace_with_retry(src, dst, tries=5, delay=0.1, fallback_direct=False):
    """os.replace 带 Windows 占用重试：目标/源被并发读写句柄占用的瞬间会
    PermissionError（杀毒扫描、对方 json.load 持有读句柄），短暂退避重试。

    fallback_direct=True 时重试耗尽后把 src 内容直写 dst（engine 的既有兜底
    语义），否则抛出最后一个 OSError。
    """
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except OSError:
            if i == tries - 1:
                if fallback_direct and os.path.exists(src):
                    with open(src, "r", encoding="utf-8") as f:
                        data = f.read()
                    with open(dst, "w", encoding="utf-8") as f:
                        f.write(data)
                    return
                raise
            time.sleep(delay)


def _fsync_quietly(fd):
    """fsync 尽力而为（R1-17）：失败只记 debug，绝不影响写入成功语义——
    个别文件系统/平台对 fsync 支持不全时，不能让全仓数据写路径跟着失败。"""
    try:
        os.fsync(fd)
    except OSError as e:
        LOG.debug("[IO] fsync 失败（已忽略，不影响写入）: %s", e)


def _fsync_parent_dir_quietly(path):
    """replace 后 fsync 父目录，让新目录项落盘防断电回滚（R1-17）。

    POSIX 可打开目录做 fsync；Windows 打开目录即失败，直接跳过。
    本函数全程尽力而为、绝不抛。"""
    if os.name == "nt":
        return
    d = os.path.dirname(os.path.abspath(path)) or "."
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        _fsync_quietly(fd)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def atomic_write_json(path, obj, *, tmp_kind="tmp", replace_tries=5,
                      replace_delay=0.1, fallback_direct=False, mode=None):
    """原子写 JSON：临时名带 pid+线程标识（同进程多线程/多窗口互不踩），
    写完 fsync tmp 文件后 replace 并对 Windows 占用做退避重试，replace 后
    再尽力 fsync 父目录（R1-17：防断电/崩溃丢失刚落盘的数据；fsync 失败
    只记 debug，绝不改变写入成功语义）。

    fallback_direct=True 时 replace 重试耗尽改为直写（丢原子性保数据，
    仅 engine 的 state.json 兜底使用这一语义）。

    mode: 可选，落盘权限（如 0o600 用于含 SMTP 授权码的 config.json）。
    在 tmp 文件上 chmod 再 replace，目标文件继承该权限；不传则保持默认
    行为不变。Windows 下 os.chmod 仅影响只读位，0o600 等价于无操作。
    """
    tmp = "{0}.{1}{2}-{3}".format(path, tmp_kind, os.getpid(),
                                  threading.get_ident())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        _fsync_quietly(f.fileno())
    if mode is not None:
        os.chmod(tmp, mode)
    replace_with_retry(tmp, path, tries=replace_tries, delay=replace_delay,
                       fallback_direct=fallback_direct)
    _fsync_parent_dir_quietly(path)
    # 双写镜像（P0）统一锚在最底层：任何写者（write_state / update_state_locked /
    # engine 直写 state / append_history / upsert_order）都经过这里，保证不漏
    # （2026-10-20 实测：engine _save_state 的直写路径曾绕过 write_state 挂点，
    # 新增任务未进镜像库）。dualwrite 内部已兜底，镜像失败只告警。
    dualwrite.mirror_written(path, obj)
    # 写后兜底清：进程在"写 tmp → replace"之间崩溃会留下残留 tmp 文件，
    # 下次成功写完顺手清理本路径的过期残留（只删旧文件，不碰它进程正在写的）。
    try:
        sweep_stale_tmp(path)
    except Exception:
        pass  # 清理是 best-effort，绝不能影响写本身


_TMP_SWEEP_MAX_AGE = 3600  # 残留 tmp 超过 1 小时才删：其它进程正在写的 tmp 一定是新鲜的
# atomic_write_json 实际使用过的 tmp_kind 全集（Task 81e）：清理正则只认
# 这些 kind，形如 state.json.bak2024-01 的用户自建文件绝不误删。
_TMP_SWEEP_KINDS = ("tmp", "launcher", "guisave", "monsave", "apisave")


def sweep_stale_tmp(path, max_age_seconds=_TMP_SWEEP_MAX_AGE):
    """清理 path 的残留 tmp 文件（{path}.<kind><pid>-<tid> 形态，kind 取自
    _TMP_SWEEP_KINDS）。

    只删 mtime 超过 max_age_seconds 的——其它进程正在写的 tmp 一定是新鲜的，
    绝不误删。返回删除数量。"""
    d = os.path.dirname(os.path.abspath(path)) or "."
    pat = re.compile(re.escape(os.path.basename(path))
                     + r"\.(?:%s)\d+-\d+$" % "|".join(_TMP_SWEEP_KINDS))
    now = time.time()
    try:
        names = os.listdir(d)
    except OSError:
        return 0
    removed = 0
    for name in names:
        if not pat.match(name):
            continue
        p = os.path.join(d, name)
        try:
            if now - os.path.getmtime(p) < max_age_seconds:
                continue
            os.remove(p)
            removed += 1
        except OSError:
            continue
    return removed


# ----------------------------- state.json 共享 plumbing -----------------------------
# 三个写方（engine 引擎线程 / gui 界面 / launcher append_monitor_task）各自的
# 防护策略（deepcopy 快照、直写兜底、mtime 跟踪、坏档后重建 or 跳过）留在
# 调用方；这里只统一「读（含坏档检测）+ 坏档时间戳挪档 + 原子写」的机械层。

def read_state_or_none(path):
    """读 state.json。返回 (state, error, fingerprint)：
    (dict, None, fp) = 正常；(None, OSError, fp) = 瞬时占用/读失败（文件本身大概率健康，
    调用方不得隔离，应跳过本次加载、稍后重试）；(None, 其他 Exception, fp) = 内容损坏
    （如 JSON 解析失败，或合法 JSON 但不是对象——如数组，调用方可隔离留证）；
    ({}, None, None) = 不存在。
    fingerprint 是本次读取结束瞬间抓取的文件指纹：调用方在持锁内调用本函数，
    该指纹即为"读失败瞬间"指纹，供 quarantine_corrupt 做 TOCTOU 守卫
    （Task 72：杜绝"释放锁后现抓指纹"架空守卫的回归）。"""
    if not os.path.exists(path):
        return {}, None, None
    # 读句柄持有可能与写方的 os.replace 撞车（Windows 对正被打开的目标执行
    # replace 报拒绝访问），短暂退避重试——只影响撞上的那一瞬
    for i in range(5):
        try:
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
        except PermissionError:
            if i == 4:
                return (None,
                        PermissionError("读 %s 被占用（重试后仍失败）" % path),
                        stat_fingerprint(path))
            time.sleep(0.1)
            continue
        except Exception as e:
            return None, e, stat_fingerprint(path)
        if not isinstance(data, dict):
            # 合法 JSON 但不是对象（如数组）：后续 state.items() 会抛
            # AttributeError；按"内容损坏"口径返回 ValueError，让调用方
            # 走隔离留证（Task 40 的错误类型划分：ValueError 系 → 隔离）。
            return None, ValueError(
                "%s 内容不是 JSON 对象（是 %s），视为损坏"
                % (path, type(data).__name__)), stat_fingerprint(path)
        return data, None, stat_fingerprint(path)
    return None, RuntimeError("unreachable"), stat_fingerprint(path)


def stat_fingerprint(path):
    """文件内容指纹（sha256 十六进制），供 quarantine_corrupt 做 TOCTOU 守卫。
    在「读失败」瞬间抓取；文件读不出（不存在/被独占）返回 None。

    必须是内容哈希，不能是 (st_mtime_ns, st_size)：Windows 时钟粒度约 15ms，
    竞态写方在同一刻换成同样大小的不同内容时 stat 指纹不变，守卫失效会把
    健康文件误判成坏档挪走（实测：16 字节坏 JSON 被改写成 17 字节健康 JSON，
    stat 指纹完全相同，守卫形同虚设）。re. 内容哈希与时钟粒度、文件大小无关。
    """
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def quarantine_corrupt(path, expected_fingerprint=None):
    """坏档时间戳挪档（state.json → state.json.bad-YYYYmmdd-HHMMSS-ffffff，
    微秒精度，反复损坏不互相覆盖）。返回挪档后的路径；None = 挪移失败或
    放弃隔离（文件被占用、或自读失败后已被另一进程改写——证据保留原地，
    绝不误伤健康文件）。
    expected_fingerprint: 读失败瞬间抓取的 stat_fingerprint；挪档前比对当前
    指纹，不一致则记 warning 并放弃隔离。None = 不校验（兼容旧调用）。"""
    if expected_fingerprint is not None:
        if stat_fingerprint(path) != expected_fingerprint:
            LOG.warning("[数据] %s 自读失败后已被改写，放弃隔离以免误伤健康文件",
                        path)
            return None
    base = "{0}.bad-{1}".format(
        path, datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    bad = base
    n = 1
    while os.path.exists(bad):   # Windows 时钟粒度 ~15ms:同微秒会重名,计数保唯一
        bad = "%s-%d" % (base, n)
        n += 1
    try:
        os.replace(path, bad)
    except OSError:
        return None
    return bad


def _quarantine_decision(path, fp):
    """quarantine_corrupt 的三分决策：("quarantined", bad) / ("abandoned", None)
    / ("move_failed", None)。

    quarantine_corrupt 本体对"放弃隔离"和"挪移失败"都返回 None；这里用指纹
    二次比对区分：指纹变了 = 自读失败后文件被改写 = 放弃隔离；没变 = 挪移失败。
    误判方向是安全的：把挪移失败误判为放弃，最多多一次重读。"""
    bad = quarantine_corrupt(path, fp)
    if bad is not None:
        return "quarantined", bad
    if stat_fingerprint(path) != fp:
        return "abandoned", None
    return "move_failed", None


def _read_history_list(path):
    """重读一次历史文件；失败/形状非法返回 None（不抛异常、不循环）。

    调用方须处理 None：不得用空列表覆写（可能存在的健康文件）。"""
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        return None
    return data if isinstance(data, list) else None


def _read_orders_db(path):
    """重读一次 orders.json；失败/形状非法返回 None（不抛异常、不循环）。

    调用方须处理 None：不得用空库覆写（可能存在的健康文件）。"""
    try:
        with open(path, encoding="utf-8-sig") as f:
            db = json.load(f)
    except Exception:
        return None
    if isinstance(db, dict) and isinstance(db.get("orders"), dict):
        return db
    return None


def write_state(path, state, *, tmp_kind="tmp", fallback_direct=False):
    """原子写 state.json（见 atomic_write_json，镜像钩子在最底层统一挂）。"""
    atomic_write_json(path, state, tmp_kind=tmp_kind,
                      fallback_direct=fallback_direct)


def append_history(path, record, keep=500):
    """追加一条购票历史(跨 engine/launcher 两个进程的写方),原子写、封顶 keep 条。
    整个读-改-写包在 filelock.file_lock(path + ".lock") 内：sidecar 锁文件，
    不直接打开数据文件加锁（Task 81a：首跑时加锁不再建出空数据文件，避免
    空文件被误判损坏隔离）。同一把锁串行化所有写方
    （Windows 用 msvcrt 字节锁；Linux/macOS 用 fcntl 跨进程锁）。
    锁争用超时（TimeoutError）时本条记录跳过并记 warning，不抛异常。
    文件损坏/形状非法时时间戳挪档留证（quarantine_corrupt）+ LOG.error，
    再追加新记录——不再静默清空整份历史。
    若隔离因"文件自读取后被改写"而放弃，则重读一次用新鲜数据继续，
    绝不用过期空读数覆盖健康文件；重读仍失败则放弃本次追加并记 error。
    若隔离挪移失败（证据保留原地），跳过本次写入，绝不覆写证据。"""
    try:
        with filelock.file_lock(path + ".lock"):
            history = []
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8-sig") as f:
                        history = json.load(f)
                except Exception as e:
                    decision, bad = _quarantine_decision(path,
                                                         stat_fingerprint(path))
                    if decision == "quarantined":
                        LOG.error("[数据] order_history.json 损坏，已隔离留证：%s（%s）；新记录继续追加",
                                  bad, e)
                        history = []
                    elif decision == "abandoned":
                        LOG.warning("[数据] order_history.json 自读取后已被改写，放弃隔离；重读最新内容继续")
                        history = _read_history_list(path)
                        if history is None:
                            LOG.error("[数据] order_history.json 放弃隔离后重读仍失败，"
                                      "放弃本次追加以免覆盖健康文件")
                            return
                    else:
                        LOG.error("[数据] order_history.json 损坏，隔离挪移失败，证据保留原地（%s）；"
                                  "跳过本次追加", e)
                        return
                else:
                    if not isinstance(history, list):
                        decision, bad = _quarantine_decision(path,
                                                             stat_fingerprint(path))
                        if decision == "quarantined":
                            LOG.error("[数据] order_history.json 结构非法，已隔离留证：%s；新记录继续追加",
                                      bad)
                            history = []
                        elif decision == "abandoned":
                            LOG.warning("[数据] order_history.json 自读取后已被改写，放弃隔离；重读最新内容继续")
                            history = _read_history_list(path)
                            if history is None:
                                LOG.error("[数据] order_history.json 放弃隔离后重读仍失败，"
                                          "放弃本次追加以免覆盖健康文件")
                                return
                        else:
                            LOG.error("[数据] order_history.json 结构非法，隔离挪移失败，"
                                      "证据保留原地；跳过本次追加")
                            return
            history.append(record)
            atomic_write_json(path, history[-keep:])
            # 镜像钩子在 atomic_write_json 最底层（dualwrite.mirror_written）
    except TimeoutError as e:
        LOG.warning("[数据] order_history.json 锁争用超时，"
                    "本条历史记录跳过: %s", e)
        return


_ORDERS_LOCK = threading.Lock()


def _load_orders_impl(path):
    """内部实现：返回 (db, write_ok)。

    write_ok=False 表示"证据保留原地"（隔离挪移失败）或"放弃隔离后重读仍
    失败"——调用方（upsert_order）必须跳过本次写入，绝不能用空库覆写。
    纯读调用方请用 load_orders（契约不变：永远返回 dict）。"""
    if not os.path.exists(path):
        return {"orders": {}}, True
    try:
        with open(path, encoding="utf-8-sig") as f:
            db = json.load(f)
    except OSError as e:
        # 瞬时占用（另一进程正在写/杀软扫描）不是坏档：放弃本次读取，
        # 绝不隔离——否则健康订单库会被挪走，防重记录全丢（2026-10-19 审查）。
        LOG.warning("[数据] orders.json 被占用，本次跳过读取：%s", e)
        return {"orders": {}}, False
    except Exception as e:
        decision, bad = _quarantine_decision(path, stat_fingerprint(path))
        if decision == "quarantined":
            LOG.error("[数据] orders.json 损坏，已隔离留证：%s（%s）；返回空库", bad, e)
            return {"orders": {}}, True
        if decision == "abandoned":
            fresh = _read_orders_db(path)
            if fresh is None:
                LOG.error("[数据] orders.json 放弃隔离后重读仍失败，返回空库；"
                          "写方不得覆写")
                return {"orders": {}}, False
            LOG.warning("[数据] orders.json 自读取后已被改写，放弃隔离；重读最新内容继续")
            return fresh, True
        LOG.error("[数据] orders.json 损坏，隔离挪移失败，证据保留原地（%s）；返回空库", e)
        return {"orders": {}}, False
    if isinstance(db, dict) and isinstance(db.get("orders"), dict):
        return db, True
    decision, bad = _quarantine_decision(path, stat_fingerprint(path))
    if decision == "quarantined":
        LOG.error("[数据] orders.json 结构非法，已隔离留证：%s；返回空库", bad)
        return {"orders": {}}, True
    if decision == "abandoned":
        fresh = _read_orders_db(path)
        if fresh is None:
            LOG.error("[数据] orders.json 放弃隔离后重读仍失败，返回空库；"
                      "写方不得覆写")
            return {"orders": {}}, False
        LOG.warning("[数据] orders.json 自读取后已被改写，放弃隔离；重读最新内容继续")
        return fresh, True
    LOG.error("[数据] orders.json 结构非法，隔离挪移失败，证据保留原地；返回空库")
    return {"orders": {}}, False


def load_orders(path):
    """orders.json → {"orders": {key: rec}}；缺失返回空库；损坏/形状非法则
    时间戳挪档留证（quarantine_corrupt）+ LOG.error，再返回空库——不再静默。
    若隔离因"文件自读取后被改写"而放弃，则重读一次用新鲜数据返回，
    绝不用过期空库覆盖健康文件（upsert_order 随后会落盘）。"""
    db, _ = _load_orders_impl(path)
    return db


def upsert_order(path, key, rec):
    """登记/更新一条订单记录(进程内锁 + 跨进程文件锁 + 原子写)。

    跨进程锁与 append_history 同口径（sidecar path + ".lock" 文件）：
    launcher 内嵌引擎线程与独立运行的 monitor.py 引擎是两个进程、
    写同一 orders.json，读-改-写必须串行化，否则丢对方订单记录。
    锁顺序恒为 _ORDERS_LOCK → file_lock（_ORDERS_LOCK 仅本函数持有，
    无逆序路径，不存在锁顺序反转）。
    证据保留原地 / 重读失败时跳过本次写入；锁争用超时跳过并记 warning。"""
    try:
        with _ORDERS_LOCK:
            with filelock.file_lock(path + ".lock"):
                db, write_ok = _load_orders_impl(path)
                if not write_ok:
                    LOG.error("[数据] orders.json 证据保留原地或重读失败，"
                              "跳过本次订单登记以免覆盖")
                    return
                rec = dict(rec)
                rec.setdefault("first_seen", rec.get("last_check") or
                               time.strftime("%Y-%m-%d %H:%M:%S"))
                rec["last_check"] = time.strftime("%Y-%m-%d %H:%M:%S")
                db["orders"][key] = rec
                atomic_write_json(path, db)
                # 镜像钩子在 atomic_write_json 最底层（dualwrite.mirror_written）
    except TimeoutError as e:
        LOG.warning("[数据] orders.json 锁争用超时，本条订单记录跳过: %s", e)
        return
# ----------------------------- config.json 共享 plumbing -----------------------------
# api 后端 / gui 界面 / monitor CLI 曾各持一份 config.json 读写与邮件校验。共同
# 机械层收到这里；各自的策略差异（gui 弹窗、monitor 的 CLI 提示与坏档指纹拒写、
# api 的 stderr 出口）仍由调用方用钩子参数注入——参数化保留，不是抹平
# （见 RULES.md 第七节）。

# 邮箱校验的单源：取 gui 的严格口径（旧 api 版宽松，会让 gui 拒收的地址从
# Electron 设置页落进 config.json——两个前端对同一 config 不能两套校验）。
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

# 乐观并发检查/坏档守卫的哨兵：指纹取此值表示「不检查」。None 是合法指纹，
# 表示「调用方取指纹时文件不存在」。
_STAMP_UNSET = object()


def data_path(name: str) -> str:
    """数据文件名 → 绝对路径（单一解析口径）。

    解析顺序：
      1. 绝对路径直通（测试 / 外部注入用）；
      2. data/<name> 存在 → 用新布局；
      3. 根目录 <name> 存在 → 兼容旧布局（迁移前 / 未跑迁移脚本的旧副本）；
      4. 都不存在 → 返回 data/<name>，首次运行自动落到 data/ 下。

    第 3 步是兼容回退，等所有环境都完成迁移后可删除（见 tools/migrate_data_dir.py）。
    """
    if os.path.isabs(name):
        return name
    candidate = os.path.join(DATA_DIR, name)
    if os.path.exists(candidate):
        return candidate
    legacy = os.path.join(HERE, name)
    if os.path.exists(legacy):
        return legacy
    return candidate


def resolve_data_file(base_dir: str, name: str) -> str:
    """数据文件名 + 基准目录 → 绝对路径（「项目根走 data/、临时目录严格本地」）。

    生产代码传 HERE（项目根）→ 交给 data_path（data/ 优先、旧布局回退）；
    测试把 base_dir 换成临时目录时严格落在临时目录，不能回退项目根目录——
    否则测试会读写真实数据（tests/test_all.py 有保险名单守着）。
    绝对路径原样透传。
    """
    if os.path.isabs(name):
        return name
    if os.path.abspath(base_dir) == HERE:
        return data_path(name)
    return os.path.join(base_dir, name)


def cookie_path(name=None) -> str:
    """会话 Cookie 文件名/路径 → 绝对路径（order 与 capture_session 的单源实现）。

    这两个模块曾各写一份几乎相同的解析（约 20 行 × 2），且 order.py 顶层
    `from capture_session import ...` 导致二者不能互相导入，只能下沉到这里。

    规则：绝对路径原样透传（Windows 上 join 会给 '/tmp/x' 强加盘符，用户配的
    绝对路径会悄悄变味）；相对名落 data/（data_path：新布局优先、旧根目录回退）；
    None / 非字符串回退默认文件名。
    """
    name = name if isinstance(name, str) else None
    name = name or "session_cookies.json"
    if os.path.isabs(name):
        return name
    return os.path.normpath(data_path(name))


def state_path_of(config, base_dir):
    """config 的 state_file 键 → 绝对路径（缺省 state.json）。"""
    return resolve_data_file(base_dir, config.get("state_file", "state.json"))


def history_path_of(config, base_dir):
    """config 的 history_file 键 → 绝对路径（缺省 order_history.json）。"""
    return resolve_data_file(base_dir, config.get("history_file", "order_history.json"))


def read_config_or_empty(path, *, missing=None, broken=None, non_dict=None):
    """读 config.json（容忍 UTF-8 BOM）。文件缺失/损坏/顶层非 dict 一律返回 {}。

    missing(exc) / broken(exc) / non_dict(data) 是各前端的提示钩子（省略即静默）：
    提示时机与文案属于各前端的 UI 语义，不在这里统一。坏档证据保护由调用方
    在钩子里记指纹，再用 write_config_locked / update_config_locked 的守卫拒写。
    """
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError as e:
        if missing is not None:
            missing(e)
        return {}
    except (OSError, ValueError) as e:
        # JSONDecodeError / UnicodeDecodeError 都是 ValueError 子类
        if broken is not None:
            broken(e)
        return {}
    if not isinstance(data, dict):
        if non_dict is not None:
            non_dict(data)
        return {}
    return data


def _config_refusal(path, expect_fingerprint, damaged_fingerprint):
    """锁内守卫：返回拒绝原因（"damaged"/"stale"）或 None（可写）。"""
    if damaged_fingerprint is not None \
            and stat_fingerprint(path) == damaged_fingerprint:
        # 读侧发现的坏档仍在原地（指纹未变）：绝不覆写证据
        return "damaged"
    if expect_fingerprint is not _STAMP_UNSET \
            and stat_fingerprint(path) != expect_fingerprint:
        # 交互期间有外部写入（抢票进程/另一个窗口）：放弃本次保存，不静默覆写
        return "stale"
    return None


def write_config_locked(path, config, *, expect_fingerprint=_STAMP_UNSET,
                        damaged_fingerprint=None, mode=0o600):
    """锁内整块写 config.json。已写入返回 None，否则返回拒绝原因
    （"damaged"/"stale"）供调用方打印自己的提示。锁争用 TimeoutError 原样上抛，
    提示口径（弹窗/print）归调用方。"""
    with filelock.file_lock(path + ".lock"):
        reason = _config_refusal(path, expect_fingerprint, damaged_fingerprint)
        if reason is not None:
            return reason
        atomic_write_json(path, config, mode=mode)
        return None


def update_config_locked(path, mutator, *, expect_fingerprint=_STAMP_UNSET,
                         damaged_fingerprint=None, on_corrupt=None,
                         on_non_dict=None, mode=0o600, return_result=False):
    """锁内读-改-写 config.json。

    on_corrupt(exc) / on_non_dict(data) 是损坏与顶层非 dict 的处置钩子：
    - 抛异常 → 原样上抛（钩子自己负责提示）
    - 返回 False → 视为「已处置」，放弃本次写入并返回 False（monitor 口径）
    - 省略 / 返回其它 → 抛 RuntimeError（文案与旧 api/gui 逐字一致）

    return_result=True 时返回 mutator 的返回值（gui 口径），否则返回是否写入的
    布尔。锁争用 TimeoutError 原样上抛。
    """
    with filelock.file_lock(path + ".lock"):
        if _config_refusal(path, expect_fingerprint,
                           damaged_fingerprint) is not None:
            return False
        try:
            with open(path, encoding="utf-8-sig") as f:
                config = json.load(f)
        except FileNotFoundError:
            config = {}   # 首跑：还没有配置文件，从空配置开始写是正常的
        except (OSError, ValueError) as e:
            if on_corrupt is not None and on_corrupt(e) is False:
                return False
            raise RuntimeError(
                "config.json 已损坏：本次保存已取消，旧文件未被覆盖")
        if not isinstance(config, dict):
            if on_non_dict is not None and on_non_dict(config) is False:
                return False
            raise RuntimeError(
                "config.json 内容非 dict：本次保存已取消，旧文件未被覆盖")
        result = mutator(config)
        atomic_write_json(path, config, mode=mode)
        return result if return_result else True


def drop_dedup_state(state):
    """丢弃旧 state 的防重数据（功能已下线，数据不再参与任何判断）。

    旧版有两种落盘形态：早期把「区间|日期|车次|席别」平铺在顶层，后期收进
    dedup 节。两种都在这里一次清掉，避免死数据被反复写回、越积越多。
    engine 的读路径与 grabber 的写路径共用本函数（单点口径）。
    """
    if not isinstance(state, dict):
        return state
    state.pop("dedup", None)
    for k in [k for k in state if isinstance(k, str) and "|" in k]:
        del state[k]
    return state


def remove_task_from_config(config, task):
    """从配置删除一个任务：有 uid 按 uid 删；无 uid 只删第一个同名。

    单点口径（api / legacy/gui 共用）：编辑任务 = 先建后删，任务名不含
    日期，改日期后新旧同名——按名字删全部会把刚建的新任务一起删掉。
    """
    uid = task.get("uid")
    name = task.get("name")
    tasks = list(config.get("tasks") or [])
    if uid and any(t.get("uid") == uid for t in tasks):
        config["tasks"] = [t for t in tasks if t.get("uid") != uid]
        return config
    for idx, t in enumerate(tasks):
        if t.get("name") == name:
            tasks.pop(idx)
            break
    config["tasks"] = tasks
    return config


def update_state_locked(path, mutator, *, read_state, tmp_kind="tmp",
                        on_non_dict=None):
    """锁内读-改-写 state.json。

    read_state(path) 由调用方注入（engine.load_state_file）——读实现保持单源，
    同时保住各模块既有的 patch 契约（测试打的是 gui.engine_mod.load_state_file）。
    on_non_dict(data)：顶层非 dict 的提示钩子（省略即静默按空状态处理）。
    返回 mutator 的返回值；锁争用 TimeoutError 原样上抛。
    """
    with filelock.file_lock(path + ".lock"):
        state = read_state(path)
        if not isinstance(state, dict) or state == {}:
            # 注入的读实现（engine.load_state_file）会把读失败吞成 {}，与
            # "文件本来就是空对象" 无法区分。文件存在时复核一次原始 JSON：
            # 解析失败就拒写，避免 mutator 作用在空状态上整份覆写，把其它
            # 任务的 status/retry 清空（2026-10-19 审查）。
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8-sig") as f:
                        json.load(f)
                except Exception as e:
                    raise RuntimeError(
                        "state.json 读取失败，本次写入已取消：%s" % e)
        if not isinstance(state, dict):
            if on_non_dict is not None:
                on_non_dict(state)
            state = {}
        result = mutator(state)
        write_state(path, state, tmp_kind=tmp_kind)
        return result


def normalize_email_settings(email):
    """校验并清洗邮件配置（api/gui 两份逐字相同的原版逻辑）：
    - 发件邮箱必须是完整地址，否则抛 ValueError
    - 发件人地址为空或格式不对（如只填 @qq.com）时自动回退为发件邮箱
    - 收件人逐个校验格式，非法时抛 ValueError
    返回清洗后的配置字典。"""
    user = (email.get("username") or "").strip()
    if not EMAIL_RE.match(user):
        raise ValueError("发件邮箱格式不正确：「%s」" % user)
    from_addr = (email.get("from") or "").strip()
    if not EMAIL_RE.match(from_addr):
        from_addr = user
    email["username"] = user
    email["from"] = from_addr
    raw_to = email.get("to") or []
    if isinstance(raw_to, str):
        # 单字符串曾被逐字符迭代，报出「a、@、b…」这种误导性错误
        raw_to = re.split(r"[,，;；\s]+", raw_to)
    to_list = [str(x).strip() for x in raw_to if str(x).strip()]
    bad = [x for x in to_list if not EMAIL_RE.match(x)]
    if bad:
        raise ValueError("收件人格式不正确：%s" % "、".join(bad))
    if not to_list:
        raise ValueError("请至少填写一个收件人邮箱")
    email["to"] = to_list
    return email


def read_history_records(path, *, limit=200, on_non_list=None, on_error=None):
    """读购票历史：最近 limit 条、最新在前。文件缺失/损坏返回空列表；
    顶层非 list 时走 on_non_list 钩子后按空历史展示（旧代码切片在 try 外，
    非 list 经 eager 构造的 HistoryPanel 崩 GUI 启动）。"""
    try:
        with open(path, encoding="utf-8-sig") as f:
            history = json.load(f)
        if not isinstance(history, list):
            if on_non_list is not None:
                on_non_list(history)
            return []
        return list(reversed(history[-limit:]))
    except Exception as e:
        if on_error is not None:
            on_error(e)
        return []

