# Product Requirements: Aggregato — Self-Hosted Media Log Aggregator

**Feature Branch**: `001-media-log-aggregator`

**Created**: 2026-07-29

**Status**: Draft

**Input**: User description: Aggregato Specification v1.0 — a self-hosted service that pulls a
user's media logs from multiple third-party logging platforms, normalizes them into a single
local database, and exposes them through one unified HTTP API and web UI, with per-platform
support provided by opt-in provider plugins.

**Source design documents**: the v1.0 design's technical decisions (architecture, data model,
plugin contract, API surface, and acquisition policy) are maintained in
[architecture.md](architecture.md), [data-model.md](data-model.md), and the
[contract documents](contracts/). This file states the capability, the user-visible behaviour, and
the constraints any implementation must honour.

## User Scenarios & Testing *(mandatory)*

### Product Journey 1: One platform's history, local and browsable (Priority: P1)

A self-hoster deploys Aggregato with a single command, supplies their username or credentials for
one logging platform, and enables it. The service fetches their history on a schedule and they
browse it — dated entries, ratings, reviews — in a local web UI, with no third-party service
involved beyond the platform they already use.

**Why this priority**: This is the smallest thing that delivers the core promise: a media history
that is local, queryable, and durable. Everything else compounds on it.

**Independent Test**: Deploy from a clean state, enable exactly one platform, wait for a sync, and
confirm the browsed entries match what the platform shows. Delivers a working personal archive with
one platform configured.

**Acceptance Scenarios**:

1. **Given** a clean install with no platform enabled, **When** the operator starts the service,
   **Then** it starts successfully, contacts no external host, and reports zero configured
   platforms.
2. **Given** one platform enabled with valid credentials, **When** the first sync completes,
   **Then** every entry the platform exposes for that account is present locally with its logged
   date, its date precision, and its rating in both the platform's original scale and a normalized
   form.
3. **Given** a completed sync, **When** the same sync runs again over unchanged data, **Then** no
   duplicate entries are created and no existing entry is modified.
4. **Given** an entry the platform recorded with only a month or a year, **When** the operator views
   it, **Then** the display reflects that imprecision rather than showing an invented time.

---

### Product Journey 2: Two platforms, one log (Priority: P1)

The operator enables a second platform. A film they logged on both platforms appears as one item
with both platforms' ratings and reviews side by side, not as two unrelated rows.

**Why this priority**: Unification is the reason the product exists rather than an export folder. It
is also where the hard problem lives, so it must be exercised early.

**Independent Test**: Enable two platforms whose accounts share overlapping items, sync both, and
confirm shared items unify while genuinely distinct items stay distinct. Delivers a cross-platform
view even with only two platforms configured.

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

### Product Journey 3: Sync the operator can trust (Priority: P2)

A platform goes down, changes, rate-limits, or rejects the operator's credentials. Aggregato
recovers on its own where recovery is possible, says exactly what is wrong where it is not, keeps
every other platform working, and never destroys history in the process.

**Why this priority**: An archive that silently stops updating, or silently deletes, is worse than no
archive. This is what makes the tool leavable-alone.

**Independent Test**: Force each failure class against a test platform and confirm the recorded
outcome, retry behaviour, and operator-facing message for each. Delivers dependable unattended
operation independent of which platforms are configured.

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

### Product Journey 4: Fixing identity by hand (Priority: P2)

The operator works through a queue of items and creators Aggregato refused to guess about, merges
duplicates it created, and splits apart creators it wrongly joined — for example two different
people who share a name.

**Why this priority**: Because Aggregato deliberately looks up nothing externally (see Assumptions),
manual curation is the compensating mechanism, not an admin afterthought. It must be fast enough
that a real queue is clearable.

**Independent Test**: Seed known-ambiguous and known-conflated data, then resolve it entirely through
the UI. Delivers a correctable archive regardless of automatic match quality.

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

### Product Journey 5: Platforms with no API (Priority: P3)

For a platform offering no usable programmatic surface, the operator downloads their own export file
from that platform and uploads it. Those entries land in the same archive, filterable alongside
everything else.

**Why this priority**: Several significant platforms have no other path. It also proves the
platform-support contract is general rather than shaped around one convenient API.

**Independent Test**: Upload a real export file from a supported platform and confirm its entries,
ratings, and identifiers appear correctly in the unified log.

**Acceptance Scenarios**:

1. **Given** an export file from a supported platform, **When** the operator uploads it, **Then** its
   entries are ingested, unified against existing items, and shown in the log.
2. **Given** the same file uploaded twice, **When** the second upload completes, **Then** no
   duplicate entries exist.
3. **Given** an unrelated or corrupt file, **When** it is uploaded, **Then** the operator gets a clear
   rejection and the existing archive is unchanged.

---

### Product Journey 6: Adding support for a new platform (Priority: P3)

A contributor adds support for a platform Aggregato does not yet cover, by implementing a narrow
contract — fetch, convert, verify credentials — without needing to understand or modify the core.
Scheduling, retries, rate limiting, storage, and migrations are already handled.

**Why this priority**: Platform coverage is the product's long-term growth path, and it must not
require core changes. Not needed for the first release to be useful.

**Independent Test**: Write a new platform integration against the documented contract, using only
recorded sample data and no live credentials, and confirm it syncs and appears in the UI with no core
modification.

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

### Product Journey 7: Owning the archive (Priority: P3)

The operator takes a portable backup of everything, sees what disk is being consumed and by what,
and turns off the space-hungry parts if they choose.

**Why this priority**: A self-hosted archive that cannot be backed up or budgeted is not an archive.
Lower priority only because it is worthless before there is data to back up.

**Independent Test**: Produce a backup, restore it into a fresh instance, and confirm the archive is
intact and browsable.

**Acceptance Scenarios**:

1. **Given** a populated archive, **When** the operator requests a backup, **Then** a single portable
   artefact is produced containing the data and the configuration, with no secrets in it.
2. **Given** that artefact, **When** it is restored into a clean instance, **Then** the archive is
   complete and browsable without re-syncing any platform.
3. **Given** the storage view, **When** the operator opens it, **Then** current usage is broken out by
   the optional retention features, each of which can be turned off.

---

### Edge Cases

- A platform returns an empty result: distinguishing "nothing was logged this week" from "this
  integration is broken" is mandatory, because inferring deletion from the second would erase
  history. Deletion is only ever inferred for platforms that explicitly report deletions, and never
  by default.
- A run returns far fewer items than the previous run for the same window: flagged and surfaced
  against a configurable threshold, not silently accepted.
- A platform records activity below the level Aggregato treats as an item (an individual episode of a
  series): the activity is preserved, attached to the item it belongs to, and **excluded from that
  item's aggregate statistics by default**, with an explicit opt-in to include it. Reversing this
  default would silently corrupt every statistic shown.
- Two different people share a name: they will be joined automatically, and splitting them apart at
  the level of individual credits is a required operation, not a future nicety.
- The same person is credited under a native script and a romanization: they will not join
  automatically, and the queue must suggest the pair.
- Ratings from platforms with different scales and different community distributions: raw value and
  scale are always retained alongside the normalized value, and the UI must not present normalized
  scores as equivalent across platforms.
- An image host is slow or unreachable: image handling never fails or delays a sync.
- Configuration for one platform is invalid: that platform is disabled with a clear error and the
  service still starts. The only configuration error that prevents startup is a missing access
  token, because there is no unauthenticated mode.
- A very large history (hundreds of thousands of entries) browsed to its far end: paging must not
  degrade with depth.
- The archive outgrows the default embedded database: the operator can move to a server database
  without a change to the data model or a loss of features.

## Requirements *(mandatory)*

### Functional Requirements

**Aggregation and storage**

- ****: System MUST store log entries, ratings, reviews, and creator credits from any number of
  configured platforms in one local database.
- ****: System MUST retain, for every ingested record, the platform's original payload
  verbatim, and MUST be able to rebuild all derived data from those payloads without contacting the
  platform again. Retention is on by default; disabling it MUST warn the operator.
- ****: System MUST retain each rating's original value and scale alongside its normalized
  value, and MUST NOT present normalized values as cross-platform equivalents.
- ****: System MUST record the precision of every logged date and MUST NOT present a fabricated
  time as exact.
- ****: System MUST treat re-ingesting already-seen data as a no-op.
- ****: System MUST support a single user only, and MUST NOT carry any concept of user accounts
  in its data.
- ****: System MUST preserve activity recorded below item level, attached to its parent item,
  and MUST exclude such activity from that item's aggregate statistics unless explicitly requested.
- ****: System MUST NOT allow a platform integration to introduce new media categories or write
  arbitrary data; all ingest MUST be validated against the system's own defined shapes.

**Identity**

- ****: System MUST unify items across platforms using identifiers those platforms publish in
  their own responses, and MUST capture every such identifier a payload offers.
- ****: System MUST NOT make outbound requests to any third-party metadata database. The only
  outbound request to a non-platform host permitted is fetching an image from a URL a platform itself
  supplied.
- ****: System MUST unify items lacking a shared identifier only on an unambiguous title,
  category, and year match; ambiguous cases MUST be queued for the operator, never guessed.
- ****: System MUST prefer creating a duplicate item over performing an uncertain merge.
- ****: System MUST provide operator-driven merge for items and both merge and **credit-level
  split** for creators, and MUST record which links were made by name matching versus by a
  platform-supplied identifier so a split is informed.
- ****: System MUST make every operator identity decision durable against all subsequent syncs.
- ****: System MUST NOT automatically join creators by name across unrelated media domains, and
  MUST instead surface such pairs as suggestions.
- ****: System MUST record every credit's role, the platform's own term for that role verbatim,
  the name as that item credited them, and their billing order.
- ****: Every merge, split, and queue decision MUST be reversible.

**Sync and failure handling**

- ****: System MUST sync each platform on its own configurable schedule, defaulting to a value
  the integration declares rather than one global value, and MUST support manual triggering.
- ****: System MUST record every sync attempt with its outcome, item counts, and failure
  detail, and MUST group retries of the same failure together for display.
- ****: System MUST retry recoverable failures automatically at increasing delays without
  operator action, and MUST resume a partially-completed sync from its last fully-processed point.
- ****: System MUST NOT retry failures that retrying cannot fix — invalid credentials, an access
  block, or a platform whose structure has changed — and MUST tell the operator which occurred and
  what it requires.
- ****: System MUST honour platform-supplied rate-limit signals and MUST slow its own cadence in
  response rather than resuming the previous rate immediately.
- ****: System MUST set aside individual records that fail conversion, with their original
  payload, continue the run, report the count, and allow them to be re-processed after a fix.
- ****: System MUST NOT infer deletions for platforms that do not report deletions; inferred
  deletion MUST be off by default.
- ****: A failing, hanging, or crashing platform integration MUST NOT affect any other
  platform, the browsing experience, or service startup.
- ****: System MUST flag, rather than silently accept, a sync returning fewer items than a
  configurable proportion of what the previous run returned for the same window.

**Reading the archive**

- ****: System MUST expose one documented read interface covering items, entries, opinions,
  creators, platforms, sync history, failures, and the resolution queue.
- ****: System MUST support filtering the log by media category, media domain, platform, entry
  kind, creator, role, date range, score range, presence of a review, and free text over titles and
  review text, with sorting by logged date, ingest date, or score.
- ****: System MUST support filtering by media domain as a first-class alternative to
  enumerating individual categories, while keeping every individual category separately addressable.
- ****: System MUST page results in a way that does not degrade as the caller reads deeper into
  a large history.
- ****: The web UI MUST consume only the same public read interface available to any other
  client; if a screen needs data the interface cannot express, the interface is extended.
- ****: System MUST require authentication for all access, with no unauthenticated mode, and
  MUST NOT place the operator's token in browser URLs or page source.
- ****: System MUST serve platform-supplied images through a local cache rather than linking to
  the third-party host, MUST fetch them lazily so image availability never affects a sync, and MUST
  show a placeholder rather than falling back to third-party linking when caching is off.

**Platform integrations**

- ****: Support for each platform MUST be an independent integration implementing a narrow
  contract, addable without modifying the core.
- ****: The system MUST own scheduling, retrying, rate limiting, request pacing, storage, and
  migrations; an integration MUST NOT need to reimplement them.
- ****: An integration's data-conversion step MUST be free of network, clock, and storage access
  so it is testable offline from recorded samples with no credentials.
- ****: An integration MUST receive only its own credentials and configuration, and MUST NOT
  receive direct storage access or any other integration's secrets.
- ****: Integrations MUST be discovered automatically but MUST remain inert until explicitly
  enabled; a fresh install MUST contact nothing.
- ****: An integration MUST declare its configuration such that a working settings form is
  produced with no additional interface work.
- ****: Each integration MUST declare how it obtains data, and that provenance MUST be visible
  to the operator.
- ****: Integrations MUST ship with the core release and MUST NOT be installable from within the
  application. Any move toward in-application installation or third-party distribution MUST be
  preceded by a re-evaluation of the isolation model, because integrations run with the service's own
  privileges.
- ****: Integrations MUST use the highest-fidelity data surface a platform offers, resorting to
  scraping only when every better surface is insufficient, and MUST document that evaluation.
- ****: For scraping integrations the system MUST enforce, independent of integration behaviour:
  conservative pacing with a floor the integration cannot raise, one request at a time per host, an
  identifying user agent naming the project and a contact URL, and backoff on refusal responses.
- ****: System MUST NOT circumvent access controls, paywalls, or anti-bot measures, MUST NOT
  ship or integrate any solver for them, and MUST stop and report when one is encountered.
- ****: A scraping integration MUST retrieve only data the authenticating operator can see in
  their own browser on their own account, and MUST NOT bundle or share credentials.
- ****: A scraping integration MUST ship recorded samples and offline tests as a condition of
  acceptance.
- ****: When a platform later offers a better data surface, the integration MUST be able to
  migrate to it without duplicating already-ingested entries where identifiers permit.

**Operation**

- ****: System MUST deploy as a single unit with one storage location and one command, with no
  additional services and no third-party API keys beyond the operator's own platform credentials.
- ****: System MUST apply schema migrations automatically on startup, forward-only, taking a
  backup of an embedded database first.
- ****: System MUST produce a single portable backup artefact containing the archive and the
  configuration, excluding secrets, restorable into a clean instance without re-syncing.
- ****: System MUST report per-platform health including last successful sync, and MUST surface
  degraded platforms prominently.
- ****: System MUST keep secrets out of on-disk configuration it writes, referencing them from
  the environment instead.
- ****: System MUST show current storage usage broken out by each optional retention feature,
  each of which MUST be independently switchable.
- ****: Sync-history retention MUST be configurable, keeping failures longer than successes by
  default.

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

## Success Criteria *(mandatory)*

### Measurable Outcomes

- ****: From a clean machine, an operator reaches a first browsable synced history with one
  platform configured in under 15 minutes, having supplied no credentials except their own for that
  platform.
- ****: For items logged on two platforms where at least one platform publishes a shared
  identifier, at least 95% unify into a single item with no operator involvement.
- ****: No sequence of failed and retried syncs ever produces a lost or duplicated entry;
  verified by repeatedly interrupting syncs over a fixed data set and comparing the result to an
  uninterrupted run.
- ****: A single platform failing — for any of the classified failure reasons — leaves every
  other platform's sync unaffected 100% of the time, and never prevents the service from starting.
- ****: For every failure the operator sees, the interface names what is wrong and what action
  it needs; no failure surfaces only as a generic error.
- ****: An operator clears a resolution decision in under 10 seconds per decision, working from
  the keyboard, with an undo available.
- ****: Browsing and filtering an archive of 1,000,000 entries returns the first page of results
  in under 1 second at the 95th percentile, and paging to the far end of that history is no slower
  than paging near the start.
- ****: Sustained ingest of a high-volume platform processes at least 50,000 entries per hour on
  modest single-board-class hardware, and ingest throughput does not degrade measurably as the archive
  grows to 1,000,000 entries.
- ****: A contributor with no prior knowledge of the codebase adds support for a new platform by
  implementing the documented contract only, with zero changes to core code and zero interface work
  for its settings screen.
- ****: An integration's data conversion is fully testable with no network access and no
  credentials, from recorded samples alone.
- ****: A correction to how a platform's data is interpreted can be applied to the entire
  existing archive without re-contacting that platform.
- ****: A backup taken from a populated instance restores into a clean instance with a complete,
  browsable archive and zero re-syncing.
- ****: A fresh install with no platform enabled makes zero outbound network requests.
- ****: No inferred deletion ever occurs for a platform that does not report deletions, verified
  against a platform that exposes only recent activity.

## Out of Scope

Stated so the boundary is not rediscovered mid-build:

- **Multiple users on one instance.** No user concept exists in the data.
- **Writing anything back to a platform.** The relationship is read-only upstream, permanently.
- **Replacing a logging platform.** Aggregato aggregates a history; the operator keeps logging
  wherever they log today.
- **Managing, scanning, or streaming media files.** Aggregato indexes logged activity, not files.
- **Public or multi-tenant hosting.** The security model assumes one trusted operator.
- **Enriching records from third-party metadata databases.** Aggregato holds what its platforms gave
  it and nothing else.
- **Local editing and manual entry creation in this version**, beyond the identity curation in User
  Story 4.

## Assumptions

Decisions taken from the supplied v1.0 design, recorded here because they bound the scope above and
are not derivable from the requirements alone.

- **Single user, no multi-tenancy.** There is no user concept in the data at all. Sharing an instance
  is not supported; the answer is a second instance. Adding multi-user later is a breaking data
  change, accepted knowingly.
- **Read-only toward platforms, now and later.** Aggregato never writes back to a platform.
- **No local editing or manual entry creation in this version.** Deferred rather than rejected; the
  data model reserves what is needed to add it additively, and the operator-facing merge and split
  operations serve as the first, limited curation path.
- **No metadata enrichment, ever, as a product position.** The accepted cost is measurably weaker
  automatic unification for platform pairs with no shared identifier — more queue volume, surviving
  duplicates where titles differ, and some genuinely unmatchable pairs. This is why manual curation
  quality (Product Journey 4) is load-bearing rather than optional.
- **Items are modelled at the level a platform assigns identity and a rating to**, which differs by
  medium: whole for films and books; series and season for episodic video; track and album for music;
  episode for podcasts. Finer-grained activity is preserved as a reference within an entry, not as its
  own item.
- **Creator name matching is trusted only within a media domain**, while platform-supplied identifiers
  are trusted everywhere. The accepted cost is duplicate creators for people who work across domains,
  which the queue proactively suggests merging.
- **Scraping integrations are accepted as project policy**, because some significant platforms have no
  other path. The obligations that come with that — the surface hierarchy, host-enforced politeness,
  disclosure to the operator, offline sample tests, and the hard line at access-control circumvention
  — are requirements above, and must be published as contributor policy before the repository is
  public.
- **Integrations ship with the core and are reviewed by the project.** The accepted cost is that the
  core's release cadence becomes coupled to other platforms' page structure, mitigated by cheap,
  frequent patch releases rather than by code.
- **Terms-of-service compliance is the operator's call**, stated plainly in the documentation. That is
  a policy position, not legal advice.
- **Embedded storage by default, server database optional** via the same configuration, with the data
  model staying compatible with both.
- **Web interface is a built single-page application** (amended 2026-07-29; the original assumption
  was server-rendered specifically to avoid a front-end build toolchain). Operators are unaffected —
  the assets are built during image creation, so `docker compose up` needs no Node — but **building
  from source now requires Node**. Recorded in [architecture.md](architecture.md)'s complexity
  tracking. Either way the UI is designed for typography and density rather than assuming
  third-party artwork exists.
- **Two features have reserved surface area and need no further design now**: platform-pushed updates
  instead of polling, and the local write path.
