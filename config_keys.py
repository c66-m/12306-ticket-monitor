# -*- coding: utf-8 -*-
"""配置键的唯一定义处：代码读取的键 ⊆ 对应 example 模板键（tests 有回归测试）。"""

CONFIG_KEYS = frozenset({
    "poll_interval_seconds", "min_interval_seconds", "order_retry_times",
    "order_retry_delay_seconds", "order_retry_cooldown_seconds", "adaptive",
    "session_cookies_file", "state_file", "history_file", "passengers_file",
    "log_dir", "http_timeout", "notify", "tasks",
    "order_mode", "browser_headless",
})
NOTIFY_EMAIL_KEYS = frozenset({
    "enabled", "smtp_host", "smtp_port", "username", "password", "from", "to",
})
LAUNCHER_CONFIG_KEYS = frozenset({
    "version", "passenger_names", "from", "to", "trains", "seat_types",
    "date", "date_to", "start_time", "remind_minutes", "warm_minutes",
    "poll_seconds", "presets", "update_url", "station_history",
    "purpose_code", "query_history", "synced_trains",
    "pax_purpose", "seat_priority",
})
# grab_tasks.json 任务条目在 launcher 配置形态之外额外的字段
LAUNCHER_TASK_EXTRA_KEYS = frozenset({"id", "name", "status"})
