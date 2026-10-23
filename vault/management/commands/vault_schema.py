"""Read-only inventory/check for the vault model and PostgreSQL schema."""

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from vault.import_app.schema_management import schema_inventory


class Command(BaseCommand):
    help = (
        "Inventory or validate the current vault schema without changing it. "
        "This command never creates, alters, or drops database objects."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "action", nargs="?", choices=("inventory", "check"),
            default="inventory",
        )
        parser.add_argument(
            "--format", choices=("text", "json"), default="text",
            help="Use JSON for a machine-readable catalog snapshot.",
        )

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            raise CommandError("Vault schema inventory requires PostgreSQL.")
        inventory = schema_inventory()
        if options["format"] == "json":
            self.stdout.write(json.dumps(inventory, indent=2, sort_keys=True))
        else:
            self.stdout.write(
                f"Database: {inventory['database']} "
                f"(PostgreSQL {inventory['postgresql_version']}); "
                f"role: {inventory['current_role']}"
            )
            self.stdout.write(
                f"Vault tables: {len(inventory['tables'])}; "
                f"columns: {len(inventory['columns'])}; "
                f"indexes: {len(inventory['indexes'])}; "
                f"constraints: {len(inventory['constraints'])}"
            )
            self.stdout.write(
                "Extensions: " + ", ".join(
                    f"{item['name']} {item['version']}"
                    for item in inventory["extensions"]
                )
            )
            self.stdout.write(
                "Custom finalizer: " + (
                    ", ".join(
                        f"{item['signature']} ({item['version'] or 'unversioned'})"
                        for item in inventory["custom_functions"]
                    ) or "not installed"
                )
            )
            migration = inventory["schema_migration"]
            self.stdout.write(
                "Versioned schema: " + (
                    f"{migration['migration_id']} at {migration['applied_at']} "
                    f"({migration['adoption_mode']})"
                    if migration else "not adopted/initialized"
                )
            )
            if inventory["model_schema_issues"]:
                self.stdout.write(self.style.WARNING("Model/schema differences:"))
                for issue in inventory["model_schema_issues"]:
                    self.stdout.write(f"  - {issue}")
            else:
                self.stdout.write(self.style.SUCCESS(
                    "Current vault model columns, primary keys, unique keys, "
                    "and foreign keys match the database."
                ))
            if inventory["schema_migration_issues"]:
                self.stdout.write(self.style.WARNING(
                    "Version-ledger differences:"
                ))
                for issue in inventory["schema_migration_issues"]:
                    self.stdout.write(f"  - {issue}")
            self.stdout.write(
                "Run with --format json to capture the full read-only catalog "
                "inventory, including index definitions and stage-table grants."
            )

        if options["action"] == "check" and (
            inventory["model_schema_issues"]
            or inventory["schema_migration_issues"]
        ):
            raise CommandError(
                "Vault schema or version ledger does not match the current "
                "contract; review the inventory and prepare an explicit "
                "backup-backed migration before installation."
            )
