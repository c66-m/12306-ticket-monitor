# -*- coding: utf-8 -*-
"""
JSON 主写路径的双写镜像（adapters/store，规划书 P0-7 / C1）。

P0 阶段语义：「JSON 为主、SQLite 为副」。APP 写 JSON 的主流程不受任何影响，
镜像写失败只记 warning 不阻断——SQLite 是供校验/迁移/未来单写的影子副本。

路径守卫：只有当写目标是项目「正规数据位置」（data/ 目录，或旧布局的
项目根目录）时才镜像；测试用临时目录 / 其它临时路径一律跳过——否则测试
套件每次跑都会把测试样本灌进真实 app.db（2026-10-20 实测污染：库中出现
测试样本 BOM任务/K1）。

本模块被 appcommon 顶部导入；仅依赖 adapters.store.db 与 logging，无环。
"""

import logging
import os

from adapters.store import db

LOG = logging.getLogger("monitor")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")


def _is_data_target(path):
    """判断 JSON 写目标是否位于正规数据位置（data/ 或旧布局项目根）。

    T12306_DATA_DIR 重定向：测试/集成用环境变量把数据目录伪装的临时目录
    同样视为正规目标（否则 integration 无法验证含镜像的真实链路）；生产
    未设置该变量，不放松任何守卫。
    """
    p = os.path.abspath(path or "")
    if not p:
        return False
    d = os.path.dirname(p)
    if d == DATA_DIR or d == ROOT:
        return True
    redirect = os.environ.get("T12306_DATA_DIR")
    if redirect and d == os.path.abspath(redirect):
        return True
    return False


def _mirror(fn, what):
    """执行镜像写，任何异常只告警。返回 True=同步成功 / False=None=失败跳过。"""
    try:
        ok, n = fn()
        if ok:
            return True
        LOG.warning("[双写] %s 镜像写入返回失败（数据以 JSON 为准，不影响主流程）", what)
    except Exception as e:  # noqa: BLE001 镜像不能反噬主写路径
        LOG.warning("[双写] %s 镜像写入异常（数据以 JSON 为准）：%s", what, e)
    return False


def mirror_written(path, obj):
    """统一分派钩子：挂进 appcommon.atomic_write_json 最底层，任何写者
    （write_state / engine 直写 state / append_history / upsert_order）写完
    三个白名单数据文件时自动镜像。非白名单文件 / 非正规路径直接跳过。
    """
    if not _is_data_target(path):
        return False
    name = os.path.basename(path)
    if name == "state.json":
        return mirror_state_write(path, obj)
    if name == "orders.json":
        return mirror_orders_write(path, obj)
    if name == "order_history.json":
        return mirror_history_write(path, obj)
    return False


def mirror_state_write(state_path, state):
    """state.json 的 tasks 字典 → SQLite tasks 表。非正规路径/非 dict 时跳过。"""
    if not _is_data_target(state_path) or not isinstance(state, dict):
        return False
    tasks = state.get("tasks") if isinstance(state.get("tasks"), dict) else {}
    return _mirror(
        lambda: db.mirror_dict_table(db.get_conn(), "tasks", tasks),
        "state/tasks")


def mirror_orders_write(orders_path, orders_db):
    """orders.json 的 orders 字典 → SQLite orders 表。"""
    if not _is_data_target(orders_path) or not isinstance(orders_db, dict):
        return False
    orders = (orders_db.get("orders")
              if isinstance(orders_db.get("orders"), dict) else {})
    return _mirror(
        lambda: db.mirror_dict_table(db.get_conn(), "orders", orders),
        "orders")


def mirror_history_write(history_path, history_list):
    """order_history.json 列表 → SQLite history 表。"""
    if not _is_data_target(history_path) or not isinstance(history_list, list):
        return False
    return _mirror(
        lambda: db.mirror_list_table(db.get_conn(), "history", history_list),
        "history")


__all__ = ["mirror_written", "mirror_state_write", "mirror_orders_write",
           "mirror_history_write"]