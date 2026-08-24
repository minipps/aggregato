"""Worker-side polling for transient now-playing provider state."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from aggregato.config import Config
from aggregato.db.engine import transaction
from aggregato.db.schema import provider_state, providers
from aggregato.domain.clock import SYSTEM_CLOCK, Clock
from aggregato.domain.enums import Capability, ErrorClass, RunStatus
from aggregato.images.cache import register_source_on_connection
from aggregato.providers.registry import ProviderInfo, discover_providers
from aggregato.sync.dispatch import (
    _provider_settings,
    _public_provider_settings,
    _secret_provider_settings,
)
from aggregato.sync.retry import plan_after_failure
from aggregato.sync.runner import RunOutcome, RunRequest, execute_run

log = logging.getLogger(__name__)

NOW_PLAYING_INTERVAL = timedelta(seconds=15)
NOW_PLAYING_STALE_AFTER = timedelta(seconds=45)
NOW_PLAYING_WALL_CLOCK_SECONDS = 30
NOW_PLAYING_MAX_CONCURRENT = 3
MONITOR_WAKE_SECONDS = 1
_TRANSIENT_RETRY_INTERVAL = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A parent-owned child request and the state needed to commit its result."""

    info: ProviderInfo
    provider_id: str
    config: dict[str, object]
    secrets: dict[str, str]
    state: dict[str, str]
    fingerprint: str
    failures: int


class NowPlayingMonitor:
    """Poll capable providers and persist one current item per provider."""

    def __init__(
        self,
        engine: AsyncEngine,
        config: Config,
        *,
        clock: Clock = SYSTEM_CLOCK,
        max_concurrent: int = NOW_PLAYING_MAX_CONCURRENT,
        wall_clock_seconds: float = NOW_PLAYING_WALL_CLOCK_SECONDS,
    ) -> None:
        self._engine = engine
        self._config = config
        self._clock = clock
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._wall_clock_seconds = wall_clock_seconds
        self._running: set[asyncio.Task[None]] = set()
        self._stopped = asyncio.Event()

    async def poll_once(self) -> int:
        """Run every provider currently due, returning the number of attempts started."""
        if self._stopped.is_set():
            return 0
        candidates = await self._candidates()
        if not candidates or self._stopped.is_set():
            return 0
        tasks = [asyncio.create_task(self._poll(candidate)) for candidate in candidates]
        self._running.update(tasks)
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._running.difference_update(tasks)
        return len(candidates)

    async def run_forever(self) -> None:
        """Poll until :meth:`stop` is called."""
        while not self._stopped.is_set():
            try:
                await self.poll_once()
            except Exception:
                log.exception("now-playing monitor poll failed")
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(MONITOR_WAKE_SECONDS):
                    await self._stopped.wait()

    async def stop(self) -> None:
        """Stop polling and cancel any children still being supervised."""
        self._stopped.set()
        tasks = tuple(self._running)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _candidates(self) -> list[_Candidate]:
        now = self._clock.now()
        infos = {
            info.id: info
            for info in discover_providers(self._config.provider_dir)
            if Capability.NOW_PLAYING.value in info.capabilities
        }
        if not infos:
            return []

        async with transaction(self._engine) as conn:
            rows = list(
                await conn.execute(
                    select(
                        providers.c.id,
                        providers.c.enabled,
                        provider_state.c.now_playing_next_poll_at,
                        provider_state.c.now_playing_config_fingerprint,
                        provider_state.c.now_playing_failures,
                        provider_state.c.kv,
                    )
                    .join(provider_state, provider_state.c.provider_id == providers.c.id)
                    .where(
                        providers.c.enabled.is_(True),
                        providers.c.id.in_(tuple(infos)),
                    )
                )
            )

        candidates: list[_Candidate] = []
        for row in rows:
            info = infos[row.id]
            provider_config = self._config.providers.get(row.id)
            if provider_config is not None and not provider_config.enabled:
                continue
            settings = await _provider_settings(self._engine, self._config, row.id)
            fingerprint = config_fingerprint(settings, info.schema_version)
            next_poll_at: datetime | None
            if row.now_playing_config_fingerprint != fingerprint:
                await self._reactivate(row.id, fingerprint, now)
                next_poll_at = now
                failures = 0
            else:
                next_poll_at = _aware(row.now_playing_next_poll_at)
                if next_poll_at is None or next_poll_at > now:
                    continue
                failures = int(row.now_playing_failures or 0)

            state = row.kv if isinstance(row.kv, dict) else {}
            candidates.append(
                _Candidate(
                    info=info,
                    provider_id=row.id,
                    config=_public_provider_settings(info.config_schema, settings),
                    secrets=_secret_provider_settings(info.config_schema, settings),
                    state={str(key): str(value) for key, value in state.items()},
                    fingerprint=fingerprint,
                    failures=failures,
                )
            )
        return candidates

    async def _reactivate(self, provider_id: str, fingerprint: str, now: datetime) -> None:
        """Make a settings/schema change due immediately and discard the old presence."""
        async with transaction(self._engine) as conn:
            await conn.execute(
                update(provider_state)
                .where(provider_state.c.provider_id == provider_id)
                .values(
                    now_playing_item=None,
                    now_playing_changed_at=None,
                    now_playing_checked_at=None,
                    now_playing_next_poll_at=now,
                    now_playing_failures=0,
                    now_playing_config_fingerprint=fingerprint,
                )
            )

    async def _poll(self, candidate: _Candidate) -> None:
        async with self._semaphore:
            try:
                outcome = await execute_run(
                    RunRequest(
                        provider_id=candidate.provider_id,
                        operation="now_playing",
                        config=candidate.config,
                        secrets=candidate.secrets,
                        state=candidate.state,
                        provider_dir=self._config.provider_dir,
                        host_state_dir=self._config.data_dir / "http-host-state",
                        wall_clock_seconds=self._wall_clock_seconds,
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("now-playing child failed before returning an outcome")
                outcome = RunOutcome(
                    RunStatus.FAILED,
                    error_class=ErrorClass.INTERNAL,
                    error_message=f"{type(exc).__name__}: {str(exc)[:1000]}",
                )
            await self._persist(candidate, outcome)

    async def _persist(self, candidate: _Candidate, outcome: RunOutcome) -> None:
        now = self._clock.now()
        success = outcome.status is RunStatus.SUCCESS and outcome.now_playing_result_received
        item = outcome.now_playing if success else None
        item_payload = item.model_dump(mode="json") if item is not None else None
        next_poll_at: datetime | None

        if success:
            next_poll_at = now + NOW_PLAYING_INTERVAL
            failures = 0
        else:
            error_class = outcome.error_class or ErrorClass.INTERNAL
            decision = plan_after_failure(
                clock=self._clock,
                error_class=error_class,
                retry_step=candidate.failures,
                consecutive_failures=candidate.failures,
                normal_interval=_TRANSIENT_RETRY_INTERVAL,
                lineage_id=UUID(int=0),
                retry_after=outcome.retry_after,
            )
            next_poll_at = decision.next_run_at
            if next_poll_at is not None and outcome.retry_after is not None:
                next_poll_at = max(next_poll_at, now + outcome.retry_after)
            failures = decision.consecutive_failures

        async with transaction(self._engine) as conn:
            current = (
                await conn.execute(
                    select(
                        providers.c.enabled,
                        provider_state.c.now_playing_config_fingerprint,
                        provider_state.c.now_playing_item,
                        provider_state.c.now_playing_changed_at,
                    )
                    .join(provider_state, provider_state.c.provider_id == providers.c.id)
                    .where(providers.c.id == candidate.provider_id)
                )
            ).first()
            if (
                current is None
                or not current.enabled
                or current.now_playing_config_fingerprint != candidate.fingerprint
            ):
                return

            changed_at: datetime | None = None
            if item_payload is not None:
                old_item = current.now_playing_item
                changed_at = (
                    _aware(current.now_playing_changed_at) if old_item == item_payload else now
                )
                if changed_at is None:
                    changed_at = now
                if item is not None and item.work.image_url:
                    await register_source_on_connection(conn, item.work.image_url)

            await conn.execute(
                update(provider_state)
                .where(provider_state.c.provider_id == candidate.provider_id)
                .values(
                    now_playing_item=item_payload,
                    now_playing_changed_at=changed_at,
                    now_playing_checked_at=now,
                    now_playing_next_poll_at=next_poll_at,
                    now_playing_failures=failures,
                    now_playing_config_fingerprint=candidate.fingerprint,
                )
            )


def config_fingerprint(settings: dict[str, object], schema_version: int) -> str:
    """Hash resolved settings without retaining the settings or their secret values."""
    payload = json.dumps(
        {"schema_version": schema_version, "settings": settings},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
