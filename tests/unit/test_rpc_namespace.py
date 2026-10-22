# -*- coding: utf-8 -*-
"""P4-B RPC 命名空间契约测试：旧名 ↔ 命名空间双向一致。"""

import unittest


class TestRpcNamespace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import api
        cls.api = api

    def test_every_legacy_method_has_namespace_alias(self):
        """每个旧名都有唯一的命名空间入口（41 对，一一映射）。"""
        ns = self.api._NS_TO_OLD
        self.assertEqual(len(self.api.METHODS), len(ns))
        self.assertEqual(len(self.api._NS_METHODS), len(ns))
        # 每个旧名恰好映射一次
        olds = list(ns.values())
        self.assertEqual(len(olds), len(set(olds)))

    def test_namespace_resolves_same_fn_as_legacy(self):
        """同名入口（tasks.list vs listMonitorTasks）必须解析到同一处理函数。"""
        for ns_name, old in self.api._NS_TO_OLD.items():
            self.assertIs(self.api._NS_METHODS[ns_name],
                          self.api.METHODS[old],
                          "ns=%s 应== %s" % (ns_name, old))

    def test_dispatch_resolves_namespaced_method(self):
        """rpc_dispatch 接受命名空间方法（用无害方法回包验证）。"""
        api = self.api
        replies = []
        orig = api.reply
        api.reply = lambda id_, result=None, error=None: replies.append(
            (id_, result, error))
        try:
            api.rpc_dispatch('{"id": 1, "method": "settings.get", "params": {}}')
        finally:
            api.reply = orig
        self.assertTrue(replies)
        self.assertIsNone(replies[0][2], "不应有 error")

    def test_unknown_namespace_still_rejected(self):
        api = self.api
        replies = []
        orig = api.reply
        api.reply = lambda id_, result=None, error=None: replies.append(
            (id_, result, error))
        try:
            api.rpc_dispatch('{"id": 2, "method": "nope.missing", "params": {}}')
        finally:
            api.reply = orig
        self.assertIn("未知方法", replies[0][2] or "")


if __name__ == "__main__":
    unittest.main()