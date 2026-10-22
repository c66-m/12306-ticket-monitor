# -*- coding: utf-8 -*-
"""
时钟单源（infra 层，仅依赖标准库）。

所有「墙钟 / 时间戳」判定经此进出，未来内核（core/verdict 等）一律通过
依赖注入使用本模块；测试用 freeze / unfreeze 冻结时间，保证判定逻辑可复现。

口径：北京时间（Asia/Shanghai，北京无夏令时，固定 +8 恒等于官方时区）。
Windows 无 tzdata 时退化为固定 +8，退化后结果同样正确。

收敛来源：appcommon.beijing_tz（AppCommon 时代的单源，本模块持真实现，
appcommon 保留同名转发，既有调用点零改动）。
"""

import datetime
import threading
import time

_tz_cache = None
_tz_lock = threading.Lock()


def beijing_tz():
    """北京时间时区对象（lazy 单例）。"""
    global _tz_cache
    if _tz_cache is not None:
        return _tz_cache
    with _tz_lock:
        if _tz_cache is None:
            try:
                from zoneinfo import ZoneInfo
                _tz_cache = ZoneInfo("Asia/Shanghai")
            except Exception:
                _tz_cache = datetime.timezone(
                    datetime.timedelta(hours=8), name="Asia/Shanghai")
        return _tz_cache


# ----------------------------- 冻结支持（仅测试用） -----------------------------
_freeze_lock = threading.Lock()
_frozen = None  # (aware_dt_bj, ts)


def freeze(now=None):
    """冻结时钟：now 为可选的 aware/naive datetime 或 epoch 秒。

    冻结后 now_tz() / now_ts() 恒返回该时刻。用 freeze_context() 更安全
    （自动恢复）；手工 freeze 后必须 unfreeze，否则全进程时钟停滞。
    """
    global _frozen
    if now is None:
        dt = datetime.datetime.now(beijing_tz())
    elif isinstance(now, (int, float)):
        dt = datetime.datetime.fromtimestamp(now, beijing_tz())
    else:
        dt = now
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=beijing_tz())
    with _freeze_lock:
        _frozen = (dt, dt.timestamp())
    return dt


def unfreeze():
    """解除冻结。"""
    global _frozen
    with _freeze_lock:
        _frozen = None


def is_frozen():
    with _freeze_lock:
        return _frozen is not None


def now_tz():
    """当前时刻（北京时区 aware datetime）；冻结时返回冻结时刻。"""
    with _freeze_lock:
        f = _frozen
    if f is not None:
        return f[0]
    return datetime.datetime.now(beijing_tz())


def now_ts():
    """当前时刻 epoch 秒（float）；冻结时返回冻结时刻。"""
    with _freeze_lock:
        f = _frozen
    if f is not None:
        return f[1]
    return time.time()


def now_str(fmt="%Y-%m-%d %H:%M:%S"):
    """当前北京时间墙钟字符串（默认立即读墙钟，不冻结）。"""
    return now_tz().strftime(fmt)


class freeze_context:
    """上下文管理器：with freeze(now): ... 内时钟恒定，退出自动恢复。"""

    def __init__(self, now=None):
        self._now = now
        self._prev = None

    def __enter__(self):
        self._prev = _frozen
        freeze(self._now)
        return self

    def __exit__(self, *exc):
        global _frozen
        with _freeze_lock:
            _frozen = self._prev
        return False


def parse_bj_wall(s):
    """把 12306 的北京时间墙钟串（"YYYY-MM-DD HH:MM:SS"）解析为 epoch 秒。

    显式按北京时间解析，不依赖机器本地时区。解析失败返回 None。
    语义与 order.parse_bj_wall 一致（Task 20 收敛目标，P1 会统一调用点）。
    """
    try:
        dt = datetime.datetime.strptime((s or "")[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None
    return dt.replace(tzinfo=beijing_tz()).timestamp()