"""Read-only schema inventory and version metadata for the vault app.

Django migrations define the model schema. PostgreSQL-specific import objects
and their indexes, grants, and custom SQL are tracked separately in an
immutable version ledger; existing installations must adopt a reviewed
baseline only after a verified backup and exact catalog check.
"""

import hashlib
import json
from pathlib import Path

from django.apps import apps
from django.db import connection


SCHEMA_MIGRATION_TABLE = "vault_schema_migrations"
# Append a new immutable ID for each reviewed custom-SQL/index/grant transition;
# never replace or rewrite an ID already recorded by an installation.
SCHEMA_MIGRATION_ID = "0001_import_pipeline_v1"
SCHEMA_MIGRATION_IDS = (SCHEMA_MIGRATION_ID,)
IMPORT_FINALIZER_SIGNATURE = (
    "vault_finalize_import_batch(bigint,text,integer,integer)"
)
_SCHEMA_MIGRATION_TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS public.{SCHEMA_MIGRATION_TABLE} (
    migration_id varchar(64) PRIMARY KEY,
    checksum char(64) NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    applied_by text NOT NULL,
    adoption_mode varchar(32) NOT NULL,
    backup_reference text NOT NULL DEFAULT ''
)
"""


def _normalize_type(value):
    value = " ".join(str(value or "").lower().split())
    return (
        value.replace("character varying", "varchar")
        .replace("character(", "char(")
        .replace("timestamp with time zone", "timestamptz")
        .replace("timestamp without time zone", "timestamp")
    )


def model_schema_issues(*, allow_missing_stage_table=False):
    """Compare existing public vault columns/keys with the current model contract.

    This is validation-only: it never alters or drops existing objects. During
    an explicitly backup-backed ``--adopt-existing`` install, the one new,
    additive staging table may be missing; every other table and every object
    on an existing table must match before the installer creates that table.
    """
    issues = []
    app_config = apps.get_app_config("vault")
    with connection.cursor() as cursor:
        for model in app_config.get_models():
            table = model._meta.db_table
            cursor.execute(
                "SELECT to_regclass(%s)::text",
                (f"public.{table}",),
            )
            if cursor.fetchone()[0] is None:
                if allow_missing_stage_table and table == "vault_import_stage_record":
                    continue
                issues.append(f"missing table public.{table}")
                continue

            cursor.execute("""
                SELECT a.attname,
                       format_type(a.atttypid, a.atttypmod),
                       a.attnotnull
                  FROM pg_attribute AS a
                  JOIN pg_class AS c ON c.oid = a.attrelid
                  JOIN pg_namespace AS n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public'
                   AND c.relname = %s
                   AND a.attnum > 0
                   AND NOT a.attisdropped
            """, (table,))
            actual_columns = {
                name: (_normalize_type(db_type), bool(not_null))
                for name, db_type, not_null in cursor.fetchall()
            }

            for field in model._meta.local_fields:
                if not field.column:
                    continue
                actual = actual_columns.get(field.column)
                expected_type = _normalize_type(field.db_type(connection))
                expected_not_null = not field.null or field.primary_key
                if actual is None:
                    issues.append(f"missing column public.{table}.{field.column}")
                elif actual[0] != expected_type:
                    issues.append(
                        f"type mismatch public.{table}.{field.column}: "
                        f"database={actual[0]} model={expected_type}"
                    )
                elif actual[1] != expected_not_null:
                    issues.append(
                        f"nullability mismatch public.{table}.{field.column}: "
                        f"database_not_null={actual[1]} "
                        f"model_not_null={expected_not_null}"
                    )

            constraints = connection.introspection.get_constraints(cursor, table)
            primary_key = model._meta.pk
            if not any(
                item.get("primary_key")
                and item.get("columns") == [primary_key.column]
                for item in constraints.values()
            ):
                issues.append(
                    f"missing primary key on public.{table}.{primary_key.column}"
                )

            for index in model._meta.indexes:
                installed = constraints.get(index.name)
                if not installed or not installed.get("index"):
                    issues.append(
                        f"missing model index public.{table}.{index.name}"
                    )
                elif not installed.get("valid", True) or not installed.get("ready", True):
                    issues.append(
                        f"invalid or not-ready model index public.{table}.{index.name}"
                    )

            for field_group in model._meta.unique_together:
                columns = [model._meta.get_field(name).column for name in field_group]
                if not any(
                    item.get("unique") and item.get("columns") == columns
                    for item in constraints.values()
                ):
                    issues.append(
                        f"missing composite unique constraint on public.{table}"
                    )

            for meta_constraint in model._meta.constraints:
                name = getattr(meta_constraint, "name", None)
                installed = constraints.get(name) if name else None
                if installed is None:
                    issues.append(
                        f"missing model constraint public.{table}.{name or '<unnamed>'}"
                    )

            for field in model._meta.local_fields:
                if field.unique and not field.primary_key:
                    if not any(
                        item.get("unique")
                        and item.get("columns") == [field.column]
                        for item in constraints.values()
                    ):
                        issues.append(
                            f"missing unique constraint on public.{table}.{field.column}"
                        )
                if field.is_relation and field.db_constraint and field.remote_field:
                    remote_model = field.remote_field.model
                    remote_field = remote_model._meta.get_field(
                        field.target_field.name
                    )
                    expected_fk = (
                        remote_model._meta.db_table,
                        remote_field.column,
                    )
                    if not any(
                        item.get("foreign_key") == expected_fk
                        and item.get("columns") == [field.column]
                        for item in constraints.values()
                    ):
                        issues.append(
                            f"missing foreign key on public.{table}.{field.column} "
                            f"to public.{expected_fk[0]}.{expected_fk[1]}"
                        )
    return issues


def schema_contract_checksum():
    """Stable checksum of model and custom PostgreSQL objects in migration 0001."""
    from .import_engine import _IMPORT_FINALIZER_VERSION
    from .importer import _FAST_MODE_INDEXES

    sql = Path(__file__).with_name("import_finalize.sql").read_bytes()
    model_contract = []
    for model in apps.get_app_config("vault").get_models():
        fields = []
        for field in model._meta.local_fields:
            remote = None
            if field.is_relation and field.remote_field:
                remote_model = field.remote_field.model
                remote_field = remote_model._meta.get_field(
                    field.target_field.name
                )
                remote = {
                    "table": remote_model._meta.db_table,
                    "column": remote_field.column,
                    "on_delete": getattr(
                        field.remote_field.on_delete, "__name__", "unknown",
                    ),
                    "db_constraint": bool(field.db_constraint),
                }
            fields.append({
                "name": field.name,
                "column": field.column,
                "db_type": field.db_type(connection),
                "nullable": bool(field.null),
                "primary_key": bool(field.primary_key),
                "unique": bool(field.unique),
                "db_index": bool(field.db_index),
                "relation": remote,
            })
        model_contract.append({
            "model": model._meta.label_lower,
            "table": model._meta.db_table,
            "fields": fields,
            "unique_together": [
                list(group) for group in model._meta.unique_together
            ],
            "indexes": [repr(index.deconstruct()) for index in model._meta.indexes],
            "constraints": [
                repr(item.deconstruct()) for item in model._meta.constraints
            ],
        })
    objects = {
        "migration_id": SCHEMA_MIGRATION_ID,
        "version_table_ddl": _SCHEMA_MIGRATION_TABLE_DDL,
        "models": model_contract,
        "finalizer_version": _IMPORT_FINALIZER_VERSION,
        "search_indexes": list(_FAST_MODE_INDEXES),
        "extension": "pg_trgm",
        "grants": "stage-table privileges restricted to configured role; no PUBLIC execute",
    }
    payload = sql + b"\0" + json.dumps(
        objects, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_private_stage_permissions():
    """Ensure PUBLIC has no stage-table or finalizer access and app role can use both."""
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT
                EXISTS (
                    SELECT 1
                      FROM pg_class AS c
                      CROSS JOIN LATERAL aclexplode(
                          COALESCE(c.relacl, acldefault('r', c.relowner))
                      ) AS acl
                     WHERE c.oid = to_regclass('public.vault_import_stage_record')
                       AND acl.grantee = 0
                ),
                EXISTS (
                    SELECT 1
                      FROM pg_proc AS p
                      CROSS JOIN LATERAL aclexplode(
                          COALESCE(p.proacl, acldefault('f', p.proowner))
                      ) AS acl
                     WHERE p.oid = to_regprocedure(%s)
                       AND acl.grantee = 0
                       AND acl.privilege_type = 'EXECUTE'
                ),
                COALESCE(has_table_privilege(
                    current_user, 'public.vault_import_stage_record', 'SELECT'
                ), false),
                COALESCE(has_function_privilege(
                    current_user, %s, 'EXECUTE'
                ), false)
        """, (IMPORT_FINALIZER_SIGNATURE, IMPORT_FINALIZER_SIGNATURE))
        public_table_access, public_execute, app_can_read, app_can_execute = cursor.fetchone()
    if public_table_access or public_execute or not app_can_read or not app_can_execute:
        raise RuntimeError(
            "Import staging table/finalizer privileges are not private and "
            "usable by the configured database role."
        )
    return True


def schema_migration_table_exists():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT to_regclass(%s)::oid",
            (f"public.{SCHEMA_MIGRATION_TABLE}",),
        )
        return cursor.fetchone()[0] is not None


def schema_migration_table_issues():
    """Validate the custom version ledger without changing it.

    A colliding or partially-created table must be reviewed before any schema
    installer DDL runs; ``CREATE TABLE IF NOT EXISTS`` alone would otherwise
    silently accept an incompatible object and fail only after other objects
    had already been installed.
    """
    if not schema_migration_table_exists():
        return []

    table = SCHEMA_MIGRATION_TABLE
    expected = {
        "migration_id": ("varchar(64)", True),
        "checksum": ("char(64)", True),
        "applied_at": ("timestamptz", True),
        "applied_by": ("text", True),
        "adoption_mode": ("varchar(32)", True),
        "backup_reference": ("text", True),
    }
    issues = []
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT a.attname,
                   format_type(a.atttypid, a.atttypmod),
                   a.attnotnull,
                   pg_get_expr(d.adbin, d.adrelid)
              FROM pg_attribute AS a
              JOIN pg_class AS c ON c.oid = a.attrelid
              JOIN pg_namespace AS n ON n.oid = c.relnamespace
              LEFT JOIN pg_attrdef AS d
                ON d.adrelid = c.oid AND d.adnum = a.attnum
             WHERE n.nspname = 'public'
               AND c.relname = %s
               AND a.attnum > 0
               AND NOT a.attisdropped
        """, (table,))
        actual = {
            name: (_normalize_type(db_type), bool(not_null), default)
            for name, db_type, not_null, default in cursor.fetchall()
        }

        for name, (expected_type, expected_not_null) in expected.items():
            field = actual.get(name)
            if field is None:
                issues.append(f"missing version-ledger column public.{table}.{name}")
            elif field[:2] != (expected_type, expected_not_null):
                issues.append(
                    f"mismatched version-ledger column public.{table}.{name}"
                )
        for name in sorted(set(actual) - set(expected)):
            issues.append(f"unexpected version-ledger column public.{table}.{name}")
        if actual.get("applied_at", (None, None, None))[2] is None:
            issues.append(
                f"missing default on version-ledger column public.{table}.applied_at"
            )

        if not issues:
            constraints = connection.introspection.get_constraints(cursor, table)
            if not any(
                item.get("primary_key")
                and item.get("columns") == ["migration_id"]
                for item in constraints.values()
            ):
                issues.append(
                    f"missing primary key on public.{table}.migration_id"
                )
            if not issues:
                known_ids = tuple(SCHEMA_MIGRATION_IDS)
                placeholders = ", ".join("%s" for _ in known_ids)
                cursor.execute(
                    f"SELECT migration_id FROM public.{table} "
                    f"WHERE migration_id NOT IN ({placeholders}) "
                    "ORDER BY migration_id",
                    known_ids,
                )
                unknown = [row[0] for row in cursor.fetchall()]
                issues.extend(
                    f"unrecognized schema migration record {migration_id}"
                    for migration_id in unknown
                )
    return issues


def _schema_migration_record(migration_id):
    if not schema_migration_table_exists():
        return None
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT migration_id, checksum, applied_at, applied_by, "
            f"adoption_mode, backup_reference FROM public.{SCHEMA_MIGRATION_TABLE} "
            "WHERE migration_id = %s",
            (migration_id,),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return {
        "migration_id": row[0],
        "checksum": str(row[1]).strip(),
        "applied_at": row[2].isoformat() if row[2] else None,
        "applied_by": row[3],
        "adoption_mode": row[4],
        "backup_reference": row[5],
    }


def current_schema_migration():
    """Return metadata for the latest migration shipped by this code."""
    return _schema_migration_record(SCHEMA_MIGRATION_ID)


def latest_schema_migration():
    """Return the newest recognized migration record, or None."""
    for migration_id in reversed(SCHEMA_MIGRATION_IDS):
        record = _schema_migration_record(migration_id)
        if record is not None:
            return record
    return None


def schema_migration_is_current():
    record = current_schema_migration()
    return bool(
        record
        and record["checksum"] == schema_contract_checksum()
    )


def create_schema_migration_table():
    """Create only the custom version ledger; caller must explicitly opt in."""
    with connection.cursor() as cursor:
        cursor.execute(_SCHEMA_MIGRATION_TABLE_DDL)


def record_schema_migration(*, adoption_mode, backup_reference=""):
    checksum = schema_contract_checksum()
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO public.{SCHEMA_MIGRATION_TABLE} "
            "(migration_id, checksum, applied_by, adoption_mode, backup_reference) "
            "VALUES (%s, %s, current_user, %s, %s) "
            "ON CONFLICT (migration_id) DO NOTHING",
            (SCHEMA_MIGRATION_ID, checksum, adoption_mode, backup_reference),
        )
    record = current_schema_migration()
    if not record or record["checksum"] != checksum:
        raise RuntimeError(
            "The recorded vault schema checksum differs from the current "
            "migration contract; a new versioned schema migration is required."
        )
    return record


def schema_inventory():
    """Return a read-only, credential-free snapshot of the public schema."""
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT current_database(), current_user,
                   current_setting('server_version')
        """)
        database, role, server_version = cursor.fetchone()

        cursor.execute("""
            SELECT c.relname, c.relkind,
                   pg_size_pretty(pg_total_relation_size(c.oid)),
                   s.n_live_tup
              FROM pg_class AS c
              JOIN pg_namespace AS n ON n.oid = c.relnamespace
              LEFT JOIN pg_stat_user_tables AS s ON s.relid = c.oid
             WHERE n.nspname = 'public'
               AND c.relname LIKE 'vault_%'
               AND c.relkind IN ('r', 'p', 'v', 'm')
             ORDER BY c.relname
        """)
        tables = [
            {
                "name": name,
                "kind": kind,
                "total_size": total_size,
                "estimated_live_rows": int(live_rows or 0),
            }
            for name, kind, total_size, live_rows in cursor.fetchall()
        ]

        cursor.execute("""
            SELECT c.relname, a.attname,
                   format_type(a.atttypid, a.atttypmod),
                   NOT a.attnotnull,
                   pg_get_expr(d.adbin, d.adrelid)
              FROM pg_attribute AS a
              JOIN pg_class AS c ON c.oid = a.attrelid
              JOIN pg_namespace AS n ON n.oid = c.relnamespace
              LEFT JOIN pg_attrdef AS d
                ON d.adrelid = c.oid AND d.adnum = a.attnum
             WHERE n.nspname = 'public'
               AND c.relname LIKE 'vault_%'
               AND c.relkind IN ('r', 'p')
               AND a.attnum > 0
               AND NOT a.attisdropped
             ORDER BY c.relname, a.attnum
        """)
        columns = [
            {
                "table": table,
                "name": name,
                "type": data_type,
                "nullable": bool(nullable),
                "default": default,
            }
            for table, name, data_type, nullable, default in cursor.fetchall()
        ]

        cursor.execute("""
            SELECT i.indrelid::regclass::text,
                   index_class.relname,
                   i.indisunique, i.indisvalid, i.indisready,
                   pg_get_indexdef(i.indexrelid)
              FROM pg_index AS i
              JOIN pg_class AS index_class ON index_class.oid = i.indexrelid
              JOIN pg_class AS table_class ON table_class.oid = i.indrelid
              JOIN pg_namespace AS n ON n.oid = table_class.relnamespace
             WHERE n.nspname = 'public'
               AND table_class.relname LIKE 'vault_%'
             ORDER BY table_class.relname, index_class.relname
        """)
        indexes = [
            {
                "table": table,
                "name": name,
                "unique": bool(unique),
                "valid": bool(valid),
                "ready": bool(ready),
                "definition": definition,
            }
            for table, name, unique, valid, ready, definition in cursor.fetchall()
        ]

        cursor.execute("""
            SELECT c.relname, con.conname, con.contype,
                   con.convalidated, con.condeferrable, con.condeferred,
                   pg_get_constraintdef(con.oid)
              FROM pg_constraint AS con
              JOIN pg_class AS c ON c.oid = con.conrelid
              JOIN pg_namespace AS n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public'
               AND c.relname LIKE 'vault_%'
             ORDER BY c.relname, con.conname
        """)
        constraints = [
            {
                "table": table,
                "name": name,
                "type": kind,
                "validated": bool(validated),
                "deferrable": bool(deferrable),
                "initially_deferred": bool(deferred),
                "definition": definition,
            }
            for table, name, kind, validated, deferrable, deferred, definition
            in cursor.fetchall()
        ]

        cursor.execute("""
            SELECT extname, extversion
              FROM pg_extension
             ORDER BY extname
        """)
        extensions = [
            {"name": name, "version": version}
            for name, version in cursor.fetchall()
        ]

        cursor.execute("""
            SELECT p.oid::regprocedure::text,
                   obj_description(p.oid, 'pg_proc')
              FROM pg_proc AS p
              JOIN pg_namespace AS n ON n.oid = p.pronamespace
             WHERE n.nspname = 'public'
               AND p.proname = 'vault_finalize_import_batch'
             ORDER BY p.oid::regprocedure::text
        """)
        functions = [
            {"signature": signature, "version": version}
            for signature, version in cursor.fetchall()
        ]

        cursor.execute("""
            SELECT grantee, privilege_type, is_grantable
              FROM information_schema.role_table_grants
             WHERE table_schema = 'public'
               AND table_name = 'vault_import_stage_record'
             ORDER BY grantee, privilege_type
        """)
        stage_grants = [
            {"grantee": grantee, "privilege": privilege, "grantable": grantable == "YES"}
            for grantee, privilege, grantable in cursor.fetchall()
        ]

    migration_issues = schema_migration_table_issues()
    migration = None
    if not migration_issues:
        migration = latest_schema_migration()
        if migration is None:
            migration_issues.append(
                f"versioned schema migration {SCHEMA_MIGRATION_ID} is not recorded"
            )
        elif migration["migration_id"] != SCHEMA_MIGRATION_ID:
            migration_issues.append(
                f"schema migration {migration['migration_id']} is installed; "
                f"code expects {SCHEMA_MIGRATION_ID}"
            )
        else:
            expected_checksum = schema_contract_checksum()
            if migration["checksum"] != expected_checksum:
                migration_issues.append(
                    f"schema checksum mismatch for {SCHEMA_MIGRATION_ID}: "
                    f"database={migration['checksum']} "
                    f"code={expected_checksum}"
                )
    return {
        "database": database,
        "current_role": role,
        "postgresql_version": server_version,
        "tables": tables,
        "columns": columns,
        "indexes": indexes,
        "constraints": constraints,
        "extensions": extensions,
        "custom_functions": functions,
        "stage_table_grants": stage_grants,
        "schema_migration": migration,
        "schema_migration_issues": migration_issues,
        "model_schema_issues": model_schema_issues(),
    }
