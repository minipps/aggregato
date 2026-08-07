# Ingestion, identity, and search integrity findings

The ingestion layer has strong intentions—parent-side validation, tombstones, replay, and
idempotency—but several operations are not atomic or do not preserve enough identity to replay the
same event safely.

## D-01 — Failed full-run observations poison the sanity baseline (Critical data loss)

The full-run guard reads the previous window count and updates last_window_item_count before it has
established that the current run passed the sanity check. See
[dispatch.py:491](../../aggregato/sync/dispatch.py:491) and
[dispatch.py:514](../../aggregato/sync/dispatch.py:514).

Failure sequence:

1. A healthy full run observes 100 items.
2. A truncated run observes 1 item; sanity rejects it, but the baseline becomes 1.
3. A second truncated run observes 1 item; it now matches the poisoned baseline and can pass.
4. Inferred-delete logic can tombstone the 99 records missing from the original healthy window.

Fix:

- Update the baseline only after every sanity guard passes.
- Store failed observations separately for diagnosis; never use them as future baselines.
- Require a fresh, independently sane full run before inferred deletion.
- Add the exact 100 → 1 rejected → 1 rejected/no-tombstones regression test, plus a process-crash
  test between sanity evaluation and baseline commit.

## D-02 — Per-record writes are not protected by savepoints (High)

The writer catches validation failures around a record, but _write_one performs multiple related
inserts/updates before late validation and rating checks. See
[writer.py:122](../../aggregato/ingest/writer.py:122), [writer.py:163](../../aggregato/ingest/writer.py:163),
and [writer.py:536](../../aggregato/ingest/writer.py:536).

Impact:

- A record can create a provider item, work, external IDs, creators, credits, and entries, then fail
  on a rating, index operation, or later derived value.
- The failure row is captured, but the partial archive entities remain, contradicting the intended
  “bad record becomes a failure, not a write” behavior.
- Retrying the payload can encounter those partial entities and produce different results.

Fix:

- Wrap each record in a nested transaction/savepoint.
- Roll back the savepoint before inserting the failure row outside it.
- Prevalidate all derived values where possible, and classify unexpected DB errors without
  invalidating the outer batch transaction.
- Add a relational-count snapshot test that proves a rejected record leaves no provider item/work/
  credit/entry side effects.

## D-03 — Captured failures lose the provider's native identity (High)

The protocol carries a native ID, but the persisted failure model does not retain it. Replay rebuilds
an ID from payload["id"] or the failure row ID; see
[failures.py:156](../../aggregato/ingest/failures.py:156).

Impact:

- Providers whose native ID is nested, differently named, or absent from the normalized raw payload
  replay under a synthetic/wrong provider item ID.
- A replay can create a duplicate item or attach a corrected record to the wrong identity.

Fix:

- Persist the provider ID and native ID as an immutable failure envelope alongside the raw payload.
- Replay that envelope verbatim; never infer identity from arbitrary payload keys.
- Add fixtures where native ID is nested, renamed, empty-looking, and absent from the payload.

## D-04 — Replay skips stale rows when schema versions are mixed (High)

normalize_replay uses the maximum stored schema version to decide whether replay is needed. See
[normalize_replay.py:15](../../aggregato/ingest/normalize_replay.py:15).

Impact:

- If some retained provider items are at the current schema and others are stale, a maximum-version
  check can conclude that the provider is current and leave the stale subset unmigrated.
- The next fetch/replay result depends on which item happened to be newest.

Fix:

- Query for an EXISTS row with schema_version < current and replay only those rows.
- Track per-payload/version completion, not one provider-wide maximum.
- Make replay derivative cleanup update search indexes as well as relational rows.

## D-05 — No-native-ID event dedupe uses a pre-select, not a unique key (High)

The no-native-ID fallback selects an existing matching entry before inserting it. See
[writer.py:446](../../aggregato/ingest/writer.py:446) and
[writer.py:497](../../aggregato/ingest/writer.py:497).

Impact:

- Concurrent workers or retries can both observe no row and insert duplicate history.
- The implementation contradicts the repository's stated invariant that idempotency must be a
  physical unique constraint with ON CONFLICT.

Fix:

- Add a canonical normalized subject-ref key and a unique constraint over provider item, kind,
  logged-at, and that key; use an atomic upsert.
- Normalize timestamps and subject refs once before key generation.
- Add concurrent duplicate-write tests against SQLite and PostgreSQL.

## D-06 — External-ID normalization and resolution are too permissive (High/Medium)

The data-model documentation requires a registered namespace, trimmed non-empty values, and checksum
validation for ISBN namespaces, but the writer largely strips the value and upserts it. See
[data-model.md:78](../../docs/data-model.md:78) and
[writer.py:386](../../aggregato/ingest/writer.py:386).

Related identity problems:

- resolve_work returns the first matching work when a batch asserts identifiers that resolve to
  different works; see [resolve_work.py:35](../../aggregato/ingest/resolve_work.py:35).
- The resolver compares the raw value while the writer stores a stripped value, so whitespace can
  miss a match and create a duplicate.
- The unique key includes work_id, allowing one asserted namespace/value pair to be attached to
  multiple works even though the lookup treats it as identity.

Fix:

- Centralize namespace/value canonicalization and validation before resolution or writing.
- Reject unknown/empty/checksum-invalid IDs into an explicit identity-conflict failure.
- If multiple identifiers resolve to different works, reject the batch item rather than selecting
  the first result.
- Decide whether an external ID is globally unique. If yes, enforce it in the database; if not,
  model source/confidence conflicts explicitly and do not silently treat it as a resolver key.

## D-07 — Merge duplicate detection compares columns independently (High)

The merge query uses “namespace is in winner namespaces” and “value is in winner values” as separate
predicates. See [merge.py:61](../../aggregato/ingest/merge.py:61). For winner pairs (A,1) and
(B,2), loser pair (A,2) can be treated as a duplicate even though neither full pair matches. The
same shape exists in creator merge paths.

Fix:

- Use a correlated EXISTS on both namespace and value, or dialect-safe tuple equality.
- Add cross-product adversarial tests for all pair combinations before and after merge.

## D-08 — Merge and undo can corrupt identity graphs or erase newer changes (High)

Merge updates parent pointers and moves related rows but does not visibly guard against a winner that
is the loser's descendant/ancestor. It can create a self-parent or cycle; see
[merge.py:45](../../aggregato/ingest/merge.py:45).

The inverse/restore path deletes current rows and reinserts a snapshot without an optimistic version
or operation-generation check; see [identity.py:128](../../aggregato/ingest/identity.py:128).

Impact:

- A merge request can create an invalid parent graph.
- Undo performed after later ingestion or manual changes can erase those newer changes.
- Loser-only titles, images, aliases, or other fields can be discarded rather than merged by an
  explicit policy.

Fix:

- Before merge, walk/validate the ancestor graph and reject self/cycle operations.
- Add optimistic versions or affected-row hashes to merge operations; refuse undo when the current
  graph differs from the snapshot.
- Define field-by-field winner/loser merge rules and preserve aliases/history.
- Prefer an event/inverse model for durable identity operations when feasible.

## D-09 — Search indexes can silently become stale or incomplete (High/Medium)

Several write paths change archive records without rebuilding all dependent search documents:

- Merging unindexes the loser but does not clearly rebuild the winner's aggregate title/aliases;
  see [merge.py:76](../../aggregato/ingest/merge.py:76).
- Replay derivative tombstoning and identity restore do not clearly unindex/reindex affected rows.
- A later provider title update can replace a work document rather than rebuilding all title forms
  contributed by every provider.

Additionally, matching_ref_ids has a hard 500-ID cap. A common term can silently omit valid rows;
see [search.py:180](../../aggregato/db/search.py:180).

Fix:

- Make search index maintenance an explicit transactionally-consistent projection rebuild for every
  affected work/review.
- Add a rebuild command and a consistency audit that compares source rows with indexed documents.
- Remove the hidden 500 cap, or expose it as a deliberate paginated/search-result limit with a
  truncation indicator.
- Test merge, undo, replay, tombstone, and multi-provider title updates through the public search API.

## D-10 — Rating-scale IDs are globally scoped without a collision policy (Medium/High)

Rating scales are stored by ID while providers declare their own scales. If two providers choose the
same generic ID with different definitions, an upsert can overwrite the scale used to interpret older
ratings.

Fix:

- Namespace scale IDs by provider or make the database key (provider_id, scale_id).
- Store an immutable definition/version with each rating.
- Reject definition changes for an existing ID unless an explicit migration is performed.

## D-11 — Aggregate/detail limits and statistics semantics are easy to misread (Medium)

Work detail embeds only a fixed number of entries/opinions, and search results cap matching IDs. The
stats route accepts a period parameter but the top-results query does not appear to constrain the
time window; see [stats.py:140](../../aggregato/api/routes/stats.py:140).

Fix:

- Expose pagination for embedded history or state the truncation in the API contract.
- Define whether period means a date window, bucket granularity, or both; implement and test the
  same semantics in backend, OpenAPI, and frontend.
