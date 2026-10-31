# -*- coding: utf-8 -*-
"""rpc.settings —— RPC 域 settings.* + network.* + update.*：设置读写、邮件测试、
IP 归属与检查更新。

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

from rpc.common import (TASK_DEFAULTS_DEF, _STAMP_UNSET, _config_stamp, _task_defaults, _warn, load_config, normalize_email_settings, update_config_locked)

import grabber
import grabber_utils
import notify as notify_mod

def rpc_ip_locate(_p):
    """本机 IP 归属地（百度公开接口）。"""
    return {"text": grabber_utils.ip_locate()}

def rpc_check_update(_p):
    """检查更新（读 launcher_config.json 的 update_url；未配置时 has_new=False）。"""
    lc = grabber.load_launcher_config()
    has_new, ver, notes, download = grabber.check_update(lc)
    return {"has_new": bool(has_new), "version": ver or "",
            "notes": notes or "", "download": download or "",
            "configured": bool((lc.get("update_url") or "").strip()),
            "current": getattr(grabber, "__version__", "")}

# ---------------- 设置（SMTP + 会话 + order_mode） ----------------

def _notify_dict(cfg):
    """config.notify 的 dict 视图：手改成字符串等非 dict 时降级为空 dict。

    旧口径假定 notify/email 必为 dict：config 被手改成 "notify": "oops"
    时 .get() 抛错，设置页整体打不开——而设置页正是能修它的入口
    （2026-10-25 审）。与 _task_defaults 对 task_defaults 的口径一致。
    """
    notify = cfg.get("notify") if isinstance(cfg, dict) else None
    if not isinstance(notify, dict):
        return {}
    return notify


def rpc_get_settings(_p):
    cfg = load_config()
    email = dict(_notify_dict(cfg).get("email") or {})
    # 授权码脱敏显示：有密码 → 掩码占位，前端保存时才回写
    out = dict(email)
    if out.get("password"):
        try:
            pw = notify_mod.secret_of(out["password"])
            out["password"] = "******" if pw else out["password"]
        except Exception as e:
            _warn("邮箱授权码脱敏", e)
            out["password"] = "******"
    return {
        "order_mode": cfg.get("order_mode") or "browser",
        "email": out,
        "notify_enabled": bool(_notify_dict(cfg).get("email", {}).get("enabled")),
        "poll_interval_seconds": cfg.get("poll_interval_seconds"),
        "task_defaults": _task_defaults(cfg),
        # 乐观并发基线：设置页保存时原样带回（与 listPassengers 的 stamp 同口径）
        "stamp": _config_stamp(),
    }

def rpc_save_settings(p):
    """p = {email?, order_mode?, poll_interval_seconds?, task_defaults?, expect_stamp?}。

    expect_stamp 为 getSettings 下发的 stamp 时做乐观并发检查：抢票进程（grabber/
    monitor）与设置页并发写 config.json 时，后写方不再静默覆盖前写方。
    """
    try:
        def _upd(cfg):
            if p.get("email") is not None:
                # notify 被手改成非 dict 时先复位，否则 setdefault 返回
                # 字符串、后续 ["email"] 赋值抛错，保存路径同样被堵。
                if not isinstance(cfg.get("notify"), dict):
                    cfg["notify"] = {}
                email = dict(p["email"])
                # 掩码占位不动原密码；明文才加密回写
                old_pw = _notify_dict(cfg).get("email", {}).get("password", "")
                pw = email.get("password") or ""
                if pw == "******":
                    email["password"] = old_pw
                elif pw:
                    email["password"] = notify_mod.protect_secret(pw)
                elif old_pw:
                    email["password"] = old_pw     # 未提交密码：沿用旧值
                if not email.get("password"):
                    email.pop("password", None)
                email = normalize_email_settings(email)
                cfg.setdefault("notify", {})["email"] = email
            if p.get("order_mode"):
                # 引擎以 == "browser" 判断，拼写错误会把 browser 用户静默
                # 切回 http 模式（2026-10-25 审）：只接受白名单，其余报错。
                if p["order_mode"] not in ("http", "browser"):
                    raise ValueError("order_mode 只能是 http 或 browser：%s"
                                     % p["order_mode"])
                cfg["order_mode"] = p["order_mode"]
            if p.get("poll_interval_seconds") is not None:
                cfg["poll_interval_seconds"] = int(p["poll_interval_seconds"])
            if p.get("task_defaults") is not None:
                # 合并而非整块替换：只认已知键（其余丢弃），漏传的键保持原值。
                # 整块替换会让「前端只传一个键」把另外三个悄悄重置回默认。
                merged = _task_defaults(cfg)
                for k, v in dict(p["task_defaults"]).items():
                    if k in TASK_DEFAULTS_DEF:
                        merged[k] = bool(v)
                cfg["task_defaults"] = merged
        stamp = p["expect_stamp"] if "expect_stamp" in p else _STAMP_UNSET
        if not update_config_locked(_upd, expect_stamp=stamp):
            return {"ok": False, "error": "配置已被其它窗口或抢票进程修改，本次保存已取消，请刷新后重试",
                    "stamp": _config_stamp()}
        return {"ok": True, "stamp": _config_stamp()}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def rpc_test_email(_p):
    try:
        cfg = load_config()
        email = (cfg.get("notify") or {}).get("email") or {}
        ok, msg = notify_mod.send_email(dict(email), "测试邮件：12306 监控系统",
                                        "配置验证（桌面版设置页）")
        return {"ok": bool(ok), "message": msg or ""}
    except Exception as e:
        return {"ok": False, "error": str(e)}
