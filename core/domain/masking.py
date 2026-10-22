# -*- coding: utf-8 -*-
"""
脱敏与身份口径（core/domain/masking，仅依赖标准库）。

目标（规划书 P1-2）：乘车人/订单号/证件号的掩码口径单源化，供 core 判定与
未来日志统一使用。appcommon 的既有 mask_* 仍在使用期，不作删改（口径一致：
本模块与 appcommon 实现语义相同），P4 清理期再做收敛转发。

红线：任何日志/通知/台账链路不得经本模块输出完整证件号。
"""

import re

_SPACE_RE = re.compile(r"\s+")


def mask_order_no(ono):
    """订单号脱敏：保留前后各 4 位，中间打码；过短则全打码。"""
    s = str(ono or "")
    if len(s) <= 8:
        return "****"
    return s[:4] + "****" + s[-4:]


def mask_one_name(n):
    """单姓名打码：保留首字，其余打码。"""
    n = (n or "").strip()
    if not n:
        return ""
    return n[0] + "*" * (len(n) - 1) if len(n) > 1 else "*"


def mask_names(names):
    """姓名列表打码：['张三','李四'] -> '张*、李*'。"""
    out = []
    for n in names or []:
        m = mask_one_name(n)
        if m:
            out.append(m)
    return "、".join(out)


def mask_id_partial(id_no):
    """证件号脱敏：保留前 3 位与后 4 位，其余打码。"""
    if not id_no or len(id_no) < 8:
        return id_no or ""
    return id_no[:3] + "*" * (len(id_no) - 7) + id_no[-4:]


def id_tail(id_no):
    """证件尾 4 位（行程指纹与归因用）。空输入返回空串。"""
    s = str(id_no or "").strip()
    return s[-4:] if len(s) >= 4 else s