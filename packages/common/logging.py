"""结构化日志与关联 ID（ENG-005）。

所有运行时事件（会话、帧、检测、状态迁移、意图、策略拒绝、急停）
都通过 correlation_id 串联，输出单行 JSON，便于轨迹归档与检索。

用法：
    log = get_logger("policy_engine")
    with correlation_scope(cid):
        log_event(log, "policy_denied", reason="foreground_mismatch", batch_id=...)
"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from common.ids import new_correlation_id

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

# LogRecord 的标准属性，不作为业务字段序列化
_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
}


def new_and_set_correlation_id() -> str:
    """生成新的 correlation_id 并绑定到当前上下文。"""
    cid = new_correlation_id()
    _correlation_id.set(cid)
    return cid


def get_correlation_id() -> str | None:
    return _correlation_id.get()


@contextmanager
def correlation_scope(cid: str | None = None) -> Iterator[str]:
    """在作用域内绑定 correlation_id；离开作用域恢复原值。"""
    token = _correlation_id.set(cid or new_correlation_id())
    try:
        yield _correlation_id.get()
    finally:
        _correlation_id.reset(token)


class JsonFormatter(logging.Formatter):
    """单行 JSON 格式化：时间、级别、来源、消息、correlation_id 与业务字段。"""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "cid": get_correlation_id(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def get_logger(name: str) -> logging.Logger:
    """获取已配置 JSON 格式的 logger（幂等）。"""
    logger = logging.getLogger(name)
    if not any(isinstance(h.formatter, JsonFormatter) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def configure_json_logging(level: int = logging.INFO) -> None:
    """全局默认级别（幂等，可在入口处调用一次）。"""
    logging.getLogger().setLevel(level)


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """记录一条带事件类型的结构化日志。"""
    logger.info(event, extra={"event": event, **fields})
