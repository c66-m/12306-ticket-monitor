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
import re
import json
import time
import threading

import requests

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
            cand = [s for s in avail if s in SEAT_NAME_TO_CODE]  # 不限席别：过滤不可下单的展示类席别名（Task 47）
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


def load_station_map(cache_path="station_name.json"):
    """下载并解析车站代码表，返回 {名称: 代码} 与 {代码: 名称} 两个字典。
    本地缓存损坏时自动重新下载（与 load_station_index 行为一致）。"""
    import os
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), cache_path)
    if os.path.exists(here):
        try:
            with open(here, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("name2code") and data.get("code2name"):
                return data["name2code"], data["code2name"]
        except Exception:
            pass  # 缓存损坏/空：走重新下载

    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://kyfw.12306.cn/otn/leftTicket/init"})
    r = s.get(STATION_JS_URL, timeout=20)
    r.raise_for_status()
    text = r.text

    name2code, code2name = {}, {}
    # 格式：@简拼|名称|代码|拼音|...
    for m in re.finditer(r"@[a-zA-Z]+\|([^|]+)\|([A-Z]{3})\|", text):
        name, code = m.group(1), m.group(2)
        name2code[name] = code
        code2name[code] = name

    if not name2code:
        raise RuntimeError("车站代码表解析失败，请检查 station_name.js 格式是否变更")

    with open(here, "w", encoding="utf-8") as f:
        json.dump({"name2code": name2code, "code2name": code2name}, f, ensure_ascii=False, indent=2)
    return name2code, code2name


def load_station_index(cache_path="station_index.json"):
    """下载并解析车站全量索引，返回 [{"name","code","py","spy"}, ...]（约 3300 站）。

    py=全拼（beijingbei） spy=简拼（bjb），供启动器本地模糊搜索使用。
    优先读本地缓存；缓存缺失/损坏时重新下载并写缓存。"""
    import os
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), cache_path)
    if os.path.exists(here):
        try:
            with open(here, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("stations"):
                    return data["stations"]
        except Exception:
            pass

    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://kyfw.12306.cn/otn/leftTicket/init"})
    r = s.get(STATION_JS_URL, timeout=20)
    r.raise_for_status()

    stations = []
    # 格式：@bjb|北京北|VAP|beijingbei|bjb|0
    for m in re.finditer(r"@([a-z]+)\|([^|]+)\|([A-Z]{3})\|([a-z]+)\|", r.text):
        stations.append({"name": m.group(2), "code": m.group(3),
                         "py": m.group(4), "spy": m.group(1)})
    if not stations:
        raise RuntimeError("车站索引解析失败，请检查 station_name.js 格式是否变更")

    with open(here, "w", encoding="utf-8") as f:
        json.dump({"stations": stations}, f, ensure_ascii=False)
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
    """查询某天某区间余票，返回按车次分组的字典。免登录。
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
        if data.get("httpstatus") != 200:
            last = RuntimeError("查询接口返回异常：{0}".format(data))
            continue
        return (data.get("data") or {}).get("result") or []
    if last is None:
        last = RuntimeError("查询接口无可用端点")
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


def seat_names_all(codes, available=None, train_code=None):
    """该车次提供的全部席别名（含无票），按 SEAT_SHOW_ORDER 排序。

    codes = 本行 p35(seat_types) 码串（无分隔符，如 "OFAO"）；官方页面每趟车都有
    「无座」列而 p35 不含 WZ，所以固定补上；available 里出现过的名字也并入。
    codes 为空（未放票/接口没回码串）时按车型兜底（train_code → SEAT_KIND_SEATS），
    保证抢票（票还没放或已售完）时也能看到该车应有的全部席别。"""
    names = []
    for ch in str(codes or "").upper():
        n = SEAT_CODE_NAMES_ALL.get(ch)
        if n and n not in names:
            names.append(n)
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
    #   值为 "有"/数字 = 有票；"" / "无" / "*" / "候补" = 不可购
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
    no_ticket = ("", "无", "*", "候补")
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