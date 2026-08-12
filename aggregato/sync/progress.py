"""Durable, host-owned progress reporting for one supervised sync run.

The child remains database-free.  The parent observes validated protocol messages and periodically
updates the open ``sync_runs`` row, which means the API process can expose progress even though it
is separate from the scheduler process.  A provider may not know its total before fetching, so the
reported total is optional and the UI must support indeterminate progress.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.db.engine import transaction
from aggregato.db.schema import sync_runs
from aggregato.domain.clock import Clock
from aggregato.domain.enums import RunPhase, RunStatus
from aggregato.sync.protocol import (
    BatchMessage,
    CheckpointMessage,
    ChildMessage,
    ErrorMessage,
    FailureMessage,
)

# One database write per protocol message would make a large sync spend most of its time updating
# its progress row.  Checkpoints and terminal errors still flush immediately; ordinary records are
# coalesced in small groups so the UI remains live without turning progress into the hot path.
REPORT_EVERY_RECORDS = 10
_UNSET = object()


async def update_run_progress(
    engine: AsyncEngine,
    *,
    run_id: int,
    now: datetime,
    phase: RunPhase,
    items_seen: int,
    items_written: int,
    items_failed: int,
    progress_total: int | None,
    checkpoint_count: int,
    last_checkpoint_at: datetime | None = None,
    cursor_after: object = _UNSET,
) -> None:
    """Persist one validated progress snapshot while the run is still open.

    ``cursor_after`` uses a sentinel so a phase/count update does not erase the last checkpoint.
    Passing ``None`` explicitly is reserved for callers that intentionally clear it.
    """
    values: dict[str, object] = {
        "phase": str(phase),
        "items_seen": items_seen,
        "items_written": items_written,
        "items_failed": items_failed,
        "progress_total": progress_total,
        "checkpoint_count": checkpoint_count,
        "last_checkpoint_at": last_checkpoint_at,
        "updated_at": now,
        "progress_revision": sync_runs.c.progress_revision + 1,
    }
    if cursor_after is not _UNSET:
        values["cursor_after"] = cursor_after

    async with transaction(engine) as conn:
        result = await conn.execute(
            update(sync_runs)
            .where(sync_runs.c.id == run_id, sync_runs.c.status == str(RunStatus.RUNNING))
            .values(values)
        )
    if result.rowcount != 1:
        raise RuntimeError(f"run {run_id} was not open when progress was reported")


@dataclass
class RunProgress:
    """Observe child messages and persist coalesced progress for one run."""

    engine: AsyncEngine
    run_id: int
    clock: Clock
    phase: RunPhase = RunPhase.STARTING
    items_seen: int = 0
    items_written: int = 0
    items_failed: int = 0
    progress_total: int | None = None
    checkpoint_count: int = 0
    last_checkpoint_at: datetime | None = None
    cursor_after: dict[str, object] | None = None
    _last_reported_seen: int = 0

    async def set_phase(self, phase: RunPhase, *, total: int | None = None) -> None:
        """Change the host-owned stage and flush the current counters immediately."""
        self.phase = phase
        self.progress_total = total
        await self._flush()

    async def observe(self, message: ChildMessage) -> None:
        """Update counters from one already-validated child protocol message."""
        force = False
        if isinstance(message, (BatchMessage, FailureMessage)):
            self.items_seen += 1
            if isinstance(message, FailureMessage):
                self.items_failed += 1
            force = (
                self.items_seen == 1
                or self.items_seen - self._last_reported_seen >= REPORT_EVERY_RECORDS
            )
        elif isinstance(message, CheckpointMessage):
            self.checkpoint_count += 1
            self.cursor_after = message.cursor.state
            self.last_checkpoint_at = self.clock.now()
            force = True
        elif isinstance(message, ErrorMessage):
            force = True

        if force:
            await self._flush()

    async def record_ingest(self, *, items_written: int, items_failed: int) -> None:
        """Publish the parent-side ingest totals after the batch writer commits."""
        self.items_written = items_written
        self.items_failed = items_failed
        await self._flush()

    async def _flush(self) -> None:
        await update_run_progress(
            self.engine,
            run_id=self.run_id,
            now=self.clock.now(),
            phase=self.phase,
            items_seen=self.items_seen,
            items_written=self.items_written,
            items_failed=self.items_failed,
            progress_total=self.progress_total,
            checkpoint_count=self.checkpoint_count,
            last_checkpoint_at=self.last_checkpoint_at,
            cursor_after=self.cursor_after if self.cursor_after is not None else _UNSET,
        )
        self._last_reported_seen = self.items_seen
