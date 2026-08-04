"""Configuration precedence and failure modes (research.md , , )."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aggregato.config import ConfigError, MissingTokenError, load_config

TOKEN = "t0ken"


def write_config(tmp_path: Path, data: dict[str, object]) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_precedence_defaults_then_file_then_env_then_database(tmp_path: Path) -> None:
    """defaults ← file ← ${ENV} ← database overrides, one setting per rung."""
    path = write_config(tmp_path, {"api": {"port": 9001, "host": "10.0.0.1"}})

    # File beats the default (host), env beats the file (port), default survives untouched.
    cfg = load_config(env={"AGGREGATO_TOKEN": TOKEN, "AGGREGATO_PORT": "9002"}, config_file=path)
    assert cfg.api.host == "10.0.0.1"
    assert cfg.api.port == 9002
    assert cfg.data_dir == Path("./data")

    # Database overrides beat the environment.
    cfg = load_config(
        env={"AGGREGATO_TOKEN": TOKEN, "AGGREGATO_PORT": "9002"},
        config_file=path,
        db_overrides={"api.port": 9003},
    )
    assert cfg.api.port == 9003


def test_missing_token_is_fatal_and_names_the_setting() -> None:
    """The only fatal configuration error: there is no unauthenticated mode ."""
    with pytest.raises(MissingTokenError) as exc:
        load_config(env={})
    assert "api.token" in str(exc.value)
    assert "AGGREGATO_TOKEN" in str(exc.value)


def test_empty_token_counts_as_unset() -> None:
    """Copying .env.example leaves `AGGREGATO_TOKEN=`; that must not pass as a token."""
    with pytest.raises(MissingTokenError):
        load_config(env={"AGGREGATO_TOKEN": ""})


def test_invalid_provider_block_disables_only_that_provider(tmp_path: Path) -> None:
    """: one broken block never blocks startup and never affects another provider."""
    path = write_config(
        tmp_path,
        {
            "providers": {
                "broken_shape": "not-a-mapping",
                "broken_secret": {"token": "${NEVER_SET_ANYWHERE}"},
                "good": {"token": "${LISTENBRAINZ_TOKEN}", "poll_interval_seconds": 3600},
            }
        },
    )

    cfg = load_config(
        env={"AGGREGATO_TOKEN": TOKEN, "LISTENBRAINZ_TOKEN": "lb-secret"},
        config_file=path,
    )

    for pid in ("broken_shape", "broken_secret"):
        assert cfg.providers[pid].enabled is False
        assert cfg.providers[pid].error
        assert cfg.providers[pid].settings == {}
    assert "mapping" in (cfg.providers["broken_shape"].error or "")
    assert "NEVER_SET_ANYWHERE" in (cfg.providers["broken_secret"].error or "")

    # The rest of the configuration is intact and the healthy provider is usable.
    good = cfg.providers["good"]
    assert good.enabled is True
    assert good.error is None
    assert good.settings == {"token": "lb-secret", "poll_interval_seconds": 3600}
    assert cfg.api.token.get_secret_value() == TOKEN


def test_file_pinned_reporting(tmp_path: Path) -> None:
    """Only what the file set is pinned; env- and default-sourced settings are editable ."""
    path = write_config(tmp_path, {"api": {"port": 9001}, "providers": {"good": {"a": 1}}})

    cfg = load_config(env={"AGGREGATO_TOKEN": TOKEN, "AGGREGATO_HOST": "1.2.3.4"}, config_file=path)

    assert cfg.is_file_pinned("api.port")
    assert cfg.is_file_pinned("providers.good.a")
    assert not cfg.is_file_pinned("api.host")  # env-sourced
    assert not cfg.is_file_pinned("data_dir")  # default
    assert not cfg.is_file_pinned("api.token")


def test_interpolation_resolves_and_unset_reference_is_an_error(tmp_path: Path) -> None:
    """A referenced-but-unset variable is an error for that setting, never a literal leak."""
    path = write_config(tmp_path, {"database_url": "postgresql://u:${DB_PASSWORD}@db/aggregato"})

    cfg = load_config(env={"AGGREGATO_TOKEN": TOKEN, "DB_PASSWORD": "pw"}, config_file=path)
    assert cfg.database_url == "postgresql://u:pw@db/aggregato"

    with pytest.raises(ConfigError) as exc:
        load_config(env={"AGGREGATO_TOKEN": TOKEN}, config_file=path)
    assert "DB_PASSWORD" in str(exc.value)
    assert "database_url" in str(exc.value)


def test_secrets_are_absent_from_the_serialized_view(tmp_path: Path) -> None:
    """Nothing the API can display carries a resolved secret ."""
    path = write_config(
        tmp_path,
        {
            "database_url": "postgresql://u:${DB_PASSWORD}@db/aggregato",
            "providers": {"good": {"token": "${LISTENBRAINZ_TOKEN}"}},
        },
    )
    env = {
        "AGGREGATO_TOKEN": TOKEN,
        "DB_PASSWORD": "pw-secret",
        "LISTENBRAINZ_TOKEN": "lb-secret",
    }

    public = load_config(env=env, config_file=path).public_dict()

    dumped = repr(public)
    for secret in (TOKEN, "pw-secret", "lb-secret"):
        assert secret not in dumped
    assert "token" not in public["api"]
    # The reference survives instead, which is what may be written back to disk.
    assert public["database_url"] == "postgresql://u:${DB_PASSWORD}@db/aggregato"
    assert public["providers"]["good"]["settings"]["token"] == "${LISTENBRAINZ_TOKEN}"


def test_no_file_and_defaults_only() -> None:
    """The documented zero-config path: a token in the environment and nothing else."""
    cfg = load_config(env={"AGGREGATO_TOKEN": TOKEN, "AGGREGATO_DATA": "/srv/agg"})

    assert cfg.config_file is None
    assert cfg.providers == {}
    assert cfg.file_pinned == frozenset()
    assert cfg.database_url == "sqlite+aiosqlite:////srv/agg/aggregato.db"


def test_malformed_config_file_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("api: [1, 2\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(env={"AGGREGATO_TOKEN": TOKEN}, config_file=path)


def test_a_readonly_token_equal_to_the_real_one_is_refused() -> None:
    """Otherwise it would silently grant full access rather than read-only access."""
    with pytest.raises(ConfigError, match="readonly_token"):
        load_config(env={"AGGREGATO_TOKEN": "same", "AGGREGATO_READONLY_TOKEN": "same"})


def test_the_readonly_token_never_reaches_the_api_view(tmp_path: Path) -> None:
    config = load_config(
        env={
            "AGGREGATO_TOKEN": "full",
            "AGGREGATO_READONLY_TOKEN": "reader",
            "AGGREGATO_DATA": str(tmp_path),
        }
    )
    assert config.api.readonly_token is not None
    assert "readonly_token" not in config.public_dict()["api"]
