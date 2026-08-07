"""SQLite plan assertions for every cursor sort available on ``GET /entries`` ."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select, text

from aggregato.api.queries import opinion_facts
from aggregato.db.schema import entries, metadata

pytestmark = pytest.mark.bench


def _plan_for(connection: object, statement: object) -> str:
    """Compile a statement literally and return SQLite's concise planner explanation."""
    compiled = statement.compile(  # type: ignore[union-attr]
        dialect=connection.dialect,  # type: ignore[union-attr]
        compile_kwargs={"literal_binds": True},
    )
    return "\n".join(
        row[3]
        for row in connection.execute(text(f"EXPLAIN QUERY PLAN {compiled}"))  # type: ignore[union-attr]
    )


def test_live_entry_keyset_sorts_use_composite_indexes() -> None:
    """The first and deep-page forms retain an index-backed path for both entry timestamps."""
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as conn:
            metadata.create_all(conn)
            for column, index in (
                (entries.c.logged_at, "ix_entries_active_logged_at_id"),
                (entries.c.ingested_at, "ix_entries_active_ingested_at_id"),
            ):
                statement = (
                    select(entries.c.id)
                    .where(
                        entries.c.deleted_at.is_(None),
                        entries.c.subject_ref.is_(None),
                        column < datetime(2026, 1, 1, tzinfo=UTC),
                    )
                    .order_by(column.desc(), entries.c.id.desc())
                    .limit(51)
                )
                assert index in _plan_for(conn, statement)
    finally:
        engine.dispose()


def test_live_score_facts_use_provider_item_composite_index() -> None:
    """The aggregation underpinning ``sort=score`` reads live opinions by provider item."""
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as conn:
            metadata.create_all(conn)
            statement = select(opinion_facts(include_deleted=False))
            assert "ix_opinions_active_provider_item_rating" in _plan_for(conn, statement)
    finally:
        engine.dispose()
