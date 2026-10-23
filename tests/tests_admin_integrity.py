from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from vault.models import ImportJob, ImportRecord, ImportRecoveryLog


class ImportAuditAdminTests(TestCase):
    def test_import_audit_models_are_read_only_in_django_admin(self):
        request = RequestFactory().get("/admin/")
        request.user = get_user_model().objects.create_superuser(
            username="audit-admin",
            email="audit-admin@example.invalid",
            password="synthetic-test-password",
        )

        for model in (ImportJob, ImportRecord, ImportRecoveryLog):
            with self.subTest(model=model.__name__):
                model_admin = site._registry[model]
                self.assertFalse(model_admin.has_add_permission(request))
                self.assertFalse(model_admin.has_change_permission(request))
                self.assertFalse(model_admin.has_delete_permission(request))
                self.assertTrue(model_admin.has_view_permission(request))

    def test_read_only_admin_fields_exclude_legacy_secret_bearing_text(self):
        request = RequestFactory().get("/admin/")
        sensitive_fields = {
            ImportJob: {"last_error"},
            ImportRecord: {"source_line", "error_message"},
            ImportRecoveryLog: {"notes"},
        }
        for model, excluded in sensitive_fields.items():
            with self.subTest(model=model.__name__):
                model_admin = site._registry[model]
                readonly = set(model_admin.get_readonly_fields(request))
                self.assertTrue(excluded.isdisjoint(readonly))
                self.assertEqual(
                    excluded,
                    set(model_admin.get_exclude(request)),
                )
        self.assertIn(
            "source_hash",
            set(site._registry[ImportRecord].get_readonly_fields(request)),
        )
