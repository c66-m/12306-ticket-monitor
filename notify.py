# -*- coding: utf-8 -*-
"""
通知模块：订单提交成功后通过邮件发送购票详情。

只实现邮件（需求中的短信/App 推送暂不做，可自行扩展 Server酱等渠道）。
SMTP 参数在 config.json 的 notify.email 中配置。
"""

import re
import smtplib
import sys
import time
import logging
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

import passengers as pax_mod
from passengers import SecretDecryptError  # noqa: F401  供 gui/测试引用

LOG = logging.getLogger("monitor")

# 前缀常量随公开 API 迁至 passengers；此处保留兼容别名。
_DPAPI_PREFIX = pax_mod._DPAPI_PREFIX


def protect_secret(text):
    """敏感串（SMTP 授权码）加密落盘。

    公开实现已迁至 passengers.protect_secret，此处保留作兼容委托。"""
    return pax_mod.protect_secret(text)


def secret_of(text):
    """取回敏感串明文：带 dpapi1: 前缀则解密；旧明文原样返回。

    解密失败抛 SecretDecryptError（Task 60：不再吞成空串误导为 535）。"""
    return pax_mod.unprotect_secret(text)


def _normalize_recipients(to, fallback_user):
    """收件人归一化为 list（Task 60a）。

    str → 按逗号/分号（含全角）拆分；list/tuple → 逐项去空白；
    空结果回退 [fallback_user]（旧语义：to 未配置时发给发件人自己）。
    """
    if isinstance(to, str):
        items = re.split(r"[,;，；]", to)
    elif isinstance(to, (list, tuple)):
        items = list(to)
    elif to:
        items = [to]
    else:
        items = []
    addrs = [str(x).strip() for x in items]
    addrs = [a for a in addrs if a]
    return addrs or [fallback_user]


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
        from_addr = cfg["from"]
        to_addrs = _normalize_recipients(cfg.get("to"), user)
        raw_pwd = cfg["password"]
    except KeyError as e:
        return False, "邮件配置缺少字段: {0}".format(e)
    if not isinstance(raw_pwd, str):
        # Task 71：手改配置把 password 写成非字符串（如数字）时，
        # secret_of 会 AttributeError 逃出 (ok,msg) 契约——如实报错。
        return False, "邮件配置 password 非法（应为字符串）: {0!r}".format(raw_pwd)
    try:
        pwd = secret_of(raw_pwd)  # 兼容明文与 DPAPI 密文两种存储
    except SecretDecryptError as e:
        # Task 60b：解密失败如实报错，不拿空密码去登录误报 535
        return False, "邮箱授权码解密失败: {0}".format(e)

    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = formataddr((str(Header("购票监控", "utf-8")), from_addr))
    msg["To"] = ",".join(to_addrs)
    msg["Subject"] = Header(subject, "utf-8")

    # Task 60d：瞬时异常退避重试 2 次（共 3 次尝试）；认证失败不重试。
    last_err = None
    for attempt in range(3):
        try:
            if port == 465:
                s = smtplib.SMTP_SSL(host, port, timeout=15)
            else:
                s = smtplib.SMTP(host, port, timeout=15)
                s.starttls()
            s.login(user, pwd)
            s.sendmail(from_addr, to_addrs, msg.as_string())
            try:
                s.quit()
            except Exception:
                pass
            return True, "邮件已发送给 {0}".format(",".join(to_addrs))
        except smtplib.SMTPAuthenticationError as e:
            # 认证失败（535 类）：重试无意义，立即明确失败
            return False, "邮件发送失败（认证失败，请检查发件邮箱/授权码）: {0}".format(e)
        except (smtplib.SMTPException, OSError) as e:
            # 瞬时异常（连接断开/超时/4xx/网络抖动）：退避后重试
            last_err = e
            LOG.warning("[通知] SMTP 瞬时异常（第 %d/3 次尝试）: %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(2 ** attempt)
        except Exception as e:
            return False, "邮件发送失败: {0}".format(e)
    return False, "邮件发送失败（已重试 2 次）: {0}".format(last_err)


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