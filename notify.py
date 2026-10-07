# -*- coding: utf-8 -*-
"""
通知模块：订单提交成功后通过邮件发送购票详情。

只实现邮件（需求中的短信/App 推送暂不做，可自行扩展 Server酱等渠道）。
SMTP 参数在 config.json 的 notify.email 中配置。
"""

import smtplib
import sys
import logging
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

LOG = logging.getLogger("monitor")

_DPAPI_PREFIX = "dpapi1:"


def protect_secret(text):
    """敏感串（SMTP 授权码）用 Windows DPAPI 加密后落盘。

    返回带 "dpapi1:" 前缀的密文；非 Windows / 加密失败时原样返回明文
    （保持向后兼容）。已加密的原样返回，避免二次包裹。"""
    if not text or text.startswith(_DPAPI_PREFIX):
        return text
    try:
        import base64
        import passengers as pax_mod
        blob = pax_mod._dpapi_protect(text.encode("utf-8"))
        return _DPAPI_PREFIX + base64.b64encode(blob).decode("ascii")
    except Exception:
        return text


def secret_of(text):
    """取回敏感串明文：带 dpapi1: 前缀则解密；旧明文原样返回。"""
    if text and text.startswith(_DPAPI_PREFIX):
        try:
            import base64
            import passengers as pax_mod
            return pax_mod._dpapi_unprotect(
                base64.b64decode(text[len(_DPAPI_PREFIX):])).decode("utf-8")
        except Exception:
            return ""
    return text or ""


def _safe_port(cfg):
    """取 SMTP 端口：非法值记警告后回退 465，绝不抛异常。"""
    raw = cfg.get("smtp_port", 465)
    try:
        return int(raw)
    except (ValueError, TypeError):
        LOG.warning("[配置] smtp_port 非法，已回退 465：%r", raw)
        return 465


def send_email(cfg, subject, body):
    """
    cfg: config.json 中的 notify.email 字典
    返回 (ok, msg)
    """
    if not cfg.get("enabled"):
        return True, "邮件未启用"
    try:
        host = cfg["smtp_host"]
        port = _safe_port(cfg)
        user = cfg["username"]
        pwd = secret_of(cfg["password"])  # 兼容明文与 DPAPI 密文两种存储
        from_addr = cfg["from"]
        to_addrs = cfg.get("to") or [user]
    except KeyError as e:
        return False, "邮件配置缺少字段: {0}".format(e)

    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = formataddr((str(Header("购票监控", "utf-8")), from_addr))
    msg["To"] = ",".join(to_addrs)
    msg["Subject"] = Header(subject, "utf-8")

    try:
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            s = smtplib.SMTP(host, port, timeout=15)
            s.starttls()
        s.login(user, pwd)
        s.sendmail(from_addr, to_addrs, msg.as_string())
        s.quit()
        return True, "邮件已发送给 {0}".format(",".join(to_addrs))
    except Exception as e:
        return False, "邮件发送失败: {0}".format(e)


if __name__ == "__main__":
    # 联调用例：python notify.py
    sys.path.insert(0, ".")
    import json
    with open("config.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)
    ok, msg = send_email(cfg["notify"]["email"],
                         "测试邮件：12306 监控系统",
                         "这是一封测试邮件。\n如果收到说明邮件通知可用。")
    print(msg)
    sys.exit(0 if ok else 1)