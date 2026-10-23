"""Copy a reviewed SQLite snapshot into an empty PostgreSQL database.

The target schema must already be created with ``migrate`` and
``install_import_pipeline``. The command requires an exact target database-name
confirmation and wraps the whole data copy in one PostgreSQL transaction.

It copies the known vault tables (including import jobs, ledgers, accepted
staged payloads, and recovery logs) plus Django auth/session/admin data. It
preserves primary keys, resets sequences, converts SQLite date/time/boolean
storage, and verifies row counts, account fingerprints, import idempotency
keys, and staged-row consistency. Unknown source tables/columns, unsupported
triggers/views, unrecognized indexes, and unpreserved unique-index semantics
are refused instead of silently discarded. ``django_migrations`` is intentionally
not copied: the target's own migrations establish its schema history.

Take an offline, verified SQLite backup before conversion. The source is opened
read-only; failures roll back the PostgreSQL data copy and leave the source
untouched.
"""

import datetime as dt
import sqlite3
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from vault.import_app.importer import _FINGERPRINT_SQL

# Parents before children, so foreign keys can be checked in this order.
COPY_ORDER = [
    'django_content_type',
    'auth_permission',
    'auth_group',
    'auth_group_permissions',
    'auth_user',
    'auth_user_groups',
    'auth_user_user_permissions',
    'django_admin_log',
    'django_session',
    'vault_url',
    'vault_email',
    'vault_user',
    'vault_pass',
    'vault_ip',
    'vault_account',
    'vault_importjob',
    'vault_importrecord',
    'vault_import_stage_record',
    'vault_importrecoverylog',
]

CORE_VAULT_TABLES = {
    'vault_url', 'vault_email', 'vault_user', 'vault_pass', 'vault_ip',
    'vault_account',
}

# These are metadata, not application rows to migrate. In particular, custom
# PostgreSQL schema history is installed against the target schema separately.
INTENTIONALLY_OMITTED_TABLES = {
    'django_migrations', 'sqlite_sequence', 'sqlite_stat1', 'sqlite_stat3',
    'sqlite_stat4', 'vault_schema_migrations',
}

# Created automatically by `migrate` (one row per model permission and content
# type). The copy replaces them with the source rows, keeping the source ids.
AUTO_CREATED = ('django_content_type', 'auth_permission')

# Tables that must be empty in the target before the copy, so nothing is lost
# or merged silently.
MUST_BE_EMPTY = [
    t for t in COPY_ORDER if t not in AUTO_CREATED
]

UTC = dt.timezone.utc


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _to_datetime(value, aware):
    if value is None or isinstance(value, dt.datetime):
        return value
    parsed = dt.datetime.fromisoformat(str(value))
    if aware and parsed.tzinfo is None:
        # Django stores aware datetimes as naive UTC text in SQLite.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def converter_for(data_type):
    """Return a function that turns one SQLite value into a PostgreSQL value."""
    if data_type == 'boolean':
        return lambda v: None if v is None else bool(v)
    if data_type == 'timestamp with time zone':
        return lambda v: _to_datetime(v, aware=True)
    if data_type == 'timestamp without time zone':
        return lambda v: _to_datetime(v, aware=False)
    if data_type == 'date':
        return lambda v: None if v is None else dt.date.fromisoformat(str(v)[:10])
    return lambda v: v


class Command(BaseCommand):
    help = (
        'Copy a read-only SQLite snapshot into the configured empty PostgreSQL '
        'database. Run migrate and install_import_pipeline first, and confirm '
        'the exact target database name.'
    )

    def add_arguments(self, parser):
        parser.add_argument('sqlite_path', help='Path to the SQLite snapshot.')
        parser.add_argument(
            '--confirm-database', required=True,
            help='Must exactly match current_database(); prevents accidental targeting.',
        )
        parser.add_argument(
            '--progress-every', type=int, default=1000000,
            help='Print progress after this many rows (default: 1,000,000).',
        )

    def handle(self, *args, **options):
        if connection.vendor != 'postgresql':
            raise CommandError('SQLite conversion requires a PostgreSQL target.')
        if options['progress_every'] < 0:
            raise CommandError('--progress-every must be zero or a positive integer.')

        with connection.cursor() as cursor:
            cursor.execute('SELECT current_database()')
            database_name = cursor.fetchone()[0]
        if options['confirm_database'] != database_name:
            raise CommandError(
                'Refusing to copy data: pass --confirm-database with the exact '
                'current PostgreSQL database name.'
            )

        path = Path(options['sqlite_path']).expanduser()
        if not path.is_file():
            raise CommandError(f'SQLite file not found: {path}')
        path = path.resolve()
        source = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            source.execute('PRAGMA query_only=ON')
            # Pin one read snapshot so counts and rows cannot come from
            # different source states while a live SQLite application writes.
            source.execute('BEGIN')
            source_tables = {
                row[0] for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            unsupported_objects = sorted(
                f"{kind} {name}"
                for kind, name in source.execute(
                    "SELECT type, name FROM sqlite_master "
                    "WHERE type IN ('trigger', 'view') "
                    "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
                )
            )
            if unsupported_objects:
                raise CommandError(
                    'The SQLite file contains triggers/views not transferred '
                    'by this data migration: '
                    + ', '.join(unsupported_objects)
                    + '. Review and port them explicitly before copying.'
                )
            unknown_tables = sorted(
                source_tables
                - set(COPY_ORDER)
                - INTENTIONALLY_OMITTED_TABLES
            )
            if unknown_tables:
                raise CommandError(
                    'The SQLite file contains tables this migration does not '
                    'know how to preserve: ' + ', '.join(unknown_tables)
                    + '. Review and migrate them explicitly; nothing was copied.'
                )
            if not source_tables.intersection(CORE_VAULT_TABLES):
                raise CommandError(
                    'The SQLite file has no recognized vault application schema; '
                    'refusing to copy an unrelated database.'
                )
            if (
                'vault_importrecord' in source_tables
                and 'vault_importjob' not in source_tables
            ):
                raise CommandError(
                    'The SQLite ledger table has no import-job table; '
                    'review the source schema before copying.'
                )
            if (
                'vault_import_stage_record' in source_tables
                and 'vault_importrecord' not in source_tables
            ):
                raise CommandError(
                    'The SQLite staging table has no import ledger table; '
                    'review the source schema before copying.'
                )
            if (
                'vault_importrecoverylog' in source_tables
                and 'vault_importjob' not in source_tables
            ):
                raise CommandError(
                    'The SQLite recovery log has no import-job table; '
                    'review the source schema before copying.'
                )
            if 'vault_schema_migrations' in source_tables:
                self.stdout.write(self.style.WARNING(
                    'SQLite vault_schema_migrations metadata is not copied; '
                    'the PostgreSQL target must be versioned independently.'
                ))

            with connection.cursor() as cursor:
                target_tables = set(
                    connection.introspection.table_names(cursor)
                )
                cursor.execute("""
                    SELECT table_class.relname, index_class.relname,
                           index_details.indisunique,
                           index_details.indpred IS NOT NULL,
                           ARRAY(
                               SELECT COALESCE(attribute.attname, '<expression>')
                                 FROM unnest(index_details.indkey)
                                      WITH ORDINALITY AS key_column(attnum, ordinal)
                                 LEFT JOIN pg_attribute AS attribute
                                   ON attribute.attrelid = index_details.indrelid
                                  AND attribute.attnum = key_column.attnum
                                WHERE key_column.ordinal <= index_details.indnkeyatts
                                ORDER BY key_column.ordinal
                           ),
                           index_details.indexprs IS NOT NULL
                      FROM pg_index AS index_details
                      JOIN pg_class AS table_class
                        ON table_class.oid = index_details.indrelid
                      JOIN pg_class AS index_class
                        ON index_class.oid = index_details.indexrelid
                      JOIN pg_namespace AS namespace
                        ON namespace.oid = table_class.relnamespace
                     WHERE namespace.nspname = current_schema()
                """)
                target_indexes = {}
                target_unique_signatures = set()
                for table, name, unique, partial, columns, expression in cursor.fetchall():
                    target_indexes[(table, name)] = (bool(unique), bool(partial))
                    if unique and not partial and not expression:
                        target_unique_signatures.add((table, tuple(columns)))
            source_indexes = {}
            source_unique_signatures = set()
            unsupported_unique_indexes = []
            for table in source_tables:
                for details in source.execute(
                    f'PRAGMA index_list({_quote(table)})'
                ):
                    name, unique, partial = details[1], bool(details[2]), bool(details[4])
                    if not name.startswith('sqlite_'):
                        source_indexes[(table, name)] = (unique, partial)
                    if not unique:
                        continue
                    key_columns = [
                        row for row in source.execute(
                            f'PRAGMA index_xinfo({_quote(name)})'
                        ) if row[5]
                    ]
                    columns = tuple(row[2] for row in key_columns)
                    non_binary_collation = any(
                        (row[4] or '').upper() != 'BINARY'
                        for row in key_columns
                    )
                    if (
                        partial or any(column is None for column in columns)
                        or non_binary_collation
                    ):
                        unsupported_unique_indexes.append(f'{table}.{name}')
                    else:
                        source_unique_signatures.add((table, columns))
            unrecognized_indexes = sorted(
                key for key in source_indexes if key not in target_indexes
            )
            if unrecognized_indexes:
                raise CommandError(
                    'SQLite indexes not represented by the current PostgreSQL '
                    'schema require an explicit migration: '
                    + ', '.join(f'{table}.{name}' for table, name in unrecognized_indexes)
                    + '. Nothing was copied.'
                )
            mismatched_indexes = sorted(
                (table, name) for (table, name), flags in source_indexes.items()
                if target_indexes[(table, name)] != flags
            )
            if mismatched_indexes:
                raise CommandError(
                    'SQLite index uniqueness/partial semantics differ from the '
                    'PostgreSQL target: '
                    + ', '.join(f'{table}.{name}' for table, name in mismatched_indexes)
                    + '. Review the schema before copying.'
                )
            if unsupported_unique_indexes:
                raise CommandError(
                    'SQLite partial, expression, or non-BINARY unique indexes '
                    'need an explicit PostgreSQL equivalent: '
                    + ', '.join(sorted(unsupported_unique_indexes))
                    + '. Nothing was copied.'
                )
            missing_unique_indexes = sorted(
                signature for signature in source_unique_signatures
                if signature not in target_unique_signatures
            )
            if missing_unique_indexes:
                raise CommandError(
                    'SQLite unique constraints are not represented by the '
                    'PostgreSQL target: '
                    + ', '.join(
                        f'{table}({", ".join(columns)})'
                        for table, columns in missing_unique_indexes
                    )
                    + '. Nothing was copied.'
                )
            missing = [table for table in COPY_ORDER if table not in target_tables]
            if missing:
                raise CommandError(
                    'The PostgreSQL target has no schema for: '
                    + ', '.join(missing)
                    + '. Run `migrate` and install the import pipeline first.'
                )

            tables = [table for table in COPY_ORDER if table in source_tables]
            copied = {}
            with transaction.atomic():
                self._require_empty()
                self._clear_auto_created()
                with connection.cursor() as cursor:
                    for table in tables:
                        copied[table] = self._copy_table(
                            source, cursor, table, options['progress_every'],
                        )
                    missing_core = sorted(CORE_VAULT_TABLES - source_tables)
                    missing_django = sorted(set(AUTO_CREATED) - source_tables)
                    if missing_core or missing_django:
                        missing_tables = missing_core + missing_django
                        raise CommandError(
                            'The SQLite snapshot is incomplete; required '
                            'application tables are missing: '
                            + ', '.join(missing_tables)
                            + '. Nothing was copied.'
                        )
                    self._reset_sequences(cursor, tables)
                self._verify(source, tables)
                with connection.cursor() as cursor:
                    cursor.execute('ANALYZE')
        finally:
            if source.in_transaction:
                source.rollback()
            source.close()

        for table in tables:
            self.stdout.write(f'  {table}: {copied[table]:,} rows')
        self.stdout.write(self.style.SUCCESS(
            'Copy complete. Row counts, account fingerprints, import keys, '
            'and staged-row state verified.'
        ))

    # --- steps -------------------------------------------------------------

    def _require_empty(self):
        with connection.cursor() as cursor:
            for table in MUST_BE_EMPTY:
                cursor.execute(f'SELECT EXISTS (SELECT 1 FROM {_quote(table)})')
                if cursor.fetchone()[0]:
                    raise CommandError(
                        f'{table} already contains rows. Copy into an empty '
                        'database (create a new one and run `migrate`).'
                    )

    def _clear_auto_created(self):
        with connection.cursor() as cursor:
            cursor.execute(
                'TRUNCATE ' + ', '.join(_quote(t) for t in AUTO_CREATED)
                + ' CASCADE'
            )

    def _target_columns(self, cursor, table):
        cursor.execute(
            'SELECT column_name, data_type FROM information_schema.columns'
            ' WHERE table_schema = current_schema() AND table_name = %s',
            [table],
        )
        return dict(cursor.fetchall())

    def _required_columns(self, cursor, table):
        """Target columns that are NOT NULL with no default.

        A source row cannot be copied into one of these when the SQLite
        file has no column of the same name, so they must be present in
        the source; everything else (nullable columns, columns with a
        default such as the id sequences) may be absent.
        """
        cursor.execute(
            'SELECT column_name FROM information_schema.columns'
            ' WHERE table_schema = current_schema() AND table_name = %s'
            " AND is_nullable = 'NO' AND column_default IS NULL",
            [table],
        )
        return {row[0] for row in cursor.fetchall()}

    def _copy_table(self, source, cursor, table, progress_every):
        source_cols = [
            row[1] for row in source.execute(
                f'PRAGMA table_info({_quote(table)})'
            )
        ]
        target = self._target_columns(cursor, table)
        missing_required = sorted(
            c for c in self._required_columns(cursor, table)
            if c not in source_cols
        )
        if missing_required:
            raise CommandError(
                f'{table}: the SQLite file has no '
                f'{", ".join(missing_required)} column(s), which the '
                'current schema requires. This command supports the '
                "workspace's SQLite schema from the revision that added "
                'record fingerprints; the file appears to predate it. '
                'Nothing was saved.'
            )
        columns = [c for c in source_cols if c in target]
        unrecognized = [c for c in source_cols if c not in target]
        if unrecognized:
            raise CommandError(
                f'{table}: source-only columns would be lost: '
                + ', '.join(unrecognized)
                + '. Review and map them explicitly before copying.'
            )
        converters = [converter_for(target[c]) for c in columns]

        select = (
            'SELECT ' + ', '.join(_quote(c) for c in columns)
            + f' FROM {_quote(table)}'
        )
        copy_sql = (
            f'COPY {_quote(table)} ({", ".join(_quote(c) for c in columns)})'
            ' FROM STDIN'
        )
        self.stdout.write(f'Copying {table}...')
        count = 0
        with cursor.copy(copy_sql) as copy:
            for row in source.execute(select):
                copy.write_row([
                    convert(value) for convert, value in zip(converters, row)
                ])
                count += 1
                if progress_every and count % progress_every == 0:
                    self.stdout.write(f'  {table}: {count:,} rows...')
        return count

    def _reset_sequences(self, cursor, tables):
        for table in tables:
            cursor.execute(
                'SELECT EXISTS (SELECT 1 FROM information_schema.columns'
                ' WHERE table_schema = current_schema()'
                ' AND table_name = %s AND column_name = %s)',
                [table, 'id'],
            )
            if not cursor.fetchone()[0]:
                continue
            cursor.execute(
                f'SELECT setval(pg_get_serial_sequence(%s, %s),'
                f' COALESCE((SELECT MAX(id) FROM {_quote(table)}), 1),'
                f' (SELECT MAX(id) IS NOT NULL FROM {_quote(table)}))',
                [table, 'id'],
            )

    def _verify(self, source, tables):
        with connection.cursor() as cursor:
            for table in tables:
                cursor.execute(f'SELECT COUNT(*) FROM {_quote(table)}')
                target_count = cursor.fetchone()[0]
                source_count = source.execute(
                    f'SELECT COUNT(*) FROM {_quote(table)}'
                ).fetchone()[0]
                if target_count != source_count:
                    raise CommandError(
                        f'{table}: {source_count} rows in SQLite but '
                        f'{target_count} in PostgreSQL. Nothing was saved.'
                    )
            if 'vault_account' in tables:
                cursor.execute(
                    'SELECT COUNT(*) FROM vault_account AS k'
                    f' WHERE k.fingerprint IS DISTINCT FROM ({_FINGERPRINT_SQL})'
                )
                bad = cursor.fetchone()[0]
                if bad:
                    raise CommandError(
                        f'{bad} records have a fingerprint that does not match '
                        'their relations. Nothing was saved.'
                    )
            if 'vault_importrecord' in tables:
                cursor.execute("""
                    SELECT COUNT(*)
                      FROM vault_importrecord AS r
                      JOIN vault_importjob AS j ON j.id = r.import_job_id
                     WHERE r.idempotency_key IS DISTINCT FROM encode(
                         sha256(convert_to(
                             j.job_id::text || ':' || r.source_position::text,
                             'UTF8'
                         )),
                         'hex'
                     )
                """)
                bad = cursor.fetchone()[0]
                if bad:
                    raise CommandError(
                        f'{bad} import ledger row(s) have an invalid idempotency '
                        'key. Nothing was saved.'
                    )
            if 'vault_import_stage_record' in tables:
                cursor.execute("""
                    SELECT COUNT(*)
                      FROM vault_import_stage_record AS s
                      JOIN vault_importrecord AS r
                        ON r.id = s.import_record_id
                     WHERE r.status NOT IN ('pending', 'processing')
                       AND NOT (r.status = 'failed' AND r.retryable)
                """)
                invalid_stage_rows = cursor.fetchone()[0]
                if invalid_stage_rows:
                    raise CommandError(
                        f'{invalid_stage_rows} staged row(s) do not have a '
                        'recoverable ledger status. Nothing was saved.'
                    )
