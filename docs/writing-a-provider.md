# Writing a provider

The normative interface is [the provider-plugin contract](../specs/001-media-log-aggregator/contracts/provider-plugin.md). This guide is a practical path to a local, unreviewed drop-in provider; it does not replace that contract.

## 1. Choose the acquisition surface

Use the highest surface the service offers: official API, documented feed, user export, then scrape only as a last resort. A scraper uses the host HTTP client, raises `BlockedError` on CAPTCHA or blocking, and never attempts to bypass it.

## 2. Create a package

Set `AGGREGATO_PROVIDER_DIR` to a directory you control. Add one package directly inside it; its directory name is the provider ID and must be a stable lowercase slug.

```text
$AGGREGATO_PROVIDER_DIR/
└── example_log/
    └── __init__.py
```

`__init__.py` exposes exactly one module-level object named `provider`. Importing it must not do I/O, make network calls, read credentials, or schedule work.

```python
from datetime import timedelta
from pydantic import BaseModel, Field, SecretStr
from aggregato.domain.enums import Acquisition, Capability, MediaType
from aggregato.providers.fixture import FixtureProvider


class ExampleConfig(BaseModel):
    token: SecretStr = Field(
        description="Personal API token", json_schema_extra={"writeOnly": True}
    )


class ExampleProvider(FixtureProvider):
    id = "example_log"
    name = "Example Log"
    acquisition = Acquisition.API
    media_types = {MediaType.FILM}
    capabilities = {Capability.POLL}
    config_model = ExampleConfig
    schema_version = 1
    default_poll_interval = timedelta(hours=6)


provider = ExampleProvider()
provider_api_version = 1
```

The inheritance above is only a compact illustration. A real provider implements the three methods in the contract: `fetch`, pure synchronous `normalize`, and asynchronous `check`.

## 3. Keep the boundary strict

`fetch(ctx, cursor, mode)` yields `RawRecord` and `Checkpoint` values. Use only `ctx.http`; do not construct an HTTP client. `normalize(raw)` is pure and returns one `NormalizedBatch`; extract every identifier in the raw payload and use only the project’s closed media-type, role, and subject-reference vocabularies. `check(ctx)` returns `CheckResult` for both success and actionable failure.

Use flat Pydantic configuration fields with descriptions. Mark secrets `writeOnly`; the host uses the schema to render the configuration reference and never returns secret values.

## 4. Test it offline

Record representative fixtures, including invalid credentials and (for a scraper) changed HTML. Register the provider with the nine conformance assertion groups and run:

```bash
uv run pytest tests/conformance
```

The provider is intentionally marked **unreviewed** in the UI. Review its source before enabling it. A warning about a different `provider_api_version` means the host can still load it, but its contract may have changed and needs review.

## 5. Ship normalizer fixes safely

Increment `schema_version` whenever `normalize` changes how retained raw data maps to entries, opinions, credits, or identifiers. On the next sync Aggregato replays saved raw payloads through the child process before fetching new data. Do not change the stable `id` to represent an acquisition change; it is the durable join key.
