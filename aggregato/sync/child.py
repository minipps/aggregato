"""The child entrypoint: fetch and normalize, write JSON lines, exit (research.md ).

This process imports **one** provider and has no database engine. That is not a convention the
review enforces — it is why the process exists. Three separate requirements collapse into it:

* hang and crash containment , because the parent can kill a process;
* no database handle and no other provider's secrets in plugin code , because neither is
  reachable from here;
* a per-run wall-clock timeout that actually works (§6.7).

Nothing here writes to the database. Every message goes to **stdout**, one line each; logging
goes to **stderr**, so a provider that prints does not corrupt the protocol stream.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import traceback
from pathlib import Path
from typing import Any

from aggregato.domain.enums import FetchMode
from aggregato.domain.models import Checkpoint, Cursor, RawRecord
from aggregato.providers.base import ProviderContext
from aggregato.providers.errors import ProviderError, RateLimited
from aggregato.providers.http import PoliteClient, PolitenessPolicy
from aggregato.providers.registry import load_provider
from aggregato.sync.errors import classify
from aggregato.sync.protocol import (
    BatchMessage,
    CheckpointMessage,
    ChildMessage,
    ErrorMessage,
    FailureMessage,
    encode,
)

log = logging.getLogger("aggregato.sync.child")


def _politeness_policy(provider: Any) -> PolitenessPolicy:
    """Build request pacing independently from the provider's scheduler cadence."""
    return PolitenessPolicy(acquisition=provider.acquisition)


def emit(message: ChildMessage) -> None:
    """Write one protocol message to stdout and flush.

    Flushing per message matters: the parent streams these, and a buffered checkpoint that never
    arrives before a crash is a checkpoint that did not happen .
    """
    sys.stdout.write(encode(message))
    sys.stdout.flush()


async def run(
    provider_id: str,
    *,
    mode: FetchMode,
    cursor: Cursor | None,
    config: dict[str, Any],
    secrets: dict[str, str],
    state: dict[str, str],
    import_path: Path | None,
    provider_dir: Path | None = None,
    replay_records: list[RawRecord] | None = None,
) -> int:
    """Fetch and normalize one run's worth of records.

    Args:
        provider_id: Which provider to load. Only this one is imported.
        mode: ``incremental``, ``full``, or ``import``.
        cursor: Where to resume, or ``None`` to start over.
        config: The provider's validated configuration, as a plain dict from the parent.
        secrets: This provider's credentials only .
        state: The provider's own opaque key/value store.
        import_path: Set only in ``import`` mode.

    Returns:
        A process exit code: 0 if the run completed, 1 if it ended on a classified error.
    """
    provider = load_provider(provider_id, provider_dir)
    # ``default_poll_interval`` is the scheduler cadence (usually hours), not an HTTP rate limit.
    # Feeding it to the request limiter made a provider with two requests per run sleep for an hour
    # after its first request.  Providers without an explicit request-rate declaration use the
    # host-owned acquisition floor.
    client = PoliteClient(_politeness_policy(provider))
    ctx = ProviderContext(
        http=client,  # type: ignore[arg-type]  # PoliteClient is the wrapper the contract promises
        config=provider.config_model.model_validate(_with_secrets(config, secrets)),
        secrets=secrets,
        log=log,
        state=state,
        import_path=import_path,
    )

    try:
        if replay_records is not None:
            # Replay never calls fetch: the durable raw payload is the source of truth.
            for record in replay_records:
                _emit_normalized(provider, record)
        else:
            async with client:
                async for item in provider.fetch(ctx, cursor, mode):
                    if isinstance(item, Checkpoint):
                        emit(CheckpointMessage(cursor=item.cursor))
                        continue
                    _emit_normalized(provider, item)
    except ProviderError as exc:
        # The child classifies, because only the child saw the exception. The parent still owns
        # retry policy (contract §4).
        emit(
            ErrorMessage(
                error_class=exc.error_class,
                message=str(exc) or type(exc).__name__,
                detail=_tail(traceback.format_exc()),
                retry_after_seconds=(exc.retry_after if isinstance(exc, RateLimited) else None),
            )
        )
        return 1
    except Exception as exc:
        # The boundary catches everything: anything not a ProviderError is `internal`.
        emit(
            ErrorMessage(
                error_class=classify(exc),
                message=f"{type(exc).__name__}: {exc}",
                detail=_tail(traceback.format_exc()),
            )
        )
        return 1
    return 0


def _emit_normalized(provider: Any, record: RawRecord) -> None:
    """Normalize one record, or report it as a single failure and keep going.

    A record that will not convert costs its own row, not the run . The exception is caught
    here rather than in the loop above so that a provider raising a *ProviderError* from
    ``normalize`` still ends the run — that signals a platform-level problem, not a bad record.
    """
    try:
        batch = provider.normalize(record)
    except ProviderError:
        raise
    except Exception as exc:
        # One poisoned record, not a failed run.
        emit(
            FailureMessage(
                native_id=record.native_id,
                payload=record.payload,
                error=f"{type(exc).__name__}: {exc}",
            )
        )
        return
    emit(BatchMessage(raw=record, batch=batch))


def _with_secrets(config: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
    """Reconstitute secret fields inside the selected provider's validated config."""
    merged = dict(config)
    for path, value in secrets.items():
        node = merged
        parts = path.split(".")
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = value
    return merged


def _tail(text: str, limit: int = 4000) -> str:
    """The end of a traceback, which is the part naming what actually failed."""
    return text if len(text) <= limit else "…" + text[-limit:]


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run. The parent invokes this as ``python -m aggregato.sync.child``.

    The run's parameters arrive as ONE JSON object on stdin rather than many flags: they include a
    nested config object, a secrets mapping, and on a replay run every retained payload, so argv is
    both the wrong size (the kernel caps a single argument at 128 KiB) and the wrong place (it is
    world-readable, and the config carries resolved credentials).
    """
    parser = argparse.ArgumentParser(
        description="Run one provider sync. Reads a JSON payload on stdin, writes JSON lines to "
        "stdout."
    )
    parser.add_argument("--provider", required=True)
    args = parser.parse_args(argv)

    # stderr, never stdout: stdout is the protocol stream.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)

    payload = json.loads(sys.stdin.read())
    cursor_state = payload.get("cursor")
    import_path = payload.get("import_path")

    return asyncio.run(
        run(
            args.provider,
            mode=FetchMode(payload.get("mode", FetchMode.INCREMENTAL)),
            cursor=Cursor(state=cursor_state) if cursor_state is not None else None,
            config=payload.get("config", {}),
            secrets=payload.get("secrets", {}),
            state=payload.get("state", {}),
            import_path=Path(import_path) if import_path else None,
            provider_dir=Path(payload["provider_dir"]) if payload.get("provider_dir") else None,
            replay_records=[
                RawRecord.model_validate(record) for record in payload["replay_records"]
            ]
            if "replay_records" in payload
            else None,
        )
    )


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    sys.exit(main())
