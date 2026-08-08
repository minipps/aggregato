from __future__ import annotations

from pathlib import Path

import yaml


def test_container_entrypoint_explicitly_scopes_forwarded_headers() -> None:
    root = Path(__file__).parents[2]
    entrypoint = (root / "docker" / "entrypoint.sh").read_text(encoding="utf-8")

    assert "--proxy-headers" in entrypoint
    assert '--forwarded-allow-ips "${AGGREGATO_FORWARDED_ALLOW_IPS:-127.0.0.1}"' in entrypoint


def test_deployment_examples_expose_quota_and_proxy_settings() -> None:
    root = Path(__file__).parents[2]
    env_example = (root / ".env.example").read_text(encoding="utf-8")
    compose = (root / "docker" / "compose.yml").read_text(encoding="utf-8")

    for setting in (
        "AGGREGATO_IMPORT_QUOTA_BYTES",
        "AGGREGATO_IMPORT_TOTAL_QUOTA_BYTES",
        "AGGREGATO_FORWARDED_ALLOW_IPS",
    ):
        assert setting in env_example
        assert setting in compose


def test_compose_always_runs_a_frontend_and_dev_selects_hot_reload_target() -> None:
    root = Path(__file__).parents[2]
    base = yaml.safe_load((root / "docker" / "compose.yml").read_text(encoding="utf-8"))
    dev = yaml.safe_load((root / "docker" / "compose.dev.yml").read_text(encoding="utf-8"))

    assert set(base["services"]) == {"aggregato", "frontend"}
    assert base["services"]["aggregato"]["expose"] == ["8000"]
    assert "ports" not in base["services"]["aggregato"]
    assert base["services"]["frontend"]["image"].startswith("ghcr.io/minipps/aggregato-frontend:")
    assert base["services"]["frontend"]["ports"] == ["${AGGREGATO_FRONTEND_PORT:-8000}:80"]

    dev_frontend = dev["services"]["frontend"]
    assert dev_frontend["build"]["target"] == "development"
    assert dev_frontend["command"][-1] == "80"
    assert "../frontend:/app" in dev_frontend["volumes"]
