#!/usr/bin/env python3
"""Compare the legacy durable flush and set-based finalizer on synthetic data.

Destructive: resets the configured database's import/account tables before
*each* run. It refuses to run unless the database is named ``credvault_bench``,
the PostgreSQL host is loopback, and ``--reset-disposable-db`` is supplied.
Never point this at production.
"""

import argparse
import decimal
import json
import logging
import os
import resource
import sys
import tempfile
import threading
import time
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'admin_LogIntel.settings')

import django  # noqa: E402

django.setup()

from django.db import connection  # noqa: E402

from vault.import_app.import_engine import (  # noqa: E402
    durable_import_path,
    ensure_import_finalizer_installed,
)
from vault.models import Account  # noqa: E402


DISPOSABLE_DATABASE = 'credvault_bench'


class _MetricCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


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


def _synthetic_lines(count):
    """Yield deterministic repeated pairs, shared references, and bad rows."""
    for index in range(count):
        if index % 997 == 996:
            yield b'this synthetic line is intentionally invalid\n'
        else:
            account_index = index // 2
            line = (
                f'https://bench-{account_index % 500}.example/login|'
                f'synthetic-user-{account_index}|'
                f'synthetic-secret-{account_index % 5000}|'
                f'192.0.2.{account_index % 254 + 1}\n'
            ).encode('ascii')
            yield line


def _write_source(path, count):
    with open(path, 'wb') as source:
        for line in _synthetic_lines(count):
            source.write(line)


def _run_one(source_path, line_count, mode, batch_rows):
    _reset_benchmark_tables()
    # Isolate the measured PostgreSQL backend from the reset/truncate work.
    connection.close()
    before_db = _database_snapshot()
    cpu_before = resource.getrusage(resource.RUSAGE_SELF)
    python_rss_before = _PythonMemorySampler._current_rss()
    memory = _PythonMemorySampler()
    capture = _MetricCapture()
    metrics_logger = logging.getLogger('vault.import.metrics')
    old_level = metrics_logger.level
    metrics_logger.setLevel(logging.INFO)
    metrics_logger.addHandler(capture)
    memory.start()
    started = time.perf_counter()
    try:
        job = durable_import_path(
            source_path,
            ext='*',
            workers=1,
            batch_rows=batch_rows,
            bulk_finalize=(mode == 'optimized'),
        )
    finally:
        elapsed = time.perf_counter() - started
        memory.stop()
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
        metrics_logger.setLevel(old_level)
        connection.close()

    if job.status not in ('completed', 'completed_with_errors'):
        raise RuntimeError(
            f'{mode} run ended in status {job.status!r}; refusing to report it.'
        )

    db_fields = (
        'blks_read', 'blks_hit', 'tup_inserted', 'tup_updated',
        'temp_files', 'temp_bytes',
    )
    database_delta = {
        f'pg_database_{name}': _delta(before, after)
        for name, before, after in zip(db_fields, before_db['database'], after_db['database'])
    }
    io_fields = (
        'reads', 'read_time_ms', 'writes', 'write_time_ms',
        'writebacks', 'writeback_time_ms', 'extends', 'extend_time_ms',
        'fsyncs', 'fsync_time_ms',
    )
    pg_io_delta = {
        f'pg_stat_io_client_{name}': _delta(before, after)
        for name, before, after in zip(io_fields, before_db['pg_io'], after_db['pg_io'])
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
        'mode': mode,
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
        **database_delta,
        **pg_io_delta,
        **{
            f'import_{name}': summary.get(name)
            for name in import_metric_fields
        },
    }
    return result


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
        '--batch-rows', type=int, default=None,
        help='Optional shared batch size for both implementations.',
    )
    args = parser.parse_args()

    # Ensure a per-run summary is available for the stage/finalizer breakdown,
    # while keeping periodic progress records out of short benchmark runs.
    os.environ['VAULT_IMPORT_METRICS_INTERVAL'] = '3600'

    database_name = str(connection.settings_dict.get('NAME', ''))
    if database_name != DISPOSABLE_DATABASE:
        parser.error(
            f'refusing to run against database {database_name!r}; '
            f'configure {DISPOSABLE_DATABASE!r} only.'
        )
    database_host = str(connection.settings_dict.get('HOST', '')).strip().lower()
    if database_host not in ('', 'localhost', '127.0.0.1', '::1'):
        parser.error('the destructive benchmark is restricted to a loopback PostgreSQL host.')
    if not args.reset_disposable_db:
        parser.error('pass --reset-disposable-db to confirm this destructive benchmark.')
    if connection.vendor != 'postgresql':
        parser.error('the importer benchmark requires PostgreSQL.')
    if any(scale < 1 for scale in args.scales):
        parser.error('all scales must be positive.')

    ensure_import_finalizer_installed()
    results = []
    with tempfile.TemporaryDirectory(prefix='credvault-bench-') as directory:
        for scale in args.scales:
            source_path = Path(directory) / f'synthetic-{scale}.txt'
            _write_source(source_path, scale)
            pair = []
            for mode in ('legacy', 'optimized'):
                result = _run_one(
                    str(source_path), scale, mode, args.batch_rows,
                )
                result['scale'] = scale
                pair.append(result)
                results.append(result)
                print(json.dumps(result, sort_keys=True), flush=True)

            comparable = ('attempted', 'committed', 'skipped', 'failed', 'accounts')
            if any(pair[0][key] != pair[1][key] for key in comparable):
                raise RuntimeError(
                    f'legacy/optimized outcome mismatch at scale {scale}; '
                    'benchmark results are not comparable.'
                )
            legacy_seconds = pair[0]['elapsed_seconds']
            optimized_seconds = pair[1]['elapsed_seconds']
            speedup = (
                legacy_seconds / optimized_seconds
                if optimized_seconds else None
            )
            print(json.dumps({
                'scale': scale,
                'legacy_seconds': legacy_seconds,
                'optimized_seconds': optimized_seconds,
                'speedup': round(speedup, 3) if speedup is not None else None,
            }, sort_keys=True), flush=True)

    return results


if __name__ == '__main__':
    main()
