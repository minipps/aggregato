"""JSON structured logging with a ``contextvars`` run binding (research.md ).

Stdlib ``logging`` plus a formatter and a filter, no dependency: ``structlog`` is the upgrade path
if binding ever gets painful, noted rather than pre-adopted. ``sync_runs.log_excerpt``
reads the tail of these records for a failed run, so every field it needs is on the record itself.

The module name shadows nothing: Python 3 imports are absolute, so ``import logging`` below is the
stdlib module.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

__all__ = ["JsonFormatter", "RunContextFilter", "bind_run", "configure_logging", "current_run"]

RUN_FIELDS = ("provider_id", "run_id", "lineage_id")

# A ContextVar rather than a thread local: the API is async, and a task inherits the binding of the
# context that spawned it. Empty means "not inside a run" — the API process logs that way.
# `None`, not `{}`: a mutable ContextVar default is shared by every context (ruff B039).
_run: ContextVar[dict[str, str] | None] = ContextVar("aggregato_run", default=None)

# Set by LogRecord's own constructor, so anything not in here is caller-supplied `extra`.
_STANDARD = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def current_run() -> dict[str, str]:
    """The run fields bound to the current context, empty outside a run."""
    return dict(_run.get() or {})


@contextmanager
def bind_run(**fields: str) -> Iterator[None]:
    """Bind run fields (``provider_id``, ``run_id``, ``lineage_id``) for the enclosed block.

    Args:
        **fields: Values to bind, merged over anything already bound.

    The binding is reset on exit, including on exception, so it cannot leak into the next run.
    """
    token = _run.set({**(_run.get() or {}), **fields})
    try:
        yield
    finally:
        _run.reset(token)


class RunContextFilter(logging.Filter):
    """Copies the bound run fields onto every record. Adds nothing when nothing is bound."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in (_run.get() or {}).items():
            setattr(record, key, value)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per record: timestamp, level, logger, message, run fields, extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            # UTC-aware, never naive — every timestamp in this system is (data-model.md).
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(
            {
                k: v
                for k, v in record.__dict__.items()
                if k not in _STANDARD and not k.startswith("_")
            }
        )
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # default=str so an unserializable `extra` degrades to a repr instead of losing the record.
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Install the JSON formatter and the run filter on the root handler.

    Idempotent: replaces the root handlers, so calling it twice does not double every line.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RunContextFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
