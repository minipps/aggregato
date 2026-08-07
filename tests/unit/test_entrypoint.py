from __future__ import annotations

from pathlib import Path


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
