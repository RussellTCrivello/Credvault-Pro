from django.contrib import admin

from .models import (
    Account, Email, ImportJob, ImportRecord, ImportRecoveryLog,
    Ip, Pass, Url, User,
)


@admin.register(Url)
class UrlAdmin(admin.ModelAdmin):
    list_display = ['name', 'created_at', 'updated_at']
    search_fields = ['name']


@admin.register(Email)
class EmailAdmin(admin.ModelAdmin):
    list_display = ['name', 'created_at', 'updated_at']
    search_fields = ['name']


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ['name', 'created_at', 'updated_at']
    search_fields = ['name']


@admin.register(Pass)
class PassAdmin(admin.ModelAdmin):
    list_display = ['name', 'created_at', 'updated_at']
    search_fields = ['name']


@admin.register(Ip)
class IpAdmin(admin.ModelAdmin):
    list_display = ['name', 'created_at', 'updated_at']
    search_fields = ['name']


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'url', 'account_type', 'identifier', 'ip', 'created_at'
    ]
    list_filter = ['account_type']
    list_select_related = ['url', 'email', 'user', 'ip']
    autocomplete_fields = ['url', 'email', 'user', 'password', 'ip']
    search_fields = [
        'url__name', 'email__name', 'user__name',
        'password__name', 'ip__name'
    ]


class ReadOnlyImportAuditAdmin(admin.ModelAdmin):
    """Keep the recovery ledger inspectable but immutable in Django admin."""

    actions = None

    def get_readonly_fields(self, request, obj=None):
        excluded = set(self.get_exclude(request, obj) or ())
        return tuple(
            field.name for field in self.model._meta.fields
            if field.name not in excluded
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ImportJob)
class ImportJobAdmin(ReadOnlyImportAuditAdmin):
    list_display = [
        'job_id', 'source_name', 'status', 'stage',
        'total_committed', 'total_skipped', 'total_failed',
        'created_at', 'completed_at',
    ]
    list_filter = ['status', 'stage']
    search_fields = ['source_name', 'source_path']
    readonly_fields = [
        'job_id', 'created_at', 'started_at', 'last_activity_at',
        'completed_at', 'worker_id', 'lease_expires_at',
    ]
    # Legacy error text may contain database parameters/source values.
    exclude = ['last_error']


@admin.register(ImportRecord)
class ImportRecordAdmin(ReadOnlyImportAuditAdmin):
    list_display = [
        'id', 'import_job', 'source_position', 'status',
        'account', 'processed_at',
    ]
    list_filter = ['status']
    # Legacy source lines and error messages may contain vault secrets.
    # New imports store only hashes and redacted error categories.
    exclude = ['source_line', 'error_message']
    search_fields = ['error_category']
    readonly_fields = ['idempotency_key', 'account_fingerprint']


@admin.register(ImportRecoveryLog)
class ImportRecoveryLogAdmin(ReadOnlyImportAuditAdmin):
    list_display = [
        'id', 'import_job', 'attempt_number', 'outcome',
        'recovery_started_at', 'recovery_completed_at',
    ]
    list_filter = ['outcome']
    # Legacy operator notes may contain unredacted error/source data.
    exclude = ['notes']
