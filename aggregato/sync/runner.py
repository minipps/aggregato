"""Spawn and supervise one sync child process.

The parent validates the child's JSON-lines output, writes all records, and enforces a wall-clock
limit. It advances the cursor only through checkpoints the child flushed. A failure after a
checkpoint leaves the run partial and preserves records already received.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from aggregato.domain.enums import ErrorClass, FetchMode, RunStatus
from aggregato.domain.models import CheckResult, Cursor, NormalizedBatch, NowPlayingItem, RawRecord
from aggregato.sync.protocol import (
    MAX_LINE_BYTES,
    BatchMessage,
    CheckMessage,
    CheckpointMessage,
    ChildMessage,
    ErrorMessage,
    FailureMessage,
    NowPlayingMessage,
    ProtocolViolation,
    ResponseMessage,
    decode,
)

#: How long one run may take before it is killed. Generous, because a first backfill of a decade of
#: history is legitimately slow; finite, because "no output and no exit" must not be forever (§6.7).
DEFAULT_WALL_CLOCK_SECONDS = 60 * 5

#: Grace between SIGTERM and SIGKILL. A child mid-write gets a moment to finish its line; a child
#: that is genuinely wedged does not get to ignore us.
TERMINATE_GRACE_SECONDS = 5.0
#: A descendant can keep a pipe open after the direct child exits. Never let stream cleanup defeat
#: the wall-clock boundary.
STDERR_CLEANUP_TIMEOUT_SECONDS = 1.0
MAX_PAYLOAD_BYTES = 128 * 1024 * 1024
MAX_REPLAY_RECORDS = 100_000
MAX_STDOUT_BYTES = 64 * 1024 * 1024
MAX_PROTOCOL_MESSAGES = 100_000
MAX_STDERR_BYTES = 1 * 1024 * 1024


@dataclass
class RunOutcome:
    """Everything the parent learned from one child process."""

    status: RunStatus
    #: Records the child produced, paired with their raw payloads, in arrival order.
    records: list[tuple[RawRecord, NormalizedBatch]] = field(default_factory=list)
    #: Records the child could not normalize, to store for replay.
    failures: list[FailureMessage] = field(default_factory=list)
    #: The last checkpoint the child actually flushed. The cursor advances to here and no further.
    cursor_after: Cursor | None = None
    error_class: ErrorClass | None = None
    error_message: str | None = None
    log_excerpt: str | None = None
    log: str | None = None
    raw_responses: list[dict[str, object]] = field(default_factory=list)
    retry_after: timedelta | None = None
    check_result: CheckResult | None = None
    now_playing: NowPlayingItem | None = None
    now_playing_result_received: bool = False

    @property
    def checkpointed(self) -> bool:
        return self.cursor_after is not None


@dataclass(frozen=True)
class RunRequest:
    """What to ask the child to do."""

    provider_id: str
    mode: FetchMode = FetchMode.INCREMENTAL
    operation: Literal["sync", "now_playing"] = "sync"
    cursor: Cursor | None = None
    config: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)
    state: dict[str, str] = field(default_factory=dict)
    import_path: Path | None = None
    provider_dir: Path | None = None
    #: Shared host pacing state stored outside the child so separately spawned runs coordinate.
    host_state_dir: Path | None = None
    #: Stored raw records to normalize again after a provider schema-version bump.  This is kept
    #: separate from ``fetch`` so replay is credential-free and makes no network request.
    replay_records: list[RawRecord] = field(default_factory=list)
    wall_clock_seconds: float = DEFAULT_WALL_CLOCK_SECONDS

    def payload(self) -> str:
        """The single JSON argument the child parses."""
        if len(self.replay_records) > MAX_REPLAY_RECORDS:
            raise ProtocolViolation(
                f"replay contains {len(self.replay_records)} records, over the "
                f"{MAX_REPLAY_RECORDS} record limit"
            )
        payload: dict[str, Any] = {
            "mode": str(self.mode),
            "operation": self.operation,
            "cursor": self.cursor.state if self.cursor else None,
            "config": self.config,
            "secrets": self.secrets,
            "state": self.state,
            "import_path": str(self.import_path) if self.import_path else None,
            "provider_dir": str(self.provider_dir) if self.provider_dir else None,
            "host_state_dir": str(self.host_state_dir) if self.host_state_dir else None,
        }
        if self.replay_records:
            payload["replay_records"] = [
                record.model_dump(mode="json") for record in self.replay_records
            ]
        encoded = json.dumps(payload)
        if len(encoded.encode()) > MAX_PAYLOAD_BYTES:
            raise ProtocolViolation(
                f"child request is over the {MAX_PAYLOAD_BYTES} byte payload limit"
            )
        return encoded


async def execute_run(
    request: RunRequest,
    *,
    on_message: Callable[[ChildMessage], Awaitable[None]] | None = None,
) -> RunOutcome:
    """Spawn a child for one run, supervise it, and return what it produced.

    Args:
        request: What the child should do.
        on_message: Optional callback invoked after each child message is validated and before it
            is added to the returned outcome. A callback failure aborts the run.

    Returns:
        A :class:`RunOutcome`. This function does not raise for provider failures — a failed run is
        an outcome to record, not an exception to propagate, or one bad provider would take down the
        scheduler loop.
    """
    spawn_options: dict[str, Any] = {
        "stdin": asyncio.subprocess.PIPE,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "env": _child_environment(),
        "limit": MAX_LINE_BYTES + 1,
    }
    if os.name == "posix":
        # A provider may fork. Starting a session makes the child the process-group leader, so the
        # supervisor can terminate the whole run rather than only the process it spawned.
        spawn_options["start_new_session"] = True
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "aggregato.sync.child",
        "--provider",
        request.provider_id,
        **spawn_options,
    )

    outcome = RunOutcome(status=RunStatus.RUNNING)
    consume_task = asyncio.create_task(_consume(process, outcome, on_message=on_message))
    stderr_task = asyncio.create_task(_read_stderr(process))
    try:
        async with asyncio.timeout(request.wall_clock_seconds):
            await _send_payload(process, request.payload())
            await asyncio.gather(consume_task, process.wait())
    except TimeoutError:
        # The child produced no end and no exit. Kill it, then classify — a hang and a crash must
        # look different to the operator (§6.7).
        await _terminate(process)
        outcome.status = RunStatus.PARTIAL if outcome.checkpointed else RunStatus.FAILED
        outcome.error_class = ErrorClass.INTERNAL
        outcome.error_message = (
            f"provider {request.provider_id} exceeded its {request.wall_clock_seconds:g}s "
            "wall-clock limit and was killed"
        )
        return outcome
    except ProtocolViolation as exc:
        await _terminate(process)
        outcome.status = RunStatus.PARTIAL if outcome.checkpointed else RunStatus.FAILED
        outcome.error_class = ErrorClass.INTERNAL
        outcome.error_message = str(exc)
        return outcome
    finally:
        await _terminate(process)
        if not consume_task.done():
            consume_task.cancel()
            with suppress(asyncio.CancelledError):
                await consume_task
        if not stderr_task.done():
            await _finish_stderr_task(stderr_task)

    stderr = stderr_task.result() if stderr_task.done() and not stderr_task.cancelled() else ""
    if stderr:
        outcome.log = stderr
        outcome.log_excerpt = _tail(stderr)

    if request.operation == "now_playing":
        # Host-owned response snapshots are diagnostics and may accompany the one result (or one
        # classified error); provider protocol messages still must not.
        has_other_messages = bool(
            outcome.records
            or outcome.failures
            or outcome.cursor_after is not None
            or outcome.check_result is not None
        )
        if outcome.error_class is not None:
            if outcome.now_playing_result_received or has_other_messages:
                outcome.error_class = ErrorClass.INTERNAL
                outcome.error_message = "now-playing emitted a result and another protocol message"
        elif has_other_messages:
            outcome.error_class = ErrorClass.INTERNAL
            outcome.error_message = "now-playing emitted an unexpected protocol message"
        elif not outcome.now_playing_result_received:
            outcome.error_class = ErrorClass.INTERNAL
            outcome.error_message = "now-playing emitted no result"
    elif request.mode is FetchMode.CHECK:
        if outcome.check_result is not None and (
            outcome.records
            or outcome.failures
            or outcome.cursor_after is not None
            or outcome.now_playing_result_received
        ):
            outcome.error_class = ErrorClass.INTERNAL
            outcome.error_message = "credential check emitted non-diagnostic protocol messages"
        elif outcome.check_result is None and outcome.error_class is None:
            outcome.error_class = ErrorClass.INTERNAL
            outcome.error_message = "credential check emitted no result"
    elif outcome.check_result is not None:
        outcome.error_class = ErrorClass.INTERNAL
        outcome.error_message = "sync emitted an unexpected credential-check result"
    elif outcome.now_playing_result_received:
        outcome.error_class = ErrorClass.INTERNAL
        outcome.error_message = "sync emitted an unexpected now-playing result"

    if outcome.error_class is not None:
        # The child reported and exited cleanly. Partial if it had already checkpointed, because the
        # work before the checkpoint is real and must not be redone.
        outcome.status = RunStatus.PARTIAL if outcome.checkpointed else RunStatus.FAILED
        return outcome

    if process.returncode != 0:
        # Crashed or was killed without reporting. There is nothing to classify from, so `internal`
        # The exit code is the only available failure detail, so classify it as internal.
        outcome.status = RunStatus.PARTIAL if outcome.checkpointed else RunStatus.FAILED
        outcome.error_class = ErrorClass.INTERNAL
        outcome.error_message = (
            f"provider {request.provider_id} exited with code {process.returncode} "
            "without reporting an error"
        )
        return outcome

    outcome.status = RunStatus.SUCCESS
    return outcome


async def _send_payload(process: asyncio.subprocess.Process, payload: str) -> None:
    """Hand the run's parameters to the child on stdin, then close it.

    On stdin rather than argv: a replay run carries every retained payload for the provider, which
    for a few hundred records is far past the kernel's 128 KiB single-argument limit — ``execve``
    answered ``E2BIG`` and the spawn raised before the run existed. It also keeps a provider's
    resolved credentials out of the process table, where argv is world-readable.

    Writing before reading stdout is safe because the child reads its whole payload before it emits
    anything; a child that does not is a hang the wall clock already covers.
    """
    assert process.stdin is not None
    process.stdin.write(payload.encode())
    with suppress(BrokenPipeError, ConnectionResetError):
        # The child died before reading its instructions. Nothing to report from here — the exit
        # code and stderr are what classify it, further down.
        await process.stdin.drain()
    process.stdin.close()


async def _consume(
    process: asyncio.subprocess.Process,
    outcome: RunOutcome,
    *,
    on_message: Callable[[ChildMessage], Awaitable[None]] | None = None,
) -> None:
    """Read and validate the child's stdout, message by message.

    Every line is validated here, in the parent, before anything derived from it is kept: the
    boundary between plugin-controlled output and host data.
    """
    assert process.stdout is not None
    total_bytes = 0
    messages = 0
    while True:
        try:
            line = await process.stdout.readline()
        except ValueError as exc:
            # asyncio raises this when a line exceeds the stream limit — an unbounded line, which is
            # a hang the wall clock cannot catch because the child looks busy.
            raise ProtocolViolation(f"child sent an over-long line: {exc}") from exc
        if not line:
            return
        total_bytes += len(line)
        messages += 1
        if total_bytes > MAX_STDOUT_BYTES:
            raise ProtocolViolation(f"child stdout exceeded the {MAX_STDOUT_BYTES} byte limit")
        if messages > MAX_PROTOCOL_MESSAGES:
            raise ProtocolViolation(f"child sent over the {MAX_PROTOCOL_MESSAGES} message limit")
        message = decode(line)
        if on_message is not None:
            await on_message(message)
        match message:
            case BatchMessage():
                outcome.records.append((message.raw, message.batch))
            case CheckpointMessage():
                outcome.cursor_after = message.cursor
            case FailureMessage():
                outcome.failures.append(message)
            case CheckMessage():
                if outcome.check_result is not None:
                    raise ProtocolViolation("credential check emitted more than one result")
                outcome.check_result = message.result
            case NowPlayingMessage():
                if outcome.now_playing_result_received:
                    raise ProtocolViolation("now-playing emitted more than one result")
                outcome.now_playing = message.result
                outcome.now_playing_result_received = True
            case ErrorMessage():
                outcome.error_class = message.error_class
                outcome.error_message = message.message
                if message.detail:
                    outcome.log_excerpt = _tail(message.detail)
                if message.retry_after_seconds is not None:
                    outcome.retry_after = timedelta(seconds=message.retry_after_seconds)
            case ResponseMessage():
                outcome.raw_responses.append(message.model_dump(exclude={"type"}))


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """Stop a child and its descendants: SIGTERM, brief grace, then SIGKILL."""
    if os.name != "posix":
        if process.returncode is None:
            process.terminate()
            try:
                async with asyncio.timeout(TERMINATE_GRACE_SECONDS):
                    await process.wait()
            except TimeoutError:
                process.kill()
                await process.wait()
        return

    if process.returncode is not None and not _process_group_exists(process.pid):
        return

    deadline = asyncio.get_running_loop().time() + TERMINATE_GRACE_SECONDS
    _signal_process_group(process.pid, signal.SIGTERM)
    if process.returncode is None:
        try:
            async with asyncio.timeout(TERMINATE_GRACE_SECONDS):
                await process.wait()
        except TimeoutError:
            pass

    remaining = deadline - asyncio.get_running_loop().time()
    if remaining > 0 and _process_group_exists(process.pid):
        await asyncio.sleep(remaining)
    if _process_group_exists(process.pid):
        _signal_process_group(process.pid, signal.SIGKILL)
    if process.returncode is None:
        await process.wait()


def _process_group_exists(pid: int) -> bool:
    """Return whether a POSIX process group still has members."""
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _signal_process_group(pid: int, sig: signal.Signals) -> None:
    """Signal a child process group, tolerating a group that exited between checks."""
    with suppress(ProcessLookupError):
        os.killpg(pid, sig)


async def _finish_stderr_task(task: asyncio.Task[str]) -> None:
    """Collect stderr briefly, then cancel a reader whose pipe never reaches EOF."""
    try:
        async with asyncio.timeout(STDERR_CLEANUP_TIMEOUT_SECONDS):
            await asyncio.shield(task)
    except TimeoutError:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def _read_stderr(process: asyncio.subprocess.Process) -> str:
    if process.stderr is None:  # pragma: no cover - always piped by execute_run
        return ""
    tail = bytearray()
    while chunk := await process.stderr.read(64 * 1024):
        tail.extend(chunk)
        if len(tail) > MAX_STDERR_BYTES:
            del tail[: len(tail) - MAX_STDERR_BYTES]
    return bytes(tail).decode(errors="replace")


def _child_environment() -> dict[str, str]:
    """Return the small runtime environment a provider child is allowed to inherit."""
    return {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    }


def _tail(text: str, limit: int = 4000) -> str:
    """Keep the end. ``sync_runs.log_excerpt`` is for diagnosis, not archival."""
    return text if len(text) <= limit else "…" + text[-limit:]
