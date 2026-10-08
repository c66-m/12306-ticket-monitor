# -*- coding: utf-8 -*-
"""
下单模块：基于已保存的登录会话，自动完成 "提交订单 -> 选取乘车人 -> 提交排队"，
最后停在【未完成订单】状态，不支付。

流程（2026 经验，每一步都保留原始响应便于排查接口变更）
    1. POST /otn/leftTicket/submitOrderRequest      -- 提交订单（带 secretStr 等）
    2. GET  /otn/confirmPassenger/initDc            -- 拿 repeatSubmitToken / leftTicketStr / key_check_isChange
    3. GET  /otn/confirmPassenger/getPassengerDTOs  -- 拉取已保存的常用乘车人（优先用配置里的名字；没配就全用）
    4. POST /otn/confirmPassenger/checkOrderInfo    -- 校验乘车人信息（若触发滑块验证码则中止并提示）
    5. POST /otn/confirmPassenger/confirmSingleForQueue -- 提交排队，成功即生成未支付订单

不做什么
    - 不支付、不解决滑块验证码（那属于绕过安全验证，不做）
    - 若中途触发验证码或接口变化，会明确打印出来，由你人工介入

用法（一般不单独跑，由 monitor.py 调用）
    python order.py
"""

import json
import logging
import os
import re
import sys
import time
import datetime
from urllib.parse import unquote, urlencode

import appcommon
import requests
# Task 85a/b：复用 capture_session 的 Cookie 序列化规则（复合键分隔符 +
# _build_cookie_dict），落盘格式单一事实来源；capture_session 只依赖
# appcommon，无循环导入。
from capture_session import COOKIE_KEY_SEP, _build_cookie_dict

LOG = logging.getLogger(__name__)


def _mask_order_no(ono):
    """订单号脱敏（日志用）：保留前后各 4 位，中间打码；过短则全打码。

    与 engine._mask_order_no 同口径（order.py 不能 import engine，会循环依赖）。
    """
    s = str(ono or "")
    if len(s) <= 8:
        return "****"
    return s[:4] + "****" + s[-4:]

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    from ticket import SEAT_NAME_TO_CODE, order_seat_code
except ImportError:
    SEAT_NAME_TO_CODE = {
        "商务座": "9", "特等座": "P", "一等座": "M", "二等座": "O",
        "高级软卧": "6", "软卧": "4", "动卧": "F", "硬卧": "3",
        "软座": "2", "硬座": "1", "无座": "WZ",
    }

    def order_seat_code(seat_name, seat_code=None, train_code=None):
        """兜底实现（ticket.py 缺席时）：无座按同价席别改判（动车组→二等座，其余→硬座）。"""
        code = seat_code or SEAT_NAME_TO_CODE.get(seat_name)
        if code == "WZ":
            emu = str(train_code or "")[:1].upper() in ("G", "D", "C")
            return ("O", "二等座") if emu else ("1", "硬座")
        return code, None

HERE = os.path.dirname(os.path.abspath(__file__))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": "https://kyfw.12306.cn",
    "X-Requested-With": "XMLHttpRequest",
}


def _resolve_cookie_path(cookie_path):
    """会话文件路径解析：相对路径按脚本目录（HERE）解析。

    与 capture_session._cookie_path 同口径（Task 99c）：写侧（capture_session）
    一直是 os.path.join(HERE, name)，读/写侧显式传相对路径时若按 CWD 解析，
    CWD≠脚本目录就会"未找到会话文件"误导或写错位置。绝对路径透传；
    None/非法值回退默认文件名。
    """
    name = cookie_path if isinstance(cookie_path, str) else None
    name = name or "session_cookies.json"
    if os.path.isabs(name):
        return name
    return os.path.join(HERE, name)


def load_session(cookie_path=None):
    """加载 saved 会话 Cookie，返回 requests.Session。

    必须保留服务端下发的 domain/path：12306 的 Cookie 有严格作用域
    （_uab_collina 只属于 /otn/resources、_passport_session 只属于 /passport），
    拍平成「全局 Cookie」会让每个接口收到本不该出现的 Cookie，属明显的非浏览器特征。
    旧格式（纯 name->value）仍兼容，回落到原来的全局作用域。"""
    path = _resolve_cookie_path(cookie_path)
    if not os.path.exists(path):
        raise RuntimeError("未找到会话文件 {0}，请先运行：python capture_session.py".format(path))
    with open(path, "r", encoding="utf-8") as f:
        cookies = json.load(f)

    s = requests.Session()
    s.headers.update(BASE_HEADERS)
    for key, val in cookies.items():
        if isinstance(val, dict):  # 新格式：带原始作用域
            # Task 69e：同名多 path 条目以复合键 "name<SEP>path<SEP>domain" 落盘
            # （分隔符见 COOKIE_KEY_SEP，\x1f 不可能出现在 Cookie 名中）；
            # 无分隔符即旧的纯 name 键，照旧读取。
            name = key.split(COOKIE_KEY_SEP)[0] if COOKIE_KEY_SEP in key else key
            s.cookies.set(name, val.get("value", ""),
                          domain=val.get("domain") or ".12306.cn",
                          path=val.get("path") or "/")
        else:                      # 旧格式：只有值
            s.cookies.set(key, val, domain=".12306.cn", path="/")
    return s


def save_session(session, cookie_path=None):
    """把会话 Cookie 回写文件。12306 会轮换 tk 等关键 Cookie，
    定期回写可延长会话有效期（避免一直用旧 Cookie 被判定过期）。

    Task 85a：与 capture_session._build_cookie_dict 同一序列化规则——同名
    多 path 按 (name,path,domain) 三元组复合键全部保留，不再按 name
    last-wins 丢数据；load_session 可解析（round-trip）。"""
    path = _resolve_cookie_path(cookie_path)
    cookies = _build_cookie_dict([
        {"name": c.name, "value": c.value,
         "domain": getattr(c, "domain", "") or "",
         "path": getattr(c, "path", "") or ""}
        for c in session.cookies
    ])
    if not cookies:
        return
    appcommon.atomic_write_json(path, cookies)


CHECK_URL = "https://kyfw.12306.cn/otn/index/initMy12306Api"


def _classify_orders(date, train, passenger_names, orders):
    """纯分类逻辑（不查接口）：对已查到的订单列表做状态分类。

    返回 (分类, 订单号, 原文状态)，与 classify_order_status 的后三元一致。
    抽出是为了让 classify_with_time 复用同一次查询结果，避免每次调用
    打两次官方订单接口（HTTP 量翻倍，频繁回读时易触 12306 限流）。
    """
    want = set(passenger_names or [])

    def _pax_hit(x):
        pax = set(x.get("passengers") or [])
        # 收紧：全部目标乘车人命中才算命中；空目标集不算命中。
        # 旧代码任一交集即中、空集恒中 → 误判重复 / 误标本行程。
        return bool(want) and want <= pax

    # 12306 规则:存在任何未完成订单就挡住新下单(不分日期车次)。
    # 先分清挡路的是不是本行程:本行程=unpaid;其它行程=blocked。
    inc = [x for x in orders if x.get("_no_complete")]
    for x in inc:
        if ((x.get("train") or "") == (train or "")
                and (x.get("date") or "")[:10] == (date or "")[:10] and _pax_hit(x)):
            return "unpaid", x.get("order_no", ""), x.get("status") or "未完成/未支付"
    if inc:
        x = inc[0]
        return ("blocked", x.get("order_no", ""),
                "存在其它行程未支付订单(%s %s)挡路" % (x.get("train"), x.get("date")))
    for x in orders:
        if (x.get("train") or "") != (train or "") or (x.get("date") or "")[:10] != (date or "")[:10]:
            continue
        if not _pax_hit(x):
            continue
        st = x.get("status") or ""
        ono = x.get("order_no", "")
        # 票级状态来自 tickets[].ticket_status_name：已退票（含业务流水号）/
        # 已出站 / 已支付等。退票后可重新购票，故归 cancelled 允许重试。
        if "退票" in st or "取消" in st:
            return "cancelled", ono, st
        if "出站" in st or "支付" in st or "未出行" in st:
            return "paid", ono, st
        return "unknown", ono, st
    return "none", "", "官方订单列表(未完成+该日历史)中未找到 %s %s" % (train, date)


def classify_order_status(date, train, passenger_names, session=None):
    """12306 官方接口订单状态分类(唯一事实来源,只读接口)。

    返回 (分类, 订单号, 原文状态):分类 ∈ paid(已支付)/unpaid(待支付)/
    cancelled(已取消)/blocked(被其它行程未完成订单挡路)/unknown(状态不明)/
    none(未找到)/error(查询失败)。匹配规则：本行程 = 车次相同 + 乘车日期
    相同 + 目标乘车人全部命中订单乘车人（目标集 ⊆ 订单乘车人；空目标集不算
    命中；任一交集不算命中）。12306 规则：存在任何未完成订单即挡住新下单，
    非本行程的未完成订单返回 blocked。只读官方接口，不下单。"""
    try:
        sess = session or session_from_browser_state()
    except Exception as e:
        return "error", "", "官方订单查询失败: %s" % e
    try:
        orders = check_existing_orders(sess, date)
    except Exception as e:
        return "error", "", "官方订单查询异常: %s" % e
    return _classify_orders(date, train, passenger_names, orders)


def _beijing_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Asia/Shanghai")
    except Exception:
        # Windows 未装 tzdata 包时 ZoneInfo 不可用；北京无夏令时，固定 +8
        # 恒等于 Asia/Shanghai，退化为此时区偏移同样正确。
        return datetime.timezone(datetime.timedelta(hours=8), name="Asia/Shanghai")


_BJ_TZ = _beijing_tz()


def parse_bj_wall(s):
    """把 12306 的北京时间墙钟串（"YYYY-MM-DD HH:MM:SS"）解析为 epoch 秒。

    显式按北京时间解析，不依赖机器本地时区（UTC 机器上 time.mktime 会偏 8 小时）。
    解析失败返回 None。后续架构 Task 20 会收敛为全仓库统一入口。
    """
    try:
        dt = datetime.datetime.strptime((s or "")[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None
    return dt.replace(tzinfo=_BJ_TZ).timestamp()


def find_recent_order(orders, date, train_code, passenger_names, not_before_ts, window_sec=180):
    """时间戳归因：在订单列表里找「本行程 + 下单时间落在本次提交窗口内」的订单。

    not_before_ts 是软件本次发起下单动作的时刻（epoch 秒）。订单的 order_ts 若
    大于 not_before_ts - window_sec（允许少量时钟/接口延迟），即可判定该订单是
    本次提交生成的；更早的就是遗留旧订单。

    上界：order_ts 大于 not_before_ts + 600 的订单一律跳过——"未来"的订单不
    可能是本次提交生成的（时区解析偏斜的旧单、他会话事后建的单），否则会被误判
    为"本次提交成功"（假成功）。返回匹配的订单 dict 或 None。
    """
    lo = not_before_ts - window_sec
    hi = not_before_ts + 600
    want = set(passenger_names or [])
    best = None
    for o in orders:
        if ((o.get("train") or "") != (train_code or "")
                or (o.get("date") or "")[:10] != (date or "")[:10]):
            continue
        pax = set(o.get("passengers") or [])
        # 收紧：全部目标乘车人命中才算命中（与 Task 59 的 find_duplicate /
        # classify 同口径）。旧代码任一交集即中 → 同车次同日期乘车人部分
        # 重叠的他人订单会被归因成本次提交（通知/历史写入错误订单号）。
        if want and not (want <= pax):
            continue
        ts = o.get("order_ts")
        if ts is None or ts < lo or ts > hi:
            continue
        if best is None or ts > best.get("order_ts", 0):
            best = o
    return best


def classify_with_time(date, train, passenger_names, not_before_ts=None, session=None):
    """classify_order_status + 下单时间归因。

    返回 (cls, order_no, raw, recent)：前三个与 classify_order_status 完全一致，
    recent 是「下单时间落在本次提交窗口内」的订单 dict（cls 为 unpaid 时才可能有值，
    其余情况为 None）。not_before_ts 传 None 时跳过归因，recent 恒为 None。

    只查一次官方订单接口：分类与归因复用同一份订单快照（旧代码查两次，
    HTTP 量翻倍且两次快照可能不一致）。
    """
    try:
        sess = session or session_from_browser_state()
    except Exception as e:
        return "error", "", "官方订单查询失败: %s" % e, None
    try:
        orders = check_existing_orders(sess, date)
    except Exception as e:
        return "error", "", "官方订单查询异常: %s" % e, None
    cls, ono, raw = _classify_orders(date, train, passenger_names, orders)
    recent = None
    if cls == "unpaid" and not_before_ts is not None:
        try:
            recent = find_recent_order(orders, date, train, passenger_names,
                                       not_before_ts)
        except Exception:
            recent = None
    return cls, ono, raw, recent


def session_from_browser_state(state_path=None):
    """从 browser_order 的 .browser_state.json Cookie 构造 requests 会话。

    浏览器下单模式下登录态在 .browser_profile 里，session_cookies.json 是
    capture_session 时代的旧载体；读只读接口（乘车人列表等）时用浏览器
    Cookie 组会话即可，不必再开浏览器。"""
    path = state_path or os.path.join(HERE, ".browser_state.json")
    if not os.path.exists(path):
        raise RuntimeError("未找到 %s（请先用浏览器方式登录一次）"
                           % os.path.basename(path))
    with open(path, encoding="utf-8") as f:
        saved = json.load(f)
    cookie_list = saved.get("cookies") if isinstance(saved, dict) else saved
    s = requests.Session()
    s.headers.update(BASE_HEADERS)
    for c in cookie_list or []:
        if "12306.cn" in (c.get("domain") or "") and c.get("name"):
            s.cookies.set(c["name"], c.get("value", ""),
                          domain=c.get("domain") or ".12306.cn",
                          path=c.get("path") or "/")
    return s


def verify_session(session):
    """校验会话是否有效。返回 (ok, who, permanent)：
    - ok=True：会话有效
    - permanent=True：确认会话已失效（跳转登录页 / 接口明确未登录）
    - permanent=False：临时故障（网络异常/非 JSON 响应，如系统繁忙页），稍后重试即可
    非 JSON 响应会重试 3 次，避免把 12306 的临时繁忙误判成登录失效。"""
    last_err = "未知错误"
    for _ in range(3):
        try:
            r = session.get(CHECK_URL, timeout=15)
            text = r.text or ""
            if text.lstrip().startswith("{") or "json" in r.headers.get("Content-Type", ""):
                data = r.json()
                user = (data.get("data") or {}).get("user_name")
                if data.get("status"):
                    return True, user, True
                return False, "接口返回 status=false", True
            # 非 JSON：只有明确跳转登录门户 / 提示未登录才是真的失效。
            # 12306 踢会话时是 302 到 /otn/passport?redirect=...（不是 /login），
            # 只认 /login 会把真失效误判成临时故障 -> 无限重试、永不下单。
            final_url = r.url or ""
            if ("/login" in final_url or "/passport" in final_url
                    or "请先登录" in text or "未登录" in text[:500]):
                return False, "已跳转登录门户：%s" % final_url, True
            last_err = "响应非 JSON（疑似系统繁忙/网关拦截）"
        except Exception as e:
            last_err = "{0}: {1}".format(type(e).__name__, e)
        time.sleep(1.2)
    return False, last_err, False


def check_login(session):
    """校验会话是否有效（调用需登录接口）。返回 (ok, 用户名或错误)。
    兼容旧调用方：临时故障也返回 False（提示信息说明原因，稍后可重试）。"""
    ok, who, _permanent = verify_session(session)
    return ok, who


def submit_order_request(session, ticket, seat_code, date, purpose="ADULT"):
    """提交订单请求（现行参数集）。

    现行 /otn/leftTicket/submitOrderRequest 只认 secretStr + train_date +
    back_train_date + tour_flag + purpose_codes + 查询站名这几项。
    老的 train_no/seatType/fromStationTelecode 参数集缺 tour_flag 等必需字段，
    服务端无法识别，一律回「系统忙，请稍后重试」——看着像限流，其实是参数不认。
    席别不在这里指定：选席别发生在后续确认页，seat_code 仅为兼容旧调用方保留。

    ticket: ticket.py parse_row 的结果
    purpose: ADULT=成人票, 0X00=学生票
    """
    url = "https://kyfw.12306.cn/otn/leftTicket/submitOrderRequest"
    # 余票接口返回的 secretStr 本身是 URL 编码串；requests 会对 data 再编码一次，
    # 先 unquote 还原，发出去的才是服务端要的原始形态。
    secret_str = unquote(ticket.get("secret_str") or "")
    data = {
        "secretStr": secret_str,
        "train_date": date,
        "back_train_date": time.strftime("%Y-%m-%d"),
        "tour_flag": "dc",  # dc=单程，wc=往返
        "purpose_codes": purpose,
        "query_from_station_name": ticket.get("from_name") or "",
        "query_to_station_name": ticket.get("to_name") or "",
        "undefined": "",
    }
    session.headers["Referer"] = "https://kyfw.12306.cn/otn/leftTicket/init"
    r = session.post(url, data=data, timeout=15)
    return r


def is_busy_error(msg):
    """判断是否为可自动重试的"系统忙"类临时错误。"""
    for kw in ("系统忙", "请稍后重试", "访问太频繁", "网络繁忙", "请求过于频繁"):
        if kw in (msg or ""):
            return True
    return False


def _post_submit_json(session, ticket, seat_code, date, purpose):
    """submitOrderRequest + JSON 解析，带非 JSON transient 重试（最多 3 次）。

    返回 (d, err)：成功时 d 为响应 dict；网络异常时 err 描述异常（不重试，
    保持旧行为）；连续 3 次非 JSON（12306 WAF/网关偶发拦截页）时 err 描述
    最后一次。重试口径与 verify_session 一致。
    """
    last_err = ""
    for _ in range(3):
        try:
            resp = submit_order_request(session, ticket, seat_code, date, purpose)
        except Exception as e:
            return None, "submitOrderRequest 异常: {0} 原始响应: ".format(e)
        try:
            return resp.json(), None
        except Exception as e:
            raw = getattr(resp, "text", "") or ""
            last_err = "submitOrderRequest 异常: {0} 原始响应: {1}".format(e, raw[:300])
            time.sleep(1.2)
    return None, last_err


def submit_with_busy_retry(session, ticket, seat_code, date, tries, delay, purpose="ADULT"):
    """提交订单请求，遇"系统忙"类错误自动重试（间隔递增）。返回 (ok, msg)。"""
    for attempt in range(1, tries + 1):
        d, err = _post_submit_json(session, ticket, seat_code, date, purpose)
        if d is None:
            return False, err
        if d.get("status"):
            return True, ""
        msg = str(d.get("validateMessages") or d.get("messages") or "")
        if is_busy_error(msg) and attempt < tries:
            time.sleep(delay * attempt)
            continue
        return False, "submitOrderRequest 失败: {0}".format(msg)
    return False, "submitOrderRequest 连续 {0} 次系统忙，稍后自动重试".format(tries)


def get_init_dc(session):
    """进入确认订单页，解析 repeatSubmitToken / leftTicketStr / key_check_isChange。"""
    url = "https://kyfw.12306.cn/otn/confirmPassenger/initDc"
    session.headers["Referer"] = "https://kyfw.12306.cn/otn/leftTicket/init"
    r = session.get(url, timeout=30)
    html = r.text

    def grab(pattern, group=1):
        m = re.search(pattern, html)
        return m.group(group) if m else None

    # 现行页面把值嵌在单引号 JSON 里（'leftTicketStr':'xxx'）。旧的
    # leftTicketStr[^']*'([^']+)' 会把键的结束引号当值的开始引号、把冒号捕获成值。
    # JSON 形式优先，老式 JS 赋值形式兜底。
    token = (grab(r"'globalRepeatSubmitToken'\s*:\s*'([^']+)'")
             or grab(r"globalRepeatSubmitToken\s*=\s*'([^']+)'")
             or grab(r"globalRepeatSubmitToken\s*=\s*\"([^\"]+)\""))
    left_ticket_str = (grab(r"'leftTicketStr'\s*:\s*'([^']*)'")
                       or grab(r"leftTicketStr\s*=\s*'([^']*)'")
                       or grab(r"leftTicketStr\s*=\s*\"([^\"]*)\""))
    key_check = (grab(r"'key_check_isChange'\s*:\s*'([^']*)'")
                 or grab(r"key_check_isChange\s*=\s*'([^']*)'")
                 or grab(r"key_check_isChange[=:]\s*['\"]*(\w+)['\"]*"))
    if key_check == "null":
        key_check = None

    return token, left_ticket_str, key_check, html


def get_passengers(session):
    """获取已保存的常用乘车人列表。返回 [{name,id_type,id_no,mobile,is_adult}]"""
    url = "https://kyfw.12306.cn/otn/confirmPassenger/getPassengerDTOs"
    session.headers["Referer"] = "https://kyfw.12306.cn/otn/confirmPassenger/initDc"
    r = session.post(url, timeout=15)
    data = r.json().get("data") or {}

    passengers = []
    for key, adult_field, name_f, idt_f, idno_f, mob_f in (
        ("normalPassengers", "isAdult", "passenger_name", "passenger_id_type_code", "passenger_id_no", "mobile_no"),
        ("passengerDTOs", "isAdult", "passenger_name", "passenger_id_type_code", "passenger_id_no", "mobile_no"),
    ):
        lst = data.get(key)
        if lst:
            for p in lst:
                passengers.append({
                    "name": p.get(name_f),
                    "id_type": p.get(idt_f, "1"),
                    "id_no": p.get(idno_f),
                    "mobile": p.get(mob_f) or "",
                    "is_adult": p.get(adult_field, 1) != 0,
                    "type_name": p.get("passenger_type_name") or "",
                })
            break

    # 兜底：若上面接口字段路径变了，用 passengers/query
    if not passengers:
        r2 = session.post("https://kyfw.12306.cn/otn/passengers/query",
                          data={"pageIndex": 1, "pageSize": 10}, timeout=15)
        for p in (r2.json().get("data") or {}).get("datas") or []:
            passengers.append({
                "name": p.get("passenger_name"),
                "id_type": p.get("passenger_id_type_code", "1"),
                "id_no": p.get("passenger_id_no"),
                "mobile": p.get("mobile_no") or "",
                "is_adult": p.get("isAdult", 1) != 0,
                "type_name": p.get("passenger_type_name") or "",
            })
    return passengers


def select_passengers(all_passengers, wanted_names):
    """按偏好选乘车人：配置点名 > 本地默认乘车人 > 账号内全部成人乘车人。"""
    if wanted_names:
        # 按 wanted_names 的顺序挑选，保持用户指定的组合顺序
        picked = []
        for name in wanted_names:
            for p in all_passengers:
                if p["name"] == name:
                    picked.append(p)
                    break
        if picked:
            return picked
    return [p for p in all_passengers if p.get("is_adult", True) and p.get("id_no")]


def _normalize_order_item(item, status):
    """把 queryMyOrderNoComplete / queryMyOrder 里的订单项解析成统一结构。"""
    passengers = []
    for p in item.get("passengerDTOList") or []:
        name = p.get("passenger_name")
        if name:
            passengers.append(name)
    # 新接口里乘车人挂在 array_passser_name_page（字符串数组，官方拼写如此）
    for name in item.get("array_passser_name_page") or []:
        if name and name not in passengers:
            passengers.append(name)
    start = item.get("start_train_date_page") or ""
    # date 语义是乘车日期：缺 start_train_date_page 时留空，不回退为下单日期
    # （旧代码回退今天 → find_duplicate 按乘车日期永远 miss → 被误判为
    # blocked"其它行程"；留空反而安全，无 date 记录会被各判定跳过）。

    def _f(v):
        # 新接口里站名是数组（如 ["长葛"]），老接口是字符串，统一成字符串
        if isinstance(v, (list, tuple)):
            return "".join(str(x) for x in v)
        return v or ""

    # 票状态藏在 tickets[].ticket_status_name（"已出站" / "已退票(业务流水号:...)"），
    # 比顶层 return_flag/resign_flag 可靠（实测这些 flag 对所有状态都一样）。
    ticket_status = ""
    for t in item.get("tickets") or []:
        sn = (t.get("ticket_status_name") or "").strip()
        if sn:
            ticket_status = sn
            break
    # 席别：tickets[] 里通常带 seat_type_name（中文名，如"硬座"），取第一张
    # 有效票的。字段名按 12306 queryMyOrder 的 DTO 形状；取不到则为空，
    # 防重判定时视为未知（不以此收窄），向后兼容。
    seat_name = ""
    for t in item.get("tickets") or []:
        stn = (t.get("seat_type_name") or "").strip()
        if stn:
            seat_name = stn
            break
    # order_date 是下单时刻（"YYYY-MM-DD HH:MM:SS"，北京时间），用于「这笔订单是不是
    # 本次提交生成的」时间戳归因；解析失败则 order_ts=None，归因时走保守分支。
    # 注意：必须用 parse_bj_wall 显式按北京时间解析——time.mktime 按机器本地时区
    # 解析，在 UTC 机器上会偏大 8 小时，导致旧单被误判为"本次提交"（假成功）。
    order_date_raw = (item.get("order_date") or "").strip()
    order_ts = parse_bj_wall(order_date_raw)
    return {
        "order_no": item.get("sequence_no") or item.get("order_no") or "",
        "train": (item.get("train_code_page") or "").replace(" ", ""),
        "from": _f(item.get("from_station_name_page")),
        "to": _f(item.get("to_station_name_page")),
        "date": start[:10] if start else "",
        "status": ticket_status or status or item.get("order_status_name_cn") or "",
        "passengers": passengers,
        "order_time": order_date_raw[:19] if order_date_raw else "",
        "order_ts": order_ts,
        "seat": seat_name,
    }


def _query_my_order(session, query_where, start, end, page_size=8, max_pages=25):
    """新版 queryMyOrder（2026-10 实测参数）。query_where: G=未出行, H=历史。

    旧参数集（_json_att + 缺 pageIndex/pageSize/query_where/sequeue_train_name）
    会拿到 200 空 body，静默失效；缺了新参数一个都不行。
    注意：H（历史）不接受含今天及未来的日期窗口，会返回空 body；
    乘车日期已过去的订单用 H 查，未出行的用 G 查。

    翻页：pageIndex 从 0 起逐页拉取，直到某页不足 page_size 条（末页）；
    max_pages 是防死循环上限（25 页 x 8 条 = 200 条，远超 60 天窗口的
    实际订单量）。旧代码 pageIndex 恒 "0"，深页订单漏检。
    """
    items = []
    for page in range(max_pages):
        data = {"come_from_flag": "my_order", "pageIndex": str(page),
                "pageSize": str(page_size), "query_where": query_where,
                "queryStartDate": start, "queryEndDate": end,
                "queryType": "1", "sequeue_train_name": ""}
        r = session.post("https://kyfw.12306.cn/otn/queryOrder/queryMyOrder",
                         data=data, timeout=15)
        batch = ((r.json().get("data") or {}).get("OrderDTODataList") or [])
        items.extend(batch)
        if len(batch) < page_size:
            break
    return items


def check_existing_orders(session, target_date):
    """查询账号订单（未完成 + 未出行 + 历史），返回统一订单列表（尽力解析）。

    target_date 是乘车日期。三个列表都必须查：已支付的未来票在「未出行」（G），
    已出行/已退票的在「历史」（H），未支付的独立接口（NoComplete）。

    任一查询失败记 warning 后向上传播（不再吞成空列表）：调用方按"未知"
    保守处理（如下单入口直接中止本轮），不拿空列表当"无重复"继续下单。
    """
    orders = []
    # 查询窗口按北京时间构造：订单时间戳已按北京时间解析（Task 36），
    # 机器本地时区非 Asia/Shanghai 时，time.strftime/time.localtime 会让
    # 窗口整体偏早一天，漏掉北京"今天"下单的已支付单。
    now_bj = datetime.datetime.now(_BJ_TZ)
    today = now_bj.strftime("%Y-%m-%d")
    yesterday = (now_bj - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    back60 = (now_bj - datetime.timedelta(days=60)).strftime("%Y-%m-%d")
    ahead60 = (now_bj + datetime.timedelta(days=60)).strftime("%Y-%m-%d")
    # 未完成订单（未支付）：不分日期车次，12306 规则是任一未完成订单都会挡新单
    try:
        r = session.post("https://kyfw.12306.cn/otn/queryOrder/queryMyOrderNoComplete",
                         data={"_json_att": ""}, timeout=15)
        for item in ((r.json().get("data") or {}).get("orderDBList") or []):
            it = _normalize_order_item(item, "未完成/未支付")
            it["_no_complete"] = True
            orders.append(it)
    except Exception as e:
        LOG.warning("查询未完成订单失败: %s", e)
        raise RuntimeError("未完成订单查询失败: {0}".format(e))
    # 未出行（已支付、乘车日期在今天之后）：G 的窗口按下单日期过滤
    # （实测：G 窗口 10-07~12-06 查不到 9-28 下单的 K225，9-28~9-28 可以），
    # 故窗口取 [60 天前, 今天]；列表含退票残留，状态看票级字段。
    try:
        for item in _query_my_order(session, "G", back60, today):
            orders.append(_normalize_order_item(item, "已支付(未出行)"))
    except Exception as e:
        LOG.warning("查询未出行订单失败: %s", e)
        raise RuntimeError("未出行订单查询失败: {0}".format(e))
    # 历史（乘车日期已过，含已出站/已退票）：H 窗口 EndDate 必须 <= 昨天，
    # 含今天会整体返回空 body；目标日期在过去时只查当天即可
    try:
        h_end = target_date if target_date <= yesterday else yesterday
        h_start = target_date if target_date <= yesterday else back60
        for item in _query_my_order(session, "H", h_start, h_end):
            orders.append(_normalize_order_item(item, "历史订单"))
    except Exception as e:
        LOG.warning("查询历史订单失败: %s", e)
        raise RuntimeError("历史订单查询失败: {0}".format(e))
    return orders


def _effective_seat_name(seat_name, train_code):
    """提交时的有效席别名：无座按同价改判（动车组→二等座，普速→硬座），其余原样。

    find_duplicate 的席别比对必须用改判后席别：已存订单的 seat 取自
    tickets[].seat_type_name，是实际出票席别（改判后）；调用方传的是用户
    勾选席别（改判前，如"无座"），直接比对会漏检同行程未支付单、
    防重守卫被放行。
    """
    name = (seat_name or "").strip()
    if not name:
        return ""
    try:
        _code, alias = order_seat_code(name, None, train_code)
    except Exception:
        return name
    return (alias or name).strip()


def find_duplicate(orders, date, train_code, passenger_names,
                   from_station=None, to_station=None, seat_name=None):
    """
    在账号已有订单里查重：同一日期 + 同一车次 + 区间一致 + 席别一致 +
    全部目标乘车人命中 = 重复。

    - 区间/席别：调用方与订单双方都有值时才比对；任一侧缺失不以此为由
      排除（向后兼容旧数据/旧调用）。
    - 席别取自订单 tickets[] 的 seat_type_name（best-effort；取不到视为
      未知，不收窄）；调用方席别先经同价改判换算为提交时有效席别
      （"无座"→"二等座"/"硬座"），再与订单席别比对。
    - 订单解析不出乘车人列表：记 warning 后跳过该条，不封锁整条线路
      （旧代码保守判重，一条畸形记录永久封锁该 date+train 的一切下单）。
    - 空目标乘车人集不算命中。
    """
    want = set(passenger_names or [])
    # 席别比对统一到提交时的有效席别：调用方 seat_name 是用户勾选（改判前），
    # 订单 seat 是实际出票席别（改判后）。_effective_seat_name 做同价改判。
    want_seat = _effective_seat_name(seat_name, train_code)
    for o in orders:
        if not o.get("date") or not o.get("train"):
            continue
        if o["date"] != date or o["train"] != train_code:
            continue
        of, ot = (o.get("from") or ""), (o.get("to") or "")
        if from_station and of and of != from_station:
            continue
        if to_station and ot and ot != to_station:
            continue
        o_seat = (o.get("seat") or "").strip()
        if want_seat and o_seat and o_seat != want_seat:
            continue
        pax = o.get("passengers") or []
        if not pax:
            LOG.warning("订单 %s 乘车人解析为空，跳过该条防重判定（不封锁线路）",
                        _mask_order_no(o.get("order_no") or "?"))
            continue
        if want and want <= set(pax):
            return o
    return None


def build_ticket_strs(passengers, seat_code, purpose_map=None):
    """构造 passengerTicketStr / oldPassengerStr（多乘客用 _ 连接，与官网一致）。

    purpose_map: {姓名: 票种代码}；ADULT=成人票（ticket_type=1），0X00=学生票（=3）。
    缺省按成人票。票种按人写，同一订单里成人票 / 学生票可以混选。
    """
    pm = purpose_map or {}

    def _ttype(p):
        return "3" if pm.get(p["name"]) == "0X00" else "1"

    passenger_ticket_str = "".join(
        "{0},0,{1},{2},1,{3},{4},N,0_".format(
            seat_code, _ttype(p), p["name"], p["id_no"], p["mobile"])
        if p["mobile"]
        else "{0},0,{1},{2},1,{3},,N,0_".format(
            seat_code, _ttype(p), p["name"], p["id_no"])
        for p in passengers
    )
    old_passenger_str = "".join(
        "{0},1,{1},1_".format(p["name"], p["id_no"]) for p in passengers
    )
    return passenger_ticket_str, old_passenger_str


def check_order_info(session, token, passenger_ticket_str, old_passenger_str):
    """校验订单信息。若要求动态验证码则返回 (False, '需要验证码')。"""
    url = "https://kyfw.12306.cn/otn/confirmPassenger/checkOrderInfo"
    session.headers["Referer"] = "https://kyfw.12306.cn/otn/confirmPassenger/initDc"
    data = {
        "cancel_flag": "2",
        "bed_level_order_num": "000000000000000000000000000000",
        "passengerTicketStr": passenger_ticket_str,
        "oldPassengerStr": old_passenger_str,
        "tour_flag": "dc",
        "randCode": "",
        "whatsSelect": "1",
        "_json_att": "",
        "REPEAT_SUBMIT_TOKEN": token,
    }
    r = session.post(url, data=data, timeout=15)
    d = r.json()
    if not d.get("status"):
        return False, "checkOrderInfo 返回 status=false: {0}".format(d.get("messages", d.get("validateMessages")))
    data_ = d.get("data") or {}
    if data_.get("ifShowPassCode") == "Y" or data_.get("ifShowOtherPassCode") == "Y":
        return False, "触发滑块验证码，自动下单中止（请人工在网页完成）"
    if data_.get("submitStatus") is False or data_.get("errMsg"):
        # 业务失败藏在 data 里（如「非法的席别」），外层 status=true 不代表通过
        return False, "checkOrderInfo 校验失败: {0}".format(data_.get("errMsg") or data_)
    return True, ""


def confirm_order(session, token, left_ticket_str, key_check, train_location,
                  passenger_ticket_str, old_passenger_str, purpose="ADULT"):
    """提交排队下单。成功即生成未支付订单。返回 (ok, 详情/错误)。"""
    url = "https://kyfw.12306.cn/otn/confirmPassenger/confirmSingleForQueue"
    session.headers["Referer"] = "https://kyfw.12306.cn/otn/confirmPassenger/initDc"
    data = {
        "passengerTicketStr": passenger_ticket_str,
        "oldPassengerStr": old_passenger_str,
        "randCode": "",
        "purpose_codes": "00" if purpose == "ADULT" else purpose,  # 00=成人票，0X00=学生票
        "key_check_isChange": key_check or "",
        "leftTicketStr": left_ticket_str or "",
        "train_location": train_location or "P3",
        "choose_seats": "",
        "seatDetailType": "000",
        "whatsSelect": "1",
        "roomType": "00",
        "dwAll": "N",
        "_json_att": "",
        "REPEAT_SUBMIT_TOKEN": token,
    }
    r = session.post(url, data=data, timeout=20)
    try:
        d = r.json()
    except Exception:
        return False, "confirmSingleForQueue 返回非 JSON（可能接口变更）: {0}".format(r.text[:300])
    if not d.get("status"):
        return False, "下单失败: {0}".format(d.get("messages", d.get("validateMessages")))
    data_ = d.get("data") or {}
    if data_.get("submitStatus"):
        return True, "订单已提交，处于【未完成订单】状态（未支付，请在 App/网页 45 分钟内支付）"
    return False, "排队未成功: {0}".format(data_)


def _confirm_json_with_retry(session, token, left_ticket_str, key_check, train_location,
                             passenger_ticket_str, old_passenger_str, purpose):
    """confirmSingleForQueue + 非 JSON transient 重试（最多 3 次）。返回 (ok, msg)。

    非 JSON 响应（12306 WAF/网关偶发拦截页）按 transient 处理，重试口径与
    verify_session 一致；网络异常直接抛给调用方（保持旧行为）。
    """
    ok, msg = False, ""
    for _ in range(3):
        ok, msg = confirm_order(session, token, left_ticket_str, key_check,
                                train_location, passenger_ticket_str,
                                old_passenger_str, purpose)
        if ok or not msg.startswith("confirmSingleForQueue 返回非 JSON"):
            return ok, msg
        time.sleep(1.2)
    return ok, msg


def confirm_with_busy_retry(session, token, left_ticket_str, key_check, train_location,
                            passenger_ticket_str, old_passenger_str, tries, delay,
                            purpose="ADULT"):
    """提交排队下单，遇"系统忙"类错误自动重试（间隔递增）。返回 (ok, msg)。"""
    msg = ""
    for attempt in range(1, tries + 1):
        try:
            ok, msg = _confirm_json_with_retry(session, token, left_ticket_str,
                                               key_check, train_location,
                                               passenger_ticket_str,
                                               old_passenger_str, purpose)
        except Exception as e:
            return False, "confirmSingleForQueue 异常: {0}".format(e)
        if ok or not is_busy_error(msg) or attempt >= tries:
            return ok, msg
        time.sleep(delay * attempt)
    return False, msg or "confirmSingleForQueue 连续 {0} 次系统忙，稍后自动重试".format(tries)


def fetch_unpaid_order_no(session, date=None, train_code=None, passenger_names=None,
                          not_before_ts=None):
    """尽力获取「本次提交生成」的未完成订单号（失败不影响主流程）。

    按 not_before_ts 做下单时间归因：只返回 order_ts 落在本次提交窗口内、且
    行程（车次 + 日期 + 乘车人全员命中）匹配的订单。有历史未支付单时不再张冠李戴。
    未传归因参数时退化为旧行为（取第一笔未完成订单）；归因无匹配时返回 None
    （宁可缺省，不给过期单号）。
    """
    try:
        orders = check_existing_orders(session, date or time.strftime("%Y-%m-%d"))
    except Exception:
        return None
    inc = [o for o in orders
           if o.get("_no_complete") and (o.get("order_no") or "")]
    if not inc:
        return None
    if date and train_code and not_before_ts is not None:
        recent = find_recent_order(inc, date, train_code, passenger_names,
                                   not_before_ts)
        return recent.get("order_no") if recent else None
    return inc[0].get("order_no") or None


def _dash_date(s):
    """YYYYMMDD → YYYY-MM-DD。

    ticket["start_date"] 是列车始发日期（p13），格式无横线；submitOrderRequest
    的 train_date 只认 YYYY-MM-DD。非 8 位数字串原样返回（下游接口会报错，
    不在此处引入新崩溃）。
    """
    s = (s or "").strip()
    if len(s) == 8 and s.isdigit():
        return "{0}-{1}-{2}".format(s[:4], s[4:6], s[6:8])
    return s


def order_ticket(config, task, ticket, seat_name):
    """
    完整下单入口。成功返回 (True, 描述, 额外信息)，失败返回 (False, 错误, None)。
    描述中带订单号（尽力获取）。state 写入由调用方负责。

    config["order_mode"] == "browser" 时转交 browser_order，用真实 Edge 下单：
    纯 HTTP 的 confirmSingleForQueue 会被易盾设备指纹 / 阿里云验证拦住
    （服务端伪装成「余票不足 / 系统繁忙」），浏览器才不会。
    """
    if (config.get("order_mode") or "http") == "browser":
        try:
            import browser_order
        except ImportError:
            return False, "已配置 order_mode=browser，但缺少 browser_order 模块", None
        return browser_order.order_ticket_via_browser(config, task, ticket, seat_name)

    sess = load_session(config.get("session_cookies_file", "session_cookies.json"))
    ok, who = check_login(sess)
    if not ok:
        return False, "会话已失效（{0}）。请重新运行 capture_session.py 登录。".format(who), None

    # 席别同价改判（与 browser_order 同一规则）：网页端不下发「无座」，
    # 勾「无座」按同价席别提交（动车组→二等座，普速→硬座）；
    # HTTP 路径此前漏了这一步，导致无座任务必失败
    seat_code, alias_name = order_seat_code(seat_name, None, ticket.get("train_code"))
    if not seat_code:
        return False, "未知席别: {0}".format(seat_name), None

    # 必须用查询时的乘车日期：start_date 是列车始发日期（跨夜车会差一天），
    # 且格式为 YYYYMMDD，而 submitOrderRequest 只接受 YYYY-MM-DD。
    date = ticket.get("query_date") or _dash_date(ticket["start_date"])
    purpose = task.get("purpose_code") or "ADULT"
    # 按人票种：勾选乘车人全是学生票时才用学生余票口径，否则按成人票请求
    purpose_map = task.get("pax_purpose") or {}
    wanted_names = list(task.get("passenger_names") or [])
    if purpose_map and wanted_names:
        codes = [purpose_map.get(n) or purpose for n in wanted_names]
        purpose = "0X00" if all(c == "0X00" for c in codes) else "ADULT"

    # 智能排队机制："系统忙"类错误在单轮内自动重试（次数/间隔可配置），提升抢票成功率
    try:
        tries = max(1, int(config.get("order_retry_times", 3)))
    except (TypeError, ValueError):
        tries = 3
    try:
        delay = float(config.get("order_retry_delay_seconds", 2))
    except (TypeError, ValueError):
        delay = 2.0

    ok1, msg1 = submit_with_busy_retry(sess, ticket, seat_code, date, tries, delay, purpose)
    if not ok1:
        return False, msg1, None

    token = left_str = key_check = None
    try:
        token, left_str, key_check, _ = get_init_dc(sess)
        if not token:
            return False, "initDc 解析失败，拿不到 repeatSubmitToken（接口可能变更）", None
    except Exception as e:
        return False, "initDc 异常: {0}".format(e), None

    try:
        passengers = get_passengers(sess)
        wanted = list(task.get("passenger_names") or [])
        if not wanted:
            # 未在任务里点名时，优先使用本地加密库中的"默认乘车人"
            try:
                import passengers as passengers_mod
                wanted = passengers_mod.default_names(config.get("passengers_file"))
            except Exception:
                wanted = []
        picked = select_passengers(passengers, wanted)
        if not picked:
            return False, "没有可用乘车人（请确认账号已保存常用联系人和乘车人）", None
    except Exception as e:
        return False, "拉取乘车人异常: {0}".format(e), None

    # 防重复下单：检查账号中是否已有同日期 + 同车次 + 同乘车人的订单
    try:
        existing = check_existing_orders(sess, date)
    except Exception as e:
        # 查询失败 = 未知：按防重复保守规则中止本次下单，不拿空列表当"无重复"。
        # 瞬时失败下一轮重试即可；重复提交的代价（重复占座/挡单）远大于少抢一轮。
        return False, ("官方订单查询失败（{0}），按防重复保守规则中止本次下单，"
                       "避免重复提交".format(e)), None
    dup = None
    try:
        dup = find_duplicate(existing, date, ticket["train_code"],
                             [p["name"] for p in picked],
                             from_station=ticket.get("from_name"),
                             to_station=ticket.get("to_name"),
                             seat_name=seat_name)
    except Exception as e:
        print("    [查重异常] {0}（忽略，继续下单）".format(e))
    if dup:
        return (False,
                "账号中已存在相同行程订单（{0} 状态：{1}），按防重复规则跳过".format(
                    ("订单号 " + dup["order_no"]) if dup.get("order_no") else "订单",
                    dup.get("status") or "未知"),
                {"reason": "dup", "order_no": dup.get("order_no")})

    p_ticket_str, old_str = build_ticket_strs(picked, seat_code, purpose_map)

    try:
        ok2, msg2 = check_order_info(sess, token, p_ticket_str, old_str)
        if not ok2:
            return False, msg2, None
    except Exception as e:
        return False, "checkOrderInfo 异常: {0}".format(e), None

    # 本次提交动作的开始时刻：用于把官方未完成订单归因到"本次新建"，
    # 有历史未支付单时不再张冠李戴（成功消息里的单号必须是我们刚建的）。
    not_before_ts = time.time()
    ok3, msg3 = confirm_with_busy_retry(sess, token, left_str, key_check,
                                        ticket["train_location"], p_ticket_str,
                                        old_str, tries, delay, purpose)
    if not ok3:
        return False, msg3, None

    order_no = fetch_unpaid_order_no(
        sess, date=date, train_code=ticket["train_code"],
        passenger_names=[p["name"] for p in picked],
        not_before_ts=not_before_ts)
    passenger_names = "、".join(p["name"] for p in picked)
    extra = {"order_no": order_no, "passengers": passenger_names, "date": date,
             "train": ticket["train_code"], "seat": seat_name,
             "from": ticket["from_name"], "to": ticket["to_name"],
             "start": ticket["start_time"], "arrive": ticket["arrive_time"]}
    msg = msg3 + (" 订单号: {0}".format(order_no) if order_no else "")
    if alias_name:
        # 席别同价改判后如实记账（与 browser_order 同口径）：extra 记改判后席别，
        # 消息里说明勾选席别与实际提交席别，避免显示改判前的"无座"误导用户。
        extra["alias_seat"] = alias_name
        extra["selected_seat"] = seat_name
        msg += "（勾选 %s，同价按 %s 下单）" % (seat_name, alias_name)
    return True, msg, extra


if __name__ == "__main__":
    # 单独运行：先测会话有效性与乘车人列表
    try:
        cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
        s = load_session(cfg.get("session_cookies_file", "session_cookies.json"))
        ok, who = check_login(s)
        print("会话有效: {0}".format(who) if ok else "会话失效: {0}".format(who))
        if ok:
            ps = get_passengers(s)
            print("已保存乘车人 {0} 位：{1}".format(len(ps), "、".join(p["name"] for p in ps if p["name"]) or "无"))
    except Exception as e:
        print("错误: {0}".format(e))