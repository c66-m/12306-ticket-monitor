# -*- coding: utf-8 -*-
"""
进程内事件总线（infra 层，仅依赖标准库）。

P4 前端推送与 app/ 层事件驱动（订单事件→通知）都走这里：任务状态变化、
订单生命周期迁移、判定结论对外广播，UI/通知/日志各自订阅，实现解耦。

设计点（先按最小可用，P4 再按需扩展）：
- 同步 dispatch：emit 在调用线程串行回调，保证同一事件的订阅者看到
  一致的时序；listener 抛异常不传染（隔离并告警）。
- tag 用点分命名空间（"task.state_changed" / "order.paid"），易于过滤。
- 线程安全：订阅表加锁，emit 时对快照迭代（允许回调中再订阅/退订）。
"""

import logging
import threading

LOG = logging.getLogger("infra.eventbus")


class EventBus:
    def __init__(self):
        self._lock = threading.RLock()
        self._handlers = {}  # tag -> set[fn]

    def subscribe(self, tag, fn):
        """订阅 tag（点分字符串）。fn(tag, payload)。同一 fn 重复订阅去重。"""
        with self._lock:
            self._handlers.setdefault(tag, set()).add(fn)
        return fn

    def unsubscribe(self, tag, fn):
        with self._lock:
            hs = self._handlers.get(tag)
            if hs is not None:
                hs.discard(fn)
                if not hs:
                    self._handlers.pop(tag, None)

    def emit(self, tag, payload=None):
        """同步派发。任何 listener 异常只记日志，不阻断其它订阅者。"""
        with self._lock:
            hs = tuple(self._handlers.get(tag, ()))
        if not hs:
            return
        for fn in hs:
            try:
                fn(tag, payload)
            except Exception as e:  # noqa: BLE001 隔离订阅者异常，总线自身不能崩
                LOG.warning("[eventbus] 订阅者异常（tag=%s %s）：%s",
                            tag, getattr(fn, "__qualname__", fn), e)

    def clear(self):
        with self._lock:
            self._handlers.clear()


# 全局唯一实例（应用级）
BUS = EventBus()


def subscribe(tag, fn):
    return BUS.subscribe(tag, fn)


def unsubscribe(tag, fn):
    BUS.unsubscribe(tag, fn)


def emit(tag, payload=None):
    BUS.emit(tag, payload)