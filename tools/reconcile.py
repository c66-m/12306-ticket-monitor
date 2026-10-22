# -*- coding: utf-8 -*-
"""
历史对账 CLI（tools/reconcile，吸收「对账功能实施任务书」语义）。

把本地台账（orders.json）未决记录与 12306 官方订单快照对齐，写回真实状态。
本命令只读官方接口 + 更新台账标注：**不下单、不改任务状态**。

用法（需已登录且有 .browser_state.json 会话）：
    python tools/reconcile.py            # 只对账待决记录（unpaid/unknown/error）
    python tools/reconcile.py --force    # 全部记录重对账
    python tools/reconcile.py --dry-run  # 只出统计不写台账

退出码：0=成功；1=会话缺失/查询失败；2=有对账失败的记录。
"""

import argparse
import os
import sys

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS_DIR)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import order as order_mod  # noqa: E402
from adapters.railway.snapshot import SnapshotProvider  # noqa: E402
from app.reconcile_service import ReconcileRunner  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(description="本地台账 × 12306 官方订单对账")
    p.add_argument("--force", action="store_true", help="全部记录重对账")
    p.add_argument("--dry-run", action="store_true", help="只统计不写台账")
    args = p.parse_args(argv)

    try:
        session = order_mod.session_from_browser_state()
    except RuntimeError as e:
        print("无法构造官方会话：%s（请先按 README 登录一次）" % e)
        return 1

    provider = SnapshotProvider(session)

    def _updater(key, rec):
        import appcommon
        appcommon.upsert_order(appcommon.data_path("orders.json"), key, rec)

    runner = ReconcileRunner(provider,
                             updater=(_updater if not args.dry_run else None),
                             emit=True)
    report = runner.reconcile_once(force=args.force)
    print(report.summary())
    return 0 if report.errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())