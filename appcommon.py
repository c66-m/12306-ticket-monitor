# -*- coding: utf-8 -*-
"""
跨界面共享的基础工具：日期区间解析、原子文件 IO。

只收编「无策略差异」的机械层。涉及锁（filelock / _CFG_WRITE_LOCK）、坏档
时间戳挪档、mtime 二次检测、deepcopy 快照与直写兜底等防护语义全部留在
调用方（参数化保留差异，不是抹平差异）——见 RULES.md。
"""

import datetime
import json
import os
import re
import threading
import time

MAX_DATE_SPAN_DAYS = 5   # 乘车日期区间最多相差天数（= 6 个日期）


# ----------------------------- 日期区间 -----------------------------

def parse_date_range(raw_from, raw_to=None, max_span_days=MAX_DATE_SPAN_DAYS):
    """把乘车日期输入解析为 (dates, date_range)，非法输入抛 ValueError。

    两种输入形态：
      - raw_to 给出（两变量形态）：raw_to 空或与 raw_from 相同 = 单日；
      - raw_to 为 None：raw_from 内允许「a~b」连写，无 ~ 即单日。
    返回 ISO 字符串；date_range 为 [起, 止] 或空列表。
    """
    if raw_to is None:
        raw = (raw_from or "").strip()
        if "~" in raw:
            a, b = [x.strip() for x in raw.split("~", 1)]
        else:
            a, b = raw, ""
    else:
        a, b = (raw_from or "").strip(), (raw_to or "").strip()
        if b == a:
            b = ""
    for part in (a, b):
        # 严格 YYYY-MM-DD：fromisoformat 也接受 20261007 之类紧凑写法，不放行
        if part and not re.match(r"^\d{4}-\d{2}-\d{2}$", part):
            raise ValueError("日期格式应为 YYYY-MM-DD（如 2026-10-07）")
    if not a:
        raise ValueError("日期格式应为 YYYY-MM-DD（如 2026-10-07）")
    d0 = datetime.date.fromisoformat(a)
    if b:
        d1 = datetime.date.fromisoformat(b)
        if d1 < d0:
            raise ValueError("结束日期不能早于开始日期")
        if (d1 - d0).days > max_span_days:
            raise ValueError("日期跨度最多相差 %d 天" % max_span_days)
        return [], [d0.isoformat(), d1.isoformat()]
    return [d0.isoformat()], []


# ----------------------------- 原子文件 IO -----------------------------

def replace_with_retry(src, dst, tries=3, delay=0.05, fallback_direct=False):
    """os.replace 带 Windows 占用重试：目标/源被并发读写句柄占用的瞬间会
    PermissionError（杀毒扫描、对方 json.load 持有读句柄），短暂退避重试。

    fallback_direct=True 时重试耗尽后把 src 内容直写 dst（engine 的既有兜底
    语义），否则抛出最后一个 OSError。
    """
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except OSError:
            if i == tries - 1:
                if fallback_direct and os.path.exists(src):
                    with open(src, "r", encoding="utf-8") as f:
                        data = f.read()
                    with open(dst, "w", encoding="utf-8") as f:
                        f.write(data)
                    return
                raise
            time.sleep(delay)


def atomic_write_json(path, obj, *, tmp_kind="tmp", replace_tries=3,
                      replace_delay=0.05, fallback_direct=False):
    """原子写 JSON：临时名带 pid+线程标识（同进程多线程/多窗口互不踩），
    写完 replace 并对 Windows 占用做退避重试。

    fallback_direct=True 时 replace 重试耗尽改为直写（丢原子性保数据，
    仅 engine 的 state.json 兜底使用这一语义）。
    """
    tmp = "{0}.{1}{2}-{3}".format(path, tmp_kind, os.getpid(),
                                  threading.get_ident())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    replace_with_retry(tmp, path, tries=replace_tries, delay=replace_delay,
                       fallback_direct=fallback_direct)
