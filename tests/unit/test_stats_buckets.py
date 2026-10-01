from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql

from aggregato.api.routes.stats import _bucket
from aggregato.db.schema import entries


def test_week_bucket_uses_iso_week_year_at_new_year(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'weeks.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE entries (logged_at TEXT NOT NULL)")
        conn.exec_driver_sql(
            "INSERT INTO entries VALUES (?), (?), (?), (?), (?), (?), (?), (?)",
            (
                "2018-12-30",
                "2018-12-31",
                "2019-12-29",
                "2019-12-30",
                "2020-12-31",
                "2021-01-03",
                "2021-01-04",
                "2021-01-10",
            ),
        )
        weeks = (
            conn.execute(
                select(_bucket("week", "sqlite")).select_from(entries).order_by(entries.c.logged_at)
            )
            .scalars()
            .all()
        )
    engine.dispose()

    assert weeks == [
        "2018-W52",
        "2019-W01",
        "2019-W52",
        "2020-W01",
        "2020-W53",
        "2020-W53",
        "2021-W01",
        "2021-W01",
    ]


def test_postgres_week_bucket_uses_iso_year_and_week() -> None:
    sql = str(
        select(_bucket("week", "postgresql")).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "to_char(entries.logged_at, 'IYYY-\"W\"IW')" in sql
