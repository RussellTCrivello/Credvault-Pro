"""Install the PostgreSQL bulk-import finalizer and staging-table grants."""

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from vault.import_app.import_engine import ensure_import_finalizer_installed


class Command(BaseCommand):
    help = (
        'Install or upgrade the PostgreSQL set-based bulk-import finalizer '
        'and restrict access to the credential staging table.'
    )

    def handle(self, *args, **options):
        if connection.vendor != 'postgresql':
            raise CommandError('The bulk import pipeline requires PostgreSQL.')
        try:
            ensure_import_finalizer_installed(force=True)
        except Exception as exc:
            # Driver errors may contain SQL details; keep the CLI output free
            # of parameters and credential-bearing values.
            raise CommandError(
                'Could not install the bulk-import finalizer '
                f'({type(exc).__name__}). Run this command as the database '
                'schema owner after `migrate --run-syncdb`.'
            ) from exc
        self.stdout.write(self.style.SUCCESS(
            'PostgreSQL bulk-import finalizer installed; staging access is '
            'limited to the configured database role.'
        ))
