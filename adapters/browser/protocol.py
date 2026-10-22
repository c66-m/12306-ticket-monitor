# -*- coding: utf-8 -*-
"""
浏览器子进程结构化结果协议（adapters/browser/protocol，P2-5）。

把 orderflow/browser_order 之间、以及子进程→父进程的「下单结果」从
易漂移的 (ok, msg, extra) 返回值约定，升级为**约定字段 + 版本号**的一行
JSON 协议。契约集中在本文档：

- 序列化：encode_line(...) → stdout 单行 JSON（协议行带 ^12306-RPC^ 前缀，
  普通日志行不与之混淆）。
- 反序列化：decode_line(line) → dict 或 None（非协议行返回 None，父进程在
  正常日志流里也能安全过滤）。
- 校验：validate(payload) 按版本做字段校验，坏包明显报错不静默。

证据字段（evidence）约定：
  url          提交后最终停留页 URL
  page_kind    pay_order / order_init / queue / unknown
  pay_evidence 是否有「席位已锁定/待付款」字样
  need_captcha 是否出现滑块（人工接管标志）
本模块不做任何判定——判定权在 core/verdict（只出证据，不出结论）。
"""

import json

PROTOCOL_V1 = 1
PREFIX = "^12306-RPC^"


def _line(data):
    payload = dict(data)
    payload.setdefault("v", PROTOCOL_V1)
    return PREFIX + json.dumps(payload, ensure_ascii=False)


def decode_line(line):
    """解析一行输出为 payload dict；非协议行返回 None。坏 JSON 返回 None。"""
    if not line or not isinstance(line, str):
        return None
    s = line.strip()
    if not s.startswith(PREFIX):
        return None
    try:
        return json.loads(s[len(PREFIX):])
    except ValueError:
        return None


def encode_ok(attempt_id=None, evidence=None, timings=None):
    return _line({"kind": "order_result", "ok": True, "result": "success",
                  "attempt_id": attempt_id, "evidence": evidence or {},
                  "timings": timings or {}})


def encode_fail(result, message, evidence=None, timings=None):
    """result ∈ dup / ambiguous / fail / user_stopped / need_captcha 等。"""
    return _line({"kind": "order_result", "ok": False, "result": result,
                  "message": message or "", "evidence": evidence or {},
                  "timings": timings or {}})


def validate(payload):
    """校验协议包。返回错误字符串列表（空 = 通过）。"""
    errs = []
    if not isinstance(payload, dict):
        return ["payload 不是对象"]
    if payload.get("v") != PROTOCOL_V1:
        errs.append("版本 %r 不受支持" % payload.get("v"))
    if payload.get("kind") != "order_result":
        errs.append("kind 必须为 order_result")
    if payload.get("ok") not in (True, False):
        errs.append("ok 缺失")
    elif not payload.get("ok") and not payload.get("result"):
        errs.append("失败包缺 result")
    return errs