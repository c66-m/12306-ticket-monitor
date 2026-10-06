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
import os
import re
import sys
import time
from urllib.parse import unquote, urlencode

import requests

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


def load_session(cookie_path=None):
    """加载 saved 会话 Cookie，返回 requests.Session。

    必须保留服务端下发的 domain/path：12306 的 Cookie 有严格作用域
    （_uab_collina 只属于 /otn/resources、_passport_session 只属于 /passport），
    拍平成「全局 Cookie」会让每个接口收到本不该出现的 Cookie，属明显的非浏览器特征。
    旧格式（纯 name->value）仍兼容，回落到原来的全局作用域。"""
    path = cookie_path or os.path.join(HERE, "session_cookies.json")
    if not os.path.exists(path):
        raise RuntimeError("未找到会话文件 {0}，请先运行：python capture_session.py".format(path))
    with open(path, "r", encoding="utf-8") as f:
        cookies = json.load(f)

    s = requests.Session()
    s.headers.update(BASE_HEADERS)
    for name, val in cookies.items():
        if isinstance(val, dict):  # 新格式：带原始作用域
            s.cookies.set(name, val.get("value", ""),
                          domain=val.get("domain") or ".12306.cn",
                          path=val.get("path") or "/")
        else:                      # 旧格式：只有值
            s.cookies.set(name, val, domain=".12306.cn", path="/")
    return s


def save_session(session, cookie_path=None):
    """把会话 Cookie 回写文件。12306 会轮换 tk 等关键 Cookie，
    定期回写可延长会话有效期（避免一直用旧 Cookie 被判定过期）。"""
    path = cookie_path or os.path.join(HERE, "session_cookies.json")
    cookies = {}
    for c in session.cookies:
        if "12306.cn" in getattr(c, "domain", "") and c.name:
            cookies[c.name] = {
                "value": c.value,
                "domain": c.domain,
                "path": c.path or "/",
            }
    if not cookies:
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


CHECK_URL = "https://kyfw.12306.cn/otn/index/initMy12306Api"


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


def submit_with_busy_retry(session, ticket, seat_code, date, tries, delay, purpose="ADULT"):
    """提交订单请求，遇"系统忙"类错误自动重试（间隔递增）。返回 (ok, msg)。"""
    for attempt in range(1, tries + 1):
        try:
            resp = submit_order_request(session, ticket, seat_code, date, purpose)
            d = resp.json()
        except Exception as e:
            raw = getattr(locals().get("resp"), "text", "") or ""
            return False, "submitOrderRequest 异常: {0} 原始响应: {1}".format(e, raw[:300])
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
    start = item.get("start_train_date_page") or ""
    return {
        "order_no": item.get("sequence_no") or item.get("order_no") or "",
        "train": (item.get("train_code_page") or "").replace(" ", ""),
        "from": item.get("from_station_name_page") or "",
        "to": item.get("to_station_name_page") or "",
        "date": start[:10] if start else (item.get("order_date") or "").replace(" ", "")[:10],
        "status": status or item.get("order_status_name_cn") or "",
        "passengers": passengers,
    }


def check_existing_orders(session, target_date):
    """查询账号中的未完成订单 + 目标日期已完成订单，返回统一订单列表（尽力解析）。"""
    orders = []
    # 未完成订单（未支付 + 待支付等）
    try:
        r = session.post("https://kyfw.12306.cn/otn/queryOrder/queryMyOrderNoComplete",
                         data={"_json_att": ""}, timeout=15)
        for item in ((r.json().get("data") or {}).get("orderDBList") or []):
            orders.append(_normalize_order_item(item, "未完成/未支付"))
    except Exception:
        pass
    # 已完成（历史）订单：按目标日期窗口查询
    try:
        data = {"_json_att": "", "queryType": "1",
                "queryStartDate": target_date, "queryEndDate": target_date,
                "come_from_flag": "my_order"}
        r = session.post("https://kyfw.12306.cn/otn/queryOrder/queryMyOrder",
                         data=data, timeout=15)
        for item in ((r.json().get("data") or {}).get("orderDBList") or []):
            orders.append(_normalize_order_item(item, item.get("order_status_name_cn") or "已完成/已支付"))
    except Exception:
        pass
    return orders


def find_duplicate(orders, date, train_code, passenger_names):
    """
    在账号已有订单里查重：同一日期 + 同一车次 + 乘车人有交集 = 重复。
    若订单解析不出乘车人列表，则保守地只按 日期+车次 判定。
    """
    for o in orders:
        if not o["date"] or not o["train"]:
            continue
        if o["date"] == date and o["train"] == train_code:
            if not o["passengers"]:
                return o  # 乘车人解析失败，保守判定为重复
            if not passenger_names or set(passenger_names) & set(o["passengers"]):
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


def confirm_with_busy_retry(session, token, left_ticket_str, key_check, train_location,
                            passenger_ticket_str, old_passenger_str, tries, delay,
                            purpose="ADULT"):
    """提交排队下单，遇"系统忙"类错误自动重试（间隔递增）。返回 (ok, msg)。"""
    msg = ""
    for attempt in range(1, tries + 1):
        try:
            ok, msg = confirm_order(session, token, left_ticket_str, key_check,
                                    train_location, passenger_ticket_str,
                                    old_passenger_str, purpose)
        except Exception as e:
            return False, "confirmSingleForQueue 异常: {0}".format(e)
        if ok or not is_busy_error(msg) or attempt >= tries:
            return ok, msg
        time.sleep(delay * attempt)
    return False, msg or "confirmSingleForQueue 连续 {0} 次系统忙，稍后自动重试".format(tries)


def fetch_unpaid_order_no(session):
    """尽力获取最近一笔未完成订单号（失败不影响主流程）。"""
    try:
        url = "https://kyfw.12306.cn/otn/queryOrder/queryMyOrderNoComplete"
        session.headers["Referer"] = "https://kyfw.12306.cn/otn/confirmPassenger/initDc"
        r = session.post(url, data={"_json_att": ""}, timeout=15)
        data = r.json().get("data") or {}
        for item in data.get("orderDBList") or []:
            if item.get("order_status_name_cn") in ("未完成", ""):
                return item.get("sequence_no") or item.get("order_no")
    except Exception:
        pass
    return None


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
    seat_code, _alias_name = order_seat_code(seat_name, None, ticket.get("train_code"))
    if not seat_code:
        return False, "未知席别: {0}".format(seat_name), None

    # 必须用查询时的乘车日期：start_date 是列车始发日期（跨夜车会差一天），
    # 且格式为 YYYYMMDD，而 submitOrderRequest 只接受 YYYY-MM-DD。
    date = ticket.get("query_date") or ticket["start_date"]
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
    except Exception:
        existing = []
    dup = None
    try:
        dup = find_duplicate(existing, date, ticket["train_code"],
                             [p["name"] for p in picked])
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

    ok3, msg3 = confirm_with_busy_retry(sess, token, left_str, key_check,
                                        ticket["train_location"], p_ticket_str,
                                        old_str, tries, delay, purpose)
    if not ok3:
        return False, msg3, None

    order_no = fetch_unpaid_order_no(sess)
    passenger_names = "、".join(p["name"] for p in picked)
    extra = {"order_no": order_no, "passengers": passenger_names, "date": date,
             "train": ticket["train_code"], "seat": seat_name,
             "from": ticket["from_name"], "to": ticket["to_name"],
             "start": ticket["start_time"], "arrive": ticket["arrive_time"]}
    return True, msg3 + (" 订单号: {0}".format(order_no) if order_no else ""), extra


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