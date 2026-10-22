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

# data/ 下的文件按「缺失的语义」分三级——一刀切会把首次运行判成故障。
#
# 一级：随代码分发的静态资产。缺失 = 故障（程序无法工作），CI 也必须过。
REQUIRED_ASSETS = (
    "stations_db.json",
    "station_index.json",
    "station_name.json",
    "station_kind.json",
)
# 二级：首次运行自动创建的运行时数据。缺失 = 正常（干净 clone / 新机器），
# 但存在时解析失败 = 故障（损坏的档比没有更危险）。
RUNTIME_DATA = (
    "state.json",
    "orders.json",
    "order_history.json",
    "grab_tasks.json",
    "passengers.json",
)
# 三级：登录后才存在的文件。缺失 = 正常。
OPTIONAL_DATA = ("session_cookies.json", ".browser_state.json")

# 已改名/已归档模块的遗留引用（生产代码里出现即失败）
LEGACY_REF_PATTERNS = (
    (re.compile(r"^\s*import launcher\s*$", re.M),
     "import launcher —— 模块已改名：请用 `import grabber`"),
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


def _validate_json(path: str) -> str | None:
    """数据文件内容校验：返回错误描述，None 表示 OK。"""
    if os.path.getsize(path) == 0:
        return "为空"
    try:
        with open(path, encoding="utf-8-sig") as f:
            json.load(f)
    except (OSError, ValueError) as exc:
        return "不可解析（%s）" % exc
    return None


def check_data(detail: list[str]) -> list[str]:
    """data/ 数据检查，按「缺失语义」分级（见 REQUIRED_ASSETS 注释）。

    分级的意义：干净 clone / 新机器上运行时数据本来就不存在（首次运行创建），
    一刀切要求全部存在会让自检与 CI 永远红；但**存在却损坏**必须算故障。
    """
    import appcommon  # 项目模块，需在 sys.path 注入 ROOT 后导入

    problems: list[str] = []
    checked = 0

    for name in REQUIRED_ASSETS:
        path = appcommon.data_path(name)
        if not os.path.exists(path):
            problems.append("缺少静态资产：%s（%s）" % (name, path))
            continue
        err = _validate_json(path)
        if err:
            problems.append("静态资产 %s %s" % (name, err))
            continue
        checked += 1

    absent = []
    for name in RUNTIME_DATA + OPTIONAL_DATA:
        path = appcommon.data_path(name)
        if not os.path.exists(path):
            absent.append(name)
            continue
        err = _validate_json(path)
        if err:
            problems.append("数据文件 %s 存在但%s" % (name, err))
            continue
        checked += 1

    if not problems:
        detail.append("已校验 %d 个文件；%d 个待首次运行/登录创建"
                      % (checked, len(absent)))
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
    """全量测试（test_all.py）必须全绿；tests/{unit,contract,integration,e2e}
    目录若存在也须全绿（P4 四层逐步填充，先占位后充实）。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    suites = [("test_all", [sys.executable,
                            os.path.join("tests", "test_all.py")])]
    for d in ("unit", "contract", "integration", "e2e"):
        if os.path.isdir(os.path.join(ROOT, "tests", d)):
            suites.append((d, [sys.executable, "-m", "unittest", "discover",
                               "-s", os.path.join("tests", d),
                               "-p", "test_*.py"]))
    started = time.time()
    ok_counts, failures = [], []
    for name, cmd in suites:
        try:
            proc = subprocess.run(
                cmd, cwd=ROOT, env=env, capture_output=True,
                encoding="utf-8", errors="replace", timeout=900,
            )
        except subprocess.TimeoutExpired:
            failures.append("%s 测试超时（>900s）" % name)
            continue
        except OSError as exc:
            failures.append("%s 测试进程无法启动：%s" % (name, exc))
            continue
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        ran = re.search(r"^Ran (\d+) tests? in ", out, re.M)
        ok = (proc.returncode == 0
              and re.search(r"^OK(?: \(.*\))?$", out, re.M) is not None)
        if ok:
            ok_counts.append("%s:%s" % (name, ran.group(1) if ran else "?"))
        else:
            failures.append("%s 未通过（退出码 %d%s）"
                            % (name, proc.returncode,
                               ("，%s" % ran.group(1)) if ran else ""))
    elapsed = time.time() - started
    if not failures:
        detail.append(" ".join(ok_counts) + " %.1fs" % elapsed)
        return []
    detail.append("测试未通过（%.1fs）：%s" % (elapsed, "; ".join(failures)))
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
