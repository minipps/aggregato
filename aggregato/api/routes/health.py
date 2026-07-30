"""``GET /health`` — per-provider status (T043).

Authenticated like every other route (FR-032): there is no public health endpoint, because the
provider list and its failure counts are operational detail about someone's private log.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select
from starlette.requests import Request

from aggregato.db.engine import transaction
from aggregato.db.schema import provider_state, providers
from aggregato.domain.enums import ProviderStatus

router = APIRouter(tags=["operations"])


class ProviderHealth(BaseModel):
    """One provider's health, exactly the object ``contracts/openapi.yaml`` declares."""

    id: str
    status: ProviderStatus
    last_success_at: datetime | None
    consecutive_failures: int


class Health(BaseModel):
    """``status`` is ``degraded`` when any provider is, ``ok`` otherwise."""

    status: Literal["ok", "degraded"]
    providers: list[ProviderHealth]


@router.get("/health", response_model=Health)
async def health(request: Request) -> Health:
    """Report every registered provider's status, last success, and consecutive failure count.

    Inputs: none beyond authentication. A fresh install has no ``providers`` rows, which is not an
    error — it answers ``{"status": "ok", "providers": []}``. A provider without a
    ``provider_state`` row yet reports ``last_success_at: null`` and zero failures, so the
    left join is deliberate.

    Failure modes: 401 problem+json without credentials; database errors surface as a 500 problem
    detail.
    """
    stmt = (
        select(
            providers.c.id,
            providers.c.status,
            provider_state.c.last_success_at,
            provider_state.c.consecutive_failures,
        )
        .select_from(providers.outerjoin(provider_state))
        .order_by(providers.c.id)
    )
    async with transaction(request.app.state.engine) as conn:
        rows = (await conn.execute(stmt)).all()

    items = [
        ProviderHealth(
            id=row.id,
            status=ProviderStatus(row.status),
            last_success_at=_aware(row.last_success_at),
            consecutive_failures=row.consecutive_failures or 0,
        )
        for row in rows
    ]
    degraded = any(item.status is ProviderStatus.DEGRADED for item in items)
    return Health(status="degraded" if degraded else "ok", providers=items)


def _aware(value: datetime | None) -> datetime | None:
    """Stamp UTC on a timestamp SQLite handed back naive.

    Every stored timestamp is UTC (db/types.py), but SQLite has no timestamp type and drops the
    offset, and the contract says timestamps carry one. This is that boundary.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)
