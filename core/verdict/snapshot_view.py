# -*- coding: utf-8 -*-
"""
官方订单快照视图（core/verdict/snapshot_view，仅依赖标准库）。

对「官方三列表（未完成 NoComplete / 未出行 G / 历史 H）合并出的统一订单列表」
做纯计算视图：去重、本行程筛选、时间戳归因（这对本次下单判定与防重复至关
重要）。60s 快照缓存在 adapters/railway/snapshot（provider 层），本模块不做 IO。

归因窗口（规划书 P1-6/P1-7）：下界 t-5s（只容忍少量时钟偏差），上界
t+180s（覆盖 12306 落库延迟 1~3s 与提交后的处理时间）——比旧 find_recent
的 [-30s, +600s] 收紧，避免把「更早遗留旧单」和「未来订单」误判成本次生成。
"""

from core.verdict.classifier import classify_entry, trip_matched_entries

# 归因窗口常量（秒）
RECENT_WINDOW_LO = 5     # 领先容差（时钟偏差容忍下限）
RECENT_WINDOW_HI = 180   # 落库 + 处理延迟上限


def dedupe(orders):
    """按订单号去重（order_no 非空优先保留；空 order_no 原样退让）。
    返回新列表，保持原顺序。"""
    seen, out = {}, []
    for e in (orders or []):
        if not isinstance(e, dict):
            continue
        ono = e.get("order_no") or ""
        if ono:
            if ono in seen:
                continue  # 首见保留
            seen[ono] = True
        out.append(e)
    return out


def for_trip(trip, orders, target_names):
    """本行程命中的有效条目（去重后）。"""
    return trip_matched_entries(trip, dedupe(orders), target_names)


def find_recent_order(trip, orders, target_names, not_before_ts,
                      window_lo=RECENT_WINDOW_LO, window_hi=RECENT_WINDOW_HI):
    """时间戳归因：在快照里找「本行程 + 下单时刻落在本次提交窗口内」的订单。

    not_before_ts 是本次发起提交动作的时刻（epoch 秒）。订单 order_ts 若
    ∈ [not_before_ts - window_lo, not_before_ts + window_hi] 才可能是本次
    生成；更早是遗留旧单，更晚是「未来」订单（不可能本次生成）。返回
    命中条目（取最新）或 None。

    与旧 find_recent_order 的收紧一致：全部目标乘车人命中才算（部分重叠
    的他人订单不会误归因）。
    """
    lo = not_before_ts - window_lo
    hi = not_before_ts + window_hi
    best = None
    for e in for_trip(trip, orders, target_names):
        ts = e.get("order_ts")
        if ts is None or ts < lo or ts > hi:
            continue
        if best is None or ts > best.get("order_ts", 0):
            best = e
    return best


def find_earliest_unpaid(trip, orders, target_names):
    """本行程最早未支付订单（供「遗留未支付单」提示）。返回条目或 None。"""
    best = None
    for e in for_trip(trip, orders, target_names):
        if not e.get("_no_complete"):
            continue
        # 缺 order_ts 的条目统一按 0 归一后再比——直接拿 None 比较会
        # TypeError（R2-9）；旧写法 `ts < best.get(...) or 0` 还有优先级坑。
        ts = e.get("order_ts") or 0
        if best is None or ts < (best.get("order_ts") or 0):
            best = e
    return best


def summarize(orders):
    """快照摘要（影子比对日志用）：条数 + 按分类梗概。不输出任何个人数据。"""
    n = len(orders or [])
    paid = sum(1 for e in orders if classify_entry(e).value == "paid")
    unpaid = sum(1 for e in orders if e.get("_no_complete"))
    return "%d 条(已支付%d 未完成%d)" % (n, paid, unpaid)