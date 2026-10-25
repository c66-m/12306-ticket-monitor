# -*- coding: utf-8 -*-
"""
影子比对报告（P1-11）：聚合 engine.py 影子挂载的 [影子比对] 日志。

用法：
    python tools/shadow_report.py              # 今天
    python tools/shadow_report.py --since 2026-10-18   # 指定起始日

统计 [影子比对]（INFO）与 [影子比对·分歧]（WARNING）的比例与明细，
退出码 0=零分歧，1=存在分歧（规划书 Gate P1 判据：7 天零分歧才切真）。

窗口边沿 triage（建议清单④）：影子窗口 [t-5s,t+180s] 比现网
[t-30s,t+600s] 收紧是 R2 有意设计；分歧中宽窗下内核结论与现网一致
的条目，日志带「窗口边沿」标记，本工具将其单列为预期差异——
不与真实分歧混在一起，但仍计入分歧总数、退出码仍为 1，门禁不放行；
窗口口径本身切 B 线时由用户定。
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
                      r"一致=(True|False)( 窗口边沿)?(.*)")
_DIFF_RE = re.compile(r"\[影子比对·分歧\] (\S+) (\S+) (\S+) 旧=(\S+) 新=(\S+)"
                      r"( 窗口边沿)? 新理由=(.*)$")


def collect(since=None, log_dir=LOGS):
    """扫描日志文件，返回 (infos, diffs)。since: 'YYYY-MM-DD' 起（含）。"""
    infos, diffs, seen_diff = [], [], set()
    pattern = os.path.join(log_dir, "*/monitor_20*.log")
    files = sorted(glob.glob(os.path.join(log_dir, "monitor_20*.log"))
                   + glob.glob(os.path.join(log_dir, "launcher_20*.log")))
    if since:
        # 按文件名前缀分别过滤（2026-10-25 审）：旧口径用 or 连接两个
        # 比较，monitor_ 文件因 'm' > 'l' 恒过第二条件，--since 对
        # monitor 日志完全失效。
        m_prefix = "monitor_%s.log" % since.replace("-", "")
        l_prefix = "launcher_%s.log" % since.replace("-", "")

        def _keep(f):
            b = os.path.basename(f)
            if b.startswith("monitor_"):
                return b >= m_prefix
            if b.startswith("launcher_"):
                return b >= l_prefix
            return True

        files = [f for f in files if _keep(f)]
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
                        # 去重键含边沿标志：同 key 在不同样本里一次是边沿、
                        # 一次是真实分歧时，两条都保留、分属两节。
                        key = (d.group(1), d.group(2), d.group(3),
                               d.group(4), d.group(5), bool(d.group(6)))
                        if key not in seen_diff:
                            seen_diff.add(key)
                            diffs.append(d.groups())
        except OSError:
            continue
    return infos, diffs


def split_diffs(diffs):
    """分歧拆分为 (真实分歧, 窗口边沿预期差异)。"""
    real = [d for d in diffs if not d[5]]
    edge = [d for d in diffs if d[5]]
    return real, edge


def _print_diffs(title, diffs):
    print(title)
    for d in diffs[:50]:
        print("  - %s %s %s 旧=%s 新=%s | %s"
              % (d[0], d[1], d[2], d[3], d[4], d[6]))
    if len(diffs) > 50:
        print("  ... 其余 %d 条略" % (len(diffs) - 50))


def main(argv=None, log_dir=None):
    p = argparse.ArgumentParser(description="影子比对报告")
    p.add_argument("--since", default=None,
                   help="起始日期 YYYY-MM-DD（默认：全部日志，含今天）")
    args = p.parse_args(argv)

    infos, diffs = collect(args.since, log_dir or LOGS)
    total = len(infos)
    agree = sum(1 for g in infos if g[5] == "True")
    disagree = total - agree
    edge_raw = sum(1 for g in infos if g[6])
    print("影子比对样本: %d 条（一致 %d / 分歧 %d，一致率 %.1f%%）"
          % (total, agree, disagree, (agree / total * 100) if total else 0))
    if not diffs:
        print("零分歧。")
        return 0
    print("其中窗口边沿预期差异 %d 条，真实分歧 %d 条。"
          % (edge_raw, disagree - edge_raw))
    real, edge = split_diffs(diffs)
    _print_diffs("真实分歧明细（去重后 %d 条，按 phase/date/train/旧/新 去重）："
                 % len(real), real)
    if edge:
        _print_diffs("窗口边沿明细（去重后 %d 条，预期差异：影子窗口 5s/180s 比"
                     "现网 30s/600s 收紧所致，宽窗下内核结论与现网一致；"
                     "窗口口径切 B 线时由用户定，此处仍计入分歧总数、"
                     "门禁不放行）：" % len(edge), edge)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())