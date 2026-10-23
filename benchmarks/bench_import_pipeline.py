#!/usr/bin/env python3
"""Compare direct durable flushes with COPY staging/finalization.

Destructive: resets the configured database's import/account tables before
*each* run. It refuses to run unless the database is named ``credvault_bench``,
the PostgreSQL host is loopback, and ``--reset-disposable-db`` is supplied.
The workload is deterministic and synthetic; never point this at production.
"""

import argparse
import decimal
import hashlib
import json
import logging
import os
import resource
import statistics
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'admin_LogIntel.settings')

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.db import connection  # noqa: E402
from django.db.models import F, Q  # noqa: E402

from vault.import_app.import_engine import (  # noqa: E402
    durable_import_path,
    ensure_import_finalizer_installed,
)
from vault.import_app.importer import (  # noqa: E402
    ensure_search_indexes,
    import_work_mem,
    maintenance_work_mem,
    stage_rows,
    validate_search_indexes,
)
from vault.models import (  # noqa: E402
    Account, ImportStageRecord, record_fingerprint,
)


DISPOSABLE_DATABASE = 'credvault_bench'
INVALID_EVERY = 997


class _MetricCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class _PythonMemorySampler:
    """Sample importer-process RSS so the benchmark reports peak memory."""

    def __init__(self, interval=0.05):
        self.interval = interval
        self.stop_event = threading.Event()
        self.peak_rss_bytes = 0
        self.thread = threading.Thread(
            target=self._sample, name='credvault-benchmark-rss', daemon=True,
        )

    @staticmethod
    def _current_rss():
        try:
            with open('/proc/self/status', encoding='ascii') as source:
                for line in source:
                    if line.startswith('VmRSS:'):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            return 0
        return 0

    def _sample(self):
        while not self.stop_event.is_set():
            self.peak_rss_bytes = max(
                self.peak_rss_bytes, self._current_rss(),
            )
            self.stop_event.wait(self.interval)
        self.peak_rss_bytes = max(
            self.peak_rss_bytes, self._current_rss(),
        )

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join()


class _PostgresActivitySampler:
    """Persist privacy-safe wait/activity samples for the measured backend.

    Samples deliberately omit ``pg_stat_activity.query`` and all row values.
    Each JSONL line is flushed immediately so evidence survives an external
    benchmark deadline even if an import is still inside a slow COMMIT.
    """

    _SQL = """
        SELECT state, wait_event_type, wait_event,
               CASE WHEN state = 'active' AND query_start IS NOT NULL
                    THEN extract(epoch FROM clock_timestamp() - query_start)
                    ELSE 0 END,
               CASE WHEN xact_start IS NOT NULL
                    THEN extract(epoch FROM clock_timestamp() - xact_start)
                    ELSE 0 END
          FROM pg_catalog.pg_stat_activity
         WHERE pid = %s
    """

    def __init__(self, backend_pid, trace_writer, label, interval=1.0):
        self.backend_pid = int(backend_pid)
        self.trace_writer = trace_writer
        self.label = dict(label or {})
        self.interval = interval
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.thread = threading.Thread(
            target=self._sample,
            name='credvault-benchmark-pg-activity',
            daemon=True,
        )
        self.samples = 0
        self.missing_samples = 0
        self.state_samples = {}
        self.wait_samples = {}
        self.error_samples = {}
        self.max_query_age_seconds = 0.0
        self.max_transaction_age_seconds = 0.0
        self.startup_timeout = False

    def _write_trace(self, event):
        if self.trace_writer is None:
            return
        self.trace_writer.write(json.dumps(event, sort_keys=True) + '\n')
        self.trace_writer.flush()

    def _connect(self):
        import psycopg

        database = connection.settings_dict
        options = database.get('OPTIONS') or {}
        params = {
            'dbname': database['NAME'],
            'user': database['USER'],
            'password': database['PASSWORD'],
            'host': database['HOST'],
            'port': database['PORT'] or None,
            'connect_timeout': 2,
            # SQL_ASCII benchmark clusters otherwise return text as bytes from
            # this raw psycopg connection, which cannot be JSON-logged.
            'client_encoding': 'UTF8',
            'options': (
                '-c statement_timeout=1500ms '
                '-c lock_timeout=500ms '
                '-c idle_in_transaction_session_timeout=2000ms'
            ),
            'application_name': 'credvault-benchmark-activity-sampler',
        }
        for key in ('sslmode', 'sslrootcert'):
            if options.get(key):
                params[key] = options[key]
        return psycopg.connect(**params, autocommit=True)

    def _sample(self):
        sample_connection = None
        while not self.stop_event.is_set():
            try:
                if sample_connection is None:
                    sample_connection = self._connect()
                    self.ready.set()
                row = sample_connection.execute(
                    self._SQL, (self.backend_pid,),
                ).fetchone()
                self.samples += 1
                if row is None:
                    self.missing_samples += 1
                    self._write_trace({
                        'event': 'pg_activity_sample',
                        **self.label,
                        'timestamp_utc': datetime.now(timezone.utc).isoformat(),
                        'backend_pid': self.backend_pid,
                        'backend_present': False,
                    })
                else:
                    state, wait_type, wait_event, query_age, xact_age = row
                    self.state_samples[state or 'unknown'] = (
                        self.state_samples.get(state or 'unknown', 0) + 1
                    )
                    if wait_type or wait_event:
                        wait_key = f'{wait_type or "unknown"}:{wait_event or "unknown"}'
                        self.wait_samples[wait_key] = (
                            self.wait_samples.get(wait_key, 0) + 1
                        )
                    self.max_query_age_seconds = max(
                        self.max_query_age_seconds, float(query_age or 0),
                    )
                    self.max_transaction_age_seconds = max(
                        self.max_transaction_age_seconds, float(xact_age or 0),
                    )
                    self._write_trace({
                        'event': 'pg_activity_sample',
                        **self.label,
                        'timestamp_utc': datetime.now(timezone.utc).isoformat(),
                        'backend_pid': self.backend_pid,
                        'backend_present': True,
                        'state': state,
                        'wait_event_type': wait_type,
                        'wait_event': wait_event,
                        'query_age_seconds': round(float(query_age or 0), 3),
                        'transaction_age_seconds': round(float(xact_age or 0), 3),
                    })
            except Exception as exc:
                error_type = type(exc).__name__
                self.error_samples[error_type] = (
                    self.error_samples.get(error_type, 0) + 1
                )
                self.ready.set()
                self._write_trace({
                    'event': 'pg_activity_sample_error',
                    **self.label,
                    'timestamp_utc': datetime.now(timezone.utc).isoformat(),
                    'backend_pid': self.backend_pid,
                    'error_type': error_type,
                })
                if sample_connection is not None:
                    try:
                        sample_connection.close()
                    except Exception:
                        pass
                    sample_connection = None
            if self.stop_event.wait(self.interval):
                break
        if sample_connection is not None:
            try:
                sample_connection.close()
            except Exception:
                pass
        self.ready.set()

    def start(self):
        self.thread.start()
        self.startup_timeout = not self.ready.wait(timeout=5)

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=5)

    def summary(self):
        return {
            'pg_activity_backend_pid': self.backend_pid,
            'pg_activity_samples': self.samples,
            'pg_activity_missing_samples': self.missing_samples,
            'pg_activity_state_samples': dict(sorted(self.state_samples.items())),
            'pg_activity_wait_samples': dict(sorted(self.wait_samples.items())),
            'pg_activity_sample_errors': dict(sorted(self.error_samples.items())),
            'pg_activity_startup_timeout': self.startup_timeout,
            'pg_activity_max_active_query_age_seconds': round(
                self.max_query_age_seconds, 3,
            ),
            'pg_activity_max_transaction_age_seconds': round(
                self.max_transaction_age_seconds, 3,
            ),
        }


def _proc_sample(pid):
    """Read best-effort Linux process CPU, RSS, and I/O counters."""
    try:
        status = {}
        with open(f'/proc/{pid}/status', encoding='ascii') as source:
            for line in source:
                if line.startswith(('VmRSS:', 'VmHWM:')):
                    key, value, *_ = line.split()
                    status[key.rstrip(':')] = int(value) * 1024
        with open(f'/proc/{pid}/stat', encoding='ascii') as source:
            fields = source.read().rsplit(')', 1)[1].split()
        clock_ticks = os.sysconf('SC_CLK_TCK')
        cpu_seconds = (int(fields[11]) + int(fields[12])) / clock_ticks
        io = {}
        with open(f'/proc/{pid}/io', encoding='ascii') as source:
            for line in source:
                key, value = line.split(':', 1)
                io[key] = int(value.strip())
        return {
            'cpu_seconds': cpu_seconds,
            'rss_bytes': status.get('VmRSS'),
            'peak_rss_bytes': status.get('VmHWM'),
            'read_bytes': io.get('read_bytes'),
            'write_bytes': io.get('write_bytes'),
            'syscr': io.get('syscr'),
            'syscw': io.get('syscw'),
        }
    except (OSError, ValueError, IndexError):
        return {}


def _database_snapshot():
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_backend_pid(), pg_current_wal_lsn()::text')
        backend_pid, lsn = cursor.fetchone()
        cursor.execute("""
            SELECT blks_read, blks_hit, tup_inserted, tup_updated,
                   temp_files, temp_bytes
              FROM pg_stat_database
             WHERE datname = current_database()
        """)
        database = cursor.fetchone()
        cursor.execute("SELECT to_regclass('pg_catalog.pg_stat_io')")
        if cursor.fetchone()[0] is not None:
            cursor.execute("""
                SELECT coalesce(sum(reads), 0), coalesce(sum(read_time), 0),
                       coalesce(sum(writes), 0), coalesce(sum(write_time), 0),
                       coalesce(sum(writebacks), 0),
                       coalesce(sum(writeback_time), 0),
                       coalesce(sum(extends), 0), coalesce(sum(extend_time), 0),
                       coalesce(sum(fsyncs), 0), coalesce(sum(fsync_time), 0)
                  FROM pg_stat_io
                 WHERE backend_type = 'client backend'
            """)
            io = cursor.fetchone()
        else:
            io = ()
    return {
        'backend_pid': int(backend_pid),
        'wal_lsn': lsn,
        'database': tuple(database or ()),
        'pg_io': tuple(io or ()),
        'process': _proc_sample(int(backend_pid)),
    }


def _delta(before, after):
    if before is None or after is None:
        return None
    try:
        value = after - before
    except TypeError:
        return None
    if isinstance(value, decimal.Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _summary_metrics(messages):
    for message in reversed(messages):
        if 'import_metrics event=summary ' not in message:
            continue
        result = {}
        for token in message.split():
            if '=' not in token:
                continue
            key, value = token.split('=', 1)
            try:
                result[key] = int(value)
            except ValueError:
                try:
                    result[key] = float(value)
                except ValueError:
                    result[key] = value
        return result
    return {}


def _reset_benchmark_tables():
    """Remove only application import/account data from the disposable DB."""
    with connection.cursor() as cursor:
        cursor.execute("""
            TRUNCATE TABLE
                vault_import_stage_record,
                vault_importrecord,
                vault_importrecoverylog,
                vault_importjob,
                vault_account,
                vault_email,
                vault_ip,
                vault_pass,
                vault_url,
                vault_user
            RESTART IDENTITY
        """)


def _synthetic_lines(count, duplicate_ratio=0.5):
    """Yield deterministic pairs at the requested duplicate ratio.

    Invalid rows (one per ``INVALID_EVERY`` source positions) are identical
    in every mode and excluded from the duplicate-ratio denominator. At least
    one valid credential is needed, so a 100% request means all valid lines
    after the first repeat that one credential.
    """
    duplicate_ratio = float(duplicate_ratio)
    if not 0.0 <= duplicate_ratio <= 1.0:
        raise ValueError('duplicate_ratio must be between 0.0 and 1.0')
    valid_count = count - (count // INVALID_EVERY)
    unique_count = max(1, round(valid_count * (1.0 - duplicate_ratio)))
    valid_position = 0
    for index in range(count):
        if index % INVALID_EVERY == INVALID_EVERY - 1:
            yield b'this synthetic line is intentionally invalid\n'
            continue
        account_index = valid_position % unique_count
        valid_position += 1
        line = (
            f'https://bench-{account_index % 500}.example/login|'
            f'synthetic-user-{account_index}|'
            f'synthetic-secret-{account_index % 5000}|'
            f'192.0.2.{account_index % 254 + 1}\n'
        ).encode('ascii')
        yield line


def _write_source(path, count, duplicate_ratio=0.5):
    with open(path, 'wb') as source:
        for line in _synthetic_lines(count, duplicate_ratio):
            source.write(line)


_NATURAL_ACCOUNT_FIELDS = (
    'url__name', 'account_type', 'email__name', 'user__name',
    'password__name', 'ip__name',
)


def _digest_rows(rows):
    """Hash a deterministic logical row stream without emitting row values."""
    digest = hashlib.sha256()
    for row in rows.iterator(chunk_size=5000):
        digest.update(json.dumps(row, separators=(',', ':')).encode('utf-8'))
        digest.update(b'\n')
    return digest.hexdigest()


def _outcome_digest(job):
    """Hash per-position outcomes plus the linked account's natural key."""
    fields = (
        'source_position', 'source_hash', 'status', 'error_category',
        'retryable', *(f'account__{name}' for name in _NATURAL_ACCOUNT_FIELDS),
    )
    rows = job.records.order_by('source_position').values_list(*fields)
    return _digest_rows(rows)


def _account_digest():
    rows = Account.objects.order_by(*_NATURAL_ACCOUNT_FIELDS).values_list(
        *_NATURAL_ACCOUNT_FIELDS,
    )
    return _digest_rows(rows)


def _assert_completed_job(job):
    job.refresh_from_db()
    if job.status not in ('completed', 'completed_with_errors'):
        raise RuntimeError(
            f'import ended in status {job.status!r}; refusing to report it.'
        )
    if job.records.filter(status__in=('pending', 'processing')).exists():
        raise RuntimeError('completed import left non-terminal ledger rows.')
    if ImportStageRecord.objects.filter(
        import_record__import_job_id=job.pk,
    ).exists():
        raise RuntimeError('completed import left credential-bearing stage rows.')
    missing_account = job.records.filter(status='committed').filter(
        Q(account__isnull=True) | Q(account_fingerprint='')
    ).exists()
    if missing_account:
        raise RuntimeError('a committed ledger row has no account fingerprint.')
    mismatch = job.records.filter(account__isnull=False).exclude(
        account_fingerprint=F('account__fingerprint'),
    ).exists()
    if mismatch:
        raise RuntimeError('a ledger fingerprint does not match its linked account.')
    account_rows = Account.objects.values_list(
        'url_id', 'account_type', 'email_id', 'user_id', 'password_id',
        'ip_id', 'fingerprint',
    )
    for url_id, account_type, email_id, user_id, password_id, ip_id, fingerprint in account_rows.iterator(
        chunk_size=5000,
    ):
        if fingerprint != record_fingerprint(
            url_id, account_type, email_id, user_id, password_id, ip_id,
        ):
            raise RuntimeError('an account fingerprint does not match its foreign keys.')


def _run_one(
    source_path, line_count, mode, batch_rows, duplicate_ratio,
    *, activity_writer=None, activity_label=None,
):
    _reset_benchmark_tables()
    # Isolate the measured PostgreSQL backend from the reset/truncate work.
    connection.close()
    before_db = _database_snapshot()
    cpu_before = resource.getrusage(resource.RUSAGE_SELF)
    python_rss_before = _PythonMemorySampler._current_rss()
    memory = _PythonMemorySampler()
    activity = _PostgresActivitySampler(
        before_db['backend_pid'], activity_writer, activity_label,
    )
    capture = _MetricCapture()
    metrics_logger = logging.getLogger('vault.import.metrics')
    old_level = metrics_logger.level
    old_propagate = metrics_logger.propagate
    old_handlers = tuple(metrics_logger.handlers)
    metrics_logger.setLevel(logging.INFO)
    metrics_logger.propagate = False
    for handler in old_handlers:
        metrics_logger.removeHandler(handler)
    metrics_logger.addHandler(capture)
    memory.start()
    activity.start()
    started = time.perf_counter()
    job = None
    try:
        job = durable_import_path(
            source_path,
            ext='*',
            workers=1,
            batch_rows=batch_rows,
            bulk_finalize=(mode == 'staged'),
        )
    finally:
        elapsed = time.perf_counter() - started
        memory.stop()
        activity.stop()
        cpu_after = resource.getrusage(resource.RUSAGE_SELF)
        # Capture the importer backend's own /proc high-water and end LSN,
        # then close it so PostgreSQL flushes its per-backend statistics before
        # the database-wide counters are sampled.
        after_active = _database_snapshot()
        backend_after = after_active['process']
        end_lsn = after_active['wal_lsn']
        connection.close()
        after_db = _database_snapshot()
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT pg_wal_lsn_diff(%s::pg_lsn, %s::pg_lsn)',
                [end_lsn, before_db['wal_lsn']],
            )
            wal_bytes = int(cursor.fetchone()[0])
        metrics_logger.removeHandler(capture)
        for handler in old_handlers:
            metrics_logger.addHandler(handler)
        metrics_logger.setLevel(old_level)
        metrics_logger.propagate = old_propagate
        connection.close()

    if job is None:
        raise RuntimeError(f'{mode} import raised before returning a job.')
    _assert_completed_job(job)

    db_fields = (
        'blks_read', 'blks_hit', 'tup_inserted', 'tup_updated',
        'temp_files', 'temp_bytes',
    )
    database_delta = {
        f'pg_database_{name}': _delta(before, after)
        for name, before, after in zip(
            db_fields, before_db['database'], after_db['database'],
        )
    }
    io_fields = (
        'reads', 'read_time_ms', 'writes', 'write_time_ms',
        'writebacks', 'writeback_time_ms', 'extends', 'extend_time_ms',
        'fsyncs', 'fsync_time_ms',
    )
    pg_io_delta = {
        f'pg_stat_io_client_{name}': _delta(before, after)
        for name, before, after in zip(
            io_fields, before_db['pg_io'], after_db['pg_io'],
        )
    }
    backend_before = before_db['process']
    backend_delta = {
        'backend_cpu_seconds': _delta(
            backend_before.get('cpu_seconds'), backend_after.get('cpu_seconds'),
        ),
        'backend_read_bytes': _delta(
            backend_before.get('read_bytes'), backend_after.get('read_bytes'),
        ),
        'backend_write_bytes': _delta(
            backend_before.get('write_bytes'), backend_after.get('write_bytes'),
        ),
    }
    usage_delta = {
        'python_user_cpu_seconds': round(cpu_after.ru_utime - cpu_before.ru_utime, 4),
        'python_system_cpu_seconds': round(cpu_after.ru_stime - cpu_before.ru_stime, 4),
    }
    summary = _summary_metrics(capture.messages)
    import_metric_fields = (
        'staging_batches', 'staging_seconds', 'finalization_batches',
        'finalized_items', 'finalization_seconds', 'db_seconds',
        'db_busy_ratio', 'batch_seconds_p50', 'batch_seconds_p95',
    )
    result = {
        'event': 'run',
        'mode': mode,
        'duplicate_ratio_requested': duplicate_ratio,
        'elapsed_seconds': round(elapsed, 3),
        'input_lines': line_count,
        'attempted': job.total_attempted,
        'committed': job.total_committed,
        'skipped': job.total_skipped,
        'failed': job.total_failed,
        'accounts': Account.objects.count(),
        'lines_per_second': round(line_count / elapsed, 1) if elapsed else 0.0,
        'wal_bytes': wal_bytes,
        'python_peak_rss_bytes': memory.peak_rss_bytes,
        'python_peak_rss_growth_bytes': max(
            0, memory.peak_rss_bytes - python_rss_before,
        ),
        'backend_peak_rss_bytes': backend_after.get('peak_rss_bytes'),
        **usage_delta,
        **backend_delta,
        **activity.summary(),
        **database_delta,
        **pg_io_delta,
        'logical_digest_scheme': 'natural-key-sha256-v1',
        **{
            f'import_{name}': summary.get(name)
            for name in import_metric_fields
        },
    }
    # Verification digests are calculated outside the timed importer interval.
    result['ledger_digest'] = _outcome_digest(job)
    result['account_digest'] = _account_digest()
    return result


def _index_snapshot():
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT index_class.relname, index_data.indisvalid,
                   index_data.indisready, pg_get_indexdef(index_data.indexrelid)
              FROM pg_index AS index_data
              JOIN pg_class AS table_class
                ON table_class.oid = index_data.indrelid
              JOIN pg_class AS index_class
                ON index_class.oid = index_data.indexrelid
              JOIN pg_namespace AS table_schema
                ON table_schema.oid = table_class.relnamespace
             WHERE table_schema.nspname = 'public'
               AND table_class.relname IN (
                   'vault_url', 'vault_email', 'vault_user', 'vault_pass',
                   'vault_ip', 'vault_account', 'vault_importrecord'
               )
             ORDER BY index_class.relname
        """)
        rows = cursor.fetchall()
    return [
        {
            'name': name,
            'valid': bool(valid),
            'ready': bool(ready),
            'definition': definition,
        }
        for name, valid, ready, definition in rows
    ]


def _environment_snapshot():
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT current_database(), current_setting('server_version'),
                   current_setting('shared_buffers'), current_setting('work_mem'),
                   current_setting('maintenance_work_mem'),
                   current_setting('synchronous_commit'),
                   current_setting('track_io_timing'),
                   current_setting('max_wal_size'),
                   current_setting('checkpoint_timeout'),
                   current_setting('checkpoint_completion_target'),
                   current_setting('fsync'),
                   current_setting('full_page_writes'),
                   current_setting('wal_compression'),
                   current_setting('statement_timeout')
        """)
        values = cursor.fetchone()
        cursor.execute("""
            SELECT extversion FROM pg_extension WHERE extname = 'pg_trgm'
        """)
        row = cursor.fetchone()
        trigram_version = row[0] if row else None
        cursor.execute("""
            SELECT obj_description(
                'vault_finalize_import_batch(bigint,text,integer,integer)'::regprocedure,
                'pg_proc'
            )
        """)
        finalizer_version = cursor.fetchone()[0]
    indexes = _index_snapshot()
    invalid = [index['name'] for index in indexes
               if not index['valid'] or not index['ready']]
    if invalid:
        raise RuntimeError(f'invalid/unready benchmark indexes: {invalid!r}')
    return {
        'database': values[0],
        'postgresql_version': values[1],
        'shared_buffers': values[2],
        'work_mem': values[3],
        'maintenance_work_mem': values[4],
        'synchronous_commit': values[5],
        'track_io_timing': values[6],
        'max_wal_size': values[7],
        'checkpoint_timeout': values[8],
        'checkpoint_completion_target': values[9],
        'fsync': values[10],
        'full_page_writes': values[11],
        'wal_compression': values[12],
        'statement_timeout': values[13],
        'vault_import_work_mem': import_work_mem(),
        'vault_maintenance_work_mem': maintenance_work_mem(),
        'default_import_batch_rows': stage_rows(),
        'finalization_batch_rows': getattr(
            settings, 'VAULT_IMPORT_FINALIZATION_BATCH_ROWS', None,
        ),
        'import_timeout_seconds': getattr(
            settings, 'VAULT_IMPORT_TIMEOUT_SECONDS', None,
        ),
        'import_statement_timeout_ms': getattr(
            settings, 'VAULT_IMPORT_STATEMENT_TIMEOUT_MS', None,
        ),
        'import_lock_timeout_ms': getattr(
            settings, 'VAULT_IMPORT_LOCK_TIMEOUT_MS', None,
        ),
        'pg_trgm_version': trigram_version,
        'finalizer_version': finalizer_version,
        'indexes': indexes,
        'pg_stat_io_available': _has_pg_stat_io(),
    }


def _has_pg_stat_io():
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('pg_catalog.pg_stat_io')")
        return cursor.fetchone()[0] is not None


def _assert_expected_outcomes(result, line_count, duplicate_ratio):
    invalid_count = line_count // INVALID_EVERY
    valid_count = line_count - invalid_count
    expected_accounts = max(
        1, round(valid_count * (1.0 - duplicate_ratio)),
    )
    expected = {
        'attempted': line_count,
        'committed': expected_accounts,
        'skipped': line_count - expected_accounts,
        'failed': 0,
        'accounts': expected_accounts,
    }
    mismatches = {
        key: (result[key], value)
        for key, value in expected.items()
        if result[key] != value
    }
    if mismatches:
        raise RuntimeError(
            f"{result['mode']} outcomes do not match the synthetic workload: "
            f'{mismatches!r}'
        )


def _assert_comparable(direct, staged):
    for key in (
        'attempted', 'committed', 'skipped', 'failed', 'accounts',
        'ledger_digest', 'account_digest',
    ):
        if direct[key] != staged[key]:
            raise RuntimeError(
                f'direct/staged mismatch for {key}: '
                f'{direct[key]!r} != {staged[key]!r}'
            )


def _median(results, key):
    values = [result[key] for result in results if result.get(key) is not None]
    return round(statistics.median(values), 3) if values else None


def _summary(runs, scale, duplicate_ratio):
    by_mode = {
        mode: [run for run in runs if run['mode'] == mode]
        for mode in ('direct', 'staged')
    }
    direct = by_mode['direct']
    staged = by_mode['staged']
    direct_seconds = _median(direct, 'elapsed_seconds')
    staged_seconds = _median(staged, 'elapsed_seconds')
    direct_wal = _median(direct, 'wal_bytes')
    staged_wal = _median(staged, 'wal_bytes')
    direct_rss = _median(direct, 'python_peak_rss_bytes')
    staged_rss = _median(staged, 'python_peak_rss_bytes')
    return {
        'event': 'summary',
        'scale': scale,
        'duplicate_ratio_requested': duplicate_ratio,
        'repeats': len(direct),
        'direct_elapsed_median_seconds': direct_seconds,
        'direct_elapsed_min_seconds': round(min(
            run['elapsed_seconds'] for run in direct
        ), 3),
        'direct_elapsed_max_seconds': round(max(
            run['elapsed_seconds'] for run in direct
        ), 3),
        'staged_elapsed_median_seconds': staged_seconds,
        'staged_elapsed_min_seconds': round(min(
            run['elapsed_seconds'] for run in staged
        ), 3),
        'staged_elapsed_max_seconds': round(max(
            run['elapsed_seconds'] for run in staged
        ), 3),
        'staged_over_direct_time_ratio': (
            round(staged_seconds / direct_seconds, 3)
            if direct_seconds else None
        ),
        'direct_wal_median_bytes': direct_wal,
        'staged_wal_median_bytes': staged_wal,
        'staged_over_direct_wal_ratio': (
            round(staged_wal / direct_wal, 3) if direct_wal else None
        ),
        'direct_python_peak_rss_median_bytes': direct_rss,
        'staged_python_peak_rss_median_bytes': staged_rss,
        'staged_over_direct_python_peak_rss_ratio': (
            round(staged_rss / direct_rss, 3) if direct_rss else None
        ),
        'direct_import_db_median_seconds': _median(direct, 'import_db_seconds'),
        'staged_import_db_median_seconds': _median(staged, 'import_db_seconds'),
        'direct_pg_activity_max_query_age_median_seconds': _median(
            direct, 'pg_activity_max_active_query_age_seconds',
        ),
        'staged_pg_activity_max_query_age_median_seconds': _median(
            staged, 'pg_activity_max_active_query_age_seconds',
        ),
        'staged_import_stage_median_seconds': _median(
            staged, 'import_staging_seconds',
        ),
        'staged_finalization_median_seconds': _median(
            staged, 'import_finalization_seconds',
        ),
        'staged_database_temp_bytes_median': _median(
            staged, 'pg_database_temp_bytes',
        ),
        'staged_db_busy_ratio_median': _median(staged, 'import_db_busy_ratio'),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--reset-disposable-db', action='store_true',
        help='Confirm that credvault_bench may be cleared between runs.',
    )
    parser.add_argument(
        '--scales', type=int, nargs='+', default=[10_000, 50_000, 100_000],
        help='Synthetic source-line counts (default: 10000 50000 100000).',
    )
    parser.add_argument(
        '--duplicate-ratios', type=float, nargs='+', default=[0.5],
        help='Duplicate fraction among valid lines (default: 0.5).',
    )
    parser.add_argument(
        '--repeats', type=int, default=3,
        help='Measured repetitions per scale/ratio (default: 3).',
    )
    parser.add_argument(
        '--warmups', type=int, default=0,
        help='Unreported repetitions per mode/workload before measurements.',
    )
    parser.add_argument(
        '--batch-rows', type=int, default=None,
        help='Optional shared batch size for both implementations.',
    )
    parser.add_argument(
        '--output', type=Path, default=None,
        help='Optional JSONL result path (created or overwritten).',
    )
    parser.add_argument(
        '--activity-log', type=Path, default=None,
        help='Privacy-safe pg_stat_activity sample log (flushed every second).',
    )
    args = parser.parse_args()

    os.environ['VAULT_IMPORT_METRICS_INTERVAL'] = '3600'

    database_name = str(connection.settings_dict.get('NAME', ''))
    if database_name != DISPOSABLE_DATABASE:
        parser.error(
            f'refusing to run against database {database_name!r}; '
            f'configure {DISPOSABLE_DATABASE!r} only.'
        )
    database_host = str(connection.settings_dict.get('HOST', '')).strip().lower()
    if database_host not in ('localhost', '127.0.0.1', '::1'):
        parser.error(
            'the destructive benchmark requires an explicitly configured '
            'loopback PostgreSQL host.'
        )
    if not args.reset_disposable_db:
        parser.error('pass --reset-disposable-db to confirm this destructive benchmark.')
    if connection.vendor != 'postgresql':
        parser.error('the importer benchmark requires PostgreSQL.')
    if any(scale < 1 for scale in args.scales):
        parser.error('all scales must be positive.')
    if args.repeats < 1 or args.warmups < 0:
        parser.error('--repeats must be at least one and --warmups cannot be negative.')
    if args.batch_rows is not None and args.batch_rows < 1:
        parser.error('--batch-rows must be positive.')
    if not args.duplicate_ratios or any(
        not 0.0 <= ratio <= 1.0 for ratio in args.duplicate_ratios
    ):
        parser.error('duplicate ratios must be between 0.0 and 1.0.')

    with connection.cursor() as cursor:
        cursor.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')
    ensure_import_finalizer_installed()
    ensure_search_indexes()
    validate_search_indexes()
    environment = _environment_snapshot()
    if not environment['pg_trgm_version']:
        parser.error('pg_trgm is not installed in the disposable benchmark database.')
    if 'v3' not in str(environment['finalizer_version']):
        parser.error('the installed finalizer is not the expected v3 function.')

    activity_path = args.activity_log or (
        args.output.with_name(args.output.name + '.activity.jsonl')
        if args.output else Path('/tmp/credvault-import-benchmark-activity.jsonl')
    )
    if args.output and args.output.resolve() == activity_path.resolve():
        parser.error('--output and --activity-log must be different files.')
    activity_path.parent.mkdir(parents=True, exist_ok=True)
    activity_file = activity_path.open('w', encoding='utf-8')

    output_file = None
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        output_file = args.output.open('w', encoding='utf-8')

    def report(event):
        line = json.dumps(event, sort_keys=True, default=str)
        print(line, flush=True)
        if output_file is not None:
            output_file.write(line + '\n')
            output_file.flush()

    report({
        'event': 'environment',
        'activity_log_path': str(activity_path),
        'activity_sample_interval_seconds': 1.0,
        'activity_sampler_statement_timeout_ms': 1500,
        'activity_sampler_lock_timeout_ms': 500,
        **environment,
    })
    all_results = []
    try:
        with tempfile.TemporaryDirectory(prefix='credvault-bench-') as directory:
            for scale in args.scales:
                for duplicate_ratio in args.duplicate_ratios:
                    ratio_tag = f'{duplicate_ratio:.4f}'.replace('.', '-')
                    source_path = Path(directory) / (
                        f'synthetic-{scale}-duplicates-{ratio_tag}.txt'
                    )
                    _write_source(source_path, scale, duplicate_ratio)

                    for warmup in range(args.warmups):
                        for mode in ('direct', 'staged'):
                            _run_one(
                                str(source_path), scale, mode,
                                args.batch_rows, duplicate_ratio,
                                activity_writer=activity_file,
                                activity_label={
                                    'phase': 'warmup',
                                    'warmup': warmup + 1,
                                    'mode': mode,
                                    'scale': scale,
                                    'duplicate_ratio_requested': duplicate_ratio,
                                },
                            )

                    measured = []
                    signatures = {}
                    for repeat in range(args.repeats):
                        order = (
                            ('direct', 'staged') if repeat % 2 == 0
                            else ('staged', 'direct')
                        )
                        pair = {}
                        for mode in order:
                            result = _run_one(
                                str(source_path), scale, mode,
                                args.batch_rows, duplicate_ratio,
                                activity_writer=activity_file,
                                activity_label={
                                    'phase': 'measured',
                                    'repeat': repeat + 1,
                                    'mode': mode,
                                    'scale': scale,
                                    'duplicate_ratio_requested': duplicate_ratio,
                                },
                            )
                            result.update({
                                'scale': scale,
                                'repeat': repeat + 1,
                                'batch_rows_requested': args.batch_rows,
                            })
                            _assert_expected_outcomes(
                                result, scale, duplicate_ratio,
                            )
                            measured.append(result)
                            all_results.append(result)
                            report(result)
                            pair[mode] = result
                            signature = tuple(
                                result[key] for key in (
                                    'attempted', 'committed', 'skipped',
                                    'failed', 'accounts', 'ledger_digest',
                                    'account_digest',
                                )
                            )
                            if mode in signatures and signatures[mode] != signature:
                                raise RuntimeError(
                                    f'{mode} outcome changed between repeats '
                                    f'at scale={scale}, duplicate_ratio={duplicate_ratio}.'
                                )
                            signatures[mode] = signature
                        _assert_comparable(pair['direct'], pair['staged'])
                    report(_summary(measured, scale, duplicate_ratio))
    finally:
        if output_file is not None:
            output_file.close()
        activity_file.close()

    return all_results


if __name__ == '__main__':
    main()
