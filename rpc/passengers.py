# -*- coding: utf-8 -*-
"""rpc.passengers —— RPC 域 passengers.*：本地乘车人管理与 12306 乘车人同步。

A-3a 自 api.py 逐字拆分：函数体未改动，仅补 import 与模块限定。
"""
import datetime
import json
import os
import queue
import re
import sys
import threading
import time
import traceback

from rpc.session import current_session

import grabber_utils as launcher_utils
import passengers as passengers_mod
import order as order_mod

# ---------------- 乘车人 ----------------

def rpc_list_passengers(_p):
    """列表接口：证件号 / 手机号一律脱敏，明文只在 passengerDetail 按需取。"""
    pax = passengers_mod.load_passengers()
    stamp = passengers_mod.passengers_stamp()
    safe = [dict(x, id_no=launcher_utils.mask_id(x.get("id_no")),
                 mobile=launcher_utils.mask_mobile(x.get("mobile"))) for x in pax]
    return {"passengers": safe, "stamp": stamp}

def rpc_passenger_detail(p):
    """p = {name}：取单个乘车人的明文记录（编辑弹窗专用）。"""
    for x in passengers_mod.load_passengers():
        if x.get("name") == p.get("name"):
            return {"ok": True, "passenger": x}
    return {"ok": False, "error": "乘车人不存在"}

def rpc_save_passengers(p):
    """p = {passengers: [...], expect_stamp: ...}（乐观并发）。

    列表接口（listPassengers）下发的是脱敏值，前端全量回写会把「****」
    写回文件 —— 含 '*' 的 id_no/mobile 一律视为「未修改」，用磁盘上的
    明文还原（证件号/手机号不会含 '*'，掩码串不可能与真实值撞车）。
    """
    try:
        old = {x.get("name"): x for x in passengers_mod.load_passengers()}
        rows = []
        for x in (p.get("passengers") or []):
            x = dict(x)
            prev = old.get(x.get("name")) or {}
            for k in ("id_no", "mobile"):
                v = x.get(k)
                if isinstance(v, str) and "*" in v:
                    x[k] = prev.get(k, "")
            rows.append(x)
        stamp = p.get("expect_stamp", passengers_mod._STAMP_UNSET)
        if stamp is None:
            # JSON null（前端尚未拿到 stamp）按「不检查」处理：旧代码把它当成
            # 「文件必须不存在」，文件已存在时每次保存都被静默拒绝。
            stamp = passengers_mod._STAMP_UNSET
        if not passengers_mod.save_passengers(rows, expect_stamp=stamp):
            return {"ok": False,
                    "error": "保存失败：文件被其它程序修改或占用，请刷新后重试"}
        return {"ok": True, "stamp": passengers_mod.passengers_stamp()}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def rpc_12306_passengers(_p):
    """从 12306 会话拉真实乘车人（需登录）。"""
    try:
        session = current_session()
        if session is None:
            return {"error": "未登录"}
        pax = order_mod.get_passengers(session)
        return {"passengers": pax}
    except Exception as e:
        return {"error": str(e)}
