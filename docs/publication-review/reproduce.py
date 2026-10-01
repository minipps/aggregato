"""Run the publication regressions using the existing offline pytest fixtures.

From the repository root: .venv/bin/python docs/publication-review/reproduce.py
"""

from __future__ import annotations

import pytest

REGRESSIONS = (
    "tests/integration/test_export_restore.py",
    "tests/contract/test_run_diagnostics_security.py",
    "tests/contract/test_sync_websocket.py",
    "tests/unit/test_child_response.py",
    "tests/unit/test_scheduler.py",
    "tests/unit/test_worker.py",
    "tests/integration/test_replay_atomicity.py",
    "tests/integration/test_manual_durability.py",
    "tests/integration/test_static_frontend.py",
    "tests/unit/test_settings_storage.py",
    "tests/integration/test_effective_image_cache_setting.py",
    "tests/unit/test_pagination.py",
    "tests/unit/test_entry_search.py",
    "tests/unit/test_search_index_migration.py",
    "tests/unit/test_stats_buckets.py",
    "tests/unit/test_migrations.py",
)

if __name__ == "__main__":
    raise SystemExit(pytest.main(["-m", "not bench", *REGRESSIONS]))
