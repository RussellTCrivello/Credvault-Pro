import hashlib
import io
import json
import sqlite3
import tempfile
import uuid
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from vault.management.commands.copy_from_sqlite import COPY_ORDER
from vault.models import (
    Account, ImportJob, ImportRecord, ImportRecoveryLog,
    ImportStageRecord, record_fingerprint,
)


def _quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


class SQLiteCopyWorkflowTests(TransactionTestCase):
    """Exercise the real all-or-nothing SQLite-to-PostgreSQL copy path."""

    def test_copies_import_audit_and_accepted_stage_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            sqlite_path = Path(directory) / "synthetic-vault.sqlite3"
            source = sqlite3.connect(sqlite_path)
            columns_by_table = {}
            unique_indexes = []
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT table_class.relname, index_class.relname,
                           ARRAY(
                               SELECT attribute.attname
                                 FROM unnest(index_details.indkey)
                                      WITH ORDINALITY AS key_column(attnum, ordinal)
                                 JOIN pg_attribute AS attribute
                                   ON attribute.attrelid = index_details.indrelid
                                  AND attribute.attnum = key_column.attnum
                                WHERE key_column.ordinal <= index_details.indnkeyatts
                                ORDER BY key_column.ordinal
                           )
                      FROM pg_index AS index_details
                      JOIN pg_class AS table_class
                        ON table_class.oid = index_details.indrelid
                      JOIN pg_class AS index_class
                        ON index_class.oid = index_details.indexrelid
                      JOIN pg_namespace AS namespace
                        ON namespace.oid = table_class.relnamespace
                     WHERE namespace.nspname = current_schema()
                       AND index_details.indisunique
                       AND index_details.indpred IS NULL
                       AND index_details.indexprs IS NULL
                """)
                unique_indexes = [
                    row for row in cursor.fetchall() if row[0] in COPY_ORDER
                ]
                for table in COPY_ORDER:
                    cursor.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = %s ORDER BY ordinal_position",
                        [table],
                    )
                    columns = [row[0] for row in cursor.fetchall()]
                    columns_by_table[table] = columns
                    source.execute(
                        f"CREATE TABLE {_quote(table)} ("
                        + ", ".join(f"{_quote(name)} TEXT" for name in columns)
                        + ")"
                    )
            for table, index_name, columns in unique_indexes:
                source.execute(
                    f"CREATE UNIQUE INDEX {_quote(index_name)} "
                    f"ON {_quote(table)} ("
                    + ", ".join(_quote(column) for column in columns)
                    + ")"
                )

            def insert(table, values):
                columns = columns_by_table[table]
                source.execute(
                    f"INSERT INTO {_quote(table)} ("
                    + ", ".join(_quote(name) for name in columns)
                    + ") VALUES ("
                    + ", ".join("?" for _ in columns)
                    + ")",
                    [values.get(name) for name in columns],
                )

            now = timezone.now().isoformat()
            source_job_id = uuid.uuid4()
            account_fp = record_fingerprint(1, "email", 1, None, 1, None)
            insert("vault_url", {
                "id": 1,
                "name": "https://sqlite-copy.example/login",
                "created_at": now,
                "updated_at": now,
            })
            insert("vault_email", {
                "id": 1,
                "name": "sqlite-copy@example.invalid",
                "created_at": now,
                "updated_at": now,
            })
            insert("vault_pass", {
                "id": 1,
                "name": "synthetic-committed-password",
                "created_at": now,
                "updated_at": now,
            })
            insert("vault_account", {
                "id": 1,
                "url_id": 1,
                "account_type": "email",
                "email_id": 1,
                "user_id": None,
                "password_id": 1,
                "ip_id": None,
                "fingerprint": account_fp,
                "created_at": now,
                "updated_at": now,
            })
            insert("vault_importjob", {
                "id": 1,
                "job_id": str(source_job_id),
                "source_name": "synthetic-upload.txt",
                "source_path": "",
                "source_size": 64,
                "source_hash": hashlib.sha256(b"synthetic-manifest").hexdigest(),
                "import_format": "auto",
                "options": json.dumps({
                    "source_kind": "upload", "source_replayable": False,
                }),
                "status": "processing",
                "stage": "importing",
                "created_at": now,
                "started_at": now,
                "last_activity_at": now,
                "completed_at": None,
                "total_discovered": 2,
                "total_attempted": 1,
                "total_committed": 1,
                "total_skipped": 0,
                "total_failed": 0,
                "last_checkpoint": json.dumps({"accounted_through": 1}),
                "recovery_attempts": 0,
                "last_error": "",
                "worker_id": "synthetic-old-worker",
                "lease_expires_at": now,
                "importer_version": "2.0",
            })
            insert("vault_importrecord", {
                "id": 1,
                "import_job_id": 1,
                "source_position": 1,
                "source_hash": hashlib.sha256(b"synthetic-line-one").hexdigest(),
                "source_line": "",
                "status": "committed",
                "account_id": 1,
                "account_fingerprint": account_fp,
                "error_message": "",
                "error_category": "",
                "retryable": 0,
                "processed_at": now,
                "idempotency_key": ImportRecord.compute_idempotency_key(
                    source_job_id, 1,
                ),
            })
            insert("vault_importrecord", {
                "id": 2,
                "import_job_id": 1,
                "source_position": 2,
                "source_hash": hashlib.sha256(b"synthetic-line-two").hexdigest(),
                "source_line": "",
                "status": "pending",
                "account_id": None,
                "account_fingerprint": "",
                "error_message": "",
                "error_category": "",
                "retryable": 0,
                "processed_at": None,
                "idempotency_key": ImportRecord.compute_idempotency_key(
                    source_job_id, 2,
                ),
            })
            insert("vault_import_stage_record", {
                "import_record_id": 2,
                "url": "https://sqlite-stage.example/login",
                "account_type": "user",
                "ident": "synthetic-stage-user",
                "secret": "synthetic-stage-password",
                "ip": None,
            })
            insert("vault_importrecoverylog", {
                "id": 1,
                "import_job_id": 1,
                "attempt_number": 1,
                "recovery_started_at": now,
                "recovery_completed_at": now,
                "records_committed": 1,
                "records_pending": 1,
                "records_failed": 0,
                "reconciliation_details": json.dumps({"staged_finalized": 0}),
                "outcome": "partial",
                "notes": "Synthetic recovery note.",
            })
            source.commit()
            source.close()

            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database()")
                target_database = cursor.fetchone()[0]
            output = io.StringIO()
            call_command(
                "copy_from_sqlite",
                str(sqlite_path),
                confirm_database=target_database,
                progress_every=0,
                stdout=output,
            )

            job = ImportJob.objects.get(job_id=source_job_id)
            self.assertEqual(job.source_name, "synthetic-upload.txt")
            self.assertEqual(job.last_checkpoint, {"accounted_through": 1})
            self.assertEqual(ImportRecord.objects.filter(import_job=job).count(), 2)
            committed = ImportRecord.objects.get(import_job=job, source_position=1)
            self.assertEqual(committed.account_id, Account.objects.get().pk)
            self.assertEqual(committed.account_fingerprint, account_fp)
            stage = ImportStageRecord.objects.get(import_record__import_job=job)
            self.assertEqual(stage.secret, "synthetic-stage-password")
            recovery = ImportRecoveryLog.objects.get(import_job=job)
            self.assertEqual(recovery.outcome, "partial")
            self.assertIn("vault_importrecord: 2 rows", output.getvalue())
            self.assertNotIn("synthetic-stage-password", output.getvalue())

            # A damaged idempotency key is detected after COPY; the whole target
            # transaction, not just the failing ledger insert, must roll back.
            with sqlite3.connect(sqlite_path) as damaged_source:
                damaged_source.execute(
                    "UPDATE vault_importrecord SET idempotency_key = 'bad-key' "
                    "WHERE id = 2"
                )
            call_command("flush", interactive=False, stdout=io.StringIO())
            with self.assertRaisesRegex(CommandError, "invalid idempotency key"):
                call_command(
                    "copy_from_sqlite",
                    str(sqlite_path),
                    confirm_database=target_database,
                    progress_every=0,
                    stdout=io.StringIO(),
                )
            self.assertEqual(ImportJob.objects.count(), 0)
            self.assertEqual(Account.objects.count(), 0)

            # Likewise, a stage payload cannot accompany a terminal ledger row.
            call_command("flush", interactive=False, stdout=io.StringIO())
            with sqlite3.connect(sqlite_path) as damaged_source:
                damaged_source.execute(
                    "UPDATE vault_importrecord "
                    "SET idempotency_key = ?, status = 'committed' WHERE id = 2",
                    [ImportRecord.compute_idempotency_key(source_job_id, 2)],
                )
            with self.assertRaisesRegex(CommandError, "staged row\\(s\\)"):
                call_command(
                    "copy_from_sqlite",
                    str(sqlite_path),
                    confirm_database=target_database,
                    progress_every=0,
                    stdout=io.StringIO(),
                )
            self.assertEqual(ImportJob.objects.count(), 0)
            self.assertEqual(Account.objects.count(), 0)

    def test_unknown_tables_and_columns_fail_closed(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            target_database = cursor.fetchone()[0]
        with tempfile.TemporaryDirectory() as directory:
            unknown_table_path = Path(directory) / "unknown-table.sqlite3"
            with sqlite3.connect(unknown_table_path) as source:
                source.execute('CREATE TABLE "vault_url" ("name" TEXT)')
                source.execute('CREATE TABLE "unreviewed_payload" ("value" TEXT)')
            with self.assertRaisesRegex(CommandError, "does not know how to preserve"):
                call_command(
                    "copy_from_sqlite",
                    str(unknown_table_path),
                    confirm_database=target_database,
                )

            custom_view_path = Path(directory) / "custom-view.sqlite3"
            with sqlite3.connect(custom_view_path) as source:
                source.execute('CREATE TABLE "vault_url" ("name" TEXT)')
                source.execute(
                    'CREATE VIEW "unreviewed_view" AS '
                    'SELECT "name" FROM "vault_url"'
                )
            with self.assertRaisesRegex(CommandError, "triggers/views"):
                call_command(
                    "copy_from_sqlite",
                    str(custom_view_path),
                    confirm_database=target_database,
                )

            custom_index_path = Path(directory) / "custom-index.sqlite3"
            with sqlite3.connect(custom_index_path) as source:
                source.execute(
                    'CREATE TABLE "vault_url" ('
                    '"id" TEXT, "name" TEXT, "created_at" TEXT, '
                    '"updated_at" TEXT)'
                )
                source.execute(
                    'CREATE INDEX "unreviewed_url_index" ON "vault_url" ("name")'
                )
            with self.assertRaisesRegex(CommandError, "indexes not represented"):
                call_command(
                    "copy_from_sqlite",
                    str(custom_index_path),
                    confirm_database=target_database,
                )

            changed_constraint_path = Path(directory) / "changed-constraint.sqlite3"
            with sqlite3.connect(changed_constraint_path) as source:
                source.execute(
                    'CREATE TABLE "vault_url" ('
                    '"id" TEXT, "name" TEXT, "created_at" TEXT, '
                    '"updated_at" TEXT)'
                )
                source.execute(
                    'CREATE UNIQUE INDEX "vault_url_name_lower_idx" '
                    'ON "vault_url" ("name")'
                )
            with self.assertRaisesRegex(CommandError, "uniqueness/partial semantics differ"):
                call_command(
                    "copy_from_sqlite",
                    str(changed_constraint_path),
                    confirm_database=target_database,
                )

            unknown_column_path = Path(directory) / "unknown-column.sqlite3"
            with sqlite3.connect(unknown_column_path) as source:
                source.execute(
                    'CREATE TABLE "vault_url" ('
                    '"id" TEXT, "name" TEXT, "created_at" TEXT, '
                    '"updated_at" TEXT, "unreviewed_column" TEXT)'
                )
            with self.assertRaisesRegex(CommandError, "source-only columns"):
                call_command(
                    "copy_from_sqlite",
                    str(unknown_column_path),
                    confirm_database=target_database,
                    progress_every=0,
                )
        self.assertEqual(Account.objects.count(), 0)
        self.assertEqual(ImportJob.objects.count(), 0)

    def test_requires_exact_target_database_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            sqlite_path = Path(directory) / "empty.sqlite3"
            sqlite3.connect(sqlite_path).close()
            with self.assertRaises(CommandError) as raised:
                call_command(
                    "copy_from_sqlite",
                    str(sqlite_path),
                    confirm_database="not-the-current-database",
                )
        self.assertIn("exact current PostgreSQL database name", str(raised.exception))
