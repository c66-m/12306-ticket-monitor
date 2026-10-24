# -*- coding: utf-8 -*-
"""rpc.history —— RPC 域 problems.*：问题台账的读取与清空。

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

import logutil

def rpc_list_problem_logs(p):
    """p = {limit}：问题台账（WARNING 及以上），最新在前。"""
    try:
        limit = int(p.get("limit") or 200)
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(limit, 1000))
    return {"records": logutil.read_problem_log(limit=limit)}

def rpc_clear_problem_logs(p):
    """清空问题台账（界面「问题日志」页的「清空」）。

    必须显式 confirm=true：RPC 冒烟测试会带空参数逐个调用方法，不加这道门
    会在跑测试时把用户真实台账清掉。
    """
    # 只认真值 True / 1 / "true"：旧口径判真值，字符串 "false" 同样通过门禁。
    if p.get("confirm") not in (True, 1, "true"):
        return {"ok": False, "error": "需要 confirm=true 才清空问题日志"}
    return {"ok": logutil.clear_problem_log()}
