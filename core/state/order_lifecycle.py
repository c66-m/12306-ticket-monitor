# -*- coding: utf-8 -*-
"""
订单生命周期状态（core/state，仅依赖标准库）。

分类状态全集与「阻止下单」语义的单一权威。取代 order.py 里散落的分支
（"支付" in st 这类子串判定）——分类文本→枚举的映射在 core/verdict/classifier，
本模块只定义状态本身、语义与迁移。

状态语义（对「本行程」而言）：
- NONE            官方快照里查无此行程（可安全下单/重试）
- UNPAID          本行程有未支付订单（12306 挡新单；要么本次生成要么遗留）
- PAID            本行程已支付（已购到，绝对阻止再下）
- CANCELLED       本行程已取消（可重新抢票）
- REFUNDED        本行程已退票（可重新抢票）
- TRAVEL_CONFLICT 行程冲突（12306 明确拒：同人同日已购另一行程）
- BLOCKED         存在非本行程的未完成订单挡路（不是本行程的问题，是账号级阻塞）
- UNKNOWN         平台状态不明（保守停任务，绝不当成 NONE 放行重试）
- ERROR           查询失败（保守停任务）

BLOCKING 集合：已购或疑似已购，任何时刻都禁止再下单。
"""

import enum


class OrderState(enum.Enum):
    NONE = "none"
    UNPAID = "unpaid"
    PAID = "paid"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"
    TRAVEL_CONFLICT = "travel_conflict"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"
    ERROR = "error"


# 阻止下单的状态：已购=PAID；有未支付=UNPAID（本次生成或遗留都挡）；
# 行程冲突=明确拒单。BLOCKED 是账号级阻塞也阻止（交人工处理）。
BLOCKING = frozenset({
    OrderState.PAID, OrderState.UNPAID,
    OrderState.TRAVEL_CONFLICT, OrderState.BLOCKED,
})

# 允许（安全）重新下单的状态：查无此单 / 已取消 / 已退票
PROCEED = frozenset({OrderState.NONE, OrderState.CANCELLED, OrderState.REFUNDED})


def is_blocking(state):
    """该状态是否阻止继续下单。"""
    return state in BLOCKING


def can_proceed(state):
    """该状态是否允许安全下单。"""
    return state in PROCEED


# 人类可读标签（UI / 日志共用一份，别各写各的）
LABELS = {
    OrderState.NONE: "官方无此订单",
    OrderState.UNPAID: "存在未支付订单(本行程)",
    OrderState.PAID: "已支付(已购得)",
    OrderState.CANCELLED: "已取消",
    OrderState.REFUNDED: "已退票",
    OrderState.TRAVEL_CONFLICT: "行程冲突",
    OrderState.BLOCKED: "其它行程未支付订单挡路",
    OrderState.UNKNOWN: "平台状态不明",
    OrderState.ERROR: "官方查询失败",
}

# 旧分类字符串（order.py classify_order_status / 对账）→ 枚举，兼容存量数据
_LEGACY_MAP = {
    "none": OrderState.NONE,
    "unpaid": OrderState.UNPAID,
    "paid": OrderState.PAID,
    "cancelled": OrderState.CANCELLED,
    "refunded": OrderState.REFUNDED,
    "blocked": OrderState.BLOCKED,
    "travel_conflict": OrderState.TRAVEL_CONFLICT,
    "unknown": OrderState.UNKNOWN,
    "error": OrderState.ERROR,
}


def from_legacy(value):
    """旧分类字符串/枚举 → OrderState；非法值回 UNKNOWN（保守）。"""
    if isinstance(value, OrderState):
        return value
    return _LEGACY_MAP.get((value or "").strip().lower(), OrderState.UNKNOWN)


def label(state):
    """人类可读中文标签。"""
    try:
        s = OrderState(state) if not isinstance(state, OrderState) else state
    except ValueError:
        return "未知状态:%r" % (state,)
    return LABELS.get(s, s.value)