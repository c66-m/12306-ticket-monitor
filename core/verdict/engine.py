# -*- coding: utf-8 -*-
"""
下单判定引擎（core/verdict/engine，仅依赖标准库，零 IO）。

三段式裁决（规划书 P1-7 / P1-8）：
- PreCheck（下单前）：官方快照已有已购/未支付/挡单 → 阻断；查无 → 放行。
- PostConfirm（提交后）：页面证据 / 官方快照 + 收紧时间戳归因 → 判定本次
  提交成败。RETRY_SAFE 必须由调用方用「双读（两次快照均 NONE）」确认后才
  真的重试——本模块只返回单快照结论。

决定集：PROCEED_ORDER / BLOCK_PAID / BLOCK_UNPAID / BLOCK_CONFLICT /
SUCCESS / RETRY_SAFE / HALT_MANUAL。每个决定带人机双读理由与证据。

红线：UNKNOWN/ERROR 永远不映射到「放行/重试」，恒 HALT_MANUAL。
"""

import dataclasses
import enum

from core.domain import masking
from core.state import order_lifecycle as OLC
from core.verdict import classifier
from core.verdict import snapshot_view as sv


class VerdictDecision(enum.Enum):
    PROCEED_ORDER = "proceed_order"    # 官方查无本行程，可安全下单
    BLOCK_PAID = "block_paid"          # 已购（官方确认已支付）
    BLOCK_UNPAID = "block_unpaid"      # 本行程已有未支付订单
    BLOCK_CONFLICT = "block_conflict"  # 其它行程未支付挡路 / 行程冲突
    SUCCESS = "success"                # 本次提交成功（页面证据或官方归因确认）
    RETRY_SAFE = "retry_safe"          # 官方确认本次无单，可安全重试（需双读）
    HALT_MANUAL = "halt_manual"        # 状态不明，停任务交人工，绝不重试
    ERROR = "error"                    # 内部错误


@dataclasses.dataclass(frozen=True)
class Verdict:
    decision: VerdictDecision
    reason: str          # 人类可读（UI/日志直接用）
    evidence: dict       # 机器可读（order_no 等均脱敏）
    confidence: float    # 0.0~1.0

    def is_blocking(self):
        return self.decision in (
            VerdictDecision.BLOCK_PAID, VerdictDecision.BLOCK_UNPAID,
            VerdictDecision.BLOCK_CONFLICT, VerdictDecision.HALT_MANUAL,
            VerdictDecision.ERROR)


_CONF_HIGH = 1.0
_CONF_NORMAL = 0.9
_CONF_LOW = 0.5


def _state_verdict(cls, extra=None):
    """OrderState → Verdict（PreCheck 语义）。"""
    ev = dict(extra or {})
    ev["state"] = OLC.label(cls)
    if cls == OLC.OrderState.PAID:
        return Verdict(VerdictDecision.BLOCK_PAID, "官方确认已购得，任务终态",
                       ev, _CONF_HIGH)
    if cls == OLC.OrderState.UNPAID:
        return Verdict(VerdictDecision.BLOCK_UNPAID, "本行程已有未支付订单，禁止重复下单",
                       ev, _CONF_NORMAL)
    if cls == OLC.OrderState.BLOCKED:
        return Verdict(VerdictDecision.BLOCK_CONFLICT,
                       "其它行程未支付订单挡路，请到 12306 处理", ev, _CONF_NORMAL)
    if cls in (OLC.OrderState.CANCELLED, OLC.OrderState.REFUNDED,
               OLC.OrderState.NONE):
        return Verdict(VerdictDecision.PROCEED_ORDER, "官方查无本行程有效订单，可安全下单",
                       ev, _CONF_NORMAL)
    return Verdict(VerdictDecision.HALT_MANUAL,
                   "平台状态不明（%s），停任务交人工核对" % OLC.label(cls),
                   ev, _CONF_LOW)


def precheck(trip, orders, target_names):
    """下单前预检：官方快照是否已存在「本行程已购/未支付/挡单」。"""
    try:
        cls = classifier.classify_snapshot(trip, orders, target_names)
    except Exception as e:
        return Verdict(VerdictDecision.ERROR, "判定异常: %s" % e,
                       {"exc": str(e)}, 0.0)
    return _state_verdict(cls, {"phase": "precheck"})


def postconfirm(trip, orders, target_names, not_before_ts,
                page_success=False,
                window_lo=sv.RECENT_WINDOW_LO,
                window_hi=sv.RECENT_WINDOW_HI):
    """提交后确认：页面证据优先，其次官方快照 + 收紧时间戳归因。

    page_success: 浏览器侧是否读到「席位已锁定/待付款页」（唯一页面级成功
    信号）。True 直接判 SUCCESS。
    注意 RETRY_SAFE 返回不代表立即重试——调用方必须双读两次均为无单。
    window_lo/window_hi: 归因窗口，默认紧窗；影子 triage 可传现网宽窗重算，
    内核默认行为不变。
    """
    ev = {"phase": "postconfirm", "page_success": page_success,
          "t_submit": round(not_before_ts, 3),
          "window": [window_lo, window_hi]}
    if page_success:
        return Verdict(VerdictDecision.SUCCESS, "页面确认席位已锁定（待付款）",
                       ev, _CONF_HIGH)
    try:
        cls = classifier.classify_snapshot(trip, orders, target_names)
    except Exception as e:
        return Verdict(VerdictDecision.ERROR, "判定异常: %s" % e,
                       {**ev, "exc": str(e)}, 0.0)
    ev["state"] = OLC.label(cls)
    if cls == OLC.OrderState.PAID:
        return Verdict(VerdictDecision.SUCCESS, "官方确认本次行程已支付（他端已付或已成功）",
                       ev, _CONF_HIGH)
    if cls == OLC.OrderState.UNPAID:
        recent = sv.find_recent_order(trip, orders, target_names, not_before_ts,
                                      window_lo, window_hi)
        if recent is not None:
            ev["order_no"] = masking.mask_order_no(recent.get("order_no"))
            ev["order_time"] = recent.get("order_time") or ""
            return Verdict(VerdictDecision.SUCCESS,
                           "官方已生成订单且下单时间落在本次提交窗口内——本次成功",
                           ev, _CONF_NORMAL)
        ev["legacy_unpaid"] = True
        return Verdict(VerdictDecision.BLOCK_UNPAID,
                       "官方存在本行程未支付订单但下单时间早于本次提交（遗留单），禁止重下",
                       ev, _CONF_NORMAL)
    if cls in (OLC.OrderState.NONE, OLC.OrderState.CANCELLED,
               OLC.OrderState.REFUNDED):
        return Verdict(VerdictDecision.RETRY_SAFE,
                       "官方确认本次无单，安全重试（请先双读确认）",
                       ev, _CONF_NORMAL)
    if cls == OLC.OrderState.BLOCKED:
        return Verdict(VerdictDecision.BLOCK_CONFLICT,
                       "其它行程未支付订单挡路，停任务交人工", ev, _CONF_NORMAL)
    return Verdict(VerdictDecision.HALT_MANUAL,
                   "平台状态不明（%s），停任务交人工核对" % OLC.label(cls),
                   ev, _CONF_LOW)


def is_window_edge_divergence(tight_verdict, trip, orders, target_names,
                              not_before_ts, legacy_decision):
    """本次分歧是否纯由归因窗口收紧所致（影子报告 triage 用，纯计算）。

    严格定义：紧窗结论已与现网不一致，且把同一快照按现网宽窗
    [LIVE_WINDOW_LO, LIVE_WINDOW_HI] 重算后结论恰好等于现网结论——
    此时窗口是分歧的唯一解释。宽窗重算仍不一致，或本来就一致，
    都不算边沿，仍按真实分歧处理。
    """
    if tight_verdict.decision.value == legacy_decision:
        return False
    try:
        wide = postconfirm(trip, orders, target_names, not_before_ts,
                           window_lo=sv.LIVE_WINDOW_LO,
                           window_hi=sv.LIVE_WINDOW_HI)
    except Exception:
        return False
    return wide.decision.value == legacy_decision