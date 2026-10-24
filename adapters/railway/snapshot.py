# -*- coding: utf-8 -*-
"""
官方订单快照 Provider（adapters/railway/snapshot）。

薄封装 order.check_existing_orders（三列表：未完成/未出行/历史，语义不动），
加同乘车日 60s 结果缓存——主动/影子/对账多条判定链复用同一份快照，既防
12306 限流又保证同一窗口内判定口径一致（消除旧代码「两次查询快照不一致」
导致的归因抖动）。

注意：查询语义仍全部在 order.py（实测资产，规划书 P1-9 只搬不重写，避免
短路回归）。这里只做「会话注入 + 缓存 + 统一出口」。
"""

import threading

from infra import clock
import order as order_mod


class SnapshotProvider:
    """按乘车日期拉取官方订单快照。线程安全，60s 缓存。"""

    def __init__(self, session, ttl=60.0):
        if not hasattr(session, "post"):
            raise TypeError("session 需要 requests.Session（含 .post）")
        self._session = session
        self._ttl = max(0.0, float(ttl))
        self._lock = threading.Lock()
        self._cache = {}  # date -> (fetched_ts, orders)

    def fetch(self, date, force=False):
        """目标乘车日期 → 统一订单列表。查询失败向上抛（调用方按未知处理）。

        force=True：跳过缓存直接打数据源并刷新缓存——双读确认的第二次读
        必须走这条路，否则 60s TTL 内必然同读一份快照，「双读」名存实亡
        （R2-2）。"""
        if not force:
            now = clock.now_ts()
            with self._lock:
                hit = self._cache.get(date)
                if hit and now - hit[0] < self._ttl:
                    # 返回副本：旧口径直接返回缓存列表，调用方一旦改动
                    # （排序/过滤原地操作）即污染缓存，后续比对读到脏
                    # 快照（2026-10-25 审）。条目 dict 仍共享，只防列表级改动。
                    return list(hit[1])
        orders = order_mod.check_existing_orders(self._session, date)
        with self._lock:
            self._cache = {date: (clock.now_ts(), orders)}  # 单条防缓存膨胀
        return list(orders)

    def invalidate(self):
        with self._lock:
            self._cache.clear()