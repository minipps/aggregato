# Writing a provider

The normative interface is [the provider-plugin contract](contracts/provider-plugin.md). This guide is a practical path to a local, unreviewed drop-in provider; it does not replace that contract.

## 1. Choose the acquisition surface

Use the highest surface the service offers: official API, documented feed, user export, then scrape only as a last resort. A scraper uses the host HTTP client, raises `BlockedError` on CAPTCHA or blocking, and never attempts to bypass it.

## 2. Create a package

Set `AGGREGATO_PROVIDER_DIR` to a directory you control. Add one package directly inside it; its directory name is the provider ID and must be a stable lowercase slug.

```text
$AGGREGATO_PROVIDER_DIR/
└── example_log/
    ├── __init__.py
    └── manifest.json
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
```

`manifest.json` is required for discovery. It contains the host-visible declaration, so listing a
provider never imports `__init__.py`:

```json
{
  "name": "Example Log",
  "media_types": ["film"],
  "capabilities": ["poll"],
  "acquisition": "api",
  "schema_version": 1,
  "default_poll_interval_seconds": 21600,
  "config_schema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "token": {
        "type": "string",
        "description": "Personal API token",
        "x-aggregato-public": false,
        "writeOnly": true
      }
    },
    "required": ["token"],
    "x-aggregato-public": true
  }
}
```

### Discovery and the trust boundary

The host lists providers from host-owned metadata. Bundled providers have a static manifest in the
core; a drop-in must supply `manifest.json` next to `__init__.py` with `name`, `media_types`,
`capabilities`, `acquisition`, `schema_version`, `default_poll_interval_seconds`, and
`config_schema`. Listing that metadata does not execute `__init__.py`. A drop-in without a manifest
is warned about and skipped; its provider code is not imported during discovery.

The package is imported only after an operator selects its provider for an operation. A scheduled
sync runs the selected provider in its short-lived child, while the parent keeps the database and
performs validation and writes. That child boundary contains crashes and hangs; it is not an OS
sandbox. The runner passes a minimal explicit runtime environment and the selected provider's
configuration/secrets over the child protocol. An unreviewed drop-in still retains filesystem and
network permissions, so review the source and deployment location.
Import errors affect the selected operation and do not make an unrelated provider part of discovery.

The inheritance above is only a compact illustration. A real provider implements the three methods in the contract: `fetch`, pure synchronous `normalize`, and asynchronous `check`.

### Optional current playback

If the platform exposes current playback, opt in explicitly in both the provider object and the
static manifest:

```python
from aggregato.domain.models import NowPlayingItem
from aggregato.providers.base import NowPlayingProvider


class ExampleProvider(FixtureProvider, NowPlayingProvider):
    capabilities = {Capability.POLL, Capability.NOW_PLAYING}

    async def now_playing(self, ctx: ProviderContext) -> NowPlayingItem | None:
        response = await ctx.http.get("/playing-now")
        # Validate the envelope; return None only for a documented idle response.
        return self._normalize_current(response.json())
```

The manifest must include `"now_playing"` in `capabilities`. Return one normalized
`NowPlayingItem` or `None`; do not create an entry, include playback progress/duration, or retain a
raw response. Reuse the historical normalization path where the platform's current and historical
payloads describe the same work. Ship recorded active, idle, malformed, and rate-limited responses;
providers without this capability need none of these fixtures.

## 3. Keep the boundary strict

`fetch(ctx, cursor, mode)` yields `RawRecord` and `Checkpoint` values. Use only `ctx.http`; do not construct an HTTP client. `normalize(raw)` is pure and returns one `NormalizedBatch`; extract every identifier in the raw payload and use only the project’s closed media-type, role, and subject-reference vocabularies. `check(ctx)` returns `CheckResult` for both success and actionable failure.

Use flat Pydantic configuration fields with descriptions. Mark secrets `writeOnly`; the host uses the schema to render the configuration reference and never returns secret values.

## 4. Test it offline

Record representative fixtures, including invalid credentials and (for a scraper) changed HTML. If
the provider declares `now_playing`, also record active and idle responses plus a structure-change
case. Register the provider with the conformance assertion groups and run:

```bash
uv run pytest tests/conformance
```

The provider is intentionally marked **unreviewed** in the UI. Review its source before enabling it.

## 5. Ship normalizer fixes safely

Increment `schema_version` whenever `normalize` changes how retained raw data maps to entries, opinions, credits, or identifiers. On the next sync Aggregato replays saved raw payloads through the child process before fetching new data. Do not change the stable `id` to represent an acquisition change; it is the durable join key.
