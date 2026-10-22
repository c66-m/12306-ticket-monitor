# -*- coding: utf-8 -*-
"""
事件驱动通知（app/notify_service，P3-6 雏形）。

订阅 infra/eventbus 的订单事件，把「台账归真」翻译成审计日志与（邮件）通知。
默认只审计（打 monitor 日志）——不发垃圾邮件；email 渠道在 config 明确启用
时才接 notify.py 发送。P4 会扩成多渠道（Server 酱/TG）的完整 notify service。

事件 tag 约定（P3 定义）：
    order.reconciled   台账对账后（payload 含 key/classify/decision）
    order.paid         官方确认已支付
    order.still_unpaid 官方仍为未支付
"""

import logging

LOG = logging.getLogger("monitor")


def _audit(tag, payload):
    """审计日志（默认动作）——日志永远有，通知是增益。"""
    key = payload.get("key", "")
    cls = payload.get("classify", "")
    decision = payload.get("decision", "")
    LOG.info("[对账·事件] %s key=%s classify=%s decision=%s",
             tag, key, cls, decision)


def _maybe_email(config, tag, payload):
    """config 明确启用 email 通知时走 notify.py 发送；其余静默。"""
    try:
        cfg = config or {}
        email = ((cfg.get("notify") or {}).get("email") or {})
        if not email.get("enabled"):
            return
        import notify as notify_mod
        subject, body = _to_email(tag, payload)
        notify_mod.send_email_if_configured(email, subject, body)
    except Exception:  # 通知失败绝不反噬主流程
        LOG.warning("[通知] 邮件发送失败（忽略）", exc_info=True)


def _to_email(tag, payload):
    key = payload.get("key", "")
    cls = payload.get("classify", "")
    decision = payload.get("decision", "")
    return ("[对账] %s" % key,
            "行程 %s 官方状态 %s\n决策：%s" % (key, cls, decision))


def start(config=None, only_audit=False):
    """接线 eventbus。进程初始化时调用一次。only_audit=True 时永不发邮件。"""
    from infra import eventbus

    def _on(tag, payload):
        _audit(tag, payload)
        if not only_audit:
            _maybe_email(config, tag, payload)

    eventbus.subscribe("order.reconciled", _on)
    eventbus.subscribe("order.paid", _on)
    eventbus.subscribe("order.still_unpaid", _on)
    return _on