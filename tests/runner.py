"""Ensure PostgreSQL integration tests start with their declared extension."""

from django.db import connections
from django.test.runner import DiscoverRunner


class ImportPipelineTestRunner(DiscoverRunner):
    """Provision the trusted ``pg_trgm`` extension in freshly-created test DBs."""

    def setup_databases(self, **kwargs):
        old_config = super().setup_databases(**kwargs)
        for connection in connections.all():
            if connection.vendor != 'postgresql':
                continue
            with connection.cursor() as cursor:
                cursor.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')
        return old_config
