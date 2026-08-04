"""JSON logging and the contextvars run binding (research.md )."""

from __future__ import annotations

import json
import logging
from typing import Any

from aggregato.logging import JsonFormatter, RunContextFilter, bind_run, current_run


def emit(level: int = logging.INFO, **extra: Any) -> dict[str, Any]:
    """Format one record through the filter and formatter, as configure_logging wires them."""
    record = logging.LogRecord("aggregato.sync", level, __file__, 1, "fetched %d", (7,), None)
    for key, value in extra.items():
        setattr(record, key, value)
    assert RunContextFilter().filter(record)
    parsed: dict[str, Any] = json.loads(JsonFormatter().format(record))
    return parsed


def test_formatter_emits_parseable_json() -> None:
    payload = emit(items=7)

    assert payload["message"] == "fetched 7"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "aggregato.sync"
    assert payload["items"] == 7
    assert payload["ts"].endswith("+00:00")  # UTC-aware, never naive


def test_bound_run_context_appears_on_the_record() -> None:
    with bind_run(provider_id="listenbrainz", run_id="17", lineage_id="abc"):
        payload = emit()

    assert payload["provider_id"] == "listenbrainz"
    assert payload["run_id"] == "17"
    assert payload["lineage_id"] == "abc"


def test_nothing_bound_means_no_run_fields() -> None:
    """The API process logs outside any run; absent fields simply do not appear."""
    payload = emit()

    assert current_run() == {}
    assert not {"provider_id", "run_id", "lineage_id"} & set(payload)


def test_binding_does_not_leak_out_of_its_context() -> None:
    with bind_run(provider_id="a"):
        with bind_run(run_id="1"):
            assert current_run() == {"provider_id": "a", "run_id": "1"}
        assert current_run() == {"provider_id": "a"}
    assert current_run() == {}
    assert "provider_id" not in emit()


def test_binding_is_reset_after_an_exception() -> None:
    try:
        with bind_run(run_id="1"):
            raise RuntimeError("run failed")
    except RuntimeError:
        pass
    assert current_run() == {}


def test_exception_info_is_included() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "aggregato.sync", logging.ERROR, __file__, 1, "failed", None, None
        )
        import sys

        record.exc_info = sys.exc_info()
        payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exception"]
