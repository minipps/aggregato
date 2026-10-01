# Local publication performance measurements

Recorded 2026-10-01 with [`scripts/publication_benchmark.py`](../../scripts/publication_benchmark.py),
the production SQLite schema, and synthetic data. These are local measurements, not ARM results.
The declared targets remain in [architecture.md](../architecture.md#performance-budgets).

## Environment and workload

The host was Linux 7.2.8 on x86_64, Python 3.13.14, 16 logical CPUs, and 31,366 MiB RAM. The
installed versions were SQLAlchemy 2.0.51, aiosqlite 0.22.1, FastAPI 0.141.1, httpx 0.28.1, and
Pydantic 2.13.5. Measurements used temporary SQLite databases under `/tmp`; no network or personal
archive data was used.

The existing `tests.bench.seed.seed_entries` helper created each archive. Its distribution is one
work, one original provider item, and live work-level watch entries with unique timestamps spaced one
second apart. The API request filtered `provider=benchmark` and `media_type=film`, used the default
active/work-level filters, and returned 50 entries. The deep cursor sat after 99.9948% of the
descending keyset. Every entry belongs to the same work, so the full route also recomputes that
work's one-million-entry aggregate on each request.

The API numbers are 101 timed requests per page after five warmups, alternating first and deep page
order. p95 uses nearest rank. Timing covers the complete in-process ASGI request, including query,
response construction, and JSON serialization; it does not include a TCP listener. The first run was
before the keyset range fix; the second used the same database after the fix.

| Code state | First-page p95 | Deep-page p95 | Deep / first | Result against local target |
|---|---:|---:|---:|---|
| Before keyset fix | 98.271 ms | 156.779 ms | 1.5954 | First-page latency passes; deep-page relation fails the 1.20 limit |
| After keyset fix | 184.935 ms | 186.277 ms | 1.0073 | Both API targets pass locally |

Absolute latency varied between the two runs; the deep/first relationship is the useful before/after
comparison. The keyset fix removed the unnecessary nullable-sort branch from the range predicate,
which had kept SQLite from applying the descending timestamp index for the deep cursor.

## Writer throughput and memory

`write_batches` wrote one unique provider item and one event entry per synthetic record, all matched
to the seeded work. Each trial began from a fresh SQLite backup of the 10k-entry or 1M-entry seed,
created a valid synthetic provider and sync-run row, and asserted that all 1,000 records were
accepted. The 1,000-record batch ran three times per archive size. The bounded 10k-record trial set
was stopped after one full run and most of a second; those incomplete results are excluded.

Peak memory is the writer subprocess's `resource.getrusage(RUSAGE_SELF).ru_maxrss`, converted from
Linux KiB to MiB. It includes interpreter imports, the materialized 1,000-record input, and the
writer's SQLite connection; it excludes filesystem page cache and other processes. It is a process
high-water mark, not an incremental allocation measurement.

| Baseline entries | Trial | Writer time | Entries/hour | Peak process RSS |
|---:|---:|---:|---:|---:|
| 10,000 | 1 | 4.022 s | 895,186 | 95.2 MiB |
| 10,000 | 2 | 4.256 s | 845,858 | 95.3 MiB |
| 10,000 | 3 | 3.361 s | 1,071,118 | 99.3 MiB |
| 1,000,000 | 1 | 4.023 s | 894,955 | 96.1 MiB |
| 1,000,000 | 2 | 4.743 s | 759,046 | 95.6 MiB |
| 1,000,000 | 3 | 4.898 s | 734,944 | 99.7 MiB |

The median rate was 895,186 entries/hour at 10k rows and 759,046 entries/hour at 1M rows, a 0.8479
ratio (15.2% lower at 1M). That point estimate is outside the ±10% target, but each group has only
three short trials and the ranges overlap. Treat the scaling target as unproven, with a possible
degradation signal. The local rates do not establish the ≥50,000 entries/hour budget on 4-core ARM
with 2 GiB RAM. The concentrated one-work seed also does not represent varied work, creator, or
provider-item cardinality.

## Reproduction

Use a new empty work directory; the script refuses to overwrite its database files. The full run
seeds the 1M API archive and 10k writer archive, takes 101 API samples per page, then performs the
three 1,000-record writer trials at both baseline sizes:

```bash
.venv/bin/python scripts/publication_benchmark.py \
  --workdir /tmp/aggregato-publication-benchmark-run-01 \
  --api-samples 101 --writer-entries 1000 --writer-trials 3
```

The sandboxed aiosqlite run stalled before completing its first ASGI request; the recorded ASGI and
writer measurements were run with the host interpreter against local `/tmp` databases. The API
post-fix measurement includes the shared keyset range-predicate fix; the benchmark harness itself
does not change application behavior.
