# -*- coding: utf-8 -*-
"""
contract 层示例（tests/contract）：12306 官方响应结构锁定测试。

思路：把真实 queryMyOrder/queryMyOrderNoComplete 的 DTO 形状「录制」为 fixture，
用 order._normalize_order_item 解析并断言关键字段——12306 一旦改了字段名/类型，
本层立即报警（无需真实网络）。这是判定内核与解析库之间的「契约」：

对账（reconcile）/ 影子（shadow）都依赖 _normalize_order_item 输出的统一结构；
字段塌了就全线失效，所以必须有一层盯着。

fixture 形状取自 README/对账任务书记载的 2026-10 实测 DTO：
  start_train_date_page / train_code_page / passengerDTOList /
  array_passser_name_page / tickets[].ticket_status_name / order_date / sequence_no
"""

import unittest

import order as order_mod


# 「录制」自真实 queryMyOrder（G=未出行）响应的一项（脱敏，仅结构）
G_ITEM = {
    "sequence_no": "E123456789012345",
    "train_code_page": "K 123",          # 官方含空格
    "start_train_date_page": "2026-10-20 06:30:00",
    "from_station_name_page": ["长葛"],
    "to_station_name_page": ["确山"],
    "array_passser_name_page": ["张三", "李四", "李四"],
    "passengerDTOList": [
        {"passenger_name": "张三"}, {"passenger_name": "李四"}
    ],
    "tickets": [
        {"ticket_status_name": "已支付", "seat_type_name": "硬座"},
        {"ticket_status_name": "已支付", "seat_type_name": "硬座"},
    ],
    "order_date": "2026-10-19 21:03:12",
}

# 未完成订单（NoComplete）项
NC_ITEM = {
    "sequence_no": "E987654321098765",
    "train_code_page": "K225",
    "start_train_date_page": "2026-10-21 08:00:00",
    "from_station_name_page": "长葛",
    "to_station_name_page": "确山",
    "array_passser_name_page": ["张三"],
    "tickets": [{"ticket_status_name": "未完成/未支付"}],
    "order_date": "2026-10-20 10:00:00",
}


class TestQueryGFixture(unittest.TestCase):
    def test_normalize_g_item(self):
        e = order_mod._normalize_order_item(G_ITEM, "已支付(未出行)")
        self.assertEqual(e["train"], "K123", "车次去空格")
        self.assertEqual(e["date"], "2026-10-20", "乘车日期取 start_train_date_page")
        self.assertEqual(e["status"], "已支付", "票级状态优先于 fallback")
        self.assertEqual(e["seat"], "硬座")
        self.assertEqual(e["order_no"], "E123456789012345")
        self.assertEqual(e["order_ts"] is not None, True, "order_date 可解析")
        self.assertEqual(e["from"], "长葛")
        # array_passser_name_page 与 passengerDTOList 去重合并
        self.assertEqual(sorted(e["passengers"]), ["张三", "李四"])

    def test_normalize_nc_item(self):
        e = order_mod._normalize_order_item(NC_ITEM, "未完成/未支付")
        self.assertEqual(e["status"], "未完成/未支付")
        self.assertEqual(e["order_ts"] is not None, True)
        self.assertEqual(e["date"], "2026-10-21")

    def test_normalize_garbage_fields_do_not_crash(self):
        # 字段缺失/空 → 解析器必须给安全默认，不抛异常
        e = order_mod._normalize_order_item({}, "x")
        self.assertEqual(e["train"], "")
        self.assertEqual(e["date"], "")
        self.assertEqual(e["order_ts"], None)
        self.assertEqual(e["passengers"], [])

    def test_cache_block_item(self):
        e = order_mod._normalize_order_item(
            {"_cache_block": True, "status_text": "今日将不能继续受理"},
            "未完成/未支付")
        self.assertTrue(e.get("_cache_block"))
        self.assertIn("今日", e.get("status", ""))


if __name__ == "__main__":
    unittest.main()