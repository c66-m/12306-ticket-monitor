# -*- coding: utf-8 -*-
"""R2 存储层修复测试：dualwrite ROOT 路径守卫 / migrate 备份 md5 校验。"""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from adapters.store import db, dualwrite, migrate


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


if __name__ == "__main__":
    unittest.main()
