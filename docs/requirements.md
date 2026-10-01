# Product requirements

This document defines product behavior and acceptance criteria, including requirements that may
still be in progress. See the [architecture](architecture.md), [data model](data-model.md), and
[contracts](contracts/) for implementation details; use the [README](../README.md) and
[operations guide](operations.md) for current user workflows.

## Product journeys

### Product Journey 1: One platform's history, local and browsable

A self-hoster deploys Aggregato with a single command, supplies their username or credentials for
one logging platform, and enables it. The service fetches the history available through that
platform's configured source on a schedule. They browse the results — dated entries, ratings, and
reviews — in a local web UI, with no third-party service involved beyond the platform they already
use.

**Acceptance Scenarios**:

1. **Given** a clean install with no platform enabled, **When** the operator starts the service,
   **Then** it starts successfully, contacts no external host, and reports zero configured
   platforms.
2. **Given** one platform enabled with valid credentials, **When** the first sync completes,
   **Then** every entry available through the configured source for that account is present locally
   with its logged date, date precision, and rating in both the platform's original scale and a
   normalized form.
3. **Given** a completed sync, **When** the same sync runs again over unchanged data, **Then** no
   duplicate entries are created and no existing entry is modified.
4. **Given** an entry the platform recorded with only a month or a year, **When** the operator views
   it, **Then** the display reflects that imprecision rather than showing an invented time.

---

### Product Journey 2: Two platforms, one log

The operator enables a second platform. A film they logged on both platforms appears as one item
with both platforms' ratings and reviews side by side, not as two unrelated rows.

**Acceptance Scenarios**:

1. **Given** two platforms that both publish a shared identifier for the same item, **When** both
   have synced, **Then** the item appears once, listing both platforms' opinions.
2. **Given** two platforms that publish no shared identifier, **When** the same title and year
   appear on both, **Then** they unify only when the match is unambiguous; an ambiguous match is
   never guessed and is queued for the operator instead.
3. **Given** an item that unified incorrectly or failed to unify, **When** the operator corrects it
   by hand, **Then** the correction survives every subsequent sync.
4. **Given** a creator credited on items from two different platforms, **When** both have synced,
   **Then** the creator's page lists both sets of credits with each credit's role.

---

### Product Journey 3: Sync the operator can trust

A platform goes down, changes, rate-limits, or rejects the operator's credentials. Aggregato
recovers on its own where recovery is possible, says exactly what is wrong where it is not, keeps
every other platform working, and never destroys history in the process.

**Acceptance Scenarios**:

1. **Given** a platform returning transient errors, **When** a sync fails, **Then** it retries
   automatically at increasing delays, each attempt is recorded and grouped with its earlier
   attempts, and no operator action is required.
2. **Given** repeated failure past a configured threshold, **When** the threshold is crossed,
   **Then** the platform is marked degraded and the condition is visible in the UI, in the health
   report, and in the application logs.
3. **Given** invalid credentials, an anti-bot block, or a platform whose structure no longer matches,
   **When** the failure occurs, **Then** the service stops retrying and tells the operator which of
   those three happened and what action it requires.
4. **Given** a sync that ingests part of its history then fails, **When** it retries, **Then** it
   resumes from the last fully-processed point rather than restarting or skipping ahead.
5. **Given** one malformed record the platform keeps returning, **When** a sync encounters it,
   **Then** the record is set aside with its original payload, the rest of the sync completes, and
   the failure count is reported.
6. **Given** a platform that only exposes recent activity, **When** a sync sees none of the older
   history, **Then** no older entry is marked deleted.
7. **Given** one platform misconfigured or crashing, **When** the service runs, **Then** it starts
   normally and every other platform syncs unaffected.

---

### Product Journey 4: Fixing identity by hand

The operator works through a queue of items and creators Aggregato refused to guess about, merges
duplicates it created, and splits apart creators it wrongly joined — for example two different
people who share a name.

**Acceptance Scenarios**:

1. **Given** a queued ambiguous match, **When** the operator views it, **Then** the candidates are
   shown ranked with the reason each was proposed, and the operator can link, create, or ignore.
2. **Given** two items that are the same work, **When** the operator merges them, **Then** their
   entries, opinions, and identifiers combine under one item and nothing is lost.
3. **Given** one creator holding credits that belong to two different people, **When** the operator
   splits them, **Then** they choose which individual credits move, and the split shows which
   credits were joined by name rather than by a platform-supplied identifier.
4. **Given** a creator duplicated because their work spans different media, **When** the operator
   opens the queue, **Then** the duplicate pair is proactively suggested for merging — never merged
   automatically.
5. **Given** any merge, split, or queue decision, **When** it is applied, **Then** it can be undone.

---

### Product Journey 5: Platforms without a usable automatic source

For a platform with no usable API or feed, the operator uploads an export file obtained from that
platform. Those entries land in the same archive, filterable alongside everything else.

**Acceptance Scenarios**:

1. **Given** an export file from a supported platform, **When** the operator uploads it, **Then** its
   entries are ingested, unified against existing items, and shown in the log.
2. **Given** the same file uploaded twice, **When** the second upload completes, **Then** no
   duplicate entries exist.
3. **Given** an unrelated or corrupt file, **When** it is uploaded, **Then** the operator gets a clear
   rejection and the existing archive is unchanged.

---

### Product Journey 6: Adding support for a new platform

A contributor adds support for a platform Aggregato does not yet cover, by implementing a narrow
contract — fetch, convert, verify credentials — without needing to understand or modify the core.
Scheduling, retries, rate limiting, storage, and migrations are already handled.

**Acceptance Scenarios**:

1. **Given** a new integration declaring what it supports and what configuration it needs, **When**
   the service starts, **Then** it is discovered, offered in the UI with a working configuration form
   produced from its declared configuration, and remains inert until explicitly enabled.
2. **Given** an integration's data-conversion step, **When** it is tested, **Then** it runs offline
   against recorded sample data with no network, no clock, and no credentials.
3. **Given** an integration that ships a corrected conversion, **When** the correction is released,
   **Then** the archive is rebuilt from already-stored original payloads without re-contacting the
   platform.
4. **Given** an integration that obtains data by scraping, **When** it is offered to the operator,
   **Then** it is labelled as scraping, its risks are stated at the point of enabling it, and the
   service enforces conservative request pacing on it regardless of what the integration requests.

---

### Product Journey 7: Backing up the archive

The operator can back up a SQLite database from Settings, see what disk space is being used, and
adjust optional retention.

**Acceptance Scenarios**:

1. **Given** a populated archive, **When** the operator requests a backup, **Then** a single portable
   artefact is produced containing the data and the configuration, with no secrets in it.
2. **Given** that artefact, **When** it is restored into a clean instance, **Then** the archive is
   complete and browsable without re-syncing any platform.
3. **Given** the storage view, **When** the operator opens it, **Then** current usage is broken out by
   the optional retention features, each of which can be turned off.

---

### Product Journey 8: Seeing what is playing now

An authenticated client can subscribe to the current playback reported by enabled providers that
support it. Playback is transient, source-specific state: it is not added to the media log and is not
merged across providers.

**Acceptance Scenarios**:

1. **Given** an enabled, configured provider reports an active item, **When** the monitor completes
   its poll, **Then** `/api/v1/ws/now-playing` includes that provider's normalized work and local
   artwork path in the next snapshot.
2. **Given** Koito and ListenBrainz report playback at the same time, **When** a client reads the
   snapshot, **Then** it receives two source-specific items, even when their works appear identical.
3. **Given** a capable provider reports no playback, **When** its poll completes, **Then** its prior
   item is cleared and no log entry, provider item, opinion, or sync run is created for that result.
4. **Given** a poll fails transiently, **When** the failure is recorded, **Then** the current item is
   cleared immediately and the monitor retries using the existing 1m/5m/15m/1h ladder, honouring a
   longer `Retry-After`.
5. **Given** credentials are rejected, access is blocked, or the provider structure changes, **When**
   the monitor records the failure, **Then** it suspends now-playing polling until configuration,
   provider schema, or enablement changes reactivate it.
6. **Given** a provider is disabled or its last check is older than 45 seconds, **When** a client
   reads a snapshot, **Then** that provider is omitted.
7. **Given** an authenticated client connects or reconnects, **When** the handshake succeeds, **Then**
   it receives a complete current snapshot; subsequent messages are sent only when the semantic item
   set changes.

---

### Edge Cases

- An empty result may mean no recent activity or a broken integration. Deletion inference must stay
  opt-in and guarded so an unexpected empty result does not remove history.
- A run returns far fewer items than the previous run for the same window: flagged and surfaced
  against a configurable threshold, not silently accepted.
- A platform records activity below the level Aggregato treats as an item (an individual episode of a
  series): the activity is preserved, attached to the item it belongs to, and **excluded from that
  item's aggregate statistics by default**, with an explicit opt-in to include it. Reversing this
  default would silently corrupt every statistic shown.
- Two people may share a name. If name matching joins them, the operator can split them at the
  individual-credit level.
- The same person is credited under a native script and a romanization: they will not join
  automatically, and the queue must suggest the pair.
- Ratings from platforms with different scales and different community distributions: raw value and
  scale are always retained alongside the normalized value, and the UI must not present normalized
  scores as equivalent across platforms.
- An image host is slow or unreachable: image handling never fails or delays a sync.
- Configuration for one platform is invalid: that platform is disabled with a clear error and the
  service still starts. The only configuration error that prevents startup is a missing access
  token; public read-only mode still requires the server's token for protected requests.
- A very large history (hundreds of thousands of entries) browsed to its far end: paging must not
  degrade with depth.
- The archive outgrows the default embedded database: the operator can move to a server database
  without changing the data model; database-native backup and restore are used for PostgreSQL.
- A provider reports the same normalized item on consecutive polls: the host refreshes its checked
  time but preserves `changed_at`, so the WebSocket does not emit a duplicate snapshot.
- A worker or API process is unavailable: playback state is durable, but the API omits an item after
  45 seconds rather than presenting known-stale presence.

## Functional requirements

**Aggregation and storage**

- System MUST store log entries, ratings, reviews, and creator credits from any number of
  configured platforms in one local database.
- System MUST retain, for every ingested record, the platform's original payload
  verbatim, and MUST be able to rebuild all derived data from those payloads without contacting the
  platform again. Retention is on by default; disabling it MUST warn the operator.
- System MUST retain each rating's original value and scale alongside its normalized
  value, and MUST NOT present normalized values as cross-platform equivalents.
- System MUST record the precision of every logged date and MUST NOT present a fabricated
  time as exact.
- System MUST treat re-ingesting already-seen data as a no-op.
- System MUST support a single user only, and MUST NOT carry any concept of user accounts
  in its data.
- System MUST preserve activity recorded below item level, attached to its parent item,
  and MUST exclude such activity from that item's aggregate statistics unless explicitly requested.
- System MUST NOT allow a platform integration to introduce new media categories or write
  arbitrary data; all ingest MUST be validated against the system's own defined shapes.

**Identity**

- System MUST unify items across platforms using identifiers those platforms publish in
  their own responses, and MUST capture every such identifier a payload offers.
- System MUST NOT make outbound requests to any third-party metadata database. The only
  outbound request to a non-platform host permitted is fetching an image from a URL a platform itself
  supplied.
- System MUST unify items lacking a shared identifier only on an unambiguous title,
  category, and year match; ambiguous cases MUST be queued for the operator, never guessed.
- System MUST prefer creating a duplicate item over performing an uncertain merge.
- System MUST provide operator-driven merge for items and both merge and **credit-level
  split** for creators, and MUST record which links were made by name matching versus by a
  platform-supplied identifier so a split is informed.
- System MUST make every operator identity decision durable against all subsequent syncs.
- System MUST NOT automatically join creators by name across unrelated media domains, and
  MUST instead surface such pairs as suggestions.
- System MUST record every credit's role, the platform's own term for that role verbatim,
  the name as that item credited them, and their billing order.
- Every merge, split, and queue decision MUST be reversible.

**Sync and failure handling**

- System MUST sync each platform on its own configurable schedule, defaulting to a value
  the integration declares rather than one global value, and MUST support manual triggering.
- System MUST record every sync attempt with its outcome, item counts, and failure
  detail, and MUST group retries of the same failure together for display.
- System MUST retry recoverable failures automatically at increasing delays without
  operator action, and MUST resume a partially-completed sync from its last fully-processed point.
- System MUST NOT retry failures that retrying cannot fix — invalid credentials, an access
  block, or a platform whose structure has changed — and MUST tell the operator which occurred and
  what it requires.
- System MUST honour platform-supplied rate-limit signals and MUST slow its own cadence in
  response rather than resuming the previous rate immediately.
- System MUST set aside individual records that fail conversion, with their original
  payload, continue the run, report the count, and allow them to be re-processed after a fix.
- System MUST NOT infer deletions for platforms that do not report deletions; inferred
  deletion MUST be off by default.
- A failing, hanging, or crashing platform integration MUST NOT affect any other
  platform, the browsing experience, or service startup.
- System MUST flag, rather than silently accept, a sync returning fewer items than a
  configurable proportion of what the previous run returned for the same window.
- System MUST poll enabled providers declaring `now_playing` independently of history syncs,
  using a 15-second host interval and no more than three concurrent isolated children.
- System MUST persist at most one parent-validated transient now-playing item per capable
  provider, refresh its checked time on every completed attempt, and never write playback to history.
- System MUST clear an active now-playing item on an idle result or failed attempt; auth,
  blocked, and structure-change failures MUST suspend polling until configuration, schema, or
  enablement changes.

**Reading the archive**

- System MUST expose one documented read interface covering items, entries, opinions,
  creators, platforms, sync history, failures, and the resolution queue.
- System MUST support filtering the log by media category, media domain, platform, entry
  kind, creator, role, date range, score range, presence of a review, and free text over titles and
  review text, with sorting by logged date, ingest date, or score.
- System MUST support filtering by media domain as a first-class alternative to
  enumerating individual categories, while keeping every individual category separately addressable.
- System MUST page results in a way that does not degrade as the caller reads deeper into
  a large history.
- The web UI MUST consume only the same public read interface available to any other
  client; if a screen needs data the interface cannot express, the interface is extended.
- System MUST require authentication for state-changing access and by default for reads. A public
  read-only setting MAY allow credential-free GET/HEAD requests on public read routes. Exports and
  raw diagnostics MUST require operator authentication. The operator's token MUST NOT appear in
  browser URLs or page source.
- System MUST serve platform-supplied images through a local cache rather than linking to
  the third-party host, MUST fetch them lazily so image availability never affects a sync, and MUST
  show a placeholder rather than falling back to third-party linking when caching is off.
- System MUST expose current playback through an authenticated `/api/v1/ws/now-playing`
  WebSocket that sends a complete initial snapshot and change-only updates, omitting disabled,
  failed, or stale providers; it MUST accept API/read-only bearer and session-cookie authentication
  and reject an unauthenticated handshake with close code `1008`.

**Platform integrations**

- Support for each platform MUST be an independent integration implementing a narrow
  contract, addable without modifying the core.
- The system MUST own scheduling, retrying, rate limiting, request pacing, storage, and
  migrations; an integration MUST NOT need to reimplement them.
- An integration's data-conversion step MUST be free of network, clock, and storage access
  so it is testable offline from recorded samples with no credentials.
- An integration MUST receive only its own credentials and configuration, and MUST NOT
  receive direct storage access or any other integration's secrets.
- Integrations MUST be discovered automatically but MUST remain inert until explicitly
  enabled; a fresh install MUST contact nothing.
- An integration MUST declare its configuration such that a working settings form is
  produced with no additional interface work.
- Each integration MUST declare how it obtains data, and that provenance MUST be visible
  to the operator.
- Integrations MUST ship with the core release and MUST NOT be installable from within the
  application. Any move toward in-application installation or third-party distribution MUST be
  preceded by a re-evaluation of the isolation model, because integrations run with the service's own
  privileges.
- Integrations MUST use the highest-fidelity data surface a platform offers, resorting to
  scraping only when every better surface is insufficient, and MUST document that evaluation.
- For scraping integrations the system MUST enforce, independent of integration behaviour:
  conservative pacing with a floor the integration cannot raise, one request at a time per host, an
  identifying user agent naming the project and a contact URL, and backoff on refusal responses.
- System MUST NOT circumvent access controls, paywalls, or anti-bot measures, MUST NOT
  ship or integrate any solver for them, and MUST stop and report when one is encountered.
- A scraping integration MUST retrieve only data the authenticating operator can see in
  their own browser on their own account, and MUST NOT bundle or share credentials.
- A scraping integration MUST ship recorded samples and offline tests as a condition of
  acceptance.
- When a platform later offers a better data surface, the integration MUST be able to
  migrate to it without duplicating already-ingested entries where identifiers permit.
- A platform MAY declare the optional `now_playing` capability and implement
  `now_playing(ctx) -> NowPlayingItem | None`; providers that do not declare it remain valid and
  unchanged.
- A now-playing result MUST contain only normalized work, credits, and external identifiers;
  it MUST NOT contain playback progress, duration, a logged event, or a raw provider payload.

**Operation**

- System MUST deploy as a single unit with one storage location and one command, with no
  additional services and no third-party API keys beyond the operator's own platform credentials.
- System MUST apply forward-only schema migrations automatically on startup and take a consistent
  backup before migrating an on-disk SQLite database.
- Settings MUST provide a consistent archive download for on-disk SQLite, containing the database
  snapshot, cached images, and public configuration. It MUST omit configured secrets, sessions,
  ingest-failure payloads, run diagnostics, and pending jobs. The archive contains personal history
  and retained provider data and MUST be treated as private.
- The archive restore helper MUST validate and stage a restore into an empty data directory before
  publishing it. Restored providers MUST be disabled until the operator supplies fresh credentials;
  public configuration is returned for review and is not applied automatically.
- PostgreSQL backup and restore use PostgreSQL tools; Settings archive download is SQLite-only.
- System MUST report per-platform health including last successful sync, and MUST surface
  degraded platforms prominently.
- System MUST keep secrets out of on-disk configuration it writes, referencing them from
  the environment instead.
- System MUST show current storage usage broken out by each optional retention feature,
  each of which MUST be independently switchable.
- Sync-history retention MUST be configurable, keeping failures longer than successes by
  default.
- The service MUST keep now-playing state transient and bounded to one item per provider; it
  MUST NOT retain playback history, add an event broker, or require a persistent provider worker.

### Key Entities

- **Item (work)**: A canonical piece of media — one film, book, album, series, or series season —
  independent of any platform. Carries title forms, release year, category, an optional parent item,
  and any images a platform supplied.
- **Platform record**: One platform's own record of an item, with that platform's native identifier
  and its verbatim original payload. Links to an item once identity is resolved.
- **Entry**: One dated log record — a watch, a listen, a finished book, a progress update — with its
  date precision, optional progress, and an optional reference to a sub-unit of the item.
- **Opinion**: One platform's evaluation of an item by the operator — score in original and normalized
  form, review text, liked flag, spoiler flag — with an optional sub-unit reference.
- **Identifier**: An external identity for an item or creator, in a named space, recorded with how it
  was established: asserted by a platform, matched by the system, or decided by the operator.
- **Creator**: A person, group, studio, or imprint credited on items, holding every name form ever
  seen for them and every external identifier.
- **Credit**: The link between a creator and an item, carrying a normalized role, the platform's own
  role term, the name as credited on that item, and billing order.
- **Platform integration**: An independently written unit of support for one external platform,
  declaring what it supports, how it obtains data, what configuration it needs, and which rating
  scales it uses.
- **Sync run**: One execution of one platform's fetch, with outcome, counts, failure classification,
  and its relationship to earlier attempts at the same work.
- **Resolution item**: One unresolved identity question about an item or a creator, with ranked
  candidates and the reason each was proposed, plus the operator's eventual decision.
- **Ingest failure**: One record that could not be converted, kept with its original payload for
  diagnosis and later re-processing.

## Success criteria

### Measurable Outcomes

These are acceptance targets, not claims that current measurements meet them.

- From a clean machine, an operator reaches a first browsable synced history with one
  platform configured in under 15 minutes, having supplied no credentials except their own for that
  platform.
- For items logged on two platforms where at least one platform publishes a shared
  identifier, at least 95% unify into a single item with no operator involvement.
- No sequence of failed and retried syncs ever produces a lost or duplicated entry;
  verified by repeatedly interrupting syncs over a fixed data set and comparing the result to an
  uninterrupted run.
- A single platform failing — for any of the classified failure reasons — leaves every
  other platform's sync unaffected 100% of the time, and never prevents the service from starting.
- For every failure the operator sees, the interface names what is wrong and what action
  it needs; no failure surfaces only as a generic error.
- An operator clears a resolution decision in under 10 seconds per decision, working from
  the keyboard, with an undo available.
- Browsing and filtering an archive of 1,000,000 entries returns the first page of results
  in under 1 second at the 95th percentile, and paging to the far end of that history is no slower
  than paging near the start.
- Sustained ingest of a high-volume platform processes at least 50,000 entries per hour on
  modest single-board-class hardware, and ingest throughput does not degrade measurably as the archive
  grows to 1,000,000 entries.
- A contributor with no prior knowledge of the codebase adds support for a new platform by
  implementing the documented contract only, with zero changes to core code and zero interface work
  for its settings screen.
- An integration's data conversion is fully testable with no network access and no
  credentials, from recorded samples alone.
- A correction to how a platform's data is interpreted can be applied to the entire
  existing archive without re-contacting that platform.
- A backup taken from a populated instance restores into a clean instance with a complete,
  browsable archive and zero re-syncing.
- A fresh install with no platform enabled makes zero outbound network requests.
- No inferred deletion ever occurs for a platform that does not report deletions, verified
  against a platform that exposes only recent activity.
- Under normal operation, a provider change is visible on the now-playing WebSocket within
  16 seconds of the 15-second acquisition interval plus the 500-ms API polling interval, and an
  item older than 45 seconds is never served as current.

## Out of scope

- **Multiple users on one instance.** No user concept exists in the data.
- **Writing anything back to a platform.** The relationship is read-only upstream, permanently.
- **Replacing a logging platform.** Aggregato aggregates a history; the operator keeps logging
  wherever they log today.
- **Managing, scanning, or streaming media files.** Aggregato indexes logged activity, not files.
- **Multi-user or multi-tenant hosting.** Public read-only access is supported; public write access
  is not.
- **Enriching records from third-party metadata databases.** Aggregato holds what its platforms gave
  it and nothing else.
- **Creating or editing log entries locally**, beyond identity curation.

## Assumptions

These decisions define the product's scope and operating model.

- **Single user, no multi-tenancy.** There is no user concept in the data at all. Sharing an instance
  is not supported; the answer is a second instance. Adding multi-user later is a breaking data
  change, accepted knowingly.
- **Read-only toward platforms, now and later.** Aggregato never writes back to a platform.
- **No metadata enrichment, ever, as a product position.** The accepted cost is measurably weaker
  automatic unification for platform pairs with no shared identifier — more queue volume, surviving
  duplicates where titles differ, and some genuinely unmatchable pairs. This is why manual curation
  quality (Product Journey 4) is load-bearing rather than optional.
- **Items are modelled at the level a platform assigns identity and a rating to**, which differs by
  medium: whole for films and books; series and season for episodic video; track and album for music.
  Finer-grained activity is preserved as a reference within an entry, not as its own item.
- **Creator name matching is trusted only within a media domain**, while platform-supplied identifiers
  are trusted everywhere. The accepted cost is duplicate creators for people who work across domains,
  which the queue proactively suggests merging.
- **Scraping is a last resort.** Contributors follow the acquisition and scraping policy in
  [CONTRIBUTING.md](../CONTRIBUTING.md); bypassing access controls or anti-bot measures is prohibited.
- **Bundled integrations ship with the core.** Operator-supplied drop-ins are marked unreviewed and
  run with the service's filesystem and network permissions.
- The operator is responsible for complying with each platform's terms.
- **Embedded storage by default, server database optional** via the same configuration, with the data
  model staying compatible with both.
- **The web interface is a built single-page application.** Docker images include its built assets;
  building the frontend from source requires Node.
- Provider updates use polling. Playback progress and history, an HTTP fallback, and a dedicated
  playback panel are out of scope.
