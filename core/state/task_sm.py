# -*- coding: utf-8 -*-
"""
任务状态机（core/state/task_sm，仅依赖标准库）。

取代旧 state.json 里裸字符串状态（"monitoring"/"retrying"...）的无约束漂移：
状态集合 + 合法迁移表 + fulfilled 终态。fulfilled = 官方确认已购得（或本地
成功且不追单）——对下单管线物理不可达，是「已购状态正确识别并阻止重复购票」
的结构保证，不靠自觉。

与旧字符串的桥接：from_legacy/to_legacy 保兼容（P3 迁移期用）。
"""

import enum


class TaskStatus(enum.Enum):
    MONITORING = "monitoring"   # 监控轮询中
    COOLING = "cooling"         # 系统忙冷却（到期自动回 monitoring）
    RETRYING = "retrying"       # 失败退避重试中
    PAUSED = "paused"           # 用户暂停（可恢复）
    SUCCESS = "success"         # 已成功下单（终态）
    FAILED = "failed"           # 失败/停止（可手动恢复）
    FULFILLED = "fulfilled"     # 已购得（终态，物理拦截下单）
    AMBIGUOUS = "ambiguous"     # 结果未知待核对（终态，停任务交人工核对）


# 合法迁移表：from -> set(to)。表外迁移一律抛 IllegalTransition。
_TRANSITIONS = {
    TaskStatus.MONITORING: {TaskStatus.COOLING, TaskStatus.RETRYING,
                            TaskStatus.PAUSED, TaskStatus.SUCCESS,
                            TaskStatus.FAILED, TaskStatus.FULFILLED,
                            TaskStatus.AMBIGUOUS},
    TaskStatus.COOLING: {TaskStatus.MONITORING, TaskStatus.PAUSED,
                         TaskStatus.SUCCESS, TaskStatus.FAILED,
                         TaskStatus.FULFILLED, TaskStatus.AMBIGUOUS},
    TaskStatus.RETRYING: {TaskStatus.MONITORING, TaskStatus.COOLING,
                          TaskStatus.PAUSED, TaskStatus.SUCCESS,
                          TaskStatus.FAILED, TaskStatus.FULFILLED,
                          TaskStatus.AMBIGUOUS},
    TaskStatus.PAUSED: {TaskStatus.MONITORING, TaskStatus.SUCCESS,
                        TaskStatus.FAILED, TaskStatus.FULFILLED,
                        TaskStatus.AMBIGUOUS},
    # 终态：不允许任何迁移（fulfilled 尤其：已购得的任务不允许回到下单路径）
    TaskStatus.SUCCESS: set(),
    TaskStatus.FAILED: {TaskStatus.MONITORING},  # 用户手动恢复才允许
    TaskStatus.FULFILLED: set(),
    TaskStatus.AMBIGUOUS: {TaskStatus.MONITORING},  # 人工核对后手动恢复才允许（同 FAILED）
}

_TERMINAL = {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.FULFILLED,
             TaskStatus.AMBIGUOUS}


class IllegalTransition(ValueError):
    """非法状态迁移。"""


def is_terminal(s):
    return _norm(s) in _TERMINAL


def is_order_blocked(s):
    """该状态下是否禁止进入下单管线。fulfilled 唯一物理拦截；其余终态也拦截。"""
    return _norm(s) in _TERMINAL


def can_transition(frm, to):
    frm, to = _norm(frm), _norm(to)
    return to in _TRANSITIONS.get(frm, set())


def transition(frm, to, task_name=""):
    """执行迁移（纯函数，返回 to）。非法迁移抛 IllegalTransition。"""
    frm, to = _norm(frm), _norm(to)
    if to not in _TRANSITIONS.get(frm, set()):
        raise IllegalTransition(
            "任务%s 非法状态迁移 %s -> %s"
            % (task_name or "?", frm.value, to.value))
    return to


def _norm(s):
    if isinstance(s, TaskStatus):
        return s
    # 字符串经兼容映射（运行时解析，_LEGACY 已在模块加载期就绪）
    return from_legacy(s)


# 兼容旧字符串（state.json 存量数据）
_LEGACY = {
    "monitoring": TaskStatus.MONITORING,
    "cooling": TaskStatus.COOLING,
    "retrying": TaskStatus.RETRYING,
    "paused": TaskStatus.PAUSED,
    "success": TaskStatus.SUCCESS,
    "failed": TaskStatus.FAILED,
    "fulfilled": TaskStatus.FULFILLED,
    "ambiguous": TaskStatus.AMBIGUOUS,
}


def from_legacy(value):
    """旧字符串 → 枚举；未知值回 PAUSED（保守，不进入自动下单路径）。"""
    if isinstance(value, TaskStatus):
        return value
    return _LEGACY.get((value or "").strip().lower(), TaskStatus.PAUSED)


def to_legacy(s):
    return _norm(s).value