"""Direct and staged modes preserve the same logical durable outcomes."""

import hashlib
import os
import shutil
import tempfile

from django.db import connection
from django.db.models import F
from django.test import TransactionTestCase, override_settings

from vault.import_app.import_engine import durable_import_file
from vault.models import (
    Account, ImportRecord, ImportStageRecord, record_fingerprint,
)


class ImportModeEquivalenceTests(TransactionTestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="credvault-mode-")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.path = os.path.join(self.directory, "synthetic-import.txt")
        # Deliberately put the URL/user values in reverse lexical order. Direct
        # insertion sorts references within three-row source batches, while
        # staged finalization below sees one source row at a time. Their
        # surrogate IDs (and therefore ID-derived fingerprints) can differ.
        self.lines = [
            "https://three.example/login|carol|Synthetic-Pw-3|203.0.113.13\n",
            "https://two.example/login|bob@example.test|Synthetic-Pw-2|203.0.113.12\n",
            "https://one.example/login|alice|Synthetic-Pw-1|203.0.113.11\n",
            "https://three.example/login|carol|Synthetic-Pw-3|203.0.113.13\n",
            "not-a-credential-line\n",
        ]
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.writelines(self.lines)

    @override_settings(VAULT_IMPORT_FINALIZATION_BATCH_ROWS=1)
    def test_direct_and_staged_modes_have_equivalent_logical_outcomes(self):
        outcomes = {}
        for mode in ("direct", "staged"):
            self._clear_vault()
            job = durable_import_file(
                self.path,
                batch_rows=3,
                bulk_finalize=(mode == "staged"),
            )
            job.refresh_from_db()
            self.assertEqual(job.status, "completed")
            self.assertEqual(job.total_attempted, len(self.lines))
            self.assertEqual(job.total_committed, 3)
            self.assertEqual(job.total_skipped, 2)
            self.assertEqual(job.total_failed, 0)
            self.assertEqual(job.options.get("import_mode"), mode)
            self.assertEqual(
                ImportStageRecord.objects.filter(
                    import_record__import_job=job,
                ).count(),
                0,
            )

            # Fingerprints are defined by the stored FK IDs. Validate their
            # formula in each mode instead of comparing hashes whose input IDs
            # may legitimately differ because insertion order changed.
            account_rows = list(Account.objects.values_list(
                "url_id", "account_type", "email_id", "user_id",
                "password_id", "ip_id", "fingerprint",
            ))
            for url_id, account_type, email_id, user_id, password_id, ip_id, fingerprint in account_rows:
                self.assertEqual(
                    fingerprint,
                    record_fingerprint(
                        url_id, account_type, email_id, user_id,
                        password_id, ip_id,
                    ),
                )
            self.assertFalse(
                ImportRecord.objects.filter(
                    import_job=job,
                    account__isnull=False,
                ).exclude(
                    account_fingerprint=F("account__fingerprint"),
                ).exists(),
            )

            natural_account_rows = Account.objects.order_by(
                "url__name", "account_type", "email__name", "user__name",
                "password__name", "ip__name",
            ).values_list(
                "url__name", "account_type", "email__name", "user__name",
                "password__name", "ip__name",
            )
            natural_ledger_rows = ImportRecord.objects.filter(
                import_job=job,
            ).order_by("source_position").values_list(
                "source_position", "source_hash", "status", "error_category",
                "retryable", "account__url__name", "account__account_type",
                "account__email__name", "account__user__name",
                "account__password__name", "account__ip__name",
            )
            outcomes[mode] = {
                "ledger": self._digest_rows(natural_ledger_rows),
                "accounts": self._digest_rows(natural_account_rows),
                "counts": (
                    job.total_attempted,
                    job.total_committed,
                    job.total_skipped,
                    job.total_failed,
                    Account.objects.count(),
                ),
            }

        self.assertEqual(outcomes["direct"], outcomes["staged"])

    @staticmethod
    def _digest_rows(rows):
        digest = hashlib.sha256()
        for row in rows:
            digest.update(repr(tuple(row)).encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()

    @staticmethod
    def _clear_vault():
        # This method only targets Django's synthetic, isolated test database.
        with connection.cursor() as cursor:
            cursor.execute("""
                TRUNCATE TABLE
                    public.vault_import_stage_record,
                    public.vault_importrecord,
                    public.vault_importrecoverylog,
                    public.vault_importjob,
                    public.vault_account,
                    public.vault_email,
                    public.vault_user,
                    public.vault_pass,
                    public.vault_url,
                    public.vault_ip
                RESTART IDENTITY CASCADE
            """)
