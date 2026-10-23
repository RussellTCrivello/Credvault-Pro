"""The custom vault schema inventory is read-only and adoption is explicit."""

import io
import re

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.test import TransactionTestCase

from vault.management.commands.install_import_pipeline import (
    Command as InstallImportPipelineCommand,
)
from vault.import_app.schema_management import (
    SCHEMA_MIGRATION_TABLE,
    current_schema_migration,
    model_schema_issues,
    schema_contract_checksum,
    schema_inventory,
    schema_migration_table_issues,
)
from vault.models import ImportStageRecord, Url


class SchemaInventoryTests(TransactionTestCase):
    def test_inventory_matches_fresh_migrated_model_contract(self):
        self.assertEqual(model_schema_issues(), [])
        before = self._migration_table_exists()
        inventory = schema_inventory()
        after = self._migration_table_exists()
        self.assertEqual(before, after)
        self.assertEqual(inventory["model_schema_issues"], [])
        self.assertEqual(inventory["database"], connection.settings_dict["NAME"])
        self.assertIn("tables", inventory)
        self.assertIn("indexes", inventory)
        self.assertIn("constraints", inventory)
        self.assertIn("extensions", inventory)
        self.assertIn("stage_table_grants", inventory)
        self.assertEqual(
            inventory["schema_migration_issues"],
            ["versioned schema migration 0001_import_pipeline_v1 is not recorded"],
        )
        self.assertEqual(schema_migration_table_issues(), [])
        self.assertIsNone(current_schema_migration())

    def test_adoption_refuses_untracked_non_vault_django_tables(self):
        loader = MigrationLoader(connection)
        applied_vault_only = {
            key for key in loader.applied_migrations if key[0] == "vault"
        }
        with self.assertRaisesRegex(
            CommandError, "have no migration history",
        ):
            InstallImportPipelineCommand._assert_django_app_migration_history(
                loader, applied_vault_only,
            )

    def test_schema_contract_checksum_is_stable_sha256(self):
        checksum = schema_contract_checksum()
        self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", checksum))
        self.assertEqual(checksum, schema_contract_checksum())

    def test_inventory_and_check_report_stale_schema_checksum_read_only(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            database_name = cursor.fetchone()[0]
        call_command(
            "install_import_pipeline",
            initialize_new=True,
            confirm_database=database_name,
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE public.{SCHEMA_MIGRATION_TABLE} "
                    "SET checksum = repeat('0', 64) "
                    "WHERE migration_id = %s",
                    ("0001_import_pipeline_v1",),
                )
            stored_record = current_schema_migration()
            inventory = schema_inventory()
            self.assertEqual(inventory["schema_migration"], stored_record)
            self.assertTrue(any(
                "schema checksum mismatch" in issue
                for issue in inventory["schema_migration_issues"]
            ))
            with self.assertRaisesRegex(CommandError, "does not match"):
                call_command("vault_schema", "check")
            self.assertEqual(current_schema_migration(), stored_record)
        finally:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"DROP TABLE IF EXISTS public.{SCHEMA_MIGRATION_TABLE}"
                )

    def test_installer_refuses_without_exact_database_confirmation(self):
        before = self._migration_table_exists()
        with self.assertRaises(CommandError):
            call_command("install_import_pipeline")
        self.assertEqual(before, self._migration_table_exists())

    def test_new_database_mode_refuses_nonempty_vault_tables(self):
        Url.objects.create(name="https://synthetic-schema.example/login")
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            database_name = cursor.fetchone()[0]
        with self.assertRaisesRegex(CommandError, "vault tables are empty"):
            call_command(
                "install_import_pipeline",
                initialize_new=True,
                confirm_database=database_name,
            )
        self.assertFalse(self._migration_table_exists())
        self.assertEqual(Url.objects.count(), 1)

    def test_adoption_adds_only_missing_stage_table_and_preserves_data(self):
        saved_url = Url.objects.create(
            name="https://synthetic-adoption.example/login",
        )
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            database_name = cursor.fetchone()[0]

        with connection.schema_editor() as schema_editor:
            schema_editor.delete_model(ImportStageRecord)
        try:
            self.assertIn(
                "missing table public.vault_import_stage_record",
                model_schema_issues(),
            )
            self.assertEqual(
                model_schema_issues(allow_missing_stage_table=True), [],
            )
            # Simulate a legacy installation whose tables predate Django's
            # migration recorder. The installer may adopt the exact baseline,
            # but only after the full catalog preflight above.
            MigrationRecorder(connection).migration_qs.filter(
                app="vault", name="0001_initial_models",
            ).delete()

            output = io.StringIO()
            call_command(
                "install_import_pipeline",
                adopt_existing=True,
                backup_reference="synthetic-test-backup-reference",
                confirm_database=database_name,
                stdout=output,
            )

            self.assertTrue(Url.objects.filter(pk=saved_url.pk).exists())
            self.assertEqual(model_schema_issues(), [])
            self.assertTrue(MigrationRecorder(connection).migration_qs.filter(
                app="vault", name="0001_initial_models",
            ).exists())
            migration = current_schema_migration()
            self.assertEqual(
                migration["backup_reference"],
                "synthetic-test-backup-reference",
            )
            self.assertIn(
                "added the new private staging table", output.getvalue(),
            )
        finally:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DROP TABLE IF EXISTS public.vault_schema_migrations"
                )
                cursor.execute(
                    "SELECT to_regclass('public.vault_import_stage_record')"
                )
                stage_exists = cursor.fetchone()[0] is not None
            if not stage_exists:
                with connection.schema_editor() as schema_editor:
                    schema_editor.create_model(ImportStageRecord)

    def _migration_table_exists(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT to_regclass(%s)::oid",
                (f"public.{SCHEMA_MIGRATION_TABLE}",),
            )
            return cursor.fetchone()[0] is not None
