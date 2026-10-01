# Pending work

Release checks and development follow-ups.

## Release checks

- Complete manual browser zoom, gradient contrast, and screen-reader testing using the
  [accessibility checklist](accessibility.md).
- Run release-commit CI and validate the deployment on each published image architecture.
  See [validation](validation.md) for commands.
- Recheck bundled providers against their live upstream surfaces and access policies before
  announcing compatibility. Recorded fixtures establish the code contract, not current upstream
  availability.
- Measure the remaining [performance targets](architecture.md#performance-targets) on representative
  archives and deployment hardware. Include varied work, creator, and provider-item cardinality,
  complete filtered API requests, sustained ingestion, and peak process memory. Investigate writer
  scaling; short synthetic bursts have not established the target.
- Measure memory and write-lock duration during large history syncs and imports. Normal fetch
  output is accumulated before ingestion; if this becomes a bottleneck, ingest bounded batches
  while preserving flushed-checkpoint and partial-run semantics.

## Provider development

Candidate providers to evaluate include MyAnimeList, Kitsu, Last.fm, AOTY, RateYourMusic, Hardcover,
and The StoryGraph. Confirm a supported acquisition surface and record fixtures before implementing
one; these names do not imply current API availability or planned support.

Keep the [provider guide](writing-a-provider.md), [plugin contract](contracts/provider-plugin.md),
and [acquisition roadmap](provider-acquisition-roadmap.md) as the implementation references.
Existing acquisition follow-ups include broader Goodreads history, a richer Letterboxd surface,
and finer-grained Koito paging if their upstream interfaces permit them. Preserve provider IDs and
retained-payload replay during any changeover.

Scraping, push, and delete-reporting capabilities are reserved contract surfaces without bundled
implementations. Retain their policy and conformance requirements; expand the implementation only
for a concrete provider.

## Conditional architecture work

- Multi-host failover needs explicit ownership and recovery design. Current SQLite file locks and
  PostgreSQL session locks enforce one worker; they are not durable distributed leases, and losing
  a PostgreSQL session releases its lock.
- Reconsider TaskIQ or a broker when distributed execution or an existing broker creates a concrete
  need. Keep archive validation, checkpointing, replay, and identity rules independent of delivery.
- Consider persistent provider children only if measured process churn makes current-playback
  polling too costly. Preserve provider isolation and keep playback out of historical ingestion.
