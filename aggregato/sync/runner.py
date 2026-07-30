"""Spawning and supervising one sync run (research.md R2, FR-025).

The parent owns everything the child cannot be trusted with: the database, the cursor, retry policy,
and the clock on the child's life. It reads the child's stdout line by line, validates each line,
and performs every write itself.

What supervision has to survive, because each has happened to somebody:

* the child **crashes** — non-zero exit, no error message. Classified ``internal``.
* the child is **killed** — SIGKILL, no chance to report anything. The API keeps serving and other
  providers keep syncing, because they are different processes (FR-025).
* the child **hangs** — no output, no exit. Killed at the wall clock, then classified (§6.7).
* the child **lies** — malformed lines, an unknown message type, an unbounded line. Rejected at the
  boundary rather than written (FR-008).

A run that fails **after** checkpointing is ``partial``, and the cursor advances only as far as the
last checkpoint the child actually flushed — never to where the child said it got to (FR-020).
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aggregato.domain.enums import ErrorClass, FetchMode, RunStatus
from aggregato.domain.models import Cursor, NormalizedBatch, RawRecord
from aggregato.sync.protocol import (
    BatchMessage,
    CheckpointMessage,
    ErrorMessage,
    FailureMessage,
    ProtocolViolation,
    decode,
)

#: How long one run may take before it is killed. Generous, because a first backfill of a decade of
#: history is legitimately slow; finite, because "no output and no exit" must not be forever (§6.7).
DEFAULT_WALL_CLOCK_SECONDS = 60 * 30

#: Grace between SIGTERM and SIGKILL. A child mid-write gets a moment to finish its line; a child
#: that is genuinely wedged does not get to ignore us.
TERMINATE_GRACE_SECONDS = 5.0


@dataclass
class RunOutcome:
    """Everything the parent learned from one child process."""

    status: RunStatus
    #: Records the child produced, paired with their raw payloads, in arrival order.
    records: list[tuple[RawRecord, NormalizedBatch]] = field(default_factory=list)
    #: Records the child could not normalize, to store for replay (FR-023).
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
    wall_clock_seconds: float = DEFAULT_WALL_CLOCK_SECONDS

    def payload(self) -> str:
        """The single JSON argument the child parses."""
        return json.dumps(
            {
                "mode": str(self.mode),
                "cursor": self.cursor.state if self.cursor else None,
                "config": self.config,
                "secrets": self.secrets,
                "state": self.state,
                "import_path": str(self.import_path) if self.import_path else None,
            }
        )


async def execute_run(request: RunRequest, *, now: datetime | None = None) -> RunOutcome:
    """Spawn a child for one run, supervise it, and return what it produced.

    Args:
        request: What the child should do.
        now: Unused by the supervision itself; accepted so callers can pass the injected clock's
            value without the runner reaching for a clock of its own.

    Returns:
        A :class:`RunOutcome`. This function does not raise for provider failures — a failed run is
        an outcome to record, not an exception to propagate, or one bad provider would take down the
        scheduler loop (FR-025).
    """
    del now  # documented above; kept out of the body so no clock is read here
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "aggregato.sync.child",
        "--provider",
        request.provider_id,
        "--payload",
        request.payload(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    outcome = RunOutcome(status=RunStatus.RUNNING)
    try:
        async with asyncio.timeout(request.wall_clock_seconds):
            await _consume(process, outcome)
            await process.wait()
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

    stderr = await _read_stderr(process)
    if stderr:
        outcome.log_excerpt = _tail(stderr)

    if outcome.error_class is not None:
        # The child reported and exited cleanly. Partial if it had already checkpointed, because the
        # work before the checkpoint is real and must not be redone (FR-020).
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


async def _consume(process: asyncio.subprocess.Process, outcome: RunOutcome) -> None:
    """Read and validate the child's stdout, message by message.

    Every line is validated here, in the parent, before anything derived from it is kept: the
    boundary between plugin-controlled output and host data (FR-008).
    """
    assert process.stdout is not None
    while True:
        try:
            line = await process.stdout.readline()
        except ValueError as exc:
            # asyncio raises this when a line exceeds the stream limit — an unbounded line, which is
            # a hang the wall clock cannot catch because the child looks busy.
            raise ProtocolViolation(f"child sent an over-long line: {exc}") from exc
        if not line:
            return
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
    return (await process.stderr.read()).decode(errors="replace")


def _tail(text: str, limit: int = 4000) -> str:
    """Keep the end. ``sync_runs.log_excerpt`` is for diagnosis, not archival."""
    return text if len(text) <= limit else "…" + text[-limit:]
