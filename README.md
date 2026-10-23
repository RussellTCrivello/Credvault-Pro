# CredVault

CredVault is a Django + PostgreSQL application for importing, searching, and
auditing credential datasets.

## Project layout

- `admin_LogIntel/` contains Django project configuration and server entrypoints.
- `vault/` contains the application, with import logic in `import_app/`,
  management commands in `management/`, and UI assets in `static/` and
  `templates/`.
- `tests/` contains the test suite; `locale/` contains translation catalogs.
- `manage.py`, `requirements.txt`, and this README remain at the repository
  root, as expected by Django and common Python tooling.

Install dependencies and create the schema for a **new, empty** PostgreSQL
database. The final command's database name must exactly match `POSTGRES_DB`:

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py install_import_pipeline --initialize-new --confirm-database credvault
python manage.py createsuperuser
python manage.py runserver 0.0.0.0:8000
```

`vault/migrations/0001_initial_models.py` is the ordinary Django baseline for
all modeled vault tables. `migrate` applies it on a new installation. The
PostgreSQL extension, import finalizer, operational search indexes, staging
permissions, and their checksummed version history are managed separately by
`install_import_pipeline`. Do not run `migrate` against a legacy database with
unrecorded vault tables before completing the adoption procedure below: the
initial migration would otherwise try to create tables that already exist.

## Schema installation, adoption, and upgrades

The import pipeline needs a private staging table, a versioned PostgreSQL
finalizer, `pg_trgm`, and explicit search indexes. These database-specific
objects are kept separate from ordinary Django model migrations and recorded
in `vault_schema_migrations` under immutable migration IDs.

### Existing installation without Django vault migration history

Before any schema change, take and verify a database backup, rehearse a
restore, and capture the read-only catalog inventory:

```bash
python manage.py vault_schema inventory --format json > vault-schema-before.json
```

Review the actual table/column types, nullability, keys, foreign keys, indexes,
constraints, extensions, finalizer version, staging grants, row estimates, and
`model_schema_issues`. Resolve differences with a reviewed operator migration;
`vault_schema check` is expected to fail until the schema transition is
complete. During explicit `--adopt-existing`, the installer may add only the
new `vault_import_stage_record` table if it is absent. It verifies that every
modeled table, column, key, model index, constraint, and foreign key matches;
malformed or otherwise mismatched objects are not repaired automatically.

After review and backup/restore rehearsal, run the installer using the
schema-owner/application database role, exact database confirmation, and an
external backup reference (recorded as operator metadata):

```bash
python manage.py install_import_pipeline \
  --adopt-existing \
  --backup-reference 'backup-id-or-location' \
  --confirm-database 'exact-current-database-name'
python manage.py migrate
python manage.py vault_schema check --format json > vault-schema-after.json
python manage.py showmigrations vault
```

The adoption path does **not** run the Django initial migration's table-creation
operations. Only after its model-schema preflight (and additive creation of the
missing staging table, if needed) does it use `fake_initial` to record
`vault.0001_initial_models`. It refuses unknown vault migration history,
unapplied later vault migrations, and existing non-vault Django tables without
their app's migration records; it will not invent history for auth/admin
schemas. The subsequent ordinary `migrate` applies the
remaining Django apps without recreating the adopted vault tables. The
installer never alters existing model columns, drops objects, truncates rows,
or rewrites account data. If the staging table already exists but is malformed,
the installer does not repair it automatically.

### New database

On a new, empty database, run ordinary `python manage.py migrate` first, then
`install_import_pipeline --initialize-new --confirm-database <exact-name>`.
The installer refuses if any vault model table contains rows. It never infers
that a database is new from its name. Without an explicit mode and exact
database confirmation, it refuses to change schema. The command serializes
installers with a PostgreSQL advisory lock, installs `pg_trgm` and the
finalizer, builds search indexes concurrently under finite
`CREDVAULT_SCHEMA_DDL_TIMEOUT_MS`, validates definitions and private grants,
and records a checksum in `vault_schema_migrations`. A no-flag rerun is
verification-only and performs no DDL.

### Forward evolution and rollback

For future model changes, create and review normal Django migrations. For an
existing installation, take a tested backup first, apply only the specifically
reviewed Django migration with `python manage.py migrate vault`, then run the
explicit backup-backed `install_import_pipeline --adopt-existing` transition
and `vault_schema check`. Do not use `--fake` or `--fake-initial` manually.
For each PostgreSQL-specific SQL/index/grant change, append a new immutable
schema migration ID in code, retain prior IDs/checksums in history, and add a
reviewed operator procedure; the installer refuses unknown versions or stale
checksums. Destructive or data-transforming changes require a separate reviewed
migration and rollback plan; they are not inferred or automated by this
additive installer.

Installation is additive, but extension/function/index creation and version
recording are not one all-or-nothing transaction: interruption can leave some
objects installed. Inventory the result before retrying; use the same reviewed
backup reference and explicit adoption mode only after inspection. For rollback,
restore the verified pre-upgrade backup. Do not drop the staging table while
pending staged payloads exist; use the recovery procedure first.

The durable importer preserves job leases, checkpoints, idempotency,
transactional account/ledger durability, and ordinary password-column storage.
Accepted staged payloads are finalized transactionally and cleared; recovery
drains already-accepted rows before replaying a verified source. Cancellation
remains terminal and does not discard accepted rows. Metrics contain only job
IDs, mode, counts, byte totals, and durations—not imported values, fingerprints,
hashes, or error text.

### Import recovery runbook

After a worker/process interruption, inspect abandoned import jobs and any
cancelled jobs with already-accepted staged payloads:

```bash
python manage.py recoverimports
```

For path-backed jobs, confirm the recorded source is available and unchanged.
Source-less uploads can also be run safely to reconcile committed ledger
outcomes and finalize accepted staged rows; missing bytes remain unresolved.
Recover all listed jobs with `--run`, or narrow it to a specific job with
`--job <uuid>`:

```bash
python manage.py recoverimports --run
python manage.py recoverimports --run --job 'job-uuid'
```

The command never takes over a live lease. It reconciles the durable ledger,
checkpoint, and accepted staged rows before replaying only verified,
unaccounted source positions. For an interrupted upload, recovery reconciles
committed ledger outcomes and finalizes accepted staged rows, but cannot
reconstruct unread bytes; it reports the remaining gap as partial, and the
operator must upload the source again to import missing positions. A cancelled
job remains terminal while recovery drains its already
accepted rows. Do not manually edit counters or truncate stage rows.

## Backup, restore, and SQLite migration

Credential rows, staged payloads, import ledgers, PostgreSQL dumps, SQLite
snapshots, and recovery media are all sensitive data. Store backups outside the
repository, web root, upload directory, and static root; use the organization’s
approved encrypted storage, access controls, retention, and audit policy. This
application does not create backups or configure point-in-time recovery.

### PostgreSQL backup and restore rehearsal

Use a protected `pg_service.conf`/`PGSERVICE` entry or a mode-`0600`
`PGPASSFILE` populated by the secret manager; do not put passwords in shell
history, command-line arguments, source control, or logs. The example assumes
`PGSERVICE` selects the intended source database and supplies TLS verification.
`pg_dump` is a logical backup; separately operate and test base-backup/WAL
archiving if the required RPO/RTO needs point-in-time recovery.

```bash
export PGSERVICE=credvault-backup
export PGDATABASE=credvault
BACKUP_DIR=/secure/credential-backups
install -d -m 0700 "$BACKUP_DIR"
umask 077
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP="$BACKUP_DIR/credvault-$STAMP.dump"

pg_dump --format=custom --no-owner --no-acl \
  --file="$BACKUP" --dbname="$PGDATABASE"
sha256sum "$BACKUP" > "$BACKUP.sha256"
pg_restore --list "$BACKUP" > "$BACKUP.contents.txt"
```

`--no-owner --no-acl` is appropriate when database roles and grants are
managed separately as deployment configuration; otherwise, archive and restore
those roles/privileges through the approved process. Protect the manifest and
contents listing too. A checksum and `pg_restore --list` detect some storage
problems but do **not** prove recoverability: rehearse restoration into a new,
dedicated disposable PostgreSQL database and verify the application schema
before declaring the backup usable.

```bash
# Use a distinct service that points only at an isolated restore host;
# never reuse the production backup service for this rehearsal.
export PGSERVICE=credvault-restore-rehearsal
REHEARSAL_DB=credvault_restore_rehearsal
createdb --maintenance-db=postgres --template=template0 "$REHEARSAL_DB"
pg_restore --exit-on-error --no-owner --no-acl \
  --dbname="$REHEARSAL_DB" "$BACKUP"

# Django reads POSTGRES_* (not libpq PGSERVICE); inject POSTGRES_HOST,
# POSTGRES_USER, and POSTGRES_PASSWORD from the isolated restore secret.
# Point it only at the restored database, never at the source database.
CREDVAULT_ENV=development POSTGRES_DB="$REHEARSAL_DB" \
  python manage.py vault_schema check --format json
CREDVAULT_ENV=development POSTGRES_DB="$REHEARSAL_DB" \
  python manage.py showmigrations --plan
```

Review the restore output and schema report, confirm aggregate table counts
against a separately protected inventory, and test sign-in/critical workflows
without exporting or displaying credential values. `vault_schema check` is
read-only. Do not run `migrate`, `recoverimports --run`, or a destructive
cleanup merely as part of a restore check: inspect the restored migration and
import-job state first. Rehearse cleanup only after independently confirming
the exact disposable database name; do not issue a generic `dropdb` command
against an environment whose target has not been checked.

### SQLite-to-PostgreSQL conversion

This is an explicit data conversion, not a Django migration. First stop the
SQLite application and its import workers, record the source application/code
revision, and create a protected, consistent SQLite snapshot. Preserve any
`-wal`/`-shm` files with the database until the SQLite backup API has completed.
The following creates a **new** snapshot without modifying the source and runs
SQLite’s integrity check; the destination must not already exist:

```bash
python - "$SQLITE_DATABASE" "$SQLITE_SNAPSHOT" <<'PY'
import os
import sqlite3
import sys
from pathlib import Path

source_path = Path(sys.argv[1]).expanduser().resolve()
snapshot_path = Path(sys.argv[2]).expanduser().resolve()
if snapshot_path.exists():
    raise SystemExit(f"Refusing to overwrite existing snapshot: {snapshot_path}")
snapshot_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
source.execute("PRAGMA query_only=ON")
target = sqlite3.connect(snapshot_path)
try:
    source.backup(target)
    target.commit()
finally:
    target.close()
    source.close()
os.chmod(snapshot_path, 0o600)
check = sqlite3.connect(snapshot_path.as_uri() + "?mode=ro", uri=True)
try:
    result = check.execute("PRAGMA integrity_check").fetchone()[0]
finally:
    check.close()
if result != "ok":
    raise SystemExit(f"SQLite snapshot integrity check failed: {result}")
print("SQLite snapshot integrity check passed.")
PY
```

Before converting valuable data, rehearse with a synthetic fixture and a
separate, disposable PostgreSQL target. Create that target with the current
Django model schema and custom PostgreSQL objects, then run the converter with
an exact target-name confirmation (the command refuses non-PostgreSQL or
non-matching targets):

```bash
# Use distinct PostgreSQL service/settings pointing to a disposable rehearsal host.
export PGSERVICE=credvault-sqlite-rehearsal
TARGET_DB=credvault_sqlite_rehearsal
createdb --template=template0 "$TARGET_DB"
POSTGRES_DB="$TARGET_DB" python manage.py migrate
POSTGRES_DB="$TARGET_DB" python manage.py install_import_pipeline \
  --initialize-new --confirm-database "$TARGET_DB"
POSTGRES_DB="$TARGET_DB" python manage.py copy_from_sqlite \
  "$SQLITE_SNAPSHOT" --confirm-database "$TARGET_DB"
POSTGRES_DB="$TARGET_DB" python manage.py vault_schema check --format json
POSTGRES_DB="$TARGET_DB" python manage.py recoverimports
```

Supply the correct isolated `POSTGRES_HOST`, `POSTGRES_USER`, and secret-managed
connection configuration for the rehearsal. Never point the rehearsal at the
source database or production. A separate, reviewed change authorization is
required before an operator uses a conversion procedure on operational data.
The target must be empty of vault/auth/session/admin-log rows; ordinary Django
migrations and `install_import_pipeline` must finish first. `django_migrations`
and SQLite sequence/statistics metadata are not copied: target Django
migrations establish their own history, the PostgreSQL installer records its
own `vault_schema_migrations` checksum, and PostgreSQL sequences are reset
from preserved primary keys.

The converter preserves the six core vault tables, Django auth users, groups,
permissions, sessions, and admin-log data, plus import jobs, idempotency
ledgers, accepted staged payloads, and recovery logs when present. It checks
source/target row counts, recomputed account fingerprints, import idempotency
keys, and stage rows against ledger status. The SQLite file is read-only and held at one
snapshot; PostgreSQL row inserts run in a transaction. Unknown tables or
source-only columns, missing current-model required columns, unrecognized
SQLite indexes, changed unique/partial-index semantics, and custom SQLite
triggers/views cause refusal rather than silent discard. Indexes in the
PostgreSQL target schema come from its migrations/installer; SQLite index
definitions are not copied. Inspect source
catalog objects and map every custom object before retrying. If an error occurs,
confirm the target rows are empty before retry; PostgreSQL sequence values can
advance even when a transaction later rolls back, but copied rows do not
partially commit. The command intentionally does not copy `django_migrations`
or `vault_schema_migrations` rows from SQLite.

After a successful rehearsal, compare aggregate counts and run
`python manage.py vault_schema check --format json` again. Inspect
`python manage.py recoverimports` output and every copied job’s source
availability before any recovery run. Path-backed jobs may refer to files on
the old host; upload jobs cannot replay unread source bytes, though accepted
staged rows and ledger outcomes are preserved for recovery. Keep the old
SQLite source and its verified backup read-only until the PostgreSQL application
has passed the approved cutover checks. If any schema object or job source is
uncertain, stop and prepare a reviewed mapping/recovery plan; do not manually
edit ledger counters or truncate staged credentials.

## Import benchmark (disposable database only)

`benchmarks/bench_import_pipeline.py` compares the direct durable flush with
COPY staging plus set-based finalization on deterministic synthetic data. The
default workload is 10k, 50k, and 100k lines at a 50% duplicate ratio; one
invalid line is inserted per 997 source positions and excluded from that ratio.
Use `--scales`, `--duplicate-ratios`, `--warmups`, `--repeats`, and
`--batch-rows` to shape a workload. Every measured run asserts expected
synthetic outcome counts, matching natural-key-normalized ledger and account
digests across modes, a terminal job, each account's fingerprint formula and
ledger-to-account fingerprint link, and no remaining credential-bearing stage
rows. Surrogate-key-derived fingerprint strings are validated within each run
but are not compared across modes because reference-row insertion order may
legitimately differ. The command emits one
JSONL event per run plus median and elapsed-time range summaries; `--output`
saves the same records to a file.

It clears import, account, and reference rows in `credvault_bench` between
runs. It refuses any other database name, a non-loopback PostgreSQL host, or
execution without the explicit `--reset-disposable-db` confirmation. Point it
only at a throwaway database containing synthetic data:

```bash
POSTGRES_DB=credvault_bench POSTGRES_HOST=127.0.0.1 python benchmarks/bench_import_pipeline.py --reset-disposable-db --scales 10000 50000 100000 --duplicate-ratios 0 0.5 0.9 --repeats 3 --output /tmp/credvault-import-benchmark.jsonl --activity-log /tmp/credvault-import-benchmark.activity.jsonl
```

### Repeated synthetic result (25,000-row durable batches)

PostgreSQL 16.15 with `fsync`, `full_page_writes`, and
`synchronous_commit` enabled; 250,000 synthetic lines, 10% requested duplicate
ratio, three measured repeats per mode. Both modes finished with 250,000
attempted, 224,775 committed, 25,225 skipped, and zero failed; the
natural-key-normalized account and ledger digests matched.

| Mode | Elapsed runs (s) | Median (s) | Median WAL (bytes) | Median Python peak RSS |
| --- | --- | ---: | ---: | ---: |
| Direct | 33.461, 31.822, 31.636 | 31.822 | 793,869,688 | 143,077,376 bytes |
| Staged | 49.495, 48.991, 48.957 | 48.991 | 1,123,709,312 | 125,837,312 bytes |

Staged/direct ratios were 1.54x elapsed time and 1.415x WAL; staged peak
Python RSS was 0.88x direct. This supports direct mode as the default on this
synthetic host, not a universal hardware claim. An earlier attempt with a
single 250,000-row batch ran beyond 300 seconds and left a partially processed
job; its sampled maximum transaction age was about 344 seconds. That failed
attempt is adverse evidence, not a successful benchmark or proof that rare
long-transaction stalls are resolved. The 25,000-row results and lease-renewal
regression tests support the lower cap, but production-like load and storage
still need operator measurement.

The environment event records PostgreSQL version/settings, finalizer version,
`pg_trgm` version, batch/work-memory settings, and index definitions/readiness.
Each run reports wall time and source lines/second, staging/finalizer timings,
Python CPU and sampled peak RSS, the importer backend CPU/RSS and `/proc` I/O
deltas, PostgreSQL database statistics, PostgreSQL 16+ `pg_stat_io`
client-backend counters, WAL bytes, and sampled wait/query-age summaries. A
separate activity JSONL file (default: next to `--output`, otherwise
`/tmp/credvault-import-benchmark-activity.jsonl`) is flushed every second and
contains only backend state/wait events and ages—never query text or row data.
The sampler has 1.5-second statement, 500-ms lock, and 2-second connect/idle
bounds so it cannot hold up imports. Keep an external overall deadline around
the benchmark; for example, `timeout --signal=TERM --kill-after=30s 900s ...`.
Database and `pg_stat_io` counters can include the sampler and unrelated
concurrent activity; `/proc` I/O describes the importer backend only (not
checkpointer/WAL-writer processes). PostgreSQL I/O timing counters require
`track_io_timing` to be enabled. This single-process synthetic comparison is
diagnostic, not a substitute for a controlled production-like
storage/concurrency benchmark.

Never point the benchmark at production or a database containing user data.

## Production deployment prerequisites

Settings are selected explicitly with `CREDVAULT_ENV=development|test|production`;
`manage.py test` selects the test profile when the variable is omitted. The
production profile fails closed unless required secrets and deployment
settings are present. Set at least:

```bash
CREDVAULT_ENV=production
DJANGO_DEBUG=false
SECRET_KEY='<unique random secret, at least 50 characters>'
CREDVAULT_ALLOWED_HOSTS='vault.example.com'
CREDVAULT_CSRF_TRUSTED_ORIGINS='https://vault.example.com'
CREDVAULT_HTTPS_MODE=direct                  # or proxy
CREDVAULT_HSTS_SECONDS=31536000
CREDVAULT_HSTS_INCLUDE_SUBDOMAINS=false       # choose true only if all subdomains use HTTPS
CREDVAULT_HSTS_PRELOAD=false                  # opt in only after a preload review
POSTGRES_DB='...'
POSTGRES_USER='...'
POSTGRES_PASSWORD='...'
POSTGRES_HOST='...'
CREDVAULT_POSTGRES_SSLMODE=verify-full       # or require / verify-ca
CREDVAULT_MAX_UPLOAD_BYTES=268435456
CREDVAULT_MAX_REQUEST_BYTES=285212672        # at least 64 KiB above file limit
CREDVAULT_MAX_UPLOAD_FILES=10
CREDVAULT_UPLOAD_TMP_DIR='/var/lib/credvault/upload-tmp'
CREDVAULT_STATIC_ROOT='/var/lib/credvault/static'
```

Production requires an explicit `DJANGO_DEBUG=false`; omission, a blank value,
or `true` is an error. `DEBUG` is always false in the production profile.
The app enables secure session/CSRF cookies, HTTPS redirect, HSTS, nosniff,
referrer/cross-origin policy, and clickjacking protection. The host and CSRF
allowlists must be explicit; production CSRF origins must be HTTPS. Database
connection, statement, lock, and idle-transaction timeouts are finite and
configurable. Keep certificate verification enabled when possible and provide
`CREDVAULT_POSTGRES_SSLROOTCERT` for a private CA.

For `CREDVAULT_HTTPS_MODE=proxy`, also set
`CREDVAULT_TRUSTED_PROXY_CIDRS` to the exact proxy networks. The proxy must
overwrite (not append) `X-Forwarded-Proto`; the app ignores that header from
untrusted peer addresses. Do not enable forwarded-host trust. For direct TLS,
terminate HTTPS at the application server and do not configure proxy CIDRs.

The upload handler enforces aggregate file bytes and file count while chunks
stream, even when `Content-Length` is absent. `CREDVAULT_MAX_REQUEST_BYTES`
provides an early request-body rejection when a length is available;
configure the reverse proxy/web server with the same or smaller request-body
limit because HTTP framing and non-file multipart overhead are outside the
file-byte counter. Use an existing absolute upload temporary directory owned
by the app user with mode `0700`, quota it, and restrict filesystem backups and
access. Keep `CREDVAULT_FILE_UPLOAD_MEMORY_BYTES` bounded (default 8 MiB).

Credentials are intentionally stored in ordinary vault password columns; the
application does not encrypt or hash imported passwords. Treat the database,
replicas, dumps, and backups as credential stores: restrict administrative and
service roles, protect storage/backups with the deployment's approved
at-rest controls, audit access, and rehearse restore/rotation procedures. Keep
`SECRET_KEY` and database credentials in a secret manager or protected runtime
environment, never in source control or logs.

The importer defaults to `VAULT_IMPORT_MODE=direct`; `staged` remains an
explicit opt-in because a controlled 250,000-line, 10%-duplicate run was 1.54x
slower and wrote 1.42x the WAL in staged mode. Both modes use synchronous
commits and the same durable ledger/account invariants. Durable flush/stage
batches default to 25,000 lines; `VAULT_IMPORT_BATCH_ROWS` can lower the
automatic batch size to a 10,000-row minimum but cannot raise the 25,000-row
hard ceiling. Explicit importer API/test batches may be smaller. This keeps
individual database transactions short enough
for lease, cancellation, and recovery handling. Staged finalization defaults
to 25,000 rows per transaction and can be tuned with
`VAULT_IMPORT_FINALIZATION_BATCH_ROWS` up to 250,000 after workload-specific
measurement. `VAULT_IMPORT_TIMEOUT_SECONDS` (default one hour) bounds the
session; expiration records the job as interrupted, while already committed
batches remain durable and accepted staged rows remain recoverable.
Per-transaction statement and lock timeouts are bounded separately. Because file imports run
inside the request process, configure the application server and ingress
read/graceful timeouts for the supported workload; Django cannot enforce a
reverse-proxy or WSGI worker deadline itself. For very long imports, prefer a
managed background worker rather than extending browser/proxy timeouts without
limit.

Set `CREDVAULT_STATIC_ROOT` to an absolute deployment-owned path, run
`python manage.py collectstatic --noinput` during release, and configure the
web server/CDN to serve that directory under `/static/`; Django's development
server is not the production static-file server. Keep the collected directory
read-only to the web-serving process where practical.

After the reviewed migrations and versioned installer have run, execute the
release gate with the explicitly configured production environment and database:

```bash
python manage.py check --deploy
python manage.py migrate --check
python manage.py vault_schema check --format json
python manage.py collectstatic --noinput
```

Review and retain the JSON schema inventory and deployment-check output in the
release record. `collectstatic` writes only to the configured absolute
`CREDVAULT_STATIC_ROOT`; configure the web server/CDN to serve that location
under `/static/` rather than relying on Django in production. The test runner
creates `pg_trgm` only in its isolated test databases; it does not install
extensions in the configured application/production database. Schema upgrade
and rollback requirements are documented in the versioned schema-transition
section above.

## Tests

```bash
python manage.py test
```

The PostgreSQL test runner creates `pg_trgm` in each fresh test database; the
server must have the extension installed and the test role must be allowed to
create it. This does not install extensions in the configured application or
production database.

### `postgresql.conf`

```ini
shared_buffers = 16GB          # about a quarter of 64 GB
effective_cache_size = 48GB    # what the planner may assume is cached
work_mem = 256MB               # general PostgreSQL default; importer sessions use VAULT_IMPORT_WORK_MEM (default 64MB)
maintenance_work_mem = 2GB     # importer index builds use VAULT_MAINTENANCE_WORK_MEM (default 1GB)
max_wal_size = 8GB             # fewer checkpoints during a long load
checkpoint_timeout = 30min
wal_compression = off          # on a 2-vCPU test box pglz halved the import rate; measure before enabling
random_page_cost = 1.1         # SSD
max_parallel_maintenance_workers = 3
```
