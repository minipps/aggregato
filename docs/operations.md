# Operations

## Portable archive and restore

**Settings → Download archive** requires operator authentication and is available for an on-disk
SQLite database. It creates a ZIP with a consistent database snapshot, cached images, and public
configuration. The database copy omits configured secrets, sessions, import and replay jobs,
ingest-failure payloads, run logs, raw response diagnostics, and cursor state. The archive still
contains the personal history and retained provider-item payloads; keep it private.

To restore, run `uv sync --all-extras --dev` in a checkout with the project code, and use an empty
data directory:

```bash
uv run python -c 'from pathlib import Path; from aggregato.export import restore_archive; print(restore_archive(Path("backup.zip"), Path("./restored-data")))'
```

The helper validates the ZIP, stages its files, checks the SQLite database, and then publishes them
to the empty directory. It returns public configuration for review; it does not apply that
configuration. The restored database retains safe provider settings but disables every provider and
omits configured credentials and sessions. Retained platform payloads still contain private user
data. Set `AGGREGATO_DATA=./restored-data` and a fresh `AGGREGATO_TOKEN` when
starting the API and worker, then check providers and enter their credentials again. For Compose,
mount the restored directory at `/data` instead of using an empty volume.

The Settings archive is SQLite-only. For PostgreSQL, use PostgreSQL's backup and restore tools, and
preserve the configured image-cache directory separately. The portable ZIP omits operational
evidence; if failure payloads or diagnostics are needed, keep a separate private full database
backup. A native database backup preserves that evidence but also contains provider credentials,
sessions, and personal history, so keep it private.
For an on-disk SQLite database, Python's standard-library backup API includes committed WAL data:

```bash
uv run python -c 'import sqlite3; src=sqlite3.connect("data/aggregato.db"); dst=sqlite3.connect("private-database-copy.db"); src.backup(dst); dst.close(); src.close()'
```

Replace `data/aggregato.db` with the configured database path. Keep that copy private and preserve
the image-cache directory if you need a complete local instance backup.

## A provider reports `structure_changed`

`structure_changed` means the provider's page or export format changed. Aggregato does not retry
it: repeated automated requests would only hit the same incompatible format. The standard operator
response is:

1. Check the provider's action-required message and preserve the retained failure payload.
2. Pull the current release: `docker compose -f docker/compose.yml pull`.
3. Restart it: `docker compose -f docker/compose.yml up -d`.
4. Trigger one provider sync from the Providers screen. If it still reports `structure_changed`,
   file an issue with the provider id and the sanitized failure context; do not keep retrying it.

## A migration went wrong

The worker holds its single-owner database lock before it applies migrations and recovers interrupted
runs. SQLite migrations use a separate lock as well, so they serialize with migrations from API
startup. `aggregato.db.migrate` creates a timestamped copy beside the SQLite database only when a
migration is pending and the database already exists; names look like
`aggregato.db.YYYYMMDDTHHMMSSffffff.bak`. Backups use sqlite3's backup API rather than `cp`, so each
copy includes committed WAL data. No migration backup is made for PostgreSQL, an in-memory database,
a new SQLite database, or a SQLite database already at head. The copies are not pruned automatically;
manage their storage yourself. Migrations are forward-only (data-model.md §6).

Two failure modes look different and are both recoverable.

**Startup loops on `Can't locate revision identified by 'NNNN'`.** The database is stamped at a
revision whose file is not in the running image — usually a revision that was applied and then
deleted or renumbered in the checkout. Nothing is wrong with the data. Restore the missing revision
file, matching that id, and restart. Do not "fix" it by stamping the database backwards. A failed
migration can leave another timestamped pre-migration backup on each restart while work is still
pending. A container left crash-looping can therefore fill `/data`; once it is healthy, review and
remove surplus copies yourself, keeping a backup from before the migration.

**A migration applied cleanly but rows are missing.** The likeliest cause on SQLite is a table
rebuild that cascaded into child tables (data-model.md §6). Roll back to the backup and replay the
fixed revision:

```bash
docker stop docker-aggregato-1                      # nothing may hold the database open
docker run --rm -v docker_aggregato-data:/data --entrypoint python aggregato:dev -c "
import sqlite3, glob, os, shutil
live = '/data/aggregato.db'
shutil.copy2(live, live + '.broken')                # keep the evidence
for sidecar in glob.glob(live + '-*'): os.remove(sidecar)   # a stale -wal over a restored db is corruption
with sqlite3.connect('/data/aggregato.db.TIMESTAMP.bak') as src, sqlite3.connect(live + '.tmp') as dst:
    src.backup(dst)
os.replace(live + '.tmp', live)
"
docker start docker-aggregato-1                     # startup migrates it forward again
```

Pick the newest backup that still has the rows, and confirm what you are about to restore before
swapping it in — `select version_num from alembic_version` plus a `count(*)` on `entries` and
`opinions` tells you the revision and whether the data is there. Afterwards, `pragma integrity_check`
and `pragma foreign_key_check` should both come back clean.

The volume name matters: the Compose stack's volume is **`docker_aggregato-data`** (Compose prefixes
the project directory). A one-off `docker run -v aggregato-data:/data` does not fail — Docker creates
a new empty volume of that name, and the command then reports an empty database with no revision
history, which reads exactly like data loss. Confirm with
`docker inspect docker-aggregato-1 -f '{{range .Mounts}}{{.Name}}{{end}}'`.

## Retention and storage growth

The defaults retain resolved ingest-failure payloads for 90 days, successful sync history for 30
days, and failed or partial runs for 180 days. Unresolved ingest failures are kept until they are
replayed or otherwise resolved. Provider-item payloads are the normalization-replay source and are
not currently removed by the cleanup worker, even when the similarly named retention setting is
lowered. Failures are intentionally retained because they are the evidence needed to repair a
provider or replay a poison record. The cleanup worker runs at startup and then daily.

Storage usage in Settings separates `database_bytes`, `raw_payload_bytes`, and image-cache bytes.
`raw_payload_bytes` estimates the UTF-8 byte length of the database-rendered JSON for retained
provider-item and ingest-failure payloads; it is not physical database usage and excludes indexes
and other overhead. `database_bytes` sums the active on-disk SQLite database and its `-wal` and
`-shm` files. It reports zero for PostgreSQL and in-memory SQLite. Image-cache bytes come from the
cached image sizes recorded by the service. Turning off image caching returns a local placeholder
immediately; the cleanup worker then removes cached image rows and files. Portable archives omit
ingest-failure payloads and diagnostics; keep a private full database backup if that evidence must
be retained.

## API limits and asynchronous work

Collection endpoints use keyset pagination. `limit` defaults to 50 and is clamped to 1–200; a
non-null `next_cursor` means another page is available. There is no offset mode. Title and review
search conditions run in the database query before the page limit is applied. A work-detail response
embeds only the 200 newest live entries and 200 newest opinions for that work; those embedded lists
do not have separate pagination.

`POST /providers/{id}/sync` and `POST /providers/{id}/import` return `202` after queueing work.
The returned `lineage_id` groups the attempts; consult provider run history for `running`, `partial`,
`failed`, or eventual `success` rather than treating the queue response as completion. The check
endpoint queues an isolated credential check and returns immediately; its result appears in
`Provider.last_check`. It can contact the platform while the provider is disabled. The last-run
endpoint reads history without making a platform request.

Imports accept `.csv`, `.rss`, and `.xml` filenames up to 50 MiB. The upload is written under the
private `/data/imports/<provider>` directory before the worker validates it. Completed and failed
orphan files are removed by the daily cleanup job; queued or leased jobs keep their referenced file.
Provider-specific parse failures remain in the ingest-failure queue with their retained payload for
operator diagnosis or replay. The failure endpoint and raw diagnostic routes require operator
authentication; read-only access does not include those payloads.

Recoverable transport and server failures use the retry ladder. Authentication, blocking, and
structure-change failures do not retry automatically and move the provider to `degraded`; fix the
configuration or provider issue, then trigger a manual sync. The provider status and last error are
also visible through `/health` and the Providers screen.

## Provider configuration in Docker

The normal Compose stack has two long-lived containers: `aggregato` runs the API and scheduler, and
`frontend` serves the built SPA and proxies `/api` to it. `docker compose -f docker/compose.yml pull`
pulls both release images. Only the frontend publishes a host port (8000 by default); set
`AGGREGATO_FRONTEND_PORT` to choose another one. The development overlay replaces both images with
local builds and enables Vite and API hot reload.

Settings saved from the Providers screen are stored in the archive database at
`/data/aggregato.db`. Docker Compose mounts `/data` on the named `aggregato-data` volume, so those
settings survive `docker compose stop`, `docker compose up`, image upgrades, and `docker compose
down`. Do not run `docker compose down --volumes` (or remove `aggregato-data`) unless the whole
archive, including provider settings, is intentionally being discarded. YAML-mounted provider
settings remain file-pinned and cannot be changed from the web.

Disabling a provider during an operation prevents future scheduled work but does not cancel the
active operation. Its status remains `syncing` until that operation finishes. Provider configuration
changes return `409` during that time; retry after completion.

Only one scheduler worker may own a database at a time. SQLite uses a sidecar file lock, and
PostgreSQL uses a session advisory lock. On shutdown, the worker stops admitting syncs, allows active
syncs up to three seconds to finish, then cancels and reaps remaining children while stopping the
now-playing monitor. It disposes the database engine after that shutdown work completes.
