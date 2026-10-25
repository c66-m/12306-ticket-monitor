# -*- coding: utf-8 -*-
"""影子报告窗口边沿 triage 单测（建议清单④）。

工具只读日志：collect()/main() 的 log_dir 参数指向临时目录，不碰真实 logs/。
"""

import importlib.util
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_SPEC = importlib.util.spec_from_file_location(
    "shadow_report", os.path.join(_ROOT, "tools", "shadow_report.py"))
sr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sr)

_AGREE = ("[影子比对] dup 2026-10-25 K123 旧=success 新=success "
          "一致=True 快照=1 条(已支付0 未完成0)")
_EDGE_INFO = ("[影子比对] dup 2026-10-25 K123 旧=success 新=block_unpaid "
              "一致=False 窗口边沿 快照=1 条(已支付0 未完成1)")
_EDGE_DIFF = ("[影子比对·分歧] dup 2026-10-25 K123 旧=success 新=block_unpaid "
              "窗口边沿 新理由=官方存在本行程未支付订单但下单时间早于本次提交"
              "（遗留单），禁止重下")
_REAL_INFO = ("[影子比对] dup 2026-10-25 G456 旧=success 新=block_conflict "
              "一致=False 快照=1 条(已支付0 未完成1)")
_REAL_DIFF = ("[影子比对·分歧] dup 2026-10-25 G456 旧=success 新=block_conflict "
              "新理由=其它行程未支付订单挡路，停任务交人工")
# 与边沿条目同 key 的旧格式分歧（无标记）：去重键含边沿标志，故应另计一条真实分歧
_OLD_FORMAT_DIFF = ("[影子比对·分歧] dup 2026-10-25 K123 旧=success "
                    "新=block_unpaid 新理由=官方存在本行程未支付订单但下单时间"
                    "早于本次提交（遗留单），禁止重下")


class TestShadowReportTriage(unittest.TestCase):
    def _tmp_log(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with open(os.path.join(tmp.name, "monitor_20261025.log"),
                  "w", encoding="utf-8") as f:
            f.write(text)
        return tmp.name

    def test_collect_splits_window_edge(self):
        d = self._tmp_log("\n".join([
            _AGREE, _EDGE_INFO, _EDGE_DIFF, _EDGE_DIFF,  # 边沿重复行去重为 1
            _REAL_INFO, _REAL_DIFF, _OLD_FORMAT_DIFF]) + "\n")
        infos, diffs = sr.collect(log_dir=d)
        self.assertEqual(len(infos), 3)
        self.assertEqual(sum(1 for g in infos if g[6]), 1)  # 仅边沿行带标记
        real, edge = sr.split_diffs(diffs)
        self.assertEqual(len(edge), 1)
        self.assertEqual(len(real), 2)  # G456 真实 + 旧格式 K123（同 key 另计）

    def test_main_exit_code_gate_preserved(self):
        # 仅窗口边沿分歧：仍返回 1（门禁不放行），但输出单列边沿
        d = self._tmp_log("\n".join([_EDGE_INFO, _EDGE_DIFF]) + "\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = sr.main([], log_dir=d)
        self.assertEqual(rc, 1)
        self.assertIn("窗口边沿", buf.getvalue())

    def test_main_zero_diff(self):
        d = self._tmp_log(_AGREE + "\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = sr.main([], log_dir=d)
        self.assertEqual(rc, 0)
        self.assertIn("零分歧", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
