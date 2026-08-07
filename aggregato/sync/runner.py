"""Spawning and supervising one sync run (research.md , ).

The parent owns everything the child cannot be trusted with: the database, the cursor, retry policy,
and the clock on the child's life. It reads the child's stdout line by line, validates each line,
and performs every write itself.

What supervision has to survive, because each has happened to somebody:

* the child **crashes** — non-zero exit, no error message. Classified ``internal``.
* the child is **killed** — SIGKILL, no chance to report anything. The API keeps serving and other
  providers keep syncing, because they are different processes .
* the child **hangs** — no output, no exit. Killed at the wall clock, then classified (§6.7).
* the child **lies** — malformed lines, an unknown message type, an unbounded line. Rejected at the
  boundary rather than written .

A run that fails **after** checkpointing is ``partial``, and the cursor advances only as far as the
last checkpoint the child actually flushed — never to where the child said it got to .
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aggregato.domain.enums import ErrorClass, FetchMode, RunStatus
from aggregato.domain.models import Cursor, NormalizedBatch, RawRecord
from aggregato.sync.protocol import (
    MAX_LINE_BYTES,
    BatchMessage,
    CheckpointMessage,
    ErrorMessage,
    FailureMessage,
    ProtocolViolation,
    decode,
)

#: How long one run may take before it is killed. Generous, because a first backfill of a decade of
#: history is legitimately slow; finite, because "no output and no exit" must not be forever (§6.7).
DEFAULT_WALL_CLOCK_SECONDS = 60 * 5

#: The kernel's cap on one argv entry (``MAX_ARG_STRLEN``, 32 pages). Not used by the spawn — the
#: payload travels on stdin precisely so it does not have to be — but named here because it is the
#: reason, and the reason is what a test asserts against.
MAX_ARG_STRLEN = 128 * 1024

#: Grace between SIGTERM and SIGKILL. A child mid-write gets a moment to finish its line; a child
#: that is genuinely wedged does not get to ignore us.
TERMINATE_GRACE_SECONDS = 5.0
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
    #: Records the child could not normalize, to store for replay .
    failures: list[FailureMessage] = field(default_factory=list)
    #: The last checkpoint the child actually flushed. The cursor advances to here and no further.
    cursor_after: Cursor | None = None
    error_class: ErrorClass | None = None
    error_message: str | None = None
    log_excerpt: str | None = None
    retry_after: timedelta | None = None

    @property
    def checkpointed(self) -> bool:
        return self.cursor_after is not None


@dataclass(frozen=True)
class RunRequest:
    """What to ask the child to do."""

    provider_id: str
    mode: FetchMode = FetchMode.INCREMENTAL
    cursor: Cursor | None = None
    config: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)
    state: dict[str, str] = field(default_factory=dict)
    import_path: Path | None = None
    provider_dir: Path | None = None
    #: Stored raw records to normalize again after a provider schema-version bump.  This is kept
    #: separate from ``fetch`` so replay is credential-free and makes no network request .
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
            "cursor": self.cursor.state if self.cursor else None,
            "config": self.config,
            "secrets": self.secrets,
            "state": self.state,
            "import_path": str(self.import_path) if self.import_path else None,
            "provider_dir": str(self.provider_dir) if self.provider_dir else None,
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


async def execute_run(request: RunRequest, *, now: datetime | None = None) -> RunOutcome:
    """Spawn a child for one run, supervise it, and return what it produced.

    Args:
        request: What the child should do.
        now: Unused by the supervision itself; accepted so callers can pass the injected clock's
            value without the runner reaching for a clock of its own.

    Returns:
        A :class:`RunOutcome`. This function does not raise for provider failures — a failed run is
        an outcome to record, not an exception to propagate, or one bad provider would take down the
        scheduler loop .
    """
    del now  # documented above; kept out of the body so no clock is read here
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "aggregato.sync.child",
        "--provider",
        request.provider_id,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_child_environment(),
        limit=MAX_LINE_BYTES + 1,
    )

    outcome = RunOutcome(status=RunStatus.RUNNING)
    consume_task = asyncio.create_task(_consume(process, outcome))
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
        if process.returncode is None:
            await _terminate(process)
        if not consume_task.done():
            consume_task.cancel()
            with suppress(asyncio.CancelledError):
                await consume_task
        if not stderr_task.done():
            with suppress(asyncio.CancelledError):
                await stderr_task

    stderr = stderr_task.result() if stderr_task.done() and not stderr_task.cancelled() else ""
    if stderr:
        outcome.log_excerpt = _tail(stderr)

    if outcome.error_class is not None:
        # The child reported and exited cleanly. Partial if it had already checkpointed, because the
        # work before the checkpoint is real and must not be redone .
        outcome.status = RunStatus.PARTIAL if outcome.checkpointed else RunStatus.FAILED
        return outcome

    if process.returncode != 0:
        # Crashed or was killed without reporting. There is nothing to classify from, so `internal`
        # with the exit code is the honest answer.
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


async def _consume(process: asyncio.subprocess.Process, outcome: RunOutcome) -> None:
    """Read and validate the child's stdout, message by message.

    Every line is validated here, in the parent, before anything derived from it is kept: the
    boundary between plugin-controlled output and host data .
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
        match message:
            case BatchMessage():
                outcome.records.append((message.raw, message.batch))
            case CheckpointMessage():
                outcome.cursor_after = message.cursor
            case FailureMessage():
                outcome.failures.append(message)
            case ErrorMessage():
                outcome.error_class = message.error_class
                outcome.error_message = message.message
                if message.detail:
                    outcome.log_excerpt = _tail(message.detail)
                if message.retry_after_seconds is not None:
                    outcome.retry_after = timedelta(seconds=message.retry_after_seconds)


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """Stop a child that will not stop itself: SIGTERM, brief grace, then SIGKILL."""
    if process.returncode is not None:
        return
    process.terminate()
    try:
        async with asyncio.timeout(TERMINATE_GRACE_SECONDS):
            await process.wait()
    except TimeoutError:
        process.kill()
        await process.wait()


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
