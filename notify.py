# -*- coding: utf-8 -*-
"""
通知模块：订单提交成功后通过邮件发送购票详情。

只实现邮件（需求中的短信/App 推送暂不做，可自行扩展 Server酱等渠道）。
SMTP 参数在 config.json 的 notify.email 中配置。
"""

import smtplib
import sys
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr


def send_email(cfg, subject, body):
    """
    cfg: config.json 中的 notify.email 字典
    返回 (ok, msg)
    """
    if not cfg.get("enabled"):
        return True, "邮件未启用"
    try:
        host = cfg["smtp_host"]
        port = int(cfg.get("smtp_port", 465))
        user = cfg["username"]
        pwd = cfg["password"]
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