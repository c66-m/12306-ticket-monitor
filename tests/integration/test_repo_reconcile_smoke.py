# -*- coding: utf-8 -*-
"""
integration 冒烟（tests/integration）：repo + 双写 + 对账 + attempts 全链路。

全部在临时目录执行（T12306_DATA_DIR / T12306_APP_DB 重定向），绝不触碰真实
data/。验证「写 JSON → 镜像 SQLite → 对账改判台账 → OrderGate 落 attempts」
这条真实业务链的连通性（用 FakeProvider 代替网络）。
"""

import json
import os
import shutil
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in os.sys.path:
    os.sys.path.insert(0, ROOT)

import appcommon  # noqa: E402
from adapters.store import repo, db  # noqa: E402
from app.reconcile_service import ReconcileRunner  # noqa: E402
from app.order_service import OrderGate  # noqa: E402


def _entry(date="2026-10-20", train="K123", status="已支付(未出行)"):
    return {"order_no": "E1", "train": train, "date": date, "status": status,
            "passengers": ["张三", "李四"], "order_time": "", "order_ts": None,
            "_no_complete": False}


class FakeProvider:
    def __init__(self, *snapshots):
        self._q = list(snapshots)
        self.calls = 0

    def fetch(self, date):
        self.calls += 1
        if not self._q:
            return []
        if len(self._q) > 1:
            return self._q.pop(0)
        return self._q[0]


class TestFullChainSmoke(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="t12306_integ_")
        self._prev_data = os.environ.get("T12306_DATA_DIR")
        self._prev_db = os.environ.get("T12306_APP_DB")
        os.environ["T12306_DATA_DIR"] = self._tmp
        os.environ["T12306_APP_DB"] = os.path.join(self._tmp, "app.db")
        self.orders_path = os.path.join(self._tmp, "orders.json")
        appcommon.atomic_write_json(self.orders_path,
                                    {"orders": {"k": {
                                        "date": "2026-10-20", "train": "K123",
                                        "passengers": ["张三", "李四"],
                                        "classify": "unpaid",
                                        "decision": "旧"}}})

    def tearDown(self):
        for name, val in (("T12306_DATA_DIR", self._prev_data),
                          ("T12306_APP_DB", self._prev_db)):
            if val is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = val
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_reconcile_rewrites_json_and_mirrors_db(self):
        prov = FakeProvider([_entry()])  # 官方：本行程已支付
        writes = []

        def _updater(key, rec):
            writes.append((key, rec))
            appcommon.upsert_order(self.orders_path, key, rec)  # 走镜像

        runner = ReconcileRunner(prov,
                                 loader=repo.read_orders,  # 读 tmp orders.json
                                 updater=_updater)
        rep = runner.reconcile_once()
        self.assertEqual(rep.paid, 1)

        # JSON 台账已改判
        db_obj = json.load(open(self.orders_path, encoding="utf-8"))
        self.assertEqual(db_obj["orders"]["k"]["classify"], "paid")
        # SQLite 镜像同步（同一写入路径）
        conn = db.connect()
        orders_db, _ = db.read_dict_table(conn, "orders")
        conn.close()
        self.assertEqual(orders_db["k"]["classify"], "paid")
        # repo 直读一致性
        self.assertEqual(repo.read_orders()["k"]["classify"], "paid")

    def test_order_gate_writes_attempts(self):
        prov = FakeProvider([_entry()])
        gate = OrderGate(provider=prov)
        v = gate.precheck("2026-10-20", "K123", ["张三", "李四"])
        self.assertEqual(v.decision.value, "block_paid")

        # attempts 台账落表（OrderGate 内部 best-effort 写 SQLite）
        attempts = repo.read_attempts()
        self.assertTrue(attempts, "attempts 应有记录")
        self.assertEqual(attempts[0]["trip"], "2026-10-20|K123|"
                         + attempts[0]["trip"].split("|")[2])
        self.assertEqual(attempts[0]["decision"], "block_paid")
        self.assertGreater(repo.count_attempts(), 0)


if __name__ == "__main__":
    unittest.main()