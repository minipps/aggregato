# Operations

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

Migrations run at startup, and `aggregato.db.migrate` copies the SQLite database first — that
timestamped copy beside the database (`aggregato.db.20260730T152917.bak`) is the way back, because
migrations are forward-only (data-model.md §6). Backups are written with sqlite3's backup API rather
than `cp`, so each one is complete even though the live database runs in WAL mode.

Two failure modes look different and are both recoverable.

**Startup loops on `Can't locate revision identified by 'NNNN'`.** The database is stamped at a
revision whose file is not in the running image — usually a revision that was applied and then
deleted or renumbered in the checkout. Nothing is wrong with the data. Restore the missing revision
file, matching that id, and restart. Do not "fix" it by stamping the database backwards. Note that
every failed boot takes another pre-migration backup, so a container left crash-looping fills `/data`
with copies at a few per minute; delete the surplus once it is healthy, keeping the last backup taken
at each revision.

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

The defaults retain raw payloads for 90 days, successful sync history for 30 days, and failed or
partial runs for 180 days. Failures are intentionally retained longer because they are the evidence
needed to repair a provider or replay a poison record. The cleanup worker runs at startup and then
daily.

Storage usage in Settings separates database bytes, retained raw payload bytes, and image-cache
bytes. Raw payloads are the offline replay source, so lowering that retention trades away the
ability to rebuild old derived data without contacting a platform. Turning off image caching returns
a local placeholder and cleanup removes cached image files. Export an archive before lowering any
retention setting if the old data matters.

## Provider configuration in Docker

Settings saved from the Providers screen are stored in the archive database at
`/data/aggregato.db`. Docker Compose mounts `/data` on the named `aggregato-data` volume, so those
settings survive `docker compose stop`, `docker compose up`, image upgrades, and `docker compose
down`. Do not run `docker compose down --volumes` (or remove `aggregato-data`) unless the whole
archive, including provider settings, is intentionally being discarded. YAML-mounted provider
settings remain file-pinned and cannot be changed from the web.
