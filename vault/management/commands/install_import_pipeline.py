"""Install the versioned PostgreSQL import schema after an explicit review."""

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from vault.import_app.import_engine import ensure_import_finalizer_installed
from vault.import_app.importer import (
    ensure_search_indexes,
    validate_search_indexes,
)
from vault.import_app.schema_management import (
    SCHEMA_MIGRATION_ID,
    create_schema_migration_table,
    latest_schema_migration,
    model_schema_issues,
    record_schema_migration,
    schema_contract_checksum,
    schema_migration_table_issues,
    validate_private_stage_permissions,
)


class Command(BaseCommand):
    help = (
        "Install the versioned PostgreSQL import objects. A new database "
        "must be empty; an existing database requires an explicit backup "
        "reference and typed database-name confirmation."
    )

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--initialize-new", action="store_true",
            help="Initialize the contract only when all vault tables are empty.",
        )
        mode.add_argument(
            "--adopt-existing", action="store_true",
            help="Adopt a reviewed existing schema after taking a backup.",
        )
        parser.add_argument(
            "--confirm-database", default="",
            help="Must exactly match current_database(); prevents accidental targeting.",
        )
        parser.add_argument(
            "--backup-reference", default="",
            help="External backup identifier/location required for existing data.",
        )

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            raise CommandError("The import pipeline requires PostgreSQL.")

        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            database_name = cursor.fetchone()[0]
        if options["confirm_database"] != database_name:
            raise CommandError(
                "Refusing schema changes: pass --confirm-database with the "
                "exact current database name."
            )

        migration_table_issues = schema_migration_table_issues()
        if migration_table_issues:
            preview = "; ".join(migration_table_issues[:12])
            raise CommandError(
                "The custom schema version ledger is malformed; no schema "
                "changes were applied. Review `vault_schema inventory` and "
                f"prepare an explicit repair. Differences: {preview}"
            )

        existing = latest_schema_migration()
        expected_checksum = schema_contract_checksum()
        if (
            existing
            and existing["migration_id"] == SCHEMA_MIGRATION_ID
            and existing["checksum"] != expected_checksum
        ):
            raise CommandError(
                "The installed schema migration checksum differs from this "
                "code version. Do not overwrite it; create and review a new "
                "versioned schema migration."
            )

        explicit_new = options["initialize_new"]
        explicit_existing = options["adopt_existing"]
        if existing is None and not (explicit_new or explicit_existing):
            raise CommandError(
                "No recognized vault schema version is recorded. First run "
                "`python manage.py vault_schema inventory --format json`, "
                "review the schema, and take a backup. Then use "
                "--initialize-new for an empty new database, or "
                "--adopt-existing --backup-reference <backup-id> for an "
                "existing installation. Existing-database adoption records "
                "the Django baseline only after exact catalog checks."
            )
        if (
            existing
            and existing["migration_id"] != SCHEMA_MIGRATION_ID
            and not explicit_existing
        ):
            raise CommandError(
                f"The database is at schema version {existing['migration_id']}; "
                f"this code expects {SCHEMA_MIGRATION_ID}. Review the forward "
                "migration, take a verified backup, and run "
                "--adopt-existing --backup-reference <backup-id>."
            )
        if explicit_new and existing is not None:
            raise CommandError(
                "The vault schema is already versioned; --initialize-new is "
                "only for a first install."
            )
        if explicit_existing and not options["backup_reference"].strip():
            raise CommandError(
                "--adopt-existing requires --backup-reference with the "
                "external backup identifier or location."
            )
        if options["backup_reference"] and len(options["backup_reference"]) > 512:
            raise CommandError("--backup-reference must be at most 512 characters.")

        issues = model_schema_issues(
            allow_missing_stage_table=explicit_existing,
        )
        if issues:
            preview = "; ".join(issues[:12])
            if len(issues) > 12:
                preview += f"; and {len(issues) - 12} more"
            raise CommandError(
                "The database does not match the current vault model contract; "
                "no schema changes were applied. Review `vault_schema inventory` "
                f"and prepare an explicit, backed-up migration. Differences: {preview}"
            )

        adoption_mode = (
            "initialize-new" if explicit_new else "adopt-existing"
        )
        if explicit_new:
            nonempty = self._nonempty_vault_tables()
            if nonempty:
                raise CommandError(
                    "--initialize-new is only allowed when vault tables are "
                    "empty; non-empty tables: " + ", ".join(nonempty)
                )
        elif explicit_existing and not options["backup_reference"].strip():
            raise CommandError("Existing databases require a backup reference.")

        if existing is not None and not explicit_existing:
            # The no-flag path is verification-only. Never repair or rewrite
            # production objects implicitly during an application deployment.
            try:
                ensure_import_finalizer_installed(
                    force=False, allow_install=False,
                )
                validate_search_indexes()
                validate_private_stage_permissions()
            except Exception as exc:
                raise CommandError(
                    "The versioned import schema is incomplete or invalid "
                    f"({type(exc).__name__}); inspect the read-only inventory "
                    "and use an explicit backup-backed repair plan."
                ) from exc
            self.stdout.write(self.style.SUCCESS(
                f"Verified {SCHEMA_MIGRATION_ID} on {database_name}; no DDL was run."
            ))
            return

        # A fresh install is never inferred from a database name. The explicit
        # flag and empty-table check are required even for a database called
        # "new" or "test". Keep a session-level advisory lock for the whole
        # sequence, including CREATE INDEX CONCURRENTLY, which cannot run in a
        # surrounding transaction.
        schema_lock_acquired = False
        stage_table_created = False
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
                    ("credvault:versioned-schema-install",),
                )
            schema_lock_acquired = True

            locked_migration_issues = schema_migration_table_issues()
            if locked_migration_issues:
                preview = "; ".join(locked_migration_issues[:12])
                raise CommandError(
                    "The custom schema version ledger changed while this "
                    "installer was waiting; no further DDL was applied. "
                    f"Differences: {preview}"
                )
            locked_existing = latest_schema_migration()
            if (
                locked_existing
                and locked_existing["migration_id"] == SCHEMA_MIGRATION_ID
                and locked_existing["checksum"] != expected_checksum
            ):
                raise CommandError(
                    "The installed schema migration checksum differs from this "
                    "code version. Do not overwrite it; create and review a new "
                    "versioned schema migration."
                )
            if explicit_new and locked_existing:
                raise CommandError(
                    "Another installer recorded the schema while this process "
                    "was waiting; --initialize-new is no longer applicable."
                )
            # Re-check new-database emptiness after obtaining the serialized
            # installer lock, closing the race with another setup invocation.
            if explicit_new:
                nonempty = self._nonempty_vault_tables()
                if nonempty:
                    raise CommandError(
                        "--initialize-new is only allowed when vault tables are "
                        "empty; non-empty tables: " + ", ".join(nonempty)
                    )

            locked_schema_issues = model_schema_issues(
                allow_missing_stage_table=explicit_existing,
            )
            if locked_schema_issues:
                preview = "; ".join(locked_schema_issues[:12])
                raise CommandError(
                    "The vault model schema changed while this installer was "
                    "waiting; no schema changes were applied. "
                    f"Differences: {preview}"
                )
            if explicit_existing:
                stage_table_created = self._create_missing_stage_table()
                post_create_issues = model_schema_issues()
                if post_create_issues:
                    preview = "; ".join(post_create_issues[:12])
                    raise CommandError(
                        "The additive staging-table transition did not match "
                        "the current model contract; inspect the inventory "
                        f"before retrying. Differences: {preview}"
                    )
                self._adopt_django_initial_migration()

            with connection.cursor() as cursor:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

            ensure_import_finalizer_installed(
                force=True, allow_install=True,
            )

            # Online index builds may need longer than the normal request query
            # timeout on a large existing vault. Use a finite DDL-specific cap,
            # restoring the connection's ordinary timeout afterwards.
            ddl_timeout_ms = getattr(
                settings, "CREDVAULT_SCHEMA_DDL_TIMEOUT_MS", 3_600_000,
            )
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('statement_timeout')")
                old_statement_timeout = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, false)",
                    (f"{ddl_timeout_ms}ms",),
                )
            try:
                ensure_search_indexes(concurrently=True)
                validate_search_indexes()
                validate_private_stage_permissions()
            finally:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('statement_timeout', %s, false)",
                        (old_statement_timeout,),
                    )

            with transaction.atomic():
                create_schema_migration_table()
                record_schema_migration(
                    adoption_mode=adoption_mode,
                    backup_reference=(
                        options["backup_reference"].strip()
                        if explicit_existing else ""
                    ),
                )
        except CommandError:
            raise
        except Exception as exc:
            # Do not echo SQL or driver parameters; existing records are not
            # modified by this additive installer. Re-run only after inspection.
            raise CommandError(
                "Could not install or validate the versioned PostgreSQL import "
                f"schema ({type(exc).__name__}). Existing vault data was not "
                "truncated or rewritten; inspect the inventory and database "
                "logs before retrying."
            ) from exc
        finally:
            if schema_lock_acquired:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                        ("credvault:versioned-schema-install",),
                    )

        mode_label = (
            "initialized new empty vault tables"
            if explicit_new else "adopted the reviewed existing schema"
        )
        stage_action = (
            " added the new private staging table and"
            if stage_table_created else ""
        )
        self.stdout.write(self.style.SUCCESS(
            f"Successfully {mode_label} on {database_name}; recorded "
            f"{SCHEMA_MIGRATION_ID},{stage_action} installed "
            "pg_trgm/finalizer/indexes, and validated the private staging grants."
        ))

    @staticmethod
    def _assert_django_app_migration_history(loader, applied):
        """Refuse to strand existing non-vault Django tables without history."""
        from django.apps import apps

        with connection.cursor() as cursor:
            existing_tables = set(connection.introspection.table_names(cursor))

        for app_label in sorted(loader.migrated_apps - {"vault"}):
            if not any(key[0] == app_label for key in loader.disk_migrations):
                continue
            app_config = apps.get_app_config(app_label)
            existing_app_tables = {
                model._meta.db_table
                for model in app_config.get_models()
                if model._meta.managed
                and model._meta.db_table in existing_tables
            }
            has_history = any(key[0] == app_label for key in applied)
            if existing_app_tables and not has_history:
                raise CommandError(
                    f"Existing Django tables for app '{app_label}' have no "
                    "migration history. Vault adoption will not invent or "
                    "fake history for other apps; restore their migration "
                    "records or prepare a separate reviewed adoption first."
                )

    @staticmethod
    def _adopt_django_initial_migration():
        """Soft-apply only the baseline migration after exact schema review.

        ``--fake-initial`` is never exposed as the install procedure itself:
        ``model_schema_issues()`` has already verified every modeled column,
        key, index, constraint, and foreign key, and the missing staging table
        (if any) has been added and rechecked. Future unapplied vault migrations
        require their own explicit operator plan and are not run here.
        """
        from django.db.migrations.loader import MigrationLoader
        from django.db.migrations.recorder import MigrationRecorder

        baseline = ("vault", "0001_initial_models")
        loader = MigrationLoader(connection)
        if baseline not in loader.disk_migrations:
            raise CommandError(
                "The reviewed vault baseline migration is missing; refusing "
                "to mark any schema history as applied."
            )

        recorder = MigrationRecorder(connection)
        applied = recorder.applied_migrations()
        Command._assert_django_app_migration_history(loader, applied)
        known_vault_migrations = {
            key for key in loader.disk_migrations if key[0] == "vault"
        }
        unknown_applied = sorted(
            key for key in applied
            if key[0] == "vault" and key not in known_vault_migrations
        )
        if unknown_applied:
            raise CommandError(
                "The database records unknown vault migrations; inspect "
                "migration history before adopting this version."
            )
        later_unapplied = sorted(
            key for key in known_vault_migrations
            if key != baseline and key not in applied
        )
        if later_unapplied:
            raise CommandError(
                "Unapplied vault migrations require their own reviewed "
                "backup-backed upgrade procedure; the baseline installer "
                "will not run them implicitly."
            )

        if baseline not in applied:
            call_command(
                "migrate", "vault", "0001_initial_models",
                fake_initial=True,
                interactive=False,
                verbosity=0,
            )
        if baseline not in MigrationRecorder(connection).applied_migrations():
            raise CommandError(
                "Django did not record the verified vault baseline migration."
            )

    @staticmethod
    def _create_missing_stage_table():
        """Add only the new payload table during explicit existing-schema adoption."""
        from vault.models import ImportStageRecord

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT to_regclass('public.vault_import_stage_record')"
            )
            if cursor.fetchone()[0] is not None:
                return False

        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL search_path = public")
                with connection.schema_editor() as schema_editor:
                    schema_editor.create_model(ImportStageRecord)
        except Exception as exc:
            raise CommandError(
                "Could not add the missing private staging table; no existing "
                "vault columns or rows were changed. Inspect the database "
                "inventory before retrying."
            ) from exc
        return True

    @staticmethod
    def _nonempty_vault_tables():
        from django.apps import apps

        nonempty = []
        with connection.cursor() as cursor:
            for model in apps.get_app_config("vault").get_models():
                table = connection.ops.quote_name(model._meta.db_table)
                cursor.execute(
                    f"SELECT EXISTS(SELECT 1 FROM public.{table} LIMIT 1)"
                )
                if cursor.fetchone()[0]:
                    nonempty.append(model._meta.db_table)
        return nonempty
