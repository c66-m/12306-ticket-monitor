# -*- coding: utf-8 -*-
"""
JSON → SQLite 迁移器（adapters/store，规划书 P0-6）。

把 state.json / orders.json / order_history.json 三份主数据镜像导入到
data/app.db（tasks / orders / history 表）。幂等、可 dry-run、可回滚：

- 幂等：meta.last_migrate_fingerprint = md5(三文件串联)；已等于当前指纹则
  跳过（该批数据已是迁移后状态），避免重复导入产生脏行。
- 备份：导入前把三份 JSON 复制到 data_migration_backup/<时间戳>/，md5 校验
  备份完整后才删除性操作——本迁移器不删除源文件，只留备份。
- 回滚：--rollback 从备份目录最新一批恢复原 JSON（文件级复制），SQLite 内容
  保留（P3 切换前 SQLite 只是镜像，回滚无需清库）。

注意：attempts 表 P1 才写，这里只建空表（init_schema 保证）。
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys

# Tools/脚本按根目录运行：注入 ROOT 以便 import appcommon / adapters
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from adapters.store import db  # noqa: E402

BACKUP_ROOT = os.path.join(db.HERE, "data_migration_backup")


def _md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_json(path):
    """读 JSON → obj；缺失返回 None；损坏抛 ValueError。"""
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def _source_fingerprint(state_path, orders_path, history_path):
    """三文件 md5 串联；缺失的文件按空占位（保证指纹稳定可跳过）。"""
    parts = []
    for p in (state_path, orders_path, history_path):
        parts.append(_md5_file(p) if os.path.exists(p) else "MISSING")
    return "|".join(parts)


def extract_payloads(state, orders_db, history):
    """把三份 JSON 拆成 (tasks_mapping, orders_mapping, history_list)。"""
    tasks = {}
    if isinstance(state, dict) and isinstance(state.get("tasks"), dict):
        tasks = state["tasks"]
    orders = {}
    if isinstance(orders_db, dict) and isinstance(orders_db.get("orders"), dict):
        orders = orders_db["orders"]
    history_list = history if isinstance(history, list) else []
    return tasks, orders, history_list


def backup_sources(state_path, orders_path, history_path, dry_run=False):
    """复制三份 JSON 到备份目录，并逐文件 md5 校验备份完整性。

    校验不过（复制被截断等）抛 RuntimeError——备份是回滚的唯一依据，
    坏备份不得放行迁移（模块 docstring 承诺「md5 校验备份完整后才」
    继续，R2-8 补齐实现）。返回备份目录路径（dry_run 返回计划路径）。"""
    ts = __import__("infra.clock", fromlist=["now_str"]).now_str(
        "%Y%m%d-%H%M%S-%f")
    dst_dir = os.path.join(BACKUP_ROOT, ts)
    if dry_run:
        return dst_dir
    os.makedirs(dst_dir, exist_ok=True)
    copied = []
    for src in (state_path, orders_path, history_path):
        if os.path.exists(src):
            dst = os.path.join(dst_dir, os.path.basename(src))
            shutil.copy2(src, dst)
            if _md5_file(src) != _md5_file(dst):
                raise RuntimeError(
                    "备份校验失败（md5 不一致），已中止迁移：%s -> %s"
                    % (src, dst))
            copied.append(dst)
    return dst_dir


def run_migrate(state_path, orders_path, history_path,
                db_file=None, dry_run=False, force=False):
    """执行一次迁移。返回 report dict。"""
    report = {"skipped": False, "backup_dir": None, "backup": [],
              "tasks": 0, "orders": 0, "history": 0, "errors": []}

    state = _load_json(state_path)
    orders_db = _load_json(orders_path)
    history = _load_json(history_path)
    # 三个文件都缺失（干净克隆/新机器）——无需迁移也无需备份
    if state is None and orders_db is None and history is None:
        report["skipped"] = True
        return report

    tasks_m, orders_m, history_l = extract_payloads(state, orders_db, history)

    conn = db.connect(db_file)
    try:
        old_fp = db.meta_get(conn, "last_migrate_fingerprint")
        new_fp = _source_fingerprint(state_path, orders_path, history_path)
        if not force and old_fp == new_fp:
            report["skipped"] = True  # 该批数据已迁移过
            return report
        try:
            report["backup_dir"] = backup_sources(
                state_path, orders_path, history_path, dry_run=dry_run)
        except Exception as e:  # 备份不完整 → 中止，DB 一字不改
            report["errors"].append("备份失败/校验不通过，已中止迁移：%s" % e)
            return report
        if dry_run:
            report["tasks"] = len(tasks_m)
            report["orders"] = len(orders_m)
            report["history"] = len(history_l)
            return report
        ok1, n1 = db.mirror_dict_table(conn, "tasks", tasks_m)
        ok2, n2 = db.mirror_dict_table(conn, "orders", orders_m)
        ok3, n3 = db.mirror_list_table(conn, "history", history_l)
        if not (ok1 and ok2 and ok3):
            report["errors"].append("部分表镜像写入失败（tasks=%s orders=%s history=%s）"
                                    % (ok1, ok2, ok3))
            return report
        report["tasks"], report["orders"], report["history"] = n1, n2, n3
        db.meta_set(conn, "last_migrate_fingerprint", new_fp)
        conn.commit()
        return report
    finally:
        conn.close()


def latest_backup_dir():
    """备份目录下时间戳最大的批次（字符串排序即可，ISO 时序前缀）。"""
    if not os.path.isdir(BACKUP_ROOT):
        return None
    dirs = sorted(os.listdir(BACKUP_ROOT))
    return os.path.join(BACKUP_ROOT, dirs[-1]) if dirs else None


def rollback(state_path, orders_path, history_path, backup=None, dry_run=False):
    """从备份目录恢复三份 JSON。返回放回列表。"""
    bd = backup or latest_backup_dir()
    if not bd or not os.path.isdir(bd):
        return []
    restored = []
    for name in ("state.json", "orders.json", "order_history.json"):
        src = os.path.join(bd, name)
        if not os.path.exists(src):
            continue
        dst = {"state.json": state_path, "orders.json": orders_path,
               "order_history.json": history_path}[name]
        if dry_run:
            restored.append((src, dst))
            continue
        shutil.copy2(src, dst)
        restored.append((src, dst))
    return restored


def main(argv=None):
    import appcommon  # noqa: E402 仅 CLI 入口延迟导入
    p = argparse.ArgumentParser(description="JSON → SQLite 迁移器")
    p.add_argument("--dry-run", action="store_true", help="只输出计划，不落盘")
    p.add_argument("--force", action="store_true", help="忽略幂等指纹强制重导")
    p.add_argument("--rollback", action="store_true", help="从最近备份恢复 JSON")
    args = p.parse_args(argv)

    state_path = appcommon.data_path("state.json")
    orders_path = appcommon.data_path("orders.json")
    history_path = appcommon.data_path("order_history.json")

    if args.rollback:
        restored = rollback(state_path, orders_path, history_path,
                            dry_run=args.dry_run)
        for src, dst in restored:
            print("恢复 %s -> %s" % (src, dst))
        print("恢复 %d 个文件。SQLite 镜像保留未动（P3 切换前只是镜像）。"
              % len(restored))
        return 0

    report = run_migrate(state_path, orders_path, history_path,
                         dry_run=args.dry_run, force=args.force)
    if report["skipped"]:
        print("跳过：数据已是迁移后状态（指纹一致）或三份文件均缺失。")
        return 0
    if report["errors"]:
        print("迁移失败: %s" % "; ".join(report["errors"]))
        return 1
    print("迁移计划/完成：tasks=%d orders=%d history=%d"
          % (report["tasks"], report["orders"], report["history"]))
    if not args.dry_run:
        print("备份目录: %s" % report["backup_dir"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())