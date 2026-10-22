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

Install the Python dependencies and create the schema for a new database:

```bash
pip install -r requirements.txt
python manage.py migrate --run-syncdb
python manage.py createsuperuser
python manage.py runserver 0.0.0.0:8000
```

The `vault` schema is defined in `vault/models.py`; Django migrations are
disabled for this app. `migrate --run-syncdb` creates its tables when they are
missing, but does not alter existing `vault` tables or transform existing
data. The removed migration history also contained data canonicalization and
PostgreSQL-specific index/extension setup; those operations are not run by
`syncdb`.

## Bulk import pipeline upgrade

The durable bulk importer uses one additive private table for credential
payloads that have been parsed but not yet reconciled into `vault_account`.
Set up new and existing databases in this order:

```bash
python manage.py migrate --run-syncdb
python manage.py install_import_pipeline
```

Run `install_import_pipeline` with the PostgreSQL schema-owner role configured
as Django's database `USER`. The command installs/version-checks the
`vault_finalize_import_batch` function, revokes its public access, and grants
the configured application role access to the stage table and finalizer. The
stage model is intentionally absent from Django admin and receives no model
permissions. Staged values are deleted transactionally when finalized; after
a process interruption, recovery drains accepted stage rows before rereading a
source. Cancelled jobs remain cancelled, while recovery may only finalize
payloads already accepted before cancellation.

This is an additive schema upgrade: `migrate --run-syncdb` creates the missing
stage table but does not alter existing vault tables or account data. Back up
the database and validate the upgrade on a disposable copy before deploying;
imports require PostgreSQL and the installed finalizer.

## Import benchmark (disposable database only)

`benchmarks/bench_import_pipeline.py` compares the legacy durable flush with
the staged/set-based finalizer on deterministic synthetic data with repeated
credential pairs at 10k, 50k, and 100k source-line scales. It clears the
import, account, and reference rows in `credvault_bench` between runs and
refuses any other database name or non-loopback PostgreSQL host. Set the normal
PostgreSQL connection variables for that throwaway database and pass the
explicit confirmation flag:

```bash
POSTGRES_DB=credvault_bench python benchmarks/bench_import_pipeline.py --reset-disposable-db
```

Each JSON result includes wall time and source lines/second, import-stage and
finalizer timings, Python-process CPU and sampled peak RSS, the PostgreSQL
import-backend CPU/RSS and `/proc` I/O deltas, PostgreSQL database statistics,
PostgreSQL 16+ `pg_stat_io` client-backend counters, and WAL bytes. PostgreSQL
database/cluster-wide counters can include unrelated concurrent activity; the
OS I/O values describe the importer backend only (not checkpointer/WAL-writer
processes). PostgreSQL I/O timing counters require `track_io_timing` to be
enabled. These are diagnostic measurements, not a
substitute for a controlled production-like storage benchmark.

Never point the benchmark at production or a database containing user data.

## Tests

```bash
python manage.py test
```

### `postgresql.conf`

```ini
shared_buffers = 16GB          # about a quarter of 64 GB
effective_cache_size = 48GB    # what the planner may assume is cached
work_mem = 256MB               # per join/sort; the batch SQL uses a few at once
maintenance_work_mem = 2GB     # index builds (the importer raises it per batch too)
max_wal_size = 8GB             # fewer checkpoints during a long load
checkpoint_timeout = 30min
wal_compression = off          # on a 2-vCPU test box pglz halved the import rate; measure before enabling
random_page_cost = 1.1         # SSD
max_parallel_maintenance_workers = 3
```
