# -*- coding: utf-8 -*-
"""
状态分类器（core/verdict/classifier，仅依赖标准库）。

替代 order.py `_classify_orders` 里的「子串包含当映射」（"支付" in st 会命中
"未支付" 的坑）：把 12306 实测状态文案按优先级命中文 → OrderState 枚举映射。
未命中的新文案 = UNKNOWN（绝不当成 NONE 放行重试），比对 12306 原文留证。

映射顺序敏感：先精确长串（"已退票(业务流水号…)"），再一般特征；挡单
cache_block 优先于一切（它不是真订单）。

纯函数：classify_text / classify_entry 可 100% 离线单测。
"""

from core.state.order_lifecycle import OrderState

# 文案 → 状态映射（按顺序比对，先到先得）。键的判定是「包含」语义。
_ORDERED_RULES = [
    # 按出现频率/确定性排序在前
    (("已退票", "退票"), OrderState.REFUNDED),
    (("已取消", "取消"), OrderState.CANCELLED),
    (("已出站", "出站"), OrderState.PAID),
    (("已支付", "已付款"), OrderState.PAID),
    (("未出行",), OrderState.PAID),
    # 未支付系的最强信号要排在"支付"之前（"未支付"含"支付"）
    (("未支付", "待支付", "未完成", "待付款"), OrderState.UNPAID),
    (("支付",), OrderState.PAID),  # 兜底：无"未"前缀的"支付"（如"已支付"被上面吃了）
    (("座位已锁定", "席位已锁定"), OrderState.UNPAID),  # 锁定未付也算 UNPAID 挡单
]

# 通用 CRC 状态串（模拟）
_KNOWN_NON_ORDER_NOISE = ("查看订单处理情况", "请点击“未完成订单”",
                          '请点击"未完成订单"')


def classify_text(text):
    """12306 状态文案 → OrderState。未命中 → UNKNOWN（绝不猜）。"""
    t = (text or "").strip()
    if not t:
        return OrderState.UNKNOWN
    for keys, state in _ORDERED_RULES:
        if any(k in t for k in keys):
            return state
    return OrderState.UNKNOWN


def classify_entry(entry):
    """单条订单条目（order._normalize_order_item 的统一结构）→ OrderState。

    优先处理挡单 cache_block（不是真订单，message 是服务端原文，不参与
    文案识别）；其后按 status 字段文案映射。
    """
    if not isinstance(entry, dict):
        return OrderState.UNKNOWN
    if entry.get("_cache_block"):
        return OrderState.BLOCKED
    return classify_text(entry.get("status") or "")


def _trip_match(trip, entry, target_names):
    """TripKey 与订单条目的身份匹配：车次/日期/区间精确，乘车人全部命中。"""
    if not trip.match_trip_fields(entry.get("train"), entry.get("date"),
                                  entry.get("from"), entry.get("to")):
        return False
    return trip.match_passengers(entry.get("passengers") or [], target_names)


def trip_matched_entries(trip, orders, target_names):
    """从官方快照里挑出与 TripKey 精确命中的条目（车次+日期+乘车人全中）。"""
    return [e for e in (orders or [])
            if isinstance(e, dict) and _trip_match(trip, e, target_names)]


def classify_snapshot(trip, orders, target_names):
    """对整个快照做「本行程」状态分类 → OrderState。

    规则（与 order._classify_orders 语义对齐）：
    1. 未完成订单（_no_complete）中，本行程 → UNPAID；
    2. 挡单（cache_block）→ BLOCKED；非本行程的未完成 → BLOCKED
       （12306 规则：任何未完成都挡新单）；
    3. 本行程的已支付/历史条目按票级状态归类（REFUNDED/CANCELLED 允许重抢）；
    4. 查无本行程 → NONE。

    顺序说明（2026-10-25 审）：旧口径挡单优先于行程匹配，与现网
    order._classify_orders（先行程匹配、再判挡路）相反——本行程已有
    未支付订单时旧口径报账号级 BLOCKED，掩盖本行程真相；现改回
    行程优先。单条目 classify_entry 内的 cache_block 优先不受影响
    （那是单条目文案识别，不是快照规则顺序）。
    """
    orders = list(orders or [])
    # 1. 未完成订单：本行程优先
    inc = _no_complete_entries(orders)
    for e in inc:
        if _trip_match(trip, e, target_names):
            return OrderState.UNPAID
    # 2. 挡单封禁（12306 当日取消次数过多等）与非本行程未完成
    for e in inc:
        if e.get("_cache_block") or e.get("status", "").find("受理") >= 0:
            return OrderState.BLOCKED
    if inc:
        # 12306 规则：任何未完成都挡新单；非本行程的是账号级阻塞
        return OrderState.BLOCKED
    # 3. 本行程已完成/历史条目。带时间戳时以 order_ts 最新的条目定状态：
    # 旧口径跳过 UNKNOWN 取后续确定态，旧 PAID 条目 + 更新 UNKNOWN 条目
    # 时会误判成功（危险方向）；最新态未知即判 UNKNOWN（保守停）。
    # 无时间戳时保留旧扫描口径（R2-7：靠前的未识别文案不短路后面的
    # 已支付条目）。
    matched = trip_matched_entries(trip, orders, target_names)
    stamped = [e for e in matched
               if isinstance(e.get("order_ts"), (int, float))]
    if stamped:
        # 最新时间戳组内扫描：组内先扫明确态（R2-7 在等时间戳时仍成立，
        # max() 取首条会把后置 PAID 判成 UNKNOWN）；组内无明确态但有
        # UNKNOWN 时判 UNKNOWN（最新态不明，保守停）。
        max_ts = max(e["order_ts"] for e in stamped)
        group = [e for e in stamped if e["order_ts"] == max_ts]
        for e in group:
            st = classify_entry(e)
            if st in (OrderState.PAID, OrderState.REFUNDED,
                      OrderState.CANCELLED):
                return st
        if any(classify_entry(e) == OrderState.UNKNOWN for e in group):
            return OrderState.UNKNOWN
        # 组内只有 UNPAID/BLOCKED 等非规则态：落到下方全量旧扫描。
    unknown_seen = False
    for e in matched:
        st = classify_entry(e)
        if st in (OrderState.PAID, OrderState.REFUNDED, OrderState.CANCELLED):
            return st
        if st == OrderState.UNKNOWN:
            unknown_seen = True
    if unknown_seen:
        return OrderState.UNKNOWN
    # 4. 查无本行程
    return OrderState.NONE


def _no_complete_entries(orders):
    return [e for e in orders if e.get("_no_complete")]