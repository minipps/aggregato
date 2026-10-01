"""Raw run diagnostics and retained failure payloads are private to operators."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

from aggregato.config import load_config
from aggregato.db.engine import transaction
from aggregato.db.schema import ingest_failures, sync_runs
from aggregato.main import create_app


async def test_raw_run_diagnostics_and_failure_payloads_require_operator(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    app = create_app(
        load_config(
            {
                "AGGREGATO_TOKEN": "operator-token",
                "AGGREGATO_READONLY_TOKEN": "readonly-token",
                "AGGREGATO_ALLOW_UNAUTHENTICATED_READONLY": "true",
                "AGGREGATO_DATA": str(data_dir),
            }
        ),
    )
    paths = (
        "/api/v1/providers/fixture/runs/1/diagnostics",
        "/api/v1/ingest-failures",
    )
    legacy_secret = "legacy-response-secret"
    private_run_value = "private-run-secret"
    private_cursor_value = "private-cursor-secret"

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        async with transaction(app.state.engine) as conn:
            result = await conn.execute(
                sync_runs.insert().values(
                    provider_id="fixture",
                    lineage_id=uuid.uuid4(),
                    attempt=1,
                    mode="incremental",
                    status="failed",
                    phase="failed",
                    started_at=datetime(2026, 1, 1, tzinfo=UTC),
                    error_class="internal",
                    error_message=private_run_value,
                    log_excerpt=private_run_value,
                    cursor_before={"token": private_cursor_value},
                    cursor_after={"token": private_cursor_value},
                    raw_responses=[
                        {
                            "method": "POST",
                            "url": "https://legacy-user:legacy-password@example.test/token"
                            f"?access_token={legacy_secret}#fragment-{legacy_secret}",
                            "status": 200,
                            "headers": {"Authorization": legacy_secret},
                            "body": legacy_secret,
                        }
                    ],
                )
            )
            run_id = int(result.inserted_primary_key[0])
            await conn.execute(
                ingest_failures.insert().values(
                    provider_id="fixture",
                    sync_run_id=run_id,
                    native_id="item-1",
                    raw_payload={"token": "retained-payload"},
                    error="normalization failed",
                    stage="normalize",
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )

        for path in paths:
            public = await client.get(path)
            readonly = await client.get(path, headers={"Authorization": "Bearer readonly-token"})
            assert public.status_code == 403
            assert readonly.status_code == 403

        for path in (
            "/api/v1/providers/fixture/runs",
            "/api/v1/sync/status",
        ):
            for headers in (None, {"Authorization": "Bearer readonly-token"}):
                response = await client.get(path, headers=headers)
                assert response.status_code == 200
                summary = (
                    response.json()["runs"][0]
                    if path.endswith("/sync/status")
                    else response.json()["items"][0]
                )
                assert summary["status"] == "failed"
                assert summary["error_class"] == "internal"
                assert summary["error_message"] is None
                assert summary["log_excerpt"] is None
                assert summary["cursor_before"] is None
                assert summary["cursor_after"] is None
                assert private_run_value not in response.text
                assert private_cursor_value not in response.text

        diagnostics = await client.get(
            f"/api/v1/providers/fixture/runs/{run_id}/diagnostics",
            headers={"Authorization": "Bearer operator-token"},
        )
        failures = await client.get(paths[1], headers={"Authorization": "Bearer operator-token"})
        history = await client.get(
            "/api/v1/providers/fixture/runs",
            headers={"Authorization": "Bearer operator-token"},
        )
        assert diagnostics.status_code == 200
        assert diagnostics.json()["raw_responses"] == [
            {"method": "POST", "url": "https://example.test/token", "status": 200}
        ]
        assert legacy_secret not in diagnostics.text
        assert "legacy-user" not in diagnostics.text
        assert "legacy-password" not in diagnostics.text
        assert failures.status_code == 200
        assert failures.json()["items"][0]["raw_payload"] == {"token": "retained-payload"}
        assert history.status_code == 200
        assert history.json()["items"][0]["error_message"] == private_run_value
        assert history.json()["items"][0]["log_excerpt"] == private_run_value
        assert history.json()["items"][0]["cursor_before"] == {"token": private_cursor_value}
