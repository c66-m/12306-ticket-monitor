# -*- coding: utf-8 -*-
"""
结构化日志（infra 层，仅依赖标准库）。

给新架构（app/ adapters/ core/）用的日志层：每行一个 JSON 对象
{ts(北京时间), level, logger, module, lineno, msg, correlation_id, ...}，
前端/工具可按字段解析，不再依赖正则抠文本。

旧模块的 LOG（"monitor" logger + DayFileHandler 纯文本）保持不动：
本层不注册到根 logger，不会污染既有日志流。P2 调度收编时再决定合并口径。

设计点：
- setup(name) 幂等：重复调用不叠加 handler。
- 经本层打出的每行带 correlation_id（thread-local）：下单尝试/任务一轮执行
  贯穿的日志可被一键串起来查——判定问题排查的关键基建。
"""

import json
import logging
import sys
import threading

_FORMATTER_CACHE = {}


def _beijing_now_str():
    from infra.clock import now_tz  # 延迟导入避免模块加载期耦合
    return now_tz().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class JsonFormatter(logging.Formatter):
    """把 LogRecord 转成单行 JSON。msg 必须是可 json 化对象；不可 json 化
    时降级为 str 保存（日志绝不能因为格式化失败而丢失）。"""

    def format(self, record):
        d = {
            "ts": _beijing_now_str(),
            "level": record.levelname,
            "logger": record.name,
            "module": record.module,
            "line": record.lineno,
            "msg": record.getMessage(),
        }
        cid = getattr(record, "correlation_id", None)
        if cid:
            d["correlation_id"] = cid
        extra = getattr(record, "struct_extra", None)  # 供调用方附带的结构字段
        if isinstance(extra, dict):
            for k in ("task", "trip_key", "attempt_id", "verdict_decision"):
                if k in extra:
                    d[k] = extra[k]
        try:
            return json.dumps(d, ensure_ascii=False)
        except (TypeError, ValueError):
            d["msg"] = str(record.getMessage())
            return json.dumps(d, ensure_ascii=False)


class _CorrelationFilter(logging.Filter):
    """为经本层产生的记录附加 correlation_id（读取自线程本地上下文）。"""

    def __init__(self, ctx):
        super().__init__("")
        self._ctx = ctx

    def filter(self, record):
        cid = self._ctx.get()
        if cid:
            record.correlation_id = cid
        return True


class _Ctx:
    """thread-local 的 correlation 上下文。"""

    def __init__(self):
        self._local = threading.local()

    def get(self):
        return getattr(self._local, "cid", None)

    def set(self, cid):
        self._local.cid = cid


CTX = _Ctx()


def correlation_scope(cid):
    """上下文管理器：with correlation_scope("att-123"): 块内日志自动带该 id。"""
    import contextlib

    @contextlib.contextmanager
    def _cm(cid):
        prev = CTX.get()
        CTX.set(cid)
        try:
            yield
        finally:
            CTX.set(prev)

    return _cm(cid)


def log_extra(**kwargs):
    """给下一条经本层的日志附带结构字段。用法：logger.info("x", extra={"struct_extra": log_extra(task=name)})"""
    return kwargs


def setup(name, level=logging.INFO, stream=None):
    """返回一个挂好 JSON handler 的 logger；幂等，重复调用不叠加 handler。

    stream 默认 stderr（新架构未接 DayFileHandler 前先落 stderr；
    P2 收编后统一接按天滚动文件）。
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    for h in list(logger.handlers):
        if getattr(h, "_infra_json", False):
            logger.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setLevel(level)
    handler._infra_json = True  # type: ignore[attr-defined]
    handler.setFormatter(JsonFormatter())
    handler.addFilter(_CorrelationFilter(CTX))
    logger.addHandler(handler)
    return logger


def get(name):
    """获取 logger；如需 JSON handler 请先 setup。"""
    return logging.getLogger(name)