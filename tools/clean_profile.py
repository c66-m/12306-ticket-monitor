# -*- coding: utf-8 -*-
"""清理 .browser_profile 的可再生产物（缓存 / 遥测 / 崩溃转储），保留登录会话。

用法：
    python tools/clean_profile.py          # 干跑：只列出将删的内容，不动文件
    python tools/clean_profile.py --force  # 真删

安全前提：本脚本先对 .browser_profile.lock 加字节锁（与 browser_order._ProfileLock
抢的是同一字节），抢不到即说明浏览器正在运行，立即退出、不删任何东西。

会话载体一律不在名单内：Default/Network/Cookies（12306 登录票据）、
Default/Local Storage、Default/Session Storage、Default/Preferences、
Default/Secure Preferences、Default/Login Data、根 Local State / Last Version。
"""

import os
import shutil
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE = os.path.join(HERE, ".browser_profile")
LOCK_PATH = os.path.join(HERE, ".browser_profile.lock")

# 顶层整目录：缓存命中率低、体量最大（BrowserMetrics 与 Crashpad 合计约 220MB）
ROOT_DIRS = (
    "BrowserMetrics",
    "Crashpad",
    "GrShaderCache",
    "ShaderCache",
    "GPUPersistentCache",
    "DeferredBrowserMetrics",
    "component_crx_cache",
    "extensions_crx_cache",
)
# Default/ 下整目录：渲染与页面缓存，浏览器下次启动自行重建
DEFAULT_DIRS = (
    "Cache",
    "Code Cache",
    "GPUCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "EdgeCoupons",
    "Service Worker",
)
# Default/ 下按前缀删文件：诊断日志与统计库
DEFAULT_FILE_PREFIXES = (
    "chrome_debug.log",
    "favorites_diagnostic.log",
    "settings_diagnostic.log",
    "load_statistics.db",
    "declarative_performance_observer.db",
)
# 浏览器崩溃/强杀后残留的 Singleton 锁会让下次启动直接退出（见 browser_order.py）
SINGLETON_PREFIX = "Singleton"


def _collect():
    """返回 [(path, kind)]，kind 为 "dir" 或 "file"。只收真正存在的项。"""
    out = []
    for name in ROOT_DIRS:
        p = os.path.join(PROFILE, name)
        if os.path.isdir(p):
            out.append((p, "dir"))
    default = os.path.join(PROFILE, "Default")
    for name in DEFAULT_DIRS:
        p = os.path.join(default, name)
        if os.path.isdir(p):
            out.append((p, "dir"))
    if os.path.isdir(default):
        for entry in sorted(os.listdir(default)):
            if entry.startswith(DEFAULT_FILE_PREFIXES):
                out.append((os.path.join(default, entry), "file"))
    for entry in sorted(os.listdir(PROFILE)) if os.path.isdir(PROFILE) else []:
        if entry.startswith(SINGLETON_PREFIX):
            out.append((os.path.join(PROFILE, entry), "file"))
    return out


def _size(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _remove(path):
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.remove(path)
        except OSError:
            pass


def main(argv):
    force = "--force" in argv
    if not os.path.isdir(PROFILE):
        print("未找到 .browser_profile/（%s），无需清理。" % PROFILE)
        return 0

    sys.path.insert(0, HERE)
    import filelock

    try:
        with filelock.file_lock(LOCK_PATH, timeout=0.2):
            targets = _collect()
            total = sum(_size(p) for p, _k in targets)
            if not targets:
                print("无可清理项（缓存已空）。")
                return 0
            for p, kind in targets:
                print("  %-7s %8.2f MB  %s" % (kind, _size(p) / 1048576.0, os.path.relpath(p, PROFILE)))
            print("合计 %d 项 / %.1f MB" % (len(targets), total / 1048576.0))
            if not force:
                print("干跑结束，未改动任何文件；确认后加 --force 执行。")
                return 0
            for p, _kind in targets:
                _remove(p)
            after = sum(_size(p) for p, _k in _collect())
            print("已清理。剩余可清理项 %.1f MB。" % (after / 1048576.0))
            return 0
    except TimeoutError:
        print("浏览器正在运行（.browser_profile 被占用），本次不清理。先关掉下单浏览器再试。")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
