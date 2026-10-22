# -*- coding: utf-8 -*-
"""
对账守护（app/reconcile_service，P3-5）。

把本地台账与官方订单快照对齐（吸收「对账功能实施任务书」语义，改用新判定
内核）：扫 orders.json 台账里的待决记录（unpaid/unknown/error），用官方三列表
快照 + core 分类器重新判定，写回 classify/decision，并广播顺序相关事件
（order.paid / order.still_unpaid 供 notify_service 与 UI 消费）。

原则：
- 只读官方快照 + 只更新台账标注，**不下单、不改任务状态**——任务状态联动
  （fulfilled 终态）在 P3 第二切片随影子转正接入。
- 同一乘车日集中一次 fetch（复用 SnapshotProvider 60s 缓存，防限流）。
- 对账失败只告警不中断——单条失败不影响其它记录。
"""

import dataclasses
import logging

from core.domain.tripkey import TripKey
from core.state import order_lifecycle as OLC
from core.verdict import classifier
from adapters.store import repo

LOG = logging.getLogger("monitor")

# 需要复核的记录 classify 集合（收尾态 paid/cancelled/none 不重复动）
PENDING_CLASSIFIES = {"unpaid", "unknown", "error"}


@dataclasses.dataclass
class ReconcileReport:
    total: int = 0
    pending: int = 0
    updated: int = 0
    paid: int = 0
    none: int = 0
    still_unpaid: int = 0
    skipped_no_pax: int = 0
    errors: int = 0

    def summary(self):
        return ("总记录=%d 待决=%d 已更新=%d (paid=%d none=%d 仍待付=%d "
                "空乘车人跳过=%d 失败=%d)"
                % (self.total, self.pending, self.updated, self.paid,
                   self.none, self.still_unpaid, self.skipped_no_pax,
                   self.errors))


_UNSET = object()

class ReconcileRunner:
    def __init__(self, provider, loader=None, updater=_UNSET, emit=True):
        """provider: SnapshotProvider（官方快照，60s 缓存）。
        loader: () -> orders dict {key: rec}（默认 repo.read_orders）。
        updater: (key, rec) -> None。**必传**——对账会改写真实台账，默认
                （未传/传 None）一律按 dry-run：只判定、不落盘、不镜像。
                显式传入 updater 才写入（tools/reconcile.py 传真实落盘者）。
        emit: 是否广播 eventbus（测试可关，只留审计）。"""
        self._provider = provider
        self._loader = loader or repo.read_orders
        self._emit = emit
        # fail-safe：不显式给写入者就绝不写真实数据
        self._updater = updater if updater is not _UNSET else None

    def reconcile_once(self, force=False):
        """对账一轮。返回 ReconcileReport。"""
        report = ReconcileReport()
        orders_db = self._loader()
        recs = orders_db if isinstance(orders_db, dict) else {}
        # 按乘车日期分组 → 每日期只 fetch 一次快照
        by_date = {}
        for key, rec in recs.items():
            report.total += 1
            cls = rec.get("classify") or "none"
            if not force and cls not in PENDING_CLASSIFIES:
                continue
            report.pending += 1
            by_date.setdefault(str(rec.get("date") or ""), []).append((key, rec))
        for date, items in by_date.items():
            try:
                snapshot = self._provider.fetch(date)
            except Exception as e:
                LOG.warning("[对账] %s 快照查询失败，本组跳过：%s", date, e)
                report.errors += len(items)
                continue
            for key, rec in items:
                self._reconcile_one(key, rec, snapshot, report)
        return report

    def _reconcile_one(self, key, rec, snapshot, report):
        try:
            pax = rec.get("passengers") or []
            if not pax:
                report.skipped_no_pax += 1
                return
            trip = TripKey.from_task(str(rec.get("date") or ""),
                                     str(rec.get("train") or ""), pax)
            cls = classifier.classify_snapshot(trip, snapshot, pax)
            decision = OLC.label(cls)
            new_rec = dict(rec)
            new_rec["classify"] = cls.value
            new_rec["official_status"] = decision
            new_rec["decision"] = "对账:%s" % decision
            new_rec["last_check"] = _now_str()
            if self._updater:
                self._updater(key, new_rec)
                report.updated += 1
            if cls == OLC.OrderState.PAID:
                report.paid += 1
                self._fire("order.paid", key, cls, decision)
            elif cls == OLC.OrderState.UNPAID:
                report.still_unpaid += 1
                self._fire("order.still_unpaid", key, cls, decision)
            else:
                report.none += 1
                self._fire("order.reconciled", key, cls, decision)
        except Exception as e:
            report.errors += 1
            LOG.warning("[对账] 单条失败 key=%s: %s", key, e)

    def _fire(self, tag, key, cls, decision):
        if not self._emit:
            return
        try:
            from infra import eventbus
            eventbus.emit(tag, {"key": key, "classify": cls.value,
                                "decision": decision})
        except Exception:
            pass


def _now_str():
    from infra.clock import now_str
    return now_str()