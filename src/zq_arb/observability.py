from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import structlog

from zq_arb.config import Settings

SENSITIVE_KEY = re.compile(
    r"(password|passphrase|secret|private[_-]?key|api[_-]?key|authorization|cookie|account[_-]?id)",
    re.IGNORECASE,
)


def redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if SENSITIVE_KEY.search(str(key)) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    return value


def _redact_processor(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    redacted = redact_value(event_dict)
    return dict(redacted) if isinstance(redacted, Mapping) else event_dict


def configure_logging(settings: Settings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(message)s")
    # The official TWS client can log serialized wire messages containing account
    # and order fields. Application events are normalized before they are logged.
    for logger_name in (
        "ibapi",
        "ibapi.client",
        "ibapi.wrapper",
        "ibapi.comm",
        "httpx",
        "httpcore",
    ):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    if settings.log_redact_secrets:
        processors.append(_redact_processor)
    processors.extend(
        [
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    )
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def ensure_runtime_directories(settings: Settings) -> None:
    for directory in (settings.runtime_data_dir, settings.log_dir, settings.audit_export_dir):
        Path(directory).mkdir(parents=True, exist_ok=True)
