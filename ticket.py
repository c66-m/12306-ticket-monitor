# -*- coding: utf-8 -*-
"""
12306 余票查询与解析模块（免登录可用）

职责
    1. 维护车站代码表（station_name.js => 名称<->代码映射）
    2. 调用余票查询接口（免登录，仅需先 GET leftTicket/init 拿 JSESSIONID）
    3. 解析 queryG 返回的竖线分隔字段
    4. 按 p20~p33 逐席别余票字段判定是否有票（"有"/数字=可购；无/空/*=无票；"候补"视为无票）

关键字段（0 基索引，与官方 queryLeftTicket 脚本映射一致）：
    p0  secretStr（下单用，不可复用，每次查询重新获取）
    p2  train_no
    p3  train_code（车次，如 G547）
    p4  from_telecode（发站代码）  p5  to_telecode（到站代码）
    p6  起站代码                 p7  终点代码
    p8  出发时间   p9  到达时间   p10 历时
    p11 can_buy   p13 发车日期
    p15 train_location（下单用，如 'P3'）
    p20~p32 各席别余票，其中 21/23/28 是卧铺共享字段，名字按本行 p35(seat_types)
            席别码决定（A/F/I/J…）；p27(yb_num)、p33(srrb_num) 是死字段，不参与

用法
    python ticket.py <从站名> <到站名> <日期 YYYY-MM-DD> [车次[,车次]]
    例如：python ticket.py 北京 上海 2026-10-15 G547
"""

import sys
import os
import re
import json
import time
import threading
import logging

import requests

LOG = logging.getLogger("monitor")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://kyfw.12306.cn/otn/leftTicket/init",
    "Origin": "https://kyfw.12306.cn",
    "X-Requested-With": "XMLHttpRequest",
}

STATION_JS_URL = "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"

# 界面勾选列表的唯一定义处：gui / launcher / monitor 统一 import 这一份，
# 顺序即界面展示顺序。「动卧」= F，余票取共享字段 rw_num(p23)，按本行席别码串
# p35(seat_types) 含 F 命名——靠 p27/p33 是拿不到的（见 parse_row）。
SEAT_CHOICES = ["商务座", "特等座", "优选一等座", "一等座", "二等座",
                "高级软卧", "动卧", "软卧", "硬卧", "软座", "硬座", "无座"]

# 码表口径 = 官方 seatTypeCodeForName（%TEMP%/q12306/queryLeftTicket_end_js.js）：
#   9 商务座 / P 特等座 / D 优选一等座 / M 一等座 / O 二等座 / 6 高级软卧 /
#   4 软卧 / F 动卧 / 3 硬卧 / 2 软座 / 1 硬座 / WZ 无座
# 「优选一等座」此前只在勾选列表里、不在码表里 → 勾了会被当「不支持自动下单」跳过，
# 现已补上（官方 seatTypeForHB 里 GG:"D_优选一等座"，两个来源互证）。
# ⚠ 未验证假设（UNVERIFIED）：高级动卧(A)/一等卧(I)/二等卧(J)/其他(H)不在下单码表里，
# 抢票路径按"下单接口不接受这些码"剔除——但该断言未经真实 12306 下单接口验证。
# 若实际可售，这些席别将永远无法被监控/抢购。改动此处前请先真机验证。
SEAT_CODE_TO_NAME = {
    "9": "商务座", "P": "特等座", "D": "优选一等座", "M": "一等座", "O": "二等座",
    "6": "高级软卧", "4": "软卧", "F": "动卧", "3": "硬卧",
    "2": "软座", "1": "硬座", "WZ": "无座",
}
SEAT_NAME_TO_CODE = {v: k for k, v in SEAT_CODE_TO_NAME.items()}

# 12306 网页端确认页的可售席别由服务端下发，普速车不下发「无座」（实测 K925/K225/K1969
# 长葛→确山各日期都只给 硬座/硬卧/软卧）。无座是站票，票价跟同车型的最低价席别一样，
# 所以按「同价改判」下单，监控侧仍按用户勾选的席别判断有无票：
#   G/D/C 动车组：无座票价 = 二等座 → 改判 二等座(O)
#   Z/T/K/纯数字：无座票价 = 硬座   → 改判 硬座(1)
ORDER_SEAT_ALIAS = {"WZ": "1"}
EMU_SEAT_ALIAS = {"WZ": "O"}


def is_emu_code(train_code):
    """车次号首字母判动车组（与 launcher.is_emu 同口径）。"""
    return str(train_code or "")[:1].upper() in ("G", "D", "C")


def order_seat_code(seat_name, seat_code=None, train_code=None):
    """下单实际要用的席位代码 + 别名目标名。

    返回 (code, alias_name)：命中同价改判时 code 为别名代码、alias_name 为其中文名；
    没命中时 alias_name 为 None、code 原样返回。train_code 决定「无座」的同价席别。
    """
    code = seat_code or SEAT_NAME_TO_CODE.get(seat_name)
    alias = ORDER_SEAT_ALIAS.get(code)
    if alias and is_emu_code(train_code):
        alias = EMU_SEAT_ALIAS.get(code, alias)
    if alias:
        return alias, SEAT_CODE_TO_NAME.get(alias)
    return code, None


# ---- 「首选席别」输入框的解析（启动器抢票 / 监控任务共用）----
# 用户手输的名字、官方缩写、口语都归一化到 SEAT_CHOICES 里的名字。
SEAT_INPUT_ALIASES = {
    "站票": "无座", "站座": "无座", "坐票": "硬座",
    "商务": "商务座", "特等": "特等座", "优选一等": "优选一等座",
    "一等": "一等座", "二等": "二等座", "高软": "高级软卧",
}
# 「无座」在网页端不卖、同价改判硬座下单（ORDER_SEAT_ALIAS），所以填「无座」时
# 硬座有票也算命中：候选顺序 无座 → 硬座。
SEAT_INPUT_EXPAND = {"无座": ("无座", "硬座")}


def split_seat_input(text):
    """把「首选席别」输入拆成原始词条（逗号/顿号/空格/斜杠分隔）。"""
    return [p for p in re.split(r"[,，、/;；\s]+", (text or "").strip()) if p]


def normalize_seat_name(text):
    """把用户输入的席别名归一化到 SEAT_CHOICES 里的名字；识别不了返回 None。"""
    t = (text or "").strip()
    if not t:
        return None
    if t in SEAT_NAME_TO_CODE:
        return t
    if t in SEAT_INPUT_ALIASES:
        return SEAT_INPUT_ALIASES[t]
    key = t.replace("票", "")
    if key in SEAT_NAME_TO_CODE:
        return key
    if key in SEAT_INPUT_ALIASES:
        return SEAT_INPUT_ALIASES[key]
    return SEAT_CODE_TO_NAME.get(t.upper())


def seat_priority_list(value):
    """首选席别 → 归一化的名字列表（接受字符串「硬座,无座」或已是列表）。

    识别不了的词条按「硬座」处理（规则：不存在的席别等于选硬座）。
    """
    toks = ([str(x) for x in value] if isinstance(value, (list, tuple))
            else split_seat_input(value))
    out = []
    for t in toks:
        name = normalize_seat_name(t) or "硬座"
        if name not in out:
            out.append(name)
    return out


def seat_priority_expanded(value):
    """首选席别展开后的候选名（「无座」→ 无座/硬座 同价展开）。"""
    out = []
    for name in seat_priority_list(value):
        for x in SEAT_INPUT_EXPAND.get(name, (name,)):
            if x not in out:
                out.append(x)
    return out


def seat_pick_order(checked, priority, avail=None):
    """抢票选席顺序：首选席别在前，其后是勾选项（按 SEAT_CHOICES 顺序）。

    avail 非空时只保留其中有余票的席别 —— 首选席别都没票才轮到勾选项。
    """
    cand = list(seat_priority_expanded(priority))
    order = {v: i for i, v in enumerate(SEAT_CHOICES)}
    for s in sorted(checked or [], key=lambda x: order.get(x, 99)):
        if s not in cand:
            cand.append(s)
    if avail is not None:
        cand = [s for s in cand if s in avail]
    return cand


def seat_rules_parse(value):
    """「首选席别」值 → {"rules": {车次: [席别名...]}, "bare": [...], "warnings": [...]}。

    条目用逗号/顿号/分号分隔，条目内席别用斜杠/空白分隔：
      - 「K225=硬座/无座」= K225 的**专属规则**（抢票时 ∩ 勾选集，见
        seat_candidates_for）；车次大小写不敏感，同车次重复以后者为准。
      - 不带 = 的条目 = 全局排序偏好（其余车次用），完全兼容旧格式
        （纯席别串，甚至旧任务里存的列表）。
    无法识别的席别词按「硬座」处理并记入 warnings；「无座」做同价展开。
    """
    rules, bare, warnings = {}, [], []
    if isinstance(value, (list, tuple)):
        for name in seat_priority_list(list(value)):
            for x in SEAT_INPUT_EXPAND.get(name, (name,)):
                if x not in bare:
                    bare.append(x)
        return {"rules": rules, "bare": bare, "warnings": warnings}

    for entry in re.split(r"[,，、;；]+", (str(value) or "").strip()):
        entry = entry.strip()
        if not entry:
            continue
        if "=" in entry:
            train_p, _, seats_p = entry.partition("=")
            train = train_p.strip().upper()
            toks = [t for t in re.split(r"[/\s]+", seats_p.strip()) if t]
            if not train or not toks:
                warnings.append("「%s」格式不完整（应为 车次=席别），已忽略" % entry)
                continue
            names = []
            for t in toks:
                nm = normalize_seat_name(t)
                if nm is None:
                    warnings.append("「%s」无法识别，按「硬座」处理" % t)
                    nm = "硬座"
                for x in SEAT_INPUT_EXPAND.get(nm, (nm,)):
                    if x not in names:
                        names.append(x)
            if train in rules:
                warnings.append("车次 %s 的专属规则重复，以最后一次为准" % train)
            rules[train] = names
        else:
            for t in [x for x in re.split(r"[/\s]+", entry) if x]:
                nm = normalize_seat_name(t)
                if nm is None:
                    warnings.append("「%s」无法识别，按「硬座」处理" % t)
                    nm = "硬座"
                for x in SEAT_INPUT_EXPAND.get(nm, (nm,)):
                    if x not in bare:
                        bare.append(x)
    return {"rules": rules, "bare": bare, "warnings": warnings}


_WARNED_UNORDERABLE_SKIPS = set()  # (车次, 席别名)：已提示过"有票但按不可下单跳过"


def _warn_unorderable_skipped(train_code, skipped):
    """不限席别任务遇到有票但不可下单的席别（A/I/J/H）：明确告知跳过原因。

    高级动卧(A)/一等卧(I)/二等卧(J)"不可下单"是未经真实接口验证的假设
    （见 SEAT_CODE_TO_NAME 处 UNVERIFIED 注释）；用户应知晓这些席别即使有票
    也不会被抢。同（车次，席别）进程内只提示一次，防每轮刷屏。
    """
    for name in skipped:
        key = (train_code or "", name)
        if key in _WARNED_UNORDERABLE_SKIPS:
            continue
        _WARNED_UNORDERABLE_SKIPS.add(key)
        LOG.warning("[席别] %s 的「%s」有余票，但按不可下单席别跳过"
                    "（高级动卧/一等卧/二等卧/其他不在下单码表内；"
                    "该假设未经真实下单接口验证）",
                    train_code or "?", name)


def seat_candidates_for(train_code, checked, priority, avail=None):
    """某趟车的席别候选 —— 唯一口径，launcher 抢票与 engine 监控共用。

    priority 支持「车次=席别/席别」专属规则（seat_rules_parse）：
      - 有专属规则的车：规则列表 ∩ 勾选集（保规则顺序）。勾选集为空视为
        不限制。交集为空 = 该车不抢（返回 []，调用方应跳过——用户明确
        指定了，不能回退到全局）。
      - 无专属规则的车：全局排序偏好在前（可超出勾选，与既有口径一致），
        其后是勾选项（按 SEAT_CHOICES 顺序）；两者皆空 = 不限席别。
    avail 非空时再与实际有票求交（不变量：候选永远脱离不了该车 avail）。
    """
    checked = [s for s in (checked or []) if s]
    parsed = priority if isinstance(priority, dict) else seat_rules_parse(priority)
    rules = parsed.get("rules") or {}
    train = (train_code or "").strip().upper()
    if train and train in rules:
        cand = ([s for s in rules[train] if s in set(checked)] if checked
                else list(rules[train]))
    else:
        cand = list(parsed.get("bare") or [])
        order = {v: i for i, v in enumerate(SEAT_CHOICES)}
        for s in sorted(checked, key=lambda x: order.get(x, 99)):
            if s not in cand:
                cand.append(s)
        if not cand and avail is not None:
            # 不限席别：过滤不可下单的展示类席别名（Task 47），并按 SEAT_SHOW_ORDER
            # 稳定排序（贵的在前，无座垫底），不跟随 avail 插入序——避免展示名
            # 抢先/误报「有票」（Task 54）。
            show_order = {n: i for i, n in enumerate(SEAT_SHOW_ORDER)}
            skipped = sorted({s for s in avail if s not in SEAT_NAME_TO_CODE})
            _warn_unorderable_skipped(train, skipped)  # Task 76b：非静默，说明跳过原因
            cand = sorted((s for s in avail if s in SEAT_NAME_TO_CODE),
                          key=lambda x: show_order.get(x, 99))
    if avail is not None:
        cand = [s for s in cand if s in avail]
    return cand


def seat_priority_feedback(text, checked=None, trains=None):
    """「首选席别」输入框的即时解析反馈：返回 (提示文字, 是否有需注意的问题)。

    含「车次=席别」专属规则时按车次逐行展示解析结果（含勾选交集与
    车次归属校验）；纯全局偏好保持旧提示。"""
    parsed = seat_rules_parse(text)
    parts = list(parsed["warnings"])
    has_warn = bool(parsed["warnings"])
    rules, bare = parsed["rules"], parsed["bare"]

    if not rules:
        if not bare:
            return "不填 = 只按下面勾选的席别顺序", False
        if "无座" in bare:
            parts.append("无座按硬座出票")
        parts.append("优先席别：" + " → ".join(bare))
        return "；".join(parts), has_warn

    if bare:
        parts.append("其余车次优先：" + " → ".join(bare))
    train_set = ({t.strip().upper() for t in trains if t and t.strip()}
                 if trains else None)
    checked_set = ({s for s in checked if s} if checked else None)
    for train, seats in rules.items():
        tag = ""
        if train_set is not None and train not in train_set:
            tag = "（不在车次列表，不生效）"
            has_warn = True
        if checked_set is not None:
            eff = [s for s in seats if s in checked_set]
            if not eff:
                parts.append("%s：%s ⚠ 与勾选席别无交集，该车不抢%s"
                             % (train, " → ".join(seats), tag))
                has_warn = True
                continue
            shown = " → ".join(eff)
        else:
            shown = " → ".join(seats)
        parts.append("%s：%s%s" % (train, shown, tag))
    return "；".join(parts), has_warn


_SESSION = None
_SESSION_LOCK = threading.Lock()  # GUI 多线程会同时首次建会话，防重复初始化

# ---- 查询会话健康跟踪（Task 76a）----
# _SESSION 进程内永久复用；JSESSIONID 失效后查询持续失败，engine 会按"网络瞬时
# 异常"无限退避重试同一死会话（7×24 长跑下全部任务永久漏单，只能重启恢复）。
# 对策：连续失败达阈值即判定会话已死，懒重建（置 None，下次 get_session 新建，
# 拿新 TCP 连接/JSESSIONID），成功一次即清零。
_SESSION_MAX_CONSECUTIVE_FAILURES = 5
_session_fail_streak = 0


def get_session():
    """构建并复用基础请求会话（先拿 JSESSIONID）。"""
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                s = requests.Session()
                s.headers.update(BASE_HEADERS)
                s.get("https://kyfw.12306.cn/otn/leftTicket/init", timeout=15)
                _SESSION = s
    return _SESSION


def _note_query_success():
    """一次查询成功：会话健康计数清零。"""
    global _session_fail_streak
    with _SESSION_LOCK:
        _session_fail_streak = 0


def _note_query_failure():
    """一次查询失败（全部端点均失败）：计次；连续达阈值则懒重建会话。

    失败可能是会话已死（JSESSIONID 过期），也可能是 12306 侧全站故障；
    后者时重建只是多一次 init 请求（无害），恢复后新会话即用。
    懒重建不主动 close 旧会话：在途的其它线程可能仍持有引用，交由 GC 回收
    （与既有"会话永不 close"行为一致）。
    """
    global _session_fail_streak, _SESSION
    with _SESSION_LOCK:
        _session_fail_streak += 1
        if _session_fail_streak < _SESSION_MAX_CONSECUTIVE_FAILURES:
            return
        _session_fail_streak = 0
        if _SESSION is not None:
            _SESSION = None
            LOG.warning("[余票] 查询连续 %d 次失败，会话可能已失效，"
                        "已丢弃旧会话，下次查询将重建（新 JSESSIONID）",
                        _SESSION_MAX_CONSECUTIVE_FAILURES)


_STATION_CACHE_TTL = 7 * 24 * 3600  # 车站缓存有效期：7 天
_STATION_DL_LOCK = threading.Lock()  # 下载串行化：多线程冷启动只下载一次


def _abs_cache_path(cache_path):
    import os
    if os.path.isabs(cache_path):
        return cache_path
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), cache_path)


def _sibling_cache_path(path, name):
    import os
    d = os.path.dirname(path)
    return os.path.join(d, name) if d else _abs_cache_path(name)


def _read_json_cache(path):
    import os
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass  # 损坏：走重新下载
    return None


def _cache_fresh(path):
    import os
    try:
        return (time.time() - os.path.getmtime(path)) < _STATION_CACHE_TTL
    except OSError:
        return False


def _download_station_js_text():
    """下载 station_name.js 原文（单次下载入口；调用方负责串行化）。"""
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://kyfw.12306.cn/otn/leftTicket/init"})
    r = s.get(STATION_JS_URL, timeout=20)
    r.raise_for_status()
    return r.text


def _parse_station_js(text):
    """一次解析 → (name2code, code2name, stations)。

    两个正则与旧 load_station_map / load_station_index 逐字一致，
    只是跑在同一份下载文本上：两视图不可能分叉。
    """
    name2code, code2name = {}, {}
    # 格式：@简拼|名称|代码|拼音|...
    for m in re.finditer(r"@[a-zA-Z]+\|([^|]+)\|([A-Z]{3})\|", text):
        name, code = m.group(1), m.group(2)
        name2code[name] = code
        code2name[code] = name
    stations = []
    # 格式：@bjb|北京北|VAP|beijingbei|bjb|0
    for m in re.finditer(r"@([a-z]+)\|([^|]+)\|([A-Z]{3})\|([a-z]+)\|", text):
        stations.append({"name": m.group(2), "code": m.group(3),
                         "py": m.group(4), "spy": m.group(1)})
    if not name2code:
        raise RuntimeError("车站代码表解析失败，请检查 station_name.js 格式是否变更")
    return name2code, code2name, stations


def _atomic_write_json(path, obj, **dump_kw):
    """tmp+replace 原子写：中途被 kill 只丢 tmp，不截断目标文件。"""
    tmp = "{0}.tmp{1}".format(path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, **dump_kw)
        os.replace(tmp, path)
    except BaseException:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def _write_station_caches(map_path, index_path, name2code, code2name, stations):
    _atomic_write_json(map_path,
                       {"name2code": name2code, "code2name": code2name},
                       indent=2)
    _atomic_write_json(index_path, {"stations": stations})


def _refresh_station_cache(map_path, index_path, force=False):
    """单次下载 + 单次解析 → 写两个缓存文件。

    返回 (name2code, code2name, stations)。force=False 时锁内二次检查：
    等锁期间并发线程可能已刷新，避免重复下载；force=True 用于手动刷新
    与缺站刷新（缓存"新鲜"但缺新站时也必须下载）。
    """
    with _STATION_DL_LOCK:
        if not force:
            m = _read_json_cache(map_path)
            i = _read_json_cache(index_path)
            if (m and m.get("name2code") and m.get("code2name")
                    and i and i.get("stations")
                    and _cache_fresh(map_path) and _cache_fresh(index_path)):
                return m["name2code"], m["code2name"], i["stations"]
        text = _download_station_js_text()
        name2code, code2name, stations = _parse_station_js(text)
        _write_station_caches(map_path, index_path, name2code, code2name, stations)
        return name2code, code2name, stations


def _refresh_quiet(map_path, index_path, force=False):
    try:
        _refresh_station_cache(map_path, index_path, force=force)
    except Exception:
        pass  # 后台刷新 best-effort：失败下次调用再试，不打扰主流程


def _refresh_station_cache_async(map_path, index_path, force=False):
    t = threading.Thread(target=_refresh_quiet,
                         args=(map_path, index_path, force), daemon=True)
    t.start()
    return t


def refresh_station_cache(map_path="station_name.json", index_path="station_index.json"):
    """手动刷新入口：立即重新下载车站数据并写缓存。

    返回 (name2code, code2name, stations)。
    """
    return _refresh_station_cache(_abs_cache_path(map_path),
                                  _abs_cache_path(index_path), force=True)


def note_station_missing(name=None):
    """查不到车站时调用：后台强制刷新车站缓存（best-effort），新开车站下次可查到。"""
    _refresh_station_cache_async(_abs_cache_path("station_name.json"),
                                 _abs_cache_path("station_index.json"),
                                 force=True)


def load_station_map(cache_path="station_name.json"):
    """车站代码表 ({名称: 代码}, {代码: 名称})。

    缓存 7 天 TTL：过期时立即返回旧数据并后台刷新（不阻塞调用方）；
    无可用缓存时同步下载（冷启动必须拿到数据，下载失败抛异常，
    调用方按旧语义降级/收尾）。与 load_station_index 共享单次下载：
    两视图来自同一份文本，永不分叉。
    """
    here = _abs_cache_path(cache_path)
    data = _read_json_cache(here)
    if data and data.get("name2code") and data.get("code2name"):
        if not _cache_fresh(here):
            _refresh_station_cache_async(
                here, _sibling_cache_path(here, "station_index.json"))
        return data["name2code"], data["code2name"]
    name2code, code2name, _stations = _refresh_station_cache(
        here, _sibling_cache_path(here, "station_index.json"))
    return name2code, code2name


def load_station_index(cache_path="station_index.json"):
    """车站全量索引 [{"name","code","py","spy"}, ...]（约 3300 站）。

    py=全拼（beijingbei） spy=简拼（bjb），供启动器本地模糊搜索使用。
    缓存语义同 load_station_map：7 天 TTL，过期先给旧数据、后台刷新。
    """
    here = _abs_cache_path(cache_path)
    data = _read_json_cache(here)
    if data and data.get("stations"):
        if not _cache_fresh(here):
            _refresh_station_cache_async(
                _sibling_cache_path(here, "station_name.json"), here)
        return data["stations"]
    _name2code, _code2name, stations = _refresh_station_cache(
        _sibling_cache_path(here, "station_name.json"), here)
    return stations


# 查询端点按序回退：12306 会不定期切换/下线 queryG（ symptoms 是接口突然 404 或
# 返回非 JSON），queryZ/queryA/query 是历史上轮换出现过的同名族端点
QUERY_URLS = (
    "https://kyfw.12306.cn/otn/leftTicket/queryG",
    "https://kyfw.12306.cn/otn/leftTicket/queryZ",
    "https://kyfw.12306.cn/otn/leftTicket/queryA",
    "https://kyfw.12306.cn/otn/leftTicket/query",
)


def query_tickets(from_code, to_code, date, purpose="ADULT"):
    """查询某天某区间余票，返回余票结果列表（官方 result 数组）。免登录。
    purpose: ADULT=成人票, 0X00=学生票"""
    s = get_session()
    params = {
        "leftTicketDTO.train_date": date,
        "leftTicketDTO.from_station": from_code,
        "leftTicketDTO.to_station": to_code,
        "purpose_codes": purpose,
    }
    last = None
    for url in QUERY_URLS:
        try:
            r = s.get(url, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            continue  # 端点 404/超时/返回非 JSON：换下一个端点
        if not isinstance(data, dict):
            # 某端点可能返回 JSON 数组：按"无有效数据"处理，换下一个端点，不崩
            last = RuntimeError(
                "查询接口返回非 JSON 对象：{0}".format(type(data).__name__))
            continue
        if data.get("httpstatus") != 200:
            last = RuntimeError("查询接口返回异常：{0}".format(data))
            continue
        payload = data.get("data")
        if not isinstance(payload, dict):
            last = RuntimeError("查询接口返回无有效数据")
            continue
        result = payload.get("result") or []
        _note_query_success()
        return result
    if last is None:
        last = RuntimeError("查询接口无可用端点")
    _note_query_failure()
    raise last


# ---- 「该车次全部席别」（车次列表下方的明细）----
# 官方全量码表 seatTypeCodeForName；比 SEAT_CODE_TO_NAME 多 高级动卧(A)/一等卧(I)/
# 二等卧(J)/其他(H) —— 这些能显示，但不在下单码表里（抢票路径会剔除）。
SEAT_CODE_NAMES_ALL = {
    "9": "商务座", "P": "特等座", "D": "优选一等座", "M": "一等座", "O": "二等座",
    "A": "高级动卧", "F": "动卧", "6": "高级软卧", "4": "软卧", "I": "一等卧",
    "J": "二等卧", "3": "硬卧", "2": "软座", "1": "硬座", "WZ": "无座", "W": "无座",
    "H": "其他",
}
# 展示顺序：贵的在前（与官方列头一致），无座垫底
SEAT_SHOW_ORDER = ["商务座", "特等座", "优选一等座", "一等座", "二等座",
                   "高级动卧", "高级软卧", "动卧", "软卧", "一等卧", "二等卧",
                   "硬卧", "软座", "硬座", "无座", "其他"]
# p35(seat_types) 缺失时的兜底：按车型给常见席别（12306 各车型的固定席位配置）。
# 只用于「显示」，不影响下单：能不能自动下单仍只由 SEAT_NAME_TO_CODE 决定。
SEAT_KIND_SEATS = {
    "G": ["商务座", "特等座", "一等座", "二等座", "无座"],
    "D": ["商务座", "一等座", "二等座", "动卧", "无座"],
    "C": ["一等座", "二等座", "无座"],
    "普速": ["软卧", "硬卧", "软座", "硬座", "无座"],
}


def train_seat_kind(train_code):
    """按车次号首字母判车型：G/D/C 动车组，其余（Z/T/K/纯数字/L/Y…）算普速。"""
    c = str(train_code or "").strip().upper()
    return c[0] if c[:1] in ("G", "D", "C") else "普速"


def _split_seat_codes(codes):
    """p35 席别码串 → 席别名列表（按码表 longest-match 解析）。

    官方码表含多字符码（如 "WZ"）；逐字符遍历在官方新增多字符码时会错位
    （"WZ" 只是碰巧对：'W'→无座、'Z' 被静默跳过）。未知码记 warning 后
    跳过一位，不静默。"""
    s = str(codes or "").upper()
    names = []
    keys = sorted(SEAT_CODE_NAMES_ALL, key=len, reverse=True)
    i = 0
    while i < len(s):
        for k in keys:
            if s.startswith(k, i):
                n = SEAT_CODE_NAMES_ALL[k]
                if n not in names:
                    names.append(n)
                i += len(k)
                break
        else:
            LOG.warning("[余票] 未知席别码 %r（码串 %r），已跳过", s[i:i + 4], s)
            i += 1
    return names


def seat_names_all(codes, available=None, train_code=None):
    """该车次提供的全部席别名（含无票），按 SEAT_SHOW_ORDER 排序。

    codes = 本行 p35(seat_types) 码串（无分隔符，如 "OFAO"）；官方页面每趟车都有
    「无座」列而 p35 不含 WZ，所以固定补上；available 里出现过的名字也并入。
    codes 为空（未放票/接口没回码串）时按车型兜底（train_code → SEAT_KIND_SEATS），
    保证抢票（票还没放或已售完）时也能看到该车应有的全部席别。"""
    names = _split_seat_codes(codes)
    for n in (available or {}):
        if n not in names:
            names.append(n)
    if not names:
        names = list(SEAT_KIND_SEATS.get(train_seat_kind(train_code)) or [])
    if "无座" not in names:
        names.append("无座")
    order = {n: i for i, n in enumerate(SEAT_SHOW_ORDER)}
    return sorted(names, key=lambda x: order.get(x, 99))


def parse_row(row, code2name, query_date=None):
    """解析单个查询返回行（竖线分隔）。缺索引用 None，保证不崩。

    query_date: 本次查询用的乘车日期（YYYY-MM-DD）。
    p13（start_date）是列车「始发日期」且无横线（如 20261007），
    跨夜车与乘车日可能差一天，下单必须用 query_date，不能用 p13。"""
    f = row.split("|")

    def get(i):
        return f[i] if i < len(f) else None

    # 余票按 20~33 号字段逐席别给出（映射与官方 queryLeftTicket 脚本一致）：
    #   值为 "有"/正整数 = 有票；"" / "无" / "*" / "候补" / "0" = 不可购
    # 卧铺字段在普速车与动车之间共用（官方 DTO 字段名 dd.gr_num / dd.rw_num / dd.yw_num）：
    #   21 = 高级软卧(6)/高级动卧(A)、23 = 软卧(4)/动卧(F)/一等卧(I)、28 = 硬卧(3)/二等卧(J)。
    # 本行该叫什么由席别码串 p35(seat_types) 决定，否则勾选「动卧」的任务永远命中不了；
    # p27(yb_num)、p33(srrb_num) 实测 1788 行恒空，是死字段，不再占用席别名。
    # p35 是「码直接拼接」（实测 "F" / "OF" / "OFAO" / "JOIO" / "3411"），无分隔符，
    # 官方脚本同样用 seat_types.indexOf("A")>-1 这种子串判断。
    codes = get(35) or ""
    seat_fields = [
        (20, "优选一等座"), (21, "高级动卧" if "A" in codes else "高级软卧"),
        (22, "其他"),
        # 官方表头把这一列写成「软卧/动卧 一等卧」，实际键只有一个 RW(23) →
        # 按本行码串拆名；一等卧(I)/二等卧(J) 不在下单码表里，只显示不下单。
        (23, "动卧" if "F" in codes else ("一等卧" if "I" in codes else "软卧")),
        (24, "软座"), (25, "特等座"), (26, "无座"),
        (28, "二等卧" if "J" in codes else "硬卧"),
        (29, "硬座"), (30, "二等座"), (31, "一等座"),
        (32, "商务座"),
    ]
    no_ticket = ("", "无", "*", "候补", "0")
    available = {}
    for idx, name in seat_fields:
        value = (get(idx) or "").strip()
        if value and value not in no_ticket:
            available[name] = value

    return {
        "secret_str": get(0),
        "train_no": get(2),
        "train_code": get(3),
        # p6/p7 是本区间上下车站（中间站查询时 p4/p5 是列车全程起终点），优先取 p6/p7
        "from_code": get(6) or get(4),
        "to_code": get(7) or get(5),
        "from_name": code2name.get(get(6) or get(4), get(6) or get(4)),
        "to_name": code2name.get(get(7) or get(5), get(7) or get(5)),
        "start_time": get(8),
        "arrive_time": get(9),
        "duration": get(10),
        "can_buy": get(11),
        "start_date": get(13),
        "query_date": query_date,
        "train_location": get(15),
        "available_seats": available,
        # 本行席别码串 + 该车次提供的全部席别（含无票，供车次列表下方明细显示）
        "seat_codes": codes,
        "seats_all": seat_names_all(codes, available, get(3)),
        # p37 = 官方 DTO 的 houbu_train_flag：该车次可候补（官方页面售完时显示「候补」）
        "houbu": (get(37) or "").strip() == "1",
    }


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        return
    from_name, to_name, date = sys.argv[1], sys.argv[2], sys.argv[3]
    train_filter = set(x.strip().upper() for x in sys.argv[4].split(",")) if len(sys.argv) > 4 else None

    name2code, code2name = load_station_map()
    if from_name not in name2code or to_name not in name2code:
        print("车站不存在：{0} {1}".format(from_name, to_name))
        return
    fc, tc = name2code[from_name], name2code[to_name]

    print("查询 {0} -> {1}  日期 {2}  ({3})".format(
        from_name, to_name, date, time.strftime("%H:%M:%S")))
    rows = query_tickets(fc, tc, date)
    print("共返回 {0} 趟列车".format(len(rows)))
    print("-" * 70)

    hits = 0
    for row in rows:
        info = parse_row(row, code2name)
        if train_filter and info["train_code"] not in train_filter:
            continue
        seats = " ".join("{0}{1}".format(k, v) for k, v in info["available_seats"].items()) or "无票/席位不详"
        print("{code} {from_name}{start}->{arrive} {duration}  {seats}".format(
            code=info["train_code"], from_name=info["from_name"],
            start=info["start_time"], arrive=info["arrive_time"],
            duration=info["duration"], seats=seats))
        hits += 1
    if not hits:
        print("（无匹配车次或暂无可视余票）")


if __name__ == "__main__":
    main()