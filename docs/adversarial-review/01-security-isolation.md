# Security and provider isolation findings

The repository treats provider code as a potentially faulty or unreviewed extension, but the
current process boundary is only a subprocess boundary. It is not yet a capability boundary.

## S-01 — Provider loading is not selective (Critical)

\`load_provider()\` calls \`discover_providers()\`, and discovery imports every bundled provider and
every drop-in before returning the requested object. The child then calls that loader while claiming
to import only one provider. See [registry.py:78](../../aggregato/providers/registry.py:78),
[registry.py:93](../../aggregato/providers/registry.py:93), [registry.py:105](../../aggregato/providers/registry.py:105),
and [child.py:86](../../aggregato/sync/child.py:86).

Impact:

- An import-time exception, hang, network call, or resource exhaustion in an unrelated provider
  prevents the selected provider from running.
- A drop-in can execute arbitrary import-time code in the API process whenever provider listing,
  schema loading, enabling, checking, importing, or other discovery-backed routes run.
- A malicious drop-in can observe or interfere with other installed provider modules.
- The UI's “unreviewed” label is not a sandbox.

Fix:

- Make discovery manifest-only. Read package names and static metadata without importing provider
  modules; reject metadata that cannot be represented statically.
- Resolve the selected provider's package path first, then import only that module in the child.
- Keep API operations that need provider code in the worker/child. The API may display cached
  metadata, but must not import arbitrary plugin code.
- Add a regression test that installs a provider whose module import raises and verifies that a
  different provider can still be listed and run.

## S-02 — Child inherits all worker environment variables (Critical)

The subprocess is created without an explicit \`env\`, so it inherits the scheduler environment. See
[runner.py:126](../../aggregato/sync/runner.py:126). The dispatch path currently sends an empty
secrets mapping while the child contract says it receives only the selected provider's secrets.

Impact:

- Any provider can read \`AGGREGATO_TOKEN\`, \`AGGREGATO_READONLY_TOKEN\`, database URLs, cloud
  credentials, CI secrets, and unrelated environment variables with \`os.environ\`.
- A drop-in or compromised bundled provider can exfiltrate the API bearer token and then call every
  write endpoint.
- This defeats the strongest security reason for a child process.

Fix:

- Pass an explicit minimal environment: executable/Python path, locale, and narrowly required
  runtime settings only.
- Do not use environment variables as the provider secret transport. Pass only the validated
  selected-provider secret object through the protocol or a protected one-shot file descriptor.
- Consider a dedicated OS user, read-only source tree, restricted data directory, no network access
  except through a host-owned broker, and resource limits for drop-ins.
- Decide and document whether drop-ins are trusted extensions or untrusted code. “Unreviewed” plus
  unrestricted filesystem, network, and environment access is an unsafe intermediate state.
- Add a test that places sentinel secrets in the parent environment and proves they are absent from
  the child, not just a test that checks the explicit \`secrets\` field.

## S-03 — Provider code executes in the API process (High/Critical for drop-ins)

The ingest-failure replay route imports the provider and calls \`provider.normalize\` during an API
request. See [runs.py:126](../../aggregato/api/routes/runs.py:126),
[runs.py:149](../../aggregato/api/routes/runs.py:149), and [runs.py:168](../../aggregato/api/routes/runs.py:168).
The import-upload route likewise imports the provider and calls its import configuration inference
before queuing the job; see [providers.py:394](../../aggregato/api/routes/providers.py:394).

Impact:

- A broken normalizer can hang or CPU-exhaust the API event loop.
- A drop-in can run arbitrary code with API-process privileges.
- The replay path writes directly from the API request, reintroducing the very process coupling the
  scheduler/child design is intended to avoid.

Fix:

- Make replay a durable worker job. The API should mark a failure as replay-requested and return
  \`202\` plus a job/run identifier.
- Run normalization and parent-side writing in the scheduler/worker path, with the provider loaded
  only in the child.
- Move import metadata inference into the child, or remove inference and require explicit settings.
- Add tests proving no API request imports a provider module or calls provider methods.

## S-04 — Image cache is an SSRF, memory, and stored-XSS surface (High)

The image cache performs a direct HTTP fetch with redirects enabled and no URL/host/private-network
policy. It loads the complete response into memory and accepts any \`image/*\` content type. See
[cache.py:46](../../aggregato/images/cache.py:46), [cache.py:76](../../aggregato/images/cache.py:76),
and [cache.py:86](../../aggregato/images/cache.py:86).

Impact:

- A provider-supplied URL can target loopback services, cloud metadata endpoints, private RFC1918
  addresses, multicast, unspecified addresses, or an internal host reached after a redirect.
- A server can return an unbounded body and exhaust process memory or disk.
- SVG is an active document format. Serving attacker-controlled SVG from the same origin can create
  script/XSS risk depending on browser navigation and CSP behavior.
- Concurrent misses can fetch and write the same digest simultaneously; a crash can leave a partial
  file that later requests treat as complete.

Fix:

- Parse and validate schemes (\`https\` by default), resolve every redirect, reject loopback,
  link-local, private, multicast, unspecified, and internal DNS results, and re-check DNS at connect
  time. Prefer an explicit allowlist for provider hosts.
- Stream with a hard byte limit; enforce both declared and observed size; validate magic bytes and
  decode the image rather than trusting \`Content-Type\`.
- Reject SVG or sanitize/rasterize it; send \`X-Content-Type-Options: nosniff\`, a restrictive CSP,
  and a non-inline content disposition.
- Use per-digest locking plus temporary-file/\`fsync\`/atomic-rename writes. Verify the stored digest.
- Add SSRF, redirect, oversized-body, SVG, concurrent-miss, and interrupted-write tests.

## S-05 — Child stream supervision can deadlock or reject valid protocol lines (High)

The protocol allows lines up to 8 MiB, but \`create_subprocess_exec\` is created with the asyncio
stream default limit. See [protocol.py:22](../../aggregato/sync/protocol.py:22) and
[runner.py:126](../../aggregato/sync/runner.py:126). A valid line larger than the stream limit can
fail before the explicit protocol-size check. The runner also consumes stdout and waits for the
process before reading stderr; see [runner.py:139](../../aggregato/sync/runner.py:139) and
[runner.py:161](../../aggregato/sync/runner.py:161).

Impact:

- A large but contract-valid batch is classified as an internal failure.
- A provider that writes enough logs to fill stderr can block before it can finish stdout, causing a
  false hang and killing a useful run.

Fix:

- Set the subprocess stream limit to an explicit protocol-compatible value, or lower and document
  one shared limit.
- Drain stdout and stderr concurrently; cap stderr bytes and retain only a tail.
- Bound total stdout, total raw replay payload, and batch count as well as individual line size.
- Test a line between the asyncio default and the protocol maximum and a child that fills stderr.

## S-06 — Import uploads have bounded request size but no durable quota or lifecycle (Medium/High)

Uploads are written synchronously inside an async route and are retained under the data directory;
see [providers.py:432](../../aggregato/api/routes/providers.py:432) and
[providers.py:448](../../aggregato/api/routes/providers.py:448).

Impact:

- Repeated authenticated uploads can fill the volume because retention does not remove import files.
- A process crash or DB failure leaves \`.uploading\` or completed orphan files.
- Synchronous 50 MiB writes block the API event loop.
- An unfinished import job has no lease/retry/recovery path and can remain permanently stuck.

Fix:

- Enforce a configurable total/per-provider quota before accepting data, and add orphan/retention
  cleanup.
- Use a job state machine with lease expiry, retry/error fields, and atomic file ownership.
- Stream through a bounded worker/thread path rather than blocking the async event loop.
- Delete or archive the file after successful import according to an explicit retention policy.

## S-07 — Authentication is solid at the primitive level but lacks abuse controls (Medium)

Session exchange creates a new session on every successful exchange and only removes expired rows on
that path; see [deps.py:202](../../aggregato/api/deps.py:202). There is no visible rate limit or
maximum active-session policy. Public image detection is also substring-based at
[deps.py:259](../../aggregato/api/deps.py:259).

Impact and hardening:

- A bearer holder can create unbounded sessions, and a brute-force attacker can generate expensive
  auth work without throttling.
- A future route whose path merely contains \`/media/image/\` could accidentally bypass authentication.
- Add rate limits/backoff, an active-session cap or periodic cleanup, exact route matching, and
  security headers (\`CSP\`, \`nosniff\`, \`frame-ancestors\`/frame protection).
- Make reverse-proxy trust explicit so the cookie \`Secure\` decision cannot be wrong behind TLS
  termination.

The existing constant-time comparisons, HttpOnly/SameSite cookie, CSRF binding, read-only method
gate, loopback default, and opt-in CORS are positive controls and should be preserved.

## S-08 — HTTP politeness is local, redirect-blind, and partly provider-overridable (Medium/High)

The limiter and per-host semaphores are created per \`PoliteClient\`, hence per provider run; see
[http.py:64](../../aggregato/providers/http.py:64) and [http.py:131](../../aggregato/providers/http.py:131).
Redirects are enabled on the underlying client, but only the original target is locked/rate-limited;
see [http.py:185](../../aggregato/providers/http.py:185). Request kwargs are passed through at
[http.py:164](../../aggregato/providers/http.py:164), allowing providers to override headers,
timeouts, or redirect behavior.

Impact:

- Two concurrent runs or providers can hit the same host concurrently despite the scraper promise.
- Redirect targets can bypass host pacing and any future host policy.
- A provider can override host-owned request properties.
- \`Retry-After\` parsing appears to support delta-seconds only, not the legal HTTP-date form.

Fix:

- Put host admission/rate state in a shared worker broker or scheduler-level coordinator.
- Apply policy on every redirect hop, or disable redirects and follow them explicitly.
- Protect \`User-Agent\`, timeout, redirect, and transport options; accept only explicitly allowed
  conditional headers.
- Parse both \`Retry-After\` forms and add multi-run/multi-host policy tests.
