# -*- coding: utf-8 -*-
"""
配置 schema（infra 层，仅依赖标准库）。

目标（规划书 P0-4 / C8）：config.json 与 launcher_config.json 两文件共用一份
键并集 + 类型 + 默认值定义；启动时校验、缺键补默认、非法值报清晰错误。
config_version 由本模块持有（不写进用户 config.json，避免与 config_keys.py
防漂移测试冲突；P3 数据迁移阶段再评估落盘版本号）。

红线：校验/补默认过程绝不读取、也不输出配置中的敏感值（SMTP 授权码、
会话 Cookie 等）。错误消息只含键名与期望类型。
"""

# 顶层键 → (类型判定, 默认值) 的紧凑 schema。类型用 callable 判定，兼容
# bool 是 int 子类的坑（先判 bool 再判 int）。
_CONFIG_TOP = {
    "auto_start_engine": (bool, True),
    "poll_interval_seconds": (int, 7),
    "min_interval_seconds": (int, 7),
    "order_retry_times": (int, 3),
    "order_retry_delay_seconds": (int, 2),
    "order_retry_cooldown_seconds": (int, 7),
    "adaptive": ("dict", None),
    "session_cookies_file": (str, "session_cookies.json"),
    "state_file": (str, "state.json"),
    "history_file": (str, "order_history.json"),
    "passengers_file": (str, "passengers.json"),
    "log_dir": (str, "logs"),
    "http_timeout": (int, 15),
    "notify": ("dict", None),
    "tasks": (list, []),
    "order_mode": (str, "browser"),
    "browser_headless": (bool, False),
    "keep_pay_page": (bool, True),
    "pay_page_keep_seconds": (int, 300),
    "order_flow_mode": (bool, True),
    "order_flow": ("dict", None),
    "task_defaults": ("dict", None),
}


def _is_type(v, kind):
    if kind is bool:
        return isinstance(v, bool)
    if kind is int:
        return isinstance(v, bool) is False and isinstance(v, int)
    if kind == "num":
        return isinstance(v, bool) is False and isinstance(v, (int, float))
    if kind is str:
        return isinstance(v, str)
    if kind is list:
        return isinstance(v, list)
    if kind == "dict":
        return isinstance(v, dict)
    return True  # "any"


_NESTED = {
    # config.json
    "adaptive": {
        "enabled": (bool, False),
        "peak_hours": (list, [6, 23]),
        "peak_multiplier": ("num", 1.0),
        "offpeak_multiplier": ("num", 1.6),
        "rush_within_hours": (int, 24),
        "rush_multiplier": ("num", 0.75),
    },
    "order_flow": {
        "read_poll_seconds": ("num", 0.5),
        "page_wait_seconds": ("num", 10),
        "retry_open_times": (int, 1),
        "lock_confirm_seconds": ("num", 1.5),
    },
    "task_defaults": {
        "auto_order": (bool, True),
        "stop_after_order": (bool, True),
        "notify": (bool, True),
        "start_now": (bool, True),
    },
    "notify": {
        "email": {
            "enabled": (bool, False),
            "smtp_host": (str, ""),
            "smtp_port": (int, 465),
            "username": (str, ""),
            "password": (str, ""),
            "from": (str, ""),
            "to": (list, []),
        }
    },
    # launcher_config.json
    "presets": (list, []),
    "pax_purpose": (dict, {}),
}

_LIST_OF_STR = ("trains", "seat_types", "passenger_names", "station_history",
                "synced_trains")
_LIST_OF_DICT = ("tasks", "query_history", "presets")

_DICT_TOP = {"pax_purpose"}

# launcher_config.json 顶层键 → (类型判定, 默认值)
_LAUNCHER_TOP = {
    "version": (int, 1),
    "passenger_names": (list, []),
    "from": (str, ""),
    "to": (str, ""),
    "trains": (list, []),
    "seat_types": (list, []),
    "seat_priority": (str, ""),
    "date": (str, ""),
    "date_to": (str, ""),
    "start_time": (str, ""),
    "remind_minutes": (int, 3),
    "warm_minutes": (int, 1),
    "poll_seconds": ("num", 3),
    "presets": (list, []),
    "update_url": (str, ""),
    "station_history": (list, []),
    "purpose_code": (str, "ADULT"),
    "pax_purpose": (dict, {}),
    "query_history": (list, []),
    "synced_trains": (list, []),
}

SCHEMA_VERSION = 1


def _validate_nested(v, schema, path, errs):
    """递归校验嵌套 dict（支持任意层，如 notify.email）。"""
    if not isinstance(v, dict):
        errs.append("%s: 应为对象" % path)
        return
    for sk, sv in v.items():
        if sk in ("_说明",):
            continue
        if sk not in schema:
            errs.append("%s.%s: 未知子键 '%s'" % (path, sk, sk))
            continue
        sub = schema[sk]
        if isinstance(sub, dict):
            _validate_nested(sv, sub, "%s.%s" % (path, sk), errs)
            continue
        skind, _ = sub
        if not _is_type(sv, skind):
            errs.append("%s.%s: 期望类型 %s" % (path, sk, _type_name(skind)))


def _validate_section(cfg, top, nested, source):
    """校验顶层 + 嵌套（带路径的递归）。返回错误消息列表。绝不输出敏感值。"""
    errs = []
    if not isinstance(cfg, dict):
        return ["%s: 顶层必须是 JSON 对象" % source]
    for k, v in cfg.items():
        if k in ("_说明",):
            continue
        if k not in top and k not in nested:
            errs.append("%s: 未知键 '%s'（忽略，不删除）" % (source, k))
            continue
        if k in nested and isinstance(nested[k], dict):
            _validate_nested(v, nested[k], "%s.%s" % (source, k), errs)
            continue
        kind, _ = top.get(k, (None, None))
        if kind is None or _is_type(v, kind):
            continue
        errs.append("%s.%s: 期望类型 %s" % (source, k, _type_name(kind)))
    return errs


def _type_name(kind):
    return {bool: "bool", int: "int/num", "num": "int/num", str: "str",
            list: "list", "dict": "dict"}.get(kind, str(kind))


def _merge_nested(out, schema, path):
    """递归补默认：schema 为 dict 的再进一层（如 notify.email）。"""
    for sk, spec in schema.items():
        if isinstance(spec, dict):
            if sk not in out or not isinstance(out.get(sk), dict):
                out[sk] = {}
            _merge_nested(out[sk], spec, "%s.%s" % (path, sk))
            continue
        skind, sdefault = spec
        if sk not in out:
            out[sk] = sdefault


def _merge_defaults(cfg, top, nested, source):
    """缺键补默认（嵌套 dict 逐键补）。返回补齐后的新 dict，原 dict 不修改。"""
    out = dict(cfg) if isinstance(cfg, dict) else {}
    for k, (kind, default) in top.items():
        if k not in out and not (k in nested and isinstance(nested[k], dict)):
            out[k] = default
    for k, spec in nested.items():
        if isinstance(spec, dict):
            if k not in out or not isinstance(out.get(k), dict):
                out[k] = {}
            _merge_nested(out[k], spec, k)
    return out


def validate_config(config):
    """校验 config.json 形态的配置。返回错误列表（空 = 通过）。"""
    return _validate_section(config, _CONFIG_TOP, _NESTED, "config")


def validate_launcher(launcher):
    """校验 launcher_config.json 形态的配置。返回错误列表。"""
    return _validate_section(launcher, _LAUNCHER_TOP, _NESTED, "launcher")


def merge_config_defaults(config):
    """config.json 缺键补默认。返回新 dict。"""
    return _merge_defaults(config, _CONFIG_TOP, _NESTED, "config")


def merge_launcher_defaults(launcher):
    """launcher_config.json 缺键补默认。返回新 dict。"""
    return _merge_defaults(launcher, _LAUNCHER_TOP, _NESTED, "launcher")


def repair_config(config):
    """校验 + 补默认一体：返回 (repaired, errors)。errors 非空表示存在无法
    自行修复的硬伤（类型非法），调用方应提示用户人工处理。"""
    errs = validate_config(config)
    repaired = merge_config_defaults(config)
    return repaired, errs