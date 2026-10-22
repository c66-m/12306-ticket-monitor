# -*- coding: utf-8 -*-
"""
SQLite 存储层（adapters/store，仅依赖标准库）。

规划书 P0-5：data/app.db 单库四表 + WAL，为 P3 全面切单写做准备。
P0 阶段是「双写镜像存储」（JSON 为主、库为副），本模块只提供库的 DDL、
连接与镜像读写 API，不承担业务决策。

表结构（payload 一律 JSON 序列化，结构随调用方演进，库层不感知）：
- tasks    key=任务名                        payload=任务状态 dict
- orders   key=订单条目键（date|train）       payload=订单记录 dict
- attempts key=attempt_id（P1 台账专用）      payload=下单尝试记录
- history  seq 自增                           payload=历史记录 dict
- meta     k/v 键值对（schema 版本、迁移时间戳等）

并发口径：WAL 模式；多进程读可并行，写由 SQLite 串行化。任何镜像写失败
只返回 False 由调用方决定是否告警，绝不抛出影响主写路径。

注意：本模块独立于 appcommon（不反向 import，避免 appcommon→dualwrite→db
→appcommon 依赖环）。db 路径解析自实现，与 data_path 的口径保持一致。
"""

import json
import os
import sqlite3
import threading

HERE = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def db_path(name="app.db"):
    """库文件绝对路径。T12306_APP_DB 可整体重定向（测试隔离用）。
    目录解析与 appcommon.data_path 同为：data/ 优先、旧根目录兼容、缺省 data/。"""
    override = os.environ.get("T12306_APP_DB")
    if override:
        return override
    if os.path.isabs(name):
        return name
    candidate = os.path.join(HERE, "data", name)
    if os.path.exists(candidate):
        return candidate
    legacy = os.path.join(HERE, name)
    if os.path.exists(legacy):
        return legacy
    return candidate


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
  key        TEXT PRIMARY KEY,
  payload    TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
  key        TEXT PRIMARY KEY,
  payload    TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
  key        TEXT PRIMARY KEY,
  payload    TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,
  payload    TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);
"""

_DICT_TABLES = ("tasks", "orders", "attempts")


def connect(path=None):
    """打开（并初始化 schema）连接。每个线程独立连接；WAL 模式。"""
    p = path or db_path()
    parent = os.path.dirname(p)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(p, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA_SQL)
    return conn


def _now_str():
    from infra.clock import now_str  # 延迟导入，避免与 infra 循环
    return now_str()


# ----------------------------- 镜像写（全量替换，数据量小、简单可靠） -----------------------------

def mirror_dict_table(conn, table, mapping):
    """全量替换 dict 型表（tasks/orders/attempts）。mapping: {key: obj}。
    单事务内 DELETE + 逐行 INSERT。返回 (ok, n)。"""
    if table not in _DICT_TABLES:
        raise ValueError("未知 dict 表: %s" % table)
    ts = _now_str()
    try:
        with conn:
            conn.execute("DELETE FROM %s" % table)
            for k, v in mapping.items():
                conn.execute(
                    "INSERT INTO %s (key, payload, updated_at) VALUES (?,?,?)"
                    % table,
                    (str(k), json.dumps(v, ensure_ascii=False), ts))
        return True, len(mapping)
    except (sqlite3.Error, TypeError, ValueError):
        return False, 0


def mirror_list_table(conn, table, items):
    """全量替换 list 型表（history）。items 顺序即写入顺序。返回 (ok, n)。"""
    ts = _now_str()
    try:
        with conn:
            conn.execute("DELETE FROM %s" % table)
            for it in items:
                conn.execute(
                    "INSERT INTO %s (payload, updated_at) VALUES (?,?)" % table,
                    (json.dumps(it, ensure_ascii=False), ts))
        return True, len(items)
    except (sqlite3.Error, TypeError, ValueError):
        return False, 0


# ----------------------------- 读（校验/迁移用） -----------------------------

def read_dict_table(conn, table):
    """读 dict 型表 → {key: obj}。坏 JSON 行跳过并计数。"""
    out, bad = {}, 0
    for row in conn.execute("SELECT key, payload FROM %s" % table):
        try:
            out[row["key"]] = json.loads(row["payload"])
        except (TypeError, ValueError):
            bad += 1
    return out, bad


def read_list_table(conn, table):
    """读 list 型表 → [obj, ...]（按 seq）。"""
    out, bad = [], 0
    for row in conn.execute("SELECT payload FROM %s ORDER BY seq" % table):
        try:
            out.append(json.loads(row["payload"]))
        except (TypeError, ValueError):
            bad += 1
    return out, bad


def meta_get(conn, k, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def meta_set(conn, k, v):
    conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?,?)", (k, str(v)))


# 线程内唯一连接缓存（按路径分 key，路径可被 T12306_APP_DB 重定向），
# 避免反复 open；close 由进程退出清理。
_LOCAL = threading.local()


def _conn_cache():
    if getattr(_LOCAL, "cache", None) is None:
        _LOCAL.cache = {}
    return _LOCAL.cache


def get_conn(path=None):
    p = path or db_path()
    cache = _conn_cache()
    conn = cache.get(p)
    if conn is None:
        conn = connect(p)
        cache[p] = conn
    return conn