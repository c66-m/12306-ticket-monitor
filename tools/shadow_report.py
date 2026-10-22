# -*- coding: utf-8 -*-
"""
影子比对报告（P1-11）：聚合 engine.py 影子挂载的 [影子比对] 日志。

用法：
    python tools/shadow_report.py              # 今天
    python tools/shadow_report.py --since 2026-10-18   # 指定起始日

统计 [影子比对]（INFO）与 [影子比对·分歧]（WARNING）的比例与明细，
退出码 0=零分歧，1=存在分歧（规划书 Gate P1 判据：7 天零分歧才切真）。
"""

import re
import sys
import argparse
import glob
import os

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS_DIR)
LOGS = os.path.join(ROOT, "logs")

_INFO_RE = re.compile(r"\[影子比对\] (\S+) (\S+) (\S+) 旧=(\S+) 新=(\S+) "
                      r"一致=(True|False)(.*)")
_DIFF_RE = re.compile(r"\[影子比对·分歧\] (\S+) (\S+) (\S+) 旧=(\S+) 新=(\S+)"
                      r" 新理由=(.*)$")


def collect(since=None, log_dir=LOGS):
    """扫描日志文件，返回 (infos, diffs)。since: 'YYYY-MM-DD' 起（含）。"""
    infos, diffs, seen_diff = [], [], set()
    pattern = os.path.join(log_dir, "*/monitor_20*.log")
    files = sorted(glob.glob(os.path.join(log_dir, "monitor_20*.log"))
                   + glob.glob(os.path.join(log_dir, "launcher_20*.log")))
    if since:
        files = [f for f in files if os.path.basename(f) >=
                 "monitor_%s.log" % since.replace("-", "") or
                 os.path.basename(f) >= "launcher_%s.log" % since.replace("-", "")]
    for fp in files:
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                for ln in f:
                    m = _INFO_RE.search(ln)
                    if m:
                        infos.append(m.groups())
                        continue
                    d = _DIFF_RE.search(ln)
                    if d:
                        key = (d.group(1), d.group(2), d.group(3),
                               d.group(4), d.group(5))
                        if key not in seen_diff:
                            seen_diff.add(key)
                            diffs.append(d.groups())
        except OSError:
            continue
    return infos, diffs


def main(argv=None):
    p = argparse.ArgumentParser(description="影子比对报告")
    p.add_argument("--since", default=None,
                   help="起始日期 YYYY-MM-DD（默认：全部日志，含今天）")
    args = p.parse_args(argv)

    infos, diffs = collect(args.since)
    total = len(infos)
    agree = sum(1 for g in infos if g[5] == "True")
    disagree = total - agree
    print("影子比对样本: %d 条（一致 %d / 分歧 %d，一致率 %.1f%%）"
          % (total, agree, disagree, (agree / total * 100) if total else 0))
    if not diffs:
        print("零分歧。")
        return 0
    print("分歧明细（按 phase/date/train/旧/新 去重）：")
    for d in diffs[:50]:
        print("  - %s %s %s 旧=%s 新=%s | %s" % tuple(d))
    if len(diffs) > 50:
        print("  ... 其余 %d 条略" % (len(diffs) - 50))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())