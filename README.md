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
