# -*- coding: utf-8 -*-
"""项目一键自检：语法编译 / 遗留引用 / 数据完整性 / 车站库一致性 / 全量测试。

用法：
    python tools/check.py          # 全量（含 800 测试，约 40 秒）
    python tools/check.py --quick  # 跳过测试（静态与数据检查，约 1 秒）

退出码：0 全部通过；1 有失败项。

为什么需要它：2026-10-18 的数据目录分离与模块改名（launcher→grabber、
gui→legacy）靠一次性人工核对完成，容易在后续改动中退化。本脚本把这些
约束固化成可重复执行的检查：
  1. 语法编译：任何 .py 不能有语法错误；
  2. 遗留引用：生产代码不得再 import 已改名/已归档模块；
  3. 数据完整性：data/ 下的数据文件必须存在、非空、JSON 可解析；
  4. 车站库一致性：station_db.check() 的 name↔code 漂移检查；
  5. 全量测试：tests/test_all.py 必须全绿。

依赖：仅标准库（测试用 unittest，不强制安装 pytest）。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import time
import compileall

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS_DIR)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 编译/扫描时跳过的目录（产物、依赖、备份）
SKIP_DIRS = ("__pycache__", "node_modules", "data", "data_migration_backup",
             ".browser_profile", "logs", "runs", "legacy")

# data/ 下必需的数据文件（缺失 = 迁移未完成或误删）
REQUIRED_DATA = (
    "state.json",
    "orders.json",
    "order_history.json",
    "grab_tasks.json",
    "passengers.json",
    "stations_db.json",
    "station_index.json",
    "station_name.json",
    "station_kind.json",
)
# 仅登录后才存在的文件：缺失不算失败
OPTIONAL_DATA = ("session_cookies.json", ".browser_state.json")

# 已改名/已归档模块的遗留引用（生产代码里出现即失败）
LEGACY_REF_PATTERNS = (
    (re.compile(r"^\s*import launcher\s*$", re.M),
     "import launcher —— 模块已改名：请用 `import grabber as launcher`"),
    (re.compile(r"^\s*from launcher import", re.M),
     "from launcher import —— 模块已改名：请用 `from grabber import ...`"),
    (re.compile(r"^\s*import gui\s*$", re.M),
     "import gui —— 已归档：请用 `from legacy import gui`"),
    (re.compile(r"^\s*from gui import", re.M),
     "from gui import —— 已归档：请用 `from legacy.gui import ...`"),
)

# 数据文件仍从 HERE 硬拼（用户配置留根目录，属于允许项）
HERE_JSON_RE = re.compile(r'os\.path\.join\(HERE,\s*"([^"]+\.json)"\)')
HERE_JSON_ALLOW = {"config.json", "config.example.json",
                   "launcher_config.json", "launcher_config.example.json"}


def _iter_py_files(base: str):
    """遍历生产代码 .py（跳过 SKIP_DIRS 与 tests/）。"""
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel = os.path.relpath(dirpath, base)
        if rel == "tests" or rel.startswith("tests" + os.sep):
            continue
        for name in filenames:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def check_compile() -> list[str]:
    """全部 .py 必须能编译（含 legacy/，它虽然停用但仍需语法正确）。"""
    ok = compileall.compile_dir(
        ROOT, quiet=1, force=False,
        rx=re.compile(r"[\\/](__pycache__|node_modules|data_migration_backup|data|"
                      r"\.browser_profile|logs|runs)[\\/]"))
    return [] if ok else ["存在语法错误（详见上方编译输出）"]


def check_legacy_refs() -> list[str]:
    """生产代码不得引用已改名/已归档模块，也不得把数据文件写回根目录。"""
    problems: list[str] = []
    for path in _iter_py_files(ROOT):
        rel = os.path.relpath(path, ROOT)
        try:
            with open(path, encoding="utf-8") as f:
                src = f.read()
        except OSError as exc:
            problems.append("%s: 读取失败 %s" % (rel, exc))
            continue
        for pattern, msg in LEGACY_REF_PATTERNS:
            for m in pattern.finditer(src):
                line_no = src.count("\n", 0, m.start()) + 1
                problems.append("%s:%d: %s" % (rel, line_no, msg))
        for m in HERE_JSON_RE.finditer(src):
            name = m.group(1)
            if name in HERE_JSON_ALLOW:
                continue
            line_no = src.count("\n", 0, m.start()) + 1
            problems.append(
                "%s:%d: 数据文件 %s 仍用 HERE 硬拼 —— 请用 appcommon.data_path(%r)"
                % (rel, line_no, name, name))
    return problems


def check_data(detail: list[str]) -> list[str]:
    """data/ 下的数据文件必须存在、非空、JSON 可解析。"""
    import appcommon  # 项目模块，需在 sys.path 注入 ROOT 后导入

    problems: list[str] = []
    checked = 0
    for name in REQUIRED_DATA:
        path = appcommon.data_path(name)
        if not os.path.exists(path):
            problems.append("缺少必需数据文件：%s（查找位置 %s）" % (name, path))
            continue
        if os.path.getsize(path) == 0:
            problems.append("数据文件为空：%s" % path)
            continue
        try:
            with open(path, encoding="utf-8-sig") as f:
                json.load(f)
        except (OSError, ValueError) as exc:
            problems.append("数据文件不可解析：%s（%s）" % (name, exc))
            continue
        checked += 1

    for name in OPTIONAL_DATA:
        path = appcommon.data_path(name)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            try:
                with open(path, encoding="utf-8-sig") as f:
                    json.load(f)
            except (OSError, ValueError) as exc:
                problems.append("可选数据文件存在但不可解析：%s（%s）" % (name, exc))
                continue
            checked += 1

    if not problems:
        detail.append("已校验 %d 个数据文件" % checked)
    return problems


def check_station_db(detail: list[str]) -> list[str]:
    """车站四库的 name↔code 一致性（复用 station_db.check 的退出码）。"""
    try:
        import station_db
    except ImportError as exc:
        return ["无法导入 station_db：%s" % exc]

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = station_db.check()
    except (OSError, ValueError, KeyError) as exc:
        return ["车站库检查异常：%s: %s" % (type(exc).__name__, exc)]

    if rc == 0:
        tail = buf.getvalue().strip().splitlines()
        if tail:
            detail.append(tail[-1])
        return []
    detail = buf.getvalue().strip() or "station_db.check() 返回非 0"
    return [detail.replace("\n", " / ")]


def check_tests(detail: list[str]) -> list[str]:
    """全量测试必须全绿（解析 unittest 的汇总行）。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, os.path.join("tests", "test_all.py")],
            cwd=ROOT, env=env, capture_output=True,
            encoding="utf-8", errors="replace", timeout=900,
        )
    except subprocess.TimeoutExpired:
        return ["测试超时（>900s）"]
    except OSError as exc:
        return ["测试进程无法启动：%s" % exc]

    elapsed = time.time() - started
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    ran = re.search(r"^Ran (\d+) tests? in ", out, re.M)
    failed = "FAILED (" in out
    ok = re.search(r"^OK(?: \(.*\))?$", out, re.M) is not None

    if proc.returncode == 0 and ok and not failed:
        detail.append("%s tests, %.1fs" % (ran.group(1) if ran else "?", elapsed))
        return []

    detail = ["测试未通过（退出码 %d，%.1fs）" % (proc.returncode, elapsed)]
    if ran:
        detail.append("共 %s 个用例" % ran.group(1))
    summary = re.search(r"^FAILED \(.*\)$", out, re.M)
    if summary:
        detail.append(summary.group(0))
    failures = re.findall(r"^(?:FAIL|ERROR): (\S+)", out, re.M)
    detail.extend(failures[:20])
    return detail


def _no_detail(fn):
    """把无 detail 参数的检查适配成统一签名。"""
    return lambda detail: fn()


CHECKS = (
    ("语法编译", _no_detail(check_compile), False),
    ("遗留引用", _no_detail(check_legacy_refs), False),
    ("数据完整性", check_data, False),
    ("车站库一致性", check_station_db, False),
    ("全量测试", check_tests, True),   # True = --quick 时跳过
)


def main() -> int:
    parser = argparse.ArgumentParser(description="12306 项目一键自检")
    parser.add_argument("--quick", action="store_true", help="跳过全量测试")
    args = parser.parse_args()

    print("=== 12306 项目自检（%s）===" % ROOT)
    failures = 0
    for idx, (title, fn, slow) in enumerate(CHECKS, 1):
        label = "[%d/%d] %s" % (idx, len(CHECKS), title)
        if slow and args.quick:
            print("%s ... SKIP（--quick）" % label)
            continue
        detail: list[str] = []
        try:
            problems = fn(detail)
        except Exception as exc:        # 自检脚本自身不能因单项检查崩溃而中断
            problems = ["检查项自身异常：%s: %s" % (type(exc).__name__, exc)]
        if problems:
            failures += 1
            print("%s ... FAIL" % label)
            for p in problems:
                print("      - %s" % p)
        else:
            suffix = ("  " + "；".join(detail)) if detail else ""
            print("%s ... OK%s" % (label, suffix))

    print()
    if failures:
        print("自检未通过：%d 项失败。" % failures)
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
