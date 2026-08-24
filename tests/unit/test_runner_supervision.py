"""Child-process supervision (, , , §6.7).

These drive **real subprocesses**, because the whole point of the child is what a process gives you
that a task does not: it can be killed, it can crash without taking the parent down, and it cannot
reach the parent's database handle. Mocking that away would test the mock.

The children here are tiny scripts rather than real providers, so each failure mode can be produced
on demand — a hang, a SIGKILL, a malformed line, an over-long line. A real provider cannot be made
to crash reliably, which is exactly why supervision needs its own tests.

`tests/conftest.py` blocks sockets in the parent; none of these needs the network.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path

import pytest

from aggregato.domain.enums import ErrorClass, FetchMode, RunStatus
from aggregato.domain.models import CheckResult, Cursor
from aggregato.sync import runner as runner_module
from aggregato.sync.protocol import (
    BatchMessage,
    CheckMessage,
    CheckpointMessage,
    ErrorMessage,
    FailureMessage,
    NowPlayingMessage,
    ProtocolViolation,
    decode,
    encode,
)
from aggregato.sync.runner import RunOutcome, _consume, _terminate

# --- The protocol itself ------------------------------------------------------------------------


def test_a_checkpoint_round_trips() -> None:
    message = CheckpointMessage(cursor=Cursor(state={"next_page": 3}))
    decoded = decode(encode(message))
    assert isinstance(decoded, CheckpointMessage)
    assert decoded.cursor.state == {"next_page": 3}


def test_a_check_result_round_trips() -> None:
    message = CheckMessage(result=CheckResult(ok=False, error_class=ErrorClass.AUTH, detail="nope"))
    decoded = decode(encode(message))
    assert isinstance(decoded, CheckMessage)
    assert decoded.result.error_class is ErrorClass.AUTH


def test_a_now_playing_result_round_trips_for_active_and_idle() -> None:
    active = NowPlayingMessage(result={"work": {"media_type": "track", "title": "Current track"}})
    decoded = decode(encode(active))
    assert isinstance(decoded, NowPlayingMessage)
    assert decoded.result is not None
    assert decoded.result.work.title == "Current track"

    idle = decode(encode(NowPlayingMessage(result=None)))
    assert isinstance(idle, NowPlayingMessage)
    assert idle.result is None


def test_every_message_serializes_to_exactly_one_line() -> None:
    """The protocol is line-delimited, so an embedded newline would desynchronize the stream."""
    messages = [
        CheckpointMessage(cursor=Cursor(state={"a": 1})),
        ErrorMessage(error_class=ErrorClass.AUTH, message="line1\nline2", detail="a\nb\nc"),
        FailureMessage(native_id="x", payload={"note": "has\nnewline"}, error="bad\nthing"),
    ]
    for message in messages:
        line = encode(message)
        assert line.count("\n") == 1, message
        assert line.endswith("\n")


def test_an_unknown_message_type_is_rejected() -> None:
    """A protocol we half-understand is worse than one we reject."""
    with pytest.raises(ProtocolViolation, match="invalid message"):
        decode('{"type": "something_new", "data": 1}')


def test_a_non_json_line_is_rejected() -> None:
    with pytest.raises(ProtocolViolation, match="invalid message"):
        decode("this is not json")


def test_an_empty_object_is_rejected() -> None:
    with pytest.raises(ProtocolViolation):
        decode("{}")


def test_an_over_long_line_is_rejected_before_parsing() -> None:
    """An unbounded line is a memory exhaustion the wall clock cannot catch."""
    from aggregato.sync.protocol import MAX_LINE_BYTES

    with pytest.raises(ProtocolViolation, match="over the"):
        decode("x" * (MAX_LINE_BYTES + 1))


def test_a_message_with_an_extra_field_is_rejected() -> None:
    with pytest.raises(ProtocolViolation):
        decode('{"type": "checkpoint", "cursor": {"state": {}}, "surprise": true}')


# --- Supervision against real child processes ---------------------------------------------------


async def _spawn(script: str, tmp_path: Path) -> asyncio.subprocess.Process:
    """Run a throwaway script as a child, wired like a real provider child."""
    path = tmp_path / "fake_child.py"
    path.write_text(script)
    spawn_options: dict[str, object] = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if os.name == "posix":
        spawn_options["start_new_session"] = True
    return await asyncio.create_subprocess_exec(
        sys.executable,
        str(path),
        **spawn_options,
    )


async def _run_child(script: str, tmp_path: Path, wall_clock: float = 5.0) -> RunOutcome:
    """Consume a child's output under a wall clock, the way execute_run does."""
    process = await _spawn(script, tmp_path)
    outcome = RunOutcome(status=RunStatus.RUNNING)
    try:
        async with asyncio.timeout(wall_clock):
            await _consume(process, outcome)
            await process.wait()
    except TimeoutError:
        await _terminate(process)
        outcome.status = RunStatus.PARTIAL if outcome.checkpointed else RunStatus.FAILED
        outcome.error_class = ErrorClass.INTERNAL
        outcome.error_message = "wall-clock limit exceeded"
        return outcome
    except ProtocolViolation as exc:
        await _terminate(process)
        outcome.status = RunStatus.PARTIAL if outcome.checkpointed else RunStatus.FAILED
        outcome.error_class = ErrorClass.INTERNAL
        outcome.error_message = str(exc)
        return outcome
    if outcome.error_class is None and process.returncode == 0:
        outcome.status = RunStatus.SUCCESS
    elif outcome.error_class is None:
        outcome.status = RunStatus.PARTIAL if outcome.checkpointed else RunStatus.FAILED
        outcome.error_class = ErrorClass.INTERNAL
        outcome.error_message = f"exited with code {process.returncode}"
    else:
        outcome.status = RunStatus.PARTIAL if outcome.checkpointed else RunStatus.FAILED
    return outcome


HEADER = "import sys, json, time\n"


async def test_termination_reaches_a_forked_descendant(tmp_path: Path) -> None:
    """A provider fork cannot outlive the process group the supervisor owns."""
    if os.name != "posix":
        pytest.skip("process groups use POSIX sessions")

    marker = tmp_path / "descendant-terminated"
    script = (
        "import os, signal, time\n"
        f"marker = {str(marker)!r}\n"
        "def on_term(signum, frame):\n"
        "    with open(marker, 'w', encoding='utf-8') as output:\n"
        "        output.write('terminated')\n"
        "    os._exit(0)\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    signal.signal(signal.SIGTERM, on_term)\n"
        "    print(os.getpid(), flush=True)\n"
        "    time.sleep(300)\n"
        "else:\n"
        "    time.sleep(300)\n"
    )
    process = await _spawn(script, tmp_path)
    descendant_pid: int | None = None
    try:
        assert process.stdout is not None
        descendant_pid = int((await process.stdout.readline()).decode())
        await _terminate(process)
        assert marker.read_text(encoding="utf-8") == "terminated"
    finally:
        if descendant_pid is not None and not marker.exists():
            with suppress(ProcessLookupError):
                os.kill(descendant_pid, signal.SIGKILL)
        if process.returncode is None:
            await _terminate(process)


async def test_stderr_cleanup_has_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader whose pipe never reaches EOF cannot hold the supervisor open forever."""

    async def blocked_stderr(_process: asyncio.subprocess.Process) -> str:
        await asyncio.get_running_loop().create_future()
        return ""

    monkeypatch.setattr(runner_module, "_read_stderr", blocked_stderr)
    monkeypatch.setattr(runner_module, "STDERR_CLEANUP_TIMEOUT_SECONDS", 0.01)

    from aggregato.sync.runner import RunRequest, execute_run

    outcome = await execute_run(
        RunRequest(
            provider_id="fixture",
            config={"path": str(FIXTURE_RECORDS)},
            wall_clock_seconds=5,
        )
    )

    assert outcome.status is RunStatus.SUCCESS, outcome.error_message


# Resolved at import time: ASYNC240 rightly objects to blocking filesystem calls inside a
# coroutine, and these paths are constants anyway.
FIXTURE_RECORDS = (Path(__file__).parent.parent / "fixtures/fixture/log-two-pages.jsonl").resolve()
FIXTURE_BROKEN = (
    Path(__file__).parent.parent / "fixtures/fixture/unreadable-not-json.jsonl"
).resolve()


def _write_now_playing_provider(provider_dir: Path, *, unexpected: bool = False) -> None:
    package = provider_dir / "now_playing_fixture"
    package.mkdir(parents=True)
    source = f"""from aggregato.domain.enums import Capability, MediaType
from aggregato.domain.models import NowPlayingItem, NormalizedWork
from aggregato.providers.fixture import FixtureProvider


class NowPlayingFixtureProvider(FixtureProvider):
    id = "now_playing_fixture"
    media_types = {{MediaType.TRACK}}
    capabilities = {{Capability.NOW_PLAYING}}

    async def now_playing(self, ctx):
        if {unexpected!r}:
            print('{{"type":"checkpoint","cursor":{{"state":{{}}}}}}', flush=True)
        if ctx.state.get("idle") == "1":
            return None
        return NowPlayingItem(
            work=NormalizedWork(media_type=MediaType.TRACK, title="Current track")
        )


provider = NowPlayingFixtureProvider()
"""
    (package / "__init__.py").write_text(source, encoding="utf-8")
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "name": "Now-playing fixture",
                "media_types": ["track"],
                "capabilities": ["now_playing"],
                "acquisition": "export",
                "schema_version": 1,
                "default_poll_interval_seconds": 3600,
                "config_schema": {"type": "object", "properties": {}},
            }
        ),
        encoding="utf-8",
    )


def _emit(obj: dict[str, object]) -> str:
    return f"sys.stdout.write(json.dumps({obj!r}) + '\\n'); sys.stdout.flush()\n"


async def test_now_playing_protocol_rejects_duplicate_results(tmp_path: Path) -> None:
    script = (
        HEADER
        + _emit({"type": "now_playing", "result": None})
        + _emit({"type": "now_playing", "result": None})
    )
    outcome = await _run_child(script, tmp_path)

    assert outcome.status is RunStatus.FAILED
    assert outcome.error_class is ErrorClass.INTERNAL
    assert "more than one" in (outcome.error_message or "")


def test_a_malformed_now_playing_result_is_rejected() -> None:
    with pytest.raises(ProtocolViolation, match="invalid message"):
        decode('{"type":"now_playing","result":{"work":{"media_type":"track"}}}')


def test_run_request_serializes_now_playing_operation() -> None:
    from aggregato.sync.runner import RunRequest

    assert (
        json.loads(RunRequest(provider_id="fixture", operation="now_playing").payload())[
            "operation"
        ]
        == "now_playing"
    )


async def test_a_clean_run_reports_success(tmp_path: Path) -> None:
    outcome = await _run_child(
        HEADER + _emit({"type": "checkpoint", "cursor": {"state": {"page": 2}}}), tmp_path
    )
    assert outcome.status is RunStatus.SUCCESS
    assert outcome.cursor_after is not None
    assert outcome.cursor_after.state == {"page": 2}


async def test_the_real_child_returns_active_and_idle_now_playing_results(tmp_path: Path) -> None:
    from aggregato.sync.runner import RunRequest, execute_run

    provider_dir = tmp_path / "providers"
    _write_now_playing_provider(provider_dir)
    request = RunRequest(
        provider_id="now_playing_fixture",
        operation="now_playing",
        provider_dir=provider_dir,
        config={"path": str(FIXTURE_RECORDS)},
        wall_clock_seconds=60,
    )

    active = await execute_run(request)
    assert active.status is RunStatus.SUCCESS, active.error_message
    assert active.records == []
    assert active.failures == []
    assert active.now_playing_result_received
    assert active.now_playing is not None
    assert active.now_playing.work.title == "Current track"

    idle = await execute_run(
        RunRequest(
            provider_id="now_playing_fixture",
            operation="now_playing",
            provider_dir=provider_dir,
            config={"path": str(FIXTURE_RECORDS)},
            state={"idle": "1"},
            wall_clock_seconds=60,
        )
    )
    assert idle.status is RunStatus.SUCCESS, idle.error_message
    assert idle.now_playing_result_received
    assert idle.now_playing is None


async def test_now_playing_requires_capability_and_protocol_method() -> None:
    from aggregato.sync.runner import RunRequest, execute_run

    outcome = await execute_run(
        RunRequest(
            provider_id="fixture",
            operation="now_playing",
            config={"path": str(FIXTURE_RECORDS)},
            wall_clock_seconds=60,
        )
    )

    assert outcome.status is RunStatus.FAILED
    assert outcome.error_class is ErrorClass.INTERNAL
    assert "does not declare now_playing" in (outcome.error_message or "")


async def test_now_playing_rejects_unexpected_protocol_messages(tmp_path: Path) -> None:
    from aggregato.sync.runner import RunRequest, execute_run

    provider_dir = tmp_path / "providers"
    _write_now_playing_provider(provider_dir, unexpected=True)
    outcome = await execute_run(
        RunRequest(
            provider_id="now_playing_fixture",
            operation="now_playing",
            provider_dir=provider_dir,
            config={"path": str(FIXTURE_RECORDS)},
            wall_clock_seconds=60,
        )
    )

    # The unexpected message follows a flushed checkpoint, so the supervision invariant keeps
    # already-checkpointed work resumable as partial rather than discarding it as failed.
    assert outcome.status is RunStatus.PARTIAL
    assert outcome.error_class is ErrorClass.INTERNAL
    assert "unexpected protocol message" in (outcome.error_message or "")


async def test_a_hanging_child_is_killed_at_the_wall_clock(tmp_path: Path) -> None:
    """§6.7 — "no output and no exit" must not be forever."""
    script = HEADER + "time.sleep(300)\n"
    outcome = await _run_child(script, tmp_path, wall_clock=1.0)

    assert outcome.status is RunStatus.FAILED
    assert outcome.error_class is ErrorClass.INTERNAL
    assert "wall-clock" in (outcome.error_message or "")


async def test_a_hang_after_a_checkpoint_is_partial_not_failed(tmp_path: Path) -> None:
    """The work before the checkpoint is real and must not be thrown away ."""
    script = (
        HEADER
        + _emit({"type": "checkpoint", "cursor": {"state": {"page": 5}}})
        + "time.sleep(300)\n"
    )
    outcome = await _run_child(script, tmp_path, wall_clock=1.0)

    assert outcome.status is RunStatus.PARTIAL
    assert outcome.cursor_after is not None
    assert outcome.cursor_after.state == {"page": 5}


async def test_a_crashing_child_does_not_take_the_parent_down(tmp_path: Path) -> None:
    script = HEADER + "raise SystemExit(3)\n"
    outcome = await _run_child(script, tmp_path)

    assert outcome.status is RunStatus.FAILED
    assert outcome.error_class is ErrorClass.INTERNAL
    # And we are still here to assert it, which is the actual claim.


async def test_a_child_killed_by_a_signal_is_recorded_as_failed(tmp_path: Path) -> None:
    """SIGKILL gives the child no chance to report. The parent must cope with silence."""
    script = HEADER + "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n"
    outcome = await _run_child(script, tmp_path)

    assert outcome.status is RunStatus.FAILED
    assert outcome.error_class is ErrorClass.INTERNAL


async def test_records_before_a_crash_are_kept(tmp_path: Path) -> None:
    """A crash mid-run must not discard what already arrived and validated."""
    record = {
        "type": "batch",
        "raw": {"native_id": "r1", "payload": {"x": 1}},
        "batch": {
            "work": {"media_type": "film", "title": "Kept"},
            "entries": [
                {
                    "kind": "watch",
                    "logged_at": "2026-01-01T00:00:00Z",
                    "logged_precision": "day",
                }
            ],
        },
    }
    script = HEADER + _emit(record) + "raise SystemExit(9)\n"
    outcome = await _run_child(script, tmp_path)

    assert outcome.status is RunStatus.FAILED
    assert len(outcome.records) == 1
    assert outcome.records[0][1].work.title == "Kept"


async def test_a_malformed_line_ends_the_run_rather_than_being_skipped(tmp_path: Path) -> None:
    """— the boundary rejects, it does not tolerate."""
    script = HEADER + "sys.stdout.write('not json\\n'); sys.stdout.flush()\n"
    outcome = await _run_child(script, tmp_path)

    assert outcome.status is RunStatus.FAILED
    assert outcome.error_class is ErrorClass.INTERNAL
    assert "invalid message" in (outcome.error_message or "")


async def test_a_reported_error_keeps_its_classification(tmp_path: Path) -> None:
    """The child classifies; the parent records what it said rather than guessing."""
    script = HEADER + _emit(
        {"type": "error", "error_class": "auth", "message": "token rejected", "detail": "trace"}
    )
    outcome = await _run_child(script, tmp_path)

    assert outcome.error_class is ErrorClass.AUTH
    assert outcome.error_message == "token rejected"
    assert outcome.status is RunStatus.FAILED


async def test_a_single_bad_record_is_collected_without_failing_the_run(tmp_path: Path) -> None:
    """— one poisoned record costs its own row, not the run."""
    script = HEADER + _emit(
        {"type": "failure", "native_id": "r9", "payload": {"bad": True}, "error": "KeyError: x"}
    )
    outcome = await _run_child(script, tmp_path)

    assert outcome.status is RunStatus.SUCCESS
    assert len(outcome.failures) == 1
    assert outcome.failures[0].payload == {"bad": True}


async def test_stdout_and_stderr_do_not_interfere(tmp_path: Path) -> None:
    """A provider that prints must not corrupt the protocol, so logging goes to stderr."""
    script = (
        HEADER
        + "sys.stderr.write('provider chatter\\n')\n"
        + _emit({"type": "checkpoint", "cursor": {"state": {"page": 1}}})
        + "sys.stderr.write('more chatter\\n')\n"
    )
    outcome = await _run_child(script, tmp_path)

    assert outcome.status is RunStatus.SUCCESS
    assert outcome.cursor_after is not None


# --- The real child, end to end -----------------------------------------------------------------


async def test_the_real_child_runs_the_fixture_provider(tmp_path: Path) -> None:
    """The actual `python -m aggregato.sync.child` against the actual bundled provider."""
    from aggregato.sync.runner import RunRequest, execute_run

    outcome = await execute_run(
        RunRequest(
            provider_id="fixture",
            config={"path": str(FIXTURE_RECORDS)},
            wall_clock_seconds=60,
        )
    )

    assert outcome.status is RunStatus.SUCCESS, outcome.error_message
    assert len(outcome.records) == 5
    assert outcome.failures == []
    assert outcome.cursor_after is not None
    # The last checkpoint names the page to read NEXT, so after two pages it is 3.
    assert outcome.cursor_after.state == {"next_page": 3}


async def test_the_real_child_runs_a_credential_check_without_records() -> None:
    """Check mode emits one diagnostic and never enters the fetch/normalize path."""
    from aggregato.sync.runner import RunRequest, execute_run

    outcome = await execute_run(
        RunRequest(
            provider_id="fixture",
            mode=FetchMode.CHECK,
            config={"path": str(FIXTURE_RECORDS)},
            wall_clock_seconds=60,
        )
    )

    assert outcome.status is RunStatus.SUCCESS, outcome.error_message
    assert outcome.check_result == CheckResult(ok=True, detail="2 page(s) readable")
    assert outcome.records == []
    assert outcome.failures == []
    assert outcome.cursor_after is None


async def test_the_real_child_takes_a_payload_larger_than_one_argv_argument() -> None:
    """A replay run carries every retained payload, which argv cannot hold.

    Linux caps a single argument at 128 KiB, so a few hundred records made ``execve`` answer
    ``E2BIG`` and the spawn raised before a run existed — the provider's lock was released but its
    ``sync_runs`` row stayed ``running``. The payload goes on stdin, which has no such limit.
    """
    from aggregato.sync.runner import RunRequest, execute_run

    seed = await execute_run(
        RunRequest(
            provider_id="fixture", config={"path": str(FIXTURE_RECORDS)}, wall_clock_seconds=60
        )
    )
    records = [raw for raw, _ in seed.records]
    request = RunRequest(
        provider_id="fixture",
        config={"path": str(FIXTURE_RECORDS)},
        replay_records=records * 200,
        wall_clock_seconds=60,
    )
    assert len(request.payload()) > 128 * 1024, "payload no longer exercises the limit"

    outcome = await execute_run(request)

    assert outcome.status is RunStatus.SUCCESS, outcome.error_message
    assert len(outcome.records) == len(records) * 200


async def test_the_real_child_resumes_from_a_cursor(tmp_path: Path) -> None:
    """Cursor round-trip through the real process boundary: no duplicate, no gap ."""
    from aggregato.sync.runner import RunRequest, execute_run

    full = await execute_run(
        RunRequest(
            provider_id="fixture", config={"path": str(FIXTURE_RECORDS)}, wall_clock_seconds=60
        )
    )
    resumed = await execute_run(
        RunRequest(
            provider_id="fixture",
            config={"path": str(FIXTURE_RECORDS)},
            cursor=Cursor(state={"next_page": 2}),
            wall_clock_seconds=60,
        )
    )

    all_ids = [raw.native_id for raw, _ in full.records]
    resumed_ids = [raw.native_id for raw, _ in resumed.records]
    assert resumed_ids == all_ids[3:]
    assert len(set(resumed_ids)) == len(resumed_ids)


async def test_the_real_child_reports_a_structure_change_as_such(tmp_path: Path) -> None:
    """A broken file must not look like an empty history ."""
    from aggregato.sync.runner import RunRequest, execute_run

    outcome = await execute_run(
        RunRequest(
            provider_id="fixture", config={"path": str(FIXTURE_BROKEN)}, wall_clock_seconds=60
        )
    )

    assert outcome.status is RunStatus.FAILED
    assert outcome.error_class is ErrorClass.STRUCTURE_CHANGED
    assert outcome.records == []


async def test_the_actual_child_does_not_inherit_ambient_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supervisor's explicit environment is enforced by a real child, not just serialization."""
    provider_dir = tmp_path / "providers"
    package = provider_dir / "environment_probe"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        """from __future__ import annotations

import os

from aggregato.providers.fixture import FixtureProvider


class EnvironmentProbeProvider(FixtureProvider):
    id = "environment_probe"

    async def fetch(self, ctx, cursor, mode):
        del ctx, cursor, mode
        if os.environ.get("AGGREGATO_TEST_AMBIENT_SECRET") is not None:
            raise RuntimeError("ambient environment leaked into child")
        if False:
            yield None


provider = EnvironmentProbeProvider()
""",
        encoding="utf-8",
    )
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "name": "Environment probe",
                "media_types": ["film"],
                "capabilities": [],
                "acquisition": "export",
                "schema_version": 1,
                "default_poll_interval_seconds": 3600,
                "config_schema": {"type": "object", "properties": {}},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("AGGREGATO_TEST_AMBIENT_SECRET", "must-not-reach-child")

    from aggregato.sync.runner import RunRequest, execute_run

    outcome = await execute_run(
        RunRequest(
            provider_id="environment_probe",
            provider_dir=provider_dir,
            config={"path": str(FIXTURE_RECORDS)},
        )
    )

    assert outcome.status is RunStatus.SUCCESS, outcome.error_message


def test_the_child_receives_only_its_own_explicit_secrets(tmp_path: Path) -> None:
    """The protocol carries selected credentials, never the worker's database or API config."""
    from aggregato.sync.runner import RunRequest

    request = RunRequest(
        provider_id="fixture",
        config={"path": "unused-by-this-test"},
        secrets={"FIXTURE_TOKEN": "mine"},
        host_state_dir=tmp_path / "http-host-state",
    )
    payload = json.loads(request.payload())
    assert payload["secrets"] == {"FIXTURE_TOKEN": "mine"}
    assert payload["host_state_dir"] == str(tmp_path / "http-host-state")
    serialized = request.payload()
    for forbidden in ("sqlite", "postgresql", "database_url", "api.token"):
        assert forbidden not in serialized


def test_batch_messages_preserve_decimal_and_timezone() -> None:
    """A 3.5 arriving back as 3.4999 fails its scale's step check for no visible reason."""
    from datetime import UTC, datetime
    from decimal import Decimal

    from aggregato.domain.enums import EntryKind, LoggedPrecision, MediaType
    from aggregato.domain.models import (
        NormalizedBatch,
        NormalizedEntry,
        NormalizedOpinion,
        NormalizedWork,
        RawRecord,
    )

    logged = datetime(2026, 2, 14, 20, 30, tzinfo=UTC)
    message = BatchMessage(
        raw=RawRecord(native_id="r", payload={}),
        batch=NormalizedBatch(
            work=NormalizedWork(media_type=MediaType.FILM, title="X"),
            entries=[
                NormalizedEntry(
                    kind=EntryKind.WATCH,
                    logged_at=logged,
                    logged_precision=LoggedPrecision.EXACT,
                )
            ],
            opinions=[NormalizedOpinion(rating_raw=Decimal("3.5"), rating_scale_id="s")],
        ),
    )

    decoded = decode(encode(message))
    assert isinstance(decoded, BatchMessage)
    assert decoded.batch.opinions[0].rating_raw == Decimal("3.5")
    assert isinstance(decoded.batch.opinions[0].rating_raw, Decimal)
    assert decoded.batch.entries[0].logged_at == logged
    assert decoded.batch.entries[0].logged_at.tzinfo is not None
