# -*- coding: utf-8 -*-
"""把根目录的运行时/静态数据文件迁移到 data/（一次性脚本，幂等可重跑）。

背景：项目原先代码与数据全部平铺在根目录，18 个 .py 与十几个 .json 混放。
现改为「代码留根、数据进 data/」，路径解析统一由 appcommon.data_path 负责
（data/ 优先，旧根目录布局兼容回退）。

安全设计：
  1. 先备份到 data_migration_backup/<时间戳>/，再复制到 data/；
  2. 逐个校验源/目标的 md5，一致才删除源文件；
  3. data/ 已存在同名文件时跳过，绝不覆盖（重复执行安全）；
  4. 程序运行中文件被占用会报错并整体停止，不会留下半迁移状态。

用法：
    python tools/migrate_data_dir.py --dry-run   # 只打印计划，不动文件
    python tools/migrate_data_dir.py             # 执行迁移
    python tools/migrate_data_dir.py --rollback  # 从最近一次备份恢复
"""

import argparse
import hashlib
import os
import shutil
import sys
import time

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS_DIR)
DATA_DIR = os.path.join(ROOT, "data")
BACKUP_ROOT = os.path.join(ROOT, "data_migration_backup")

# 迁移清单：只收「运行时数据 + 可再生的静态资产」。
# 故意不收：config.json / launcher_config.json（用户配置，留根方便手改）、
# .browser_profile（Playwright 身份目录，换路径 = 丢登录态）、logs（测试锁定目录形态）。
MOVE_FILES = (
    # 运行时数据（含隐私，不入库）
    "orders.json",
    "state.json",
    "order_history.json",
    "passengers.json",
    "grab_tasks.json",
    "session_cookies.json",
    ".browser_state.json",
    # 静态数据资产（随代码分发，入库）
    "stations_db.json",
    "station_index.json",
    "station_name.json",
    "station_kind.json",
)

# 旧锁文件：代码换到 data/ 后会生成新锁，旧的留着没意义，顺手清理（仅删空/过期锁）。
STALE_LOCK_FILES = tuple(name + ".lock" for name in MOVE_FILES)


def md5_of(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def newest_backup_dir():
    if not os.path.isdir(BACKUP_ROOT):
        return None
    items = [os.path.join(BACKUP_ROOT, d) for d in os.listdir(BACKUP_ROOT)
             if os.path.isdir(os.path.join(BACKUP_ROOT, d))]
    return max(items, key=os.path.getmtime) if items else None


def do_rollback() -> int:
    backup = newest_backup_dir()
    if not backup:
        print("[rollback] 没有找到备份目录：%s" % BACKUP_ROOT)
        return 1
    print("[rollback] 从备份恢复：%s" % backup)
    restored = 0
    for name in MOVE_FILES:
        src = os.path.join(backup, name)
        if not os.path.exists(src):
            continue
        dst = os.path.join(ROOT, name)
        if os.path.exists(dst):
            print("  跳过（根目录已存在）：%s" % name)
            continue
        shutil.copy2(src, dst)
        print("  恢复：%s" % name)
        restored += 1
    print("[rollback] 完成，恢复 %d 个文件。data/ 下的副本未删除，确认无误后可手工清理。" % restored)
    return 0


def do_migrate(dry_run: bool) -> int:
    if not os.path.isdir(DATA_DIR):
        if dry_run:
            print("[plan] 将创建目录：%s" % DATA_DIR)
        else:
            os.makedirs(DATA_DIR, exist_ok=True)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(BACKUP_ROOT, stamp)
    pending = []       # [(name, src, dst)]
    skipped = []

    for name in MOVE_FILES:
        src = os.path.join(ROOT, name)
        dst = os.path.join(DATA_DIR, name)
        if not os.path.exists(src):
            skipped.append((name, "根目录无此文件（可能已迁移）"))
            continue
        if os.path.exists(dst):
            skipped.append((name, "data/ 已存在同名文件，不覆盖"))
            continue
        pending.append((name, src, dst))

    for name, reason in skipped:
        print("[skip] %-24s %s" % (name, reason))

    if not pending:
        print("\n没有需要迁移的文件。")
    else:
        print("\n计划迁移 %d 个文件 → %s" % (len(pending), DATA_DIR))
        for name, src, _dst in pending:
            print("  %-24s %10d bytes" % (name, os.path.getsize(src)))
        if dry_run:
            print("\n[dry-run] 未动任何文件。去掉 --dry-run 执行。")
            return 0

        os.makedirs(backup_dir, exist_ok=True)
        print("\n备份目录：%s" % backup_dir)
        failed = []
        for name, src, dst in pending:
            backup = os.path.join(backup_dir, name)
            try:
                shutil.copy2(src, backup)
                shutil.copy2(src, dst)
                if md5_of(src) != md5_of(dst):
                    raise OSError("md5 校验不一致：%s" % name)
                os.remove(src)
                print("  [ok] %s" % name)
            except OSError as exc:
                failed.append((name, str(exc)))
                print("  [FAIL] %s：%s" % (name, exc))
                # 失败时回滚该文件，避免半迁移
                if os.path.exists(dst) and not os.path.exists(src):
                    shutil.move(dst, src)
                break

        if failed:
            print("\n迁移中止，失败文件：%s" % failed)
            print("常见原因：程序正在运行占用文件。请先关闭桌面版 / Electron / 监控进程后重试。")
            return 1
        print("\n迁移完成。原文件已备份在：%s" % backup_dir)

    # 清理旧锁文件（不动 data/ 下的新锁）
    removed_locks = []
    for lock in STALE_LOCK_FILES:
        p = os.path.join(ROOT, lock)
        if os.path.exists(p) and os.path.getsize(p) == 0:
            try:
                os.remove(p)
                removed_locks.append(lock)
            except OSError:
                pass
    if removed_locks:
        print("清理旧锁文件：%s" % ", ".join(removed_locks))

    print("\n下一步：跑全量测试确认 —— python -m unittest discover -s tests -v")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移项目数据文件到 data/ 目录")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不修改文件")
    parser.add_argument("--rollback", action="store_true", help="从最近一次备份恢复到根目录")
    args = parser.parse_args()
    if args.rollback:
        return do_rollback()
    return do_migrate(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
