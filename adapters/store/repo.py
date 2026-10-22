# -*- coding: utf-8 -*-
"""
仓储层（adapters/store/repo，P3-1）。

对应用层的统一数据入口：tasks / orders / attempts / history 四类数据。
本切片提供「JSON 主源」的只读（保证现网行为不变量：数据以 JSON 为权威）
与 attempts 的 SQLite 追加；SQLite 直读接口供对账/校验/影子使用。

P3 第二切片（JSON 停写 → SQLite 单写）到来前，本层**不替换任何现有写路径**
——写仍走 appcommon（其下已有双写镜像）。调用方不要用本层读替代 JSON 写。
"""

import json
import os

from adapters.store import db

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

# ----------------------------- 路径（与 appcommon.data_path 同口径自实现，
# 避免 repo→appcommon 依赖环；T12306_ 环境变量供测试重定向） -----------------------------

def _data_path(name):
    override = os.environ.get("T12306_DATA_DIR")
    if override:
        return os.path.join(override, name)
    candidate = os.path.join(ROOT, "data", name)
    if os.path.exists(candidate):
        return candidate
    legacy = os.path.join(ROOT, name)
    if os.path.exists(legacy):
        return legacy
    return candidate


def _load_json(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


# ----------------------------- JSON 主源只读（现网） -----------------------------

def read_tasks():
    """tasks 字典 {name: task_state}。state.json 缺失/损坏→{}。"""
    state = _load_json(_data_path("state.json"))
    if isinstance(state, dict) and isinstance(state.get("tasks"), dict):
        return state["tasks"]
    return {}


def read_orders():
    """订单台账 dict {key: rec}。order.json 缺失→{}。"""
    db_obj = _load_json(_data_path("orders.json"))
    if isinstance(db_obj, dict) and isinstance(db_obj.get("orders"), dict):
        return db_obj["orders"]
    return {}


def read_history(limit=None):
    """购票历史列表（按文件顺序）。limit 只截尾。"""
    hist = _load_json(_data_path("order_history.json"))
    if not isinstance(hist, list):
        return []
    return hist[-limit:] if limit else hist


# ----------------------------- SQLite 直读（影子/对账/校验） -----------------------------

def read_tasks_db():
    conn = db.get_conn()
    mapping, _ = db.read_dict_table(conn, "tasks")
    return mapping


def read_orders_db():
    conn = db.get_conn()
    mapping, _ = db.read_dict_table(conn, "orders")
    return mapping


# ----------------------------- attempts 台账（SQLite 追加） -----------------------------

def write_attempt(rec, conn=None):
    """写一条下单尝试台账（幂等 upsert）。失败静默不抛。"""
    try:
        conn = conn or db.get_conn()
        key = rec.get("attempt_id") or rec.get("key")
        if not key:
            return False
        payload = json.dumps(rec, ensure_ascii=False)
        ts = rec.get("ts") or 0
        conn.execute(
            "INSERT OR REPLACE INTO attempts (key, payload, updated_at)"
            " VALUES (?,?,?)", (key, payload, ts))
        conn.commit()
        return True
    except Exception:
        return False


def read_attempts(limit=200):
    conn = db.get_conn()
    out = []
    for row in conn.execute(
            "SELECT payload FROM attempts ORDER BY rowid DESC LIMIT ?",
            (limit,)):
        try:
            out.append(json.loads(row["payload"]))
        except (TypeError, ValueError):
            continue
    return out


def count_attempts():
    conn = db.get_conn()
    row = conn.execute("SELECT COUNT(*) AS n FROM attempts").fetchone()
    return row["n"]