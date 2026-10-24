# -*- coding: utf-8 -*-
"""
下单服务编排（app/order_service，规划书 P1-8）。

三段闸的连接件：PreCheck（下单前）→（浏览器执行，P2 接入）→ PostConfirm
（提交后）。本模块只做**纯编排**：拿 provider 快照喂 core/verdict，产出
Verdict；attempt 台账记录写 attempts 表；浏览器 DOM 与付款页证据采集在
P2 的 adapters/browser 接入。

关键纪律（双读）：RETRY_SAFE 只有在「连续两次官方快照都确认无单」时才
采纳——single 返回值永远不直接驱动重试。
"""

import time as _time

from core.domain import tripkey as tripkey_mod
from core.verdict import engine as verdict_engine
from core.verdict import snapshot_view as sv
from adapters.store import db


class OrderGate:
    """三段闸编排。provider: SnapshotProvider（官方快照）。"""

    def __init__(self, provider=None, attempt_store=None):
        self._provider = provider
        self._attempt_store = attempt_store  # 未注入则 attempt 只记内存

    # ------------------------- PreCheck（下单前） -------------------------
    def precheck(self, date, train_code, passenger_names,
                 from_station="", to_station=""):
        """下单前预检 → Verdict。BLOCK_* / HALT 直接终止本轮。
        from_station/to_station 为任务区间（拿不到时留空，匹配不收窄）。"""
        trip = tripkey_mod.TripKey.from_task(
            date, train_code, passenger_names,
            from_station=from_station, to_station=to_station)
        if self._provider is None:
            return verdict_engine.Verdict(
                verdict_engine.VerdictDecision.ERROR,
                "未注入 SnapshotProvider，无法预检", {}, 0.0)
        orders = self._provider.fetch(date)
        v = verdict_engine.precheck(trip, orders, passenger_names)
        self._record_attempt(trip, "precheck", v, len(orders))
        return v

    # ------------------------- PostConfirm（提交后） -------------------------
    def postconfirm(self, date, train_code, passenger_names, not_before_ts,
                    page_success=False, from_station="", to_station=""):
        """提交后单快照确认（归因窗口已收紧）。RETRY_SAFE 需再双读确认。"""
        trip = tripkey_mod.TripKey.from_task(
            date, train_code, passenger_names,
            from_station=from_station, to_station=to_station)
        if self._provider is None:
            return verdict_engine.Verdict(
                verdict_engine.VerdictDecision.ERROR,
                "未注入 SnapshotProvider，无法确认", {}, 0.0)
        orders = self._provider.fetch(date)
        v = verdict_engine.postconfirm(
            trip, orders, passenger_names, not_before_ts,
            page_success=page_success)
        if v.decision == verdict_engine.VerdictDecision.SUCCESS:
            self._record_attempt(trip, "postconfirm", v, len(orders))
        return v

    def double_check_retry_safe(self, date, train_code, passenger_names,
                                not_before_ts, gap_seconds=5.0,
                                from_station="", to_station=""):
        """双读确认：连续两次官方快照均判 RETRY_SAFE 才返回 RETRY_SAFE。

        第一次快照 NONE → 等 gap 秒（让 12306 落库延迟过期）→ 第二次快照
        归因。任一次出现非 RETRY_SAFE 结论，采用那个更保守的结论。
        第二读必须 force 绕过 provider 缓存（R2-2）：gap 默认 5s（旧 2s
        小于落库延迟上界，第二读可能仍读到落库前的空快照，假确认；
        2026-10-25 审）仍远小于快照 60s TTL。
        """
        trip = tripkey_mod.TripKey.from_task(
            date, train_code, passenger_names,
            from_station=from_station, to_station=to_station)
        if self._provider is None:
            return verdict_engine.Verdict(
                verdict_engine.VerdictDecision.ERROR,
                "未注入 SnapshotProvider，无法双读确认", {}, 0.0)
        v1 = verdict_engine.postconfirm(
            trip, self._provider.fetch(date), passenger_names, not_before_ts)
        if v1.decision != verdict_engine.VerdictDecision.RETRY_SAFE:
            return v1
        _time.sleep(max(0.0, float(gap_seconds)))
        orders2 = self._provider.fetch(date, force=True)
        v2 = verdict_engine.postconfirm(trip, orders2, passenger_names,
                                        not_before_ts)
        if v2.decision != verdict_engine.VerdictDecision.RETRY_SAFE:
            return v2
        return v2

    # ------------------------- attempt 台账 -------------------------
    def _record_attempt(self, trip, phase, verdict, snapshot_len):
        """写 attempts 表（best-effort）。attempt 含 TripKey 复合键 + 判定结论。"""
        rec = {
            "attempt_id": "%s-%s-%s" % (trip.composite(), phase,
                                        int(_time.time() * 1000)),
            "trip": trip.composite(),
            "date": trip.date, "train": trip.train,
            "phase": phase,
            "decision": verdict.decision.value,
            "reason": verdict.reason,
            "confidence": verdict.confidence,
            "snapshot_len": snapshot_len,
            "ts": _time.time(),
        }
        try:
            conn = db.get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO attempts (key, payload, updated_at)"
                " VALUES (?,?,?)",
                (rec["attempt_id"], __import__("json").dumps(
                    rec, ensure_ascii=False), rec["ts"]))
            conn.commit()
        except Exception:
            pass  # 台账失败绝不影响下单判定


__all__ = ["OrderGate"]