# -*- coding: utf-8 -*-
"""
双写一致性校验（规划书 P0-8 / Gate P0 关键项）。

读三份 JSON（state/orders/order_history，主）与 SQLite 四表（副）
逐项比对，输出差异明细；退出码 0=一致、1=有差异或读取失败。

用法：
    python tools/verify_dualwrite.py             # 默认数据目录与库
    python tools/verify_dualwrite.py --db X.db   # 指定库文件

只比对数据文件，绝不触碰/输出 config（SMTP 授权码等敏感值不在比对范围）。
"""

import argparse
import json
import os
import sys

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS_DIR)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import appcommon  # noqa: E402
from adapters.store import db  # noqa: E402


def _load_json(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def _fmt_diff(label, detail):
    return "%s: %s" % (label, detail)


def compare(conn, state_path, orders_path, history_path):
    """返回 (ok, diffs)。diffs 是逐行 diff 明细字符串。"""
    diffs = []

    state = _load_json(state_path)
    tasks_json = (state or {}).get("tasks", {}) if isinstance(state, dict) else {}
    tasks_db, _ = db.read_dict_table(conn, "tasks")
    if tasks_json != tasks_db:
        _emit_dict_diff("tasks", tasks_json, tasks_db, diffs)

    orders_db_obj = _load_json(orders_path)
    orders_json = ((orders_db_obj or {}).get("orders", {})
                   if isinstance(orders_db_obj, dict) else {})
    orders_db, _ = db.read_dict_table(conn, "orders")
    if orders_json != orders_db:
        _emit_dict_diff("orders", orders_json, orders_db, diffs)

    history_json = _load_json(history_path)
    history_json = history_json if isinstance(history_json, list) else []
    history_db, _ = db.read_list_table(conn, "history")
    if history_json != history_db:
        diffs.append(_fmt_diff(
            "history",
            "条数 JSON=%d 库=%d" % (len(history_json), len(history_db))))

    return len(diffs) == 0, diffs


def _emit_dict_diff(name, a, b, diffs):
    if list(a.keys()) != list(b.keys()):
        ka, kb = set(a), set(b)
        diffs.append(_fmt_diff(
            name, "键集差异 仅JSON=%s 仅库=%s"
            % (sorted(ka - kb)[:5], sorted(kb - ka)[:5])))
    for k in sorted(set(a) & set(b)):
        if a[k] != b[k]:
            diffs.append(_fmt_diff(name, "键 '%s' 内容不一致（截断各 200 字）"
                                   % k))
    if a == b:
        return
    # 只打印 diff 摘要，不打印整条内容（防台账/订单含 PII）
    while len(diffs) > 8:
        diffs.pop()


def main(argv=None):
    p = argparse.ArgumentParser(description="JSON 与 SQLite 双写一致性校验")
    p.add_argument("--db", default=None, help="SQLite 库文件路径（默认 data/app.db）")
    args = p.parse_args(argv)

    state_path = appcommon.data_path("state.json")
    orders_path = appcommon.data_path("orders.json")
    history_path = appcommon.data_path("order_history.json")

    conn = db.connect(args.db)
    try:
        ok, diffs = compare(conn, state_path, orders_path, history_path)
    finally:
        conn.close()

    if ok:
        print("双写一致：tasks/orders/history 三表与 JSON 完全同步。")
        return 0
    print("双写存在差异：")
    for d in diffs:
        print("  -", d)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())