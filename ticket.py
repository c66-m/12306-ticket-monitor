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
    p20~p33 各席别余票：优选一等座/高级软卧/其他/软卧/软座/特等座/无座/动卧/
            硬卧/硬座/二等座/一等座/商务座/SRRB

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
# 顺序即界面展示顺序。「动卧」= F（查询字段 yb_num），网页端可正常勾选下单。
SEAT_CHOICES = ["商务座", "特等座", "优选一等座", "一等座", "二等座",
                "高级软卧", "动卧", "软卧", "硬卧", "软座", "硬座", "无座"]

SEAT_CODE_TO_NAME = {
    "9": "商务座", "P": "特等座", "M": "一等座", "O": "二等座",
    "6": "高级软卧", "4": "软卧", "F": "动卧", "3": "硬卧",
    "2": "软座", "1": "硬座", "WZ": "无座",
}
SEAT_NAME_TO_CODE = {v: k for k, v in SEAT_CODE_TO_NAME.items()}

# 12306 网页端确认页的可售席别由服务端下发，普速车不下发「无座」（实测 K925/K225/K1969
# 长葛→确山各日期都只给 硬座/硬卧/软卧）。无座与硬座同价，按同价原则改判为硬座下单，
# 监控侧仍按用户勾选的席别判断有无票。
ORDER_SEAT_ALIAS = {"WZ": "1"}


def order_seat_code(seat_name, seat_code=None):
    """下单实际要用的席位代码 + 别名目标名。

    返回 (code, alias_name)：命中同价改判时 code 为别名代码、alias_name 为其中文名；
    没命中时 alias_name 为 None、code 原样返回。
    """
    code = seat_code or SEAT_NAME_TO_CODE.get(seat_name)
    alias = ORDER_SEAT_ALIAS.get(code)
    if alias:
        return alias, SEAT_CODE_TO_NAME.get(alias)
    return code, None

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
    seat_fields = [
        (20, "优选一等座"), (21, "高级软卧"), (22, "其他"), (23, "软卧"),
        (24, "软座"), (25, "特等座"), (26, "无座"), (27, "动卧"),
        (28, "硬卧"), (29, "硬座"), (30, "二等座"), (31, "一等座"),
        (32, "商务座"), (33, "SRRB"),
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
    }


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        return
    from_name, to_name, date = sys.argv[1], sys.argv[2], sys.argv[3]
    train_filter = set(x.strip() for x in sys.argv[4].split(",")) if len(sys.argv) > 4 else None

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