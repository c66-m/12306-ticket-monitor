# -*- coding: utf-8 -*-
"""R2 存储层修复测试：dualwrite ROOT 路径守卫 / migrate 备份 md5 校验。"""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from adapters.store import db, dualwrite, migrate, repo
from adapters.railway import snapshot as snapshot_mod
from tools import shadow_report


class TestDualwriteRoot(unittest.TestCase):
    def test_root_matches_db_here(self):
        # R2-3：ROOT 必须与 db.HERE 同为仓库根（三层 dirname）
        self.assertEqual(dualwrite.ROOT, db.HERE)
        self.assertEqual(dualwrite.DATA_DIR, os.path.join(db.HERE, "data"))

    def test_is_data_target_without_env_redirect(self):
        # R2-3 守卫：不设 T12306_DATA_DIR 时，仓库根 data/ 与根目录本身
        # 都应被认作正规数据位置（旧实现 ROOT 少剥一层恒返回 False）
        with mock.patch.dict(os.environ):
            os.environ.pop("T12306_DATA_DIR", None)
            self.assertTrue(dualwrite._is_data_target(
                os.path.join(db.HERE, "data", "state.json")))
            self.assertTrue(dualwrite._is_data_target(
                os.path.join(db.HERE, "state.json")))
            self.assertFalse(dualwrite._is_data_target(
                os.path.join(tempfile.gettempdir(), "state.json")))


class TestMigrateBackupVerify(unittest.TestCase):
    def _write_sources(self, d):
        sp = os.path.join(d, "state.json")
        op = os.path.join(d, "orders.json")
        hp = os.path.join(d, "order_history.json")
        with open(sp, "w", encoding="utf-8") as f:
            json.dump({"tasks": {"t1": {"status": "monitoring"}}}, f)
        with open(op, "w", encoding="utf-8") as f:
            json.dump({"orders": {}}, f)
        with open(hp, "w", encoding="utf-8") as f:
            json.dump([], f)
        return sp, op, hp

    def test_truncated_backup_aborts_migrate(self):
        # R2-8：备份复制被截断（md5 不一致）→ 中止报错，DB 不得被替换
        with tempfile.TemporaryDirectory() as d:
            sp, op, hp = self._write_sources(d)
            db_file = os.path.join(d, "app.db")
            real_copy2 = shutil.copy2

            def truncating_copy(src, dst, *a, **k):
                real_copy2(src, dst, *a, **k)
                with open(dst, "r+b") as f:
                    f.truncate(2)  # 造一份被截断的备份

            with mock.patch.object(migrate, "BACKUP_ROOT",
                                   os.path.join(d, "backup")), \
                    mock.patch.object(migrate.shutil, "copy2",
                                      truncating_copy):
                report = migrate.run_migrate(sp, op, hp, db_file=db_file)
            self.assertTrue(report["errors"],
                            "备份校验失败必须中止并报错: %r" % (report,))
            conn = db.connect(db_file)
            try:
                self.assertIsNone(
                    db.meta_get(conn, "last_migrate_fingerprint"),
                    "中止后不得写入迁移指纹（DB 未被替换）")
                n = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                self.assertEqual(n, 0, "中止后 tasks 表不得有迁移数据")
            finally:
                conn.close()

    def test_fingerprint_ignores_formatting(self):
        # 2026-10-25 审：指纹取已加载对象规范 JSON，同一数据换行/缩进/
        # 键序变化不再触发重迁移；数据本身变化则指纹必变。
        a = migrate._source_fingerprint({"tasks": {"t1": {"b": 1, "a": 2}}},
                                        {"orders": {}}, [])
        b = migrate._source_fingerprint({"tasks": {"t1": {"a": 2, "b": 1}}},
                                        {"orders": {}}, [])
        c = migrate._source_fingerprint({"tasks": {"t1": {"a": 3}}},
                                        {"orders": {}}, [])
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_rollback_rejects_corrupt_backup(self):
        # 2026-10-25 审：备份损坏时恢复必须中止，且不得覆盖任何好文件。
        with tempfile.TemporaryDirectory() as d:
            sp, op, hp = self._write_sources(d)
            bd = os.path.join(d, "backup", "20260101-000000-000000")
            os.makedirs(bd)
            for name, content in (("state.json", "{\"tasks\": {}}"),
                                  ("orders.json", "{\"orders\": {}}"),
                                  ("order_history.json", "[{broken")):
                with open(os.path.join(bd, name), "w", encoding="utf-8") as f:
                    f.write(content)
            with self.assertRaises(RuntimeError):
                migrate.rollback(sp, op, hp, backup=bd)
            with open(sp, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["tasks"]["t1"]["status"],
                                 "monitoring")

    def test_intact_backup_migrates(self):
        # 防过修 pin：备份完好时迁移照常完成
        with tempfile.TemporaryDirectory() as d:
            sp, op, hp = self._write_sources(d)
            db_file = os.path.join(d, "app.db")
            with mock.patch.object(migrate, "BACKUP_ROOT",
                                   os.path.join(d, "backup")):
                report = migrate.run_migrate(sp, op, hp, db_file=db_file)
            self.assertEqual(report["errors"], [])
            self.assertEqual(report["tasks"], 1)


class TestRepoHistoryLimit(unittest.TestCase):
    def test_limit_zero_returns_empty(self):
        # 2026-10-25 审：limit=0 旧口径因 falsy 返回全量。
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "order_history.json"), "w",
                      encoding="utf-8") as f:
                json.dump([{"a": 1}, {"a": 2}], f)
            with mock.patch.dict(os.environ, {"T12306_DATA_DIR": d}):
                self.assertEqual(repo.read_history(limit=0), [])
                self.assertEqual(len(repo.read_history(limit=1)), 1)
                self.assertEqual(len(repo.read_history()), 2)


class TestSnapshotFetchCopy(unittest.TestCase):
    def test_fetch_returns_copy(self):
        # 2026-10-25 审：缓存返回共享列表时调用方原地改动会污染缓存。
        class _Sess(object):
            def post(self, *a, **k):
                raise AssertionError("不应直连")

        prov = snapshot_mod.SnapshotProvider(_Sess())
        with mock.patch.object(snapshot_mod.order_mod,
                               "check_existing_orders",
                               return_value=[{"order_no": "E1"}]):
            first = prov.fetch("2026-10-20")
            first.append({"order_no": "E2"})
            second = prov.fetch("2026-10-20")
        self.assertEqual([o["order_no"] for o in second], ["E1"])


class TestShadowReportSince(unittest.TestCase):
    def test_since_filters_monitor_files(self):
        # 2026-10-25 审：--since 旧口径对 monitor 日志恒过（'m' > 'l'）。
        with tempfile.TemporaryDirectory() as d:
            old = os.path.join(d, "monitor_20261001.log")
            new = os.path.join(d, "monitor_20261020.log")
            line = ("[影子比对] dup 2026-10-20 K123 旧=success 新=success "
                    "一致=True\n")
            for fp in (old, new):
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(line)
            infos, _ = shadow_report.collect(since="2026-10-15", log_dir=d)
            # 两文件各 1 条 info；since 过滤后只剩新文件那条
            self.assertEqual(len(infos), 1)


if __name__ == "__main__":
    unittest.main()
