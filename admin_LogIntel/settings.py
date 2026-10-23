"""
Django settings for admin_LogIntel.

The default profile is for local development. Production must explicitly set
CREDVAULT_ENV=production and provide the deployment-specific secrets, host,
TLS, proxy, database, and upload settings described in README.md.
"""

import ipaddress
import os
import re
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured
from django.utils.translation import gettext_lazy as _

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name, default):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ImproperlyConfigured(f"{name} must be a boolean value.")


def _required_env_bool(name):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        raise ImproperlyConfigured(f"{name} must be explicitly set.")
    return _env_bool(name, False)


def _env_int(name, default, minimum, maximum, *, required=False):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        if required:
            raise ImproperlyConfigured(f"{name} is required.")
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured(f"{name} must be an integer.") from exc
    if not minimum <= value <= maximum:
        raise ImproperlyConfigured(
            f"{name} must be between {minimum} and {maximum}."
        )
    return value


def _csv_env(name):
    return [item.strip() for item in os.environ.get(name, "").split(",")
            if item.strip()]


def _validate_allowed_hosts(values):
    if not values or any(value == "*" for value in values):
        raise ImproperlyConfigured(
            "Production requires CREDVAULT_ALLOWED_HOSTS with explicit hosts; "
            "the wildcard host is not accepted."
        )
    for value in values:
        host = value[1:] if value.startswith(".") else value
        if not host or "://" in host or "/" in host or "*" in host:
            raise ImproperlyConfigured(
                "CREDVAULT_ALLOWED_HOSTS must contain host names only."
            )
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
                raise ImproperlyConfigured(
                    "CREDVAULT_ALLOWED_HOSTS contains an invalid host name."
                )


def _validate_csrf_origins(values, *, production):
    if production and not values:
        raise ImproperlyConfigured(
            "Production requires explicit HTTPS origins in "
            "CREDVAULT_CSRF_TRUSTED_ORIGINS."
        )
    for value in values:
        parsed = urlsplit(value)
        if (
            not parsed.hostname
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or (production and parsed.scheme != "https")
            or parsed.scheme not in {"https", "http"}
        ):
            raise ImproperlyConfigured(
                "CREDVAULT_CSRF_TRUSTED_ORIGINS must contain valid HTTPS "
                "origins in production (no paths, query strings, or fragments)."
            )


def _trusted_proxy_networks():
    raw = _csv_env("CREDVAULT_TRUSTED_PROXY_CIDRS")
    networks = []
    for value in raw:
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError as exc:
            raise ImproperlyConfigured(
                "CREDVAULT_TRUSTED_PROXY_CIDRS must contain valid IP/CIDR values."
            ) from exc
    return tuple(networks)


_PROFILE = os.environ.get("CREDVAULT_ENV", "").strip().lower()
if not _PROFILE:
    # Keep `manage.py test` isolated from the development security profile,
    # while still allowing an explicit CREDVAULT_ENV to take precedence.
    _PROFILE = "test" if "test" in sys.argv else "development"
if _PROFILE not in {"development", "test", "production"}:
    raise ImproperlyConfigured(
        "CREDVAULT_ENV must be development, test, or production."
    )
CREDVAULT_ENV = _PROFILE
IS_PRODUCTION = CREDVAULT_ENV == "production"
IS_TEST = CREDVAULT_ENV == "test"

_DEVELOPMENT_SECRET_KEY = (
    "django-insecure-a=!aw-_ai3ej@zip%bjvrg)=x8hwd7*4$_q$o7%1=mx=h!^3*w"
)
if IS_PRODUCTION:
    SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()
    if (
        len(SECRET_KEY) < 50
        or len(set(SECRET_KEY)) < 5
        or SECRET_KEY.startswith("django-insecure-")
        or SECRET_KEY == _DEVELOPMENT_SECRET_KEY
    ):
        raise ImproperlyConfigured(
            "Production requires a unique SECRET_KEY with at least 50 "
            "characters and at least five distinct characters."
        )
    if _required_env_bool("DJANGO_DEBUG"):
        raise ImproperlyConfigured(
            "DJANGO_DEBUG must not be enabled in production."
        )
    DEBUG = False
elif IS_TEST:
    SECRET_KEY = os.environ.get(
        "SECRET_KEY", "credvault-test-only-not-a-production-secret"
    )
    DEBUG = False
else:
    SECRET_KEY = os.environ.get("SECRET_KEY", _DEVELOPMENT_SECRET_KEY)
    DEBUG = _env_bool("DJANGO_DEBUG", True)

if IS_PRODUCTION:
    ALLOWED_HOSTS = _csv_env("CREDVAULT_ALLOWED_HOSTS")
    _validate_allowed_hosts(ALLOWED_HOSTS)
    CSRF_TRUSTED_ORIGINS = _csv_env("CREDVAULT_CSRF_TRUSTED_ORIGINS")
    _validate_csrf_origins(CSRF_TRUSTED_ORIGINS, production=True)
else:
    ALLOWED_HOSTS = (
        ["testserver", "localhost", "127.0.0.1"] if IS_TEST else ["*"]
    )
    CSRF_TRUSTED_ORIGINS = (
        [] if IS_TEST else [
            "https://*.e2b.app",
            "http://*.e2b.app",
            "http://localhost:8000",
            "http://127.0.0.1:8000",
        ]
    )

if IS_PRODUCTION:
    HTTPS_MODE = os.environ.get("CREDVAULT_HTTPS_MODE", "").strip().lower()
    if HTTPS_MODE not in {"direct", "proxy"}:
        raise ImproperlyConfigured(
            "Production requires CREDVAULT_HTTPS_MODE=direct or proxy."
        )
    TRUSTED_PROXY_CIDRS = _trusted_proxy_networks()
    if HTTPS_MODE == "proxy" and not TRUSTED_PROXY_CIDRS:
        raise ImproperlyConfigured(
            "CREDVAULT_HTTPS_MODE=proxy requires explicit "
            "CREDVAULT_TRUSTED_PROXY_CIDRS."
        )
    if HTTPS_MODE == "direct" and TRUSTED_PROXY_CIDRS:
        raise ImproperlyConfigured(
            "Do not configure trusted proxy CIDRs when CREDVAULT_HTTPS_MODE=direct."
        )
    SECURE_PROXY_SSL_HEADER = (
        ("HTTP_X_FORWARDED_PROTO", "https")
        if HTTPS_MODE == "proxy" else None
    )
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = _env_int(
        "CREDVAULT_HSTS_SECONDS", None, 1, 63_072_000, required=True,
    )
    SECURE_HSTS_INCLUDE_SUBDOMAINS = _required_env_bool(
        "CREDVAULT_HSTS_INCLUDE_SUBDOMAINS",
    )
    SECURE_HSTS_PRELOAD = _required_env_bool("CREDVAULT_HSTS_PRELOAD")
    if SECURE_HSTS_PRELOAD and not SECURE_HSTS_INCLUDE_SUBDOMAINS:
        raise ImproperlyConfigured(
            "CREDVAULT_HSTS_PRELOAD=true requires "
            "CREDVAULT_HSTS_INCLUDE_SUBDOMAINS=true."
        )
    if SECURE_HSTS_PRELOAD and SECURE_HSTS_SECONDS < 31_536_000:
        raise ImproperlyConfigured(
            "HSTS preload requires CREDVAULT_HSTS_SECONDS of at least one year."
        )
else:
    HTTPS_MODE = "development"
    TRUSTED_PROXY_CIDRS = ()
    SECURE_PROXY_SSL_HEADER = None
    SECURE_SSL_REDIRECT = False
    SECURE_HSTS_SECONDS = 0
    SECURE_HSTS_INCLUDE_SUBDOMAINS = False
    SECURE_HSTS_PRELOAD = False

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
USE_X_FORWARDED_HOST = False
SESSION_COOKIE_SECURE = IS_PRODUCTION
CSRF_COOKIE_SECURE = IS_PRODUCTION
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

# The HTTP server/reverse proxy must use this same limit. Django's upload
# handler also counts streamed file bytes, so a missing Content-Length header
# does not bypass the file cap.
_MAX_UPLOAD_DEFAULT = 256 * 1024 * 1024
_MAX_REQUEST_DEFAULT = _MAX_UPLOAD_DEFAULT + 16 * 1024 * 1024
CREDVAULT_MAX_UPLOAD_BYTES = _env_int(
    "CREDVAULT_MAX_UPLOAD_BYTES", _MAX_UPLOAD_DEFAULT, 1, 8 * 1024 ** 3,
    required=IS_PRODUCTION,
)
CREDVAULT_MAX_REQUEST_BYTES = _env_int(
    "CREDVAULT_MAX_REQUEST_BYTES", _MAX_REQUEST_DEFAULT, 65536, 8 * 1024 ** 3,
    required=IS_PRODUCTION,
)
if CREDVAULT_MAX_REQUEST_BYTES < CREDVAULT_MAX_UPLOAD_BYTES + 65536:
    raise ImproperlyConfigured(
        "CREDVAULT_MAX_REQUEST_BYTES must exceed "
        "CREDVAULT_MAX_UPLOAD_BYTES by at least 64 KiB for multipart overhead."
    )
CREDVAULT_MAX_UPLOAD_FILES = _env_int(
    "CREDVAULT_MAX_UPLOAD_FILES", 10, 1, 100, required=IS_PRODUCTION,
)
CREDVAULT_MAX_FORM_BYTES = _env_int(
    "CREDVAULT_MAX_FORM_BYTES", 1 * 1024 * 1024, 1024, 16 * 1024 * 1024,
)
CREDVAULT_FILE_UPLOAD_MEMORY_BYTES = _env_int(
    "CREDVAULT_FILE_UPLOAD_MEMORY_BYTES", 8 * 1024 * 1024, 0, 256 * 1024 * 1024,
)
FILE_UPLOAD_MAX_MEMORY_SIZE = min(
    CREDVAULT_FILE_UPLOAD_MEMORY_BYTES, CREDVAULT_MAX_UPLOAD_BYTES,
)

_upload_temp_dir = os.environ.get("CREDVAULT_UPLOAD_TMP_DIR", "").strip()
if IS_PRODUCTION:
    if not _upload_temp_dir:
        raise ImproperlyConfigured(
            "Production requires CREDVAULT_UPLOAD_TMP_DIR pointing to a "
            "private, quota-limited upload temporary directory."
        )
    _upload_temp_path = Path(_upload_temp_dir).expanduser()
    if not _upload_temp_path.is_absolute() or not _upload_temp_path.is_dir():
        raise ImproperlyConfigured(
            "CREDVAULT_UPLOAD_TMP_DIR must be an existing absolute directory."
        )
    _upload_temp_mode = stat.S_IMODE(_upload_temp_path.stat().st_mode)
    if _upload_temp_mode & 0o077 or not os.access(_upload_temp_path, os.W_OK):
        raise ImproperlyConfigured(
            "CREDVAULT_UPLOAD_TMP_DIR must be writable by the application "
            "and inaccessible to group/other users (mode 0700 recommended)."
        )
    FILE_UPLOAD_TEMP_DIR = str(_upload_temp_path)
elif _upload_temp_dir:
    _upload_temp_path = Path(_upload_temp_dir).expanduser()
    if not _upload_temp_path.is_dir():
        raise ImproperlyConfigured("CREDVAULT_UPLOAD_TMP_DIR is not a directory.")
    FILE_UPLOAD_TEMP_DIR = str(_upload_temp_path)
else:
    FILE_UPLOAD_TEMP_DIR = None

# Separate HTTP form fields from uploaded files: non-file form data is kept
# small, while large files spill to the private temporary directory above.
DATA_UPLOAD_MAX_NUMBER_FILES = CREDVAULT_MAX_UPLOAD_FILES
DATA_UPLOAD_MAX_NUMBER_FIELDS = 1000
DATA_UPLOAD_MAX_MEMORY_SIZE = min(
    CREDVAULT_MAX_FORM_BYTES, CREDVAULT_MAX_REQUEST_BYTES,
)
FILE_UPLOAD_HANDLERS = [
    "vault.upload_limits.BoundedFileUploadHandler",
    "django.core.files.uploadhandler.MemoryFileUploadHandler",
    "django.core.files.uploadhandler.TemporaryFileUploadHandler",
]

VAULT_IMPORT_MODE = os.environ.get("VAULT_IMPORT_MODE", "direct").strip().lower()
if VAULT_IMPORT_MODE not in {"direct", "staged"}:
    raise ImproperlyConfigured("VAULT_IMPORT_MODE must be direct or staged.")
VAULT_IMPORT_TIMEOUT_SECONDS = _env_int(
    "VAULT_IMPORT_TIMEOUT_SECONDS", 3600, 10, 86400,
)
VAULT_IMPORT_FINALIZATION_BATCH_ROWS = _env_int(
    "VAULT_IMPORT_FINALIZATION_BATCH_ROWS", 25_000, 100, 250_000,
)
VAULT_IMPORT_STATEMENT_TIMEOUT_MS = _env_int(
    "VAULT_IMPORT_STATEMENT_TIMEOUT_MS", 120_000, 1000, 3_600_000,
)
VAULT_IMPORT_LOCK_TIMEOUT_MS = _env_int(
    "VAULT_IMPORT_LOCK_TIMEOUT_MS", 30_000, 1000, 600_000,
)
CREDVAULT_SCHEMA_DDL_TIMEOUT_MS = _env_int(
    "CREDVAULT_SCHEMA_DDL_TIMEOUT_MS", 3_600_000, 1000, 86_400_000,
)


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "vault",
]

# Vault model tables use ordinary Django migrations. PostgreSQL-only objects
# (pg_trgm indexes, the import finalizer, stage-table grants, and the custom
# schema checksum ledger) remain installed by the explicit versioned command.

MIDDLEWARE = ["vault.upload_limits.ImportUploadLimitMiddleware"]
if IS_PRODUCTION and HTTPS_MODE == "proxy":
    MIDDLEWARE.append("vault.middleware.TrustedProxyForwardedProtoMiddleware")
MIDDLEWARE += [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "vault.middleware.AdministratorLoginMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "admin_LogIntel.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.i18n",
            ],
        },
    },
]
WSGI_APPLICATION = "admin_LogIntel.wsgi.application"


# Database credentials are environment-specific. Production cannot start with
# Django's development defaults or an empty password.
if IS_PRODUCTION:
    _required_database_vars = (
        "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_HOST",
    )
    _missing_database_vars = [
        name for name in _required_database_vars
        if not os.environ.get(name, "").strip()
    ]
    if _missing_database_vars:
        raise ImproperlyConfigured(
            "Production requires database variables: "
            + ", ".join(_missing_database_vars)
            + "."
        )
    _sslmode = os.environ.get("CREDVAULT_POSTGRES_SSLMODE", "").strip().lower()
    if _sslmode not in {"require", "verify-ca", "verify-full"}:
        raise ImproperlyConfigured(
            "Production requires CREDVAULT_POSTGRES_SSLMODE to be require, "
            "verify-ca, or verify-full."
        )
else:
    _sslmode = os.environ.get("CREDVAULT_POSTGRES_SSLMODE", "prefer").strip().lower()
    if _sslmode not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
        raise ImproperlyConfigured("CREDVAULT_POSTGRES_SSLMODE is invalid.")

_db_connect_timeout = _env_int(
    "CREDVAULT_DB_CONNECT_TIMEOUT_SECONDS", 10, 1, 60,
)
_db_statement_timeout = _env_int(
    "CREDVAULT_DB_STATEMENT_TIMEOUT_MS", 120_000, 1000, 3_600_000,
)
_db_lock_timeout = _env_int(
    "CREDVAULT_DB_LOCK_TIMEOUT_MS", 30_000, 1000, 600_000,
)
_db_idle_timeout = _env_int(
    "CREDVAULT_DB_IDLE_TRANSACTION_TIMEOUT_MS", 120_000, 1000, 3_600_000,
)
_db_options = (
    f"-c statement_timeout={_db_statement_timeout}ms "
    f"-c lock_timeout={_db_lock_timeout}ms "
    f"-c idle_in_transaction_session_timeout={_db_idle_timeout}ms"
)
_db_pg_options = {
    "connect_timeout": _db_connect_timeout,
    "options": _db_options,
    "sslmode": _sslmode,
}
if os.environ.get("CREDVAULT_POSTGRES_SSLROOTCERT"):
    _db_pg_options["sslrootcert"] = os.environ["CREDVAULT_POSTGRES_SSLROOTCERT"]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "credvault"),
        "USER": os.environ.get("POSTGRES_USER", "postgres"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        "HOST": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": _env_int(
            "CREDVAULT_DB_CONN_MAX_AGE_SECONDS", 60, 0, 3600,
        ),
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": _db_pg_options,
    }
}

# A fresh PostgreSQL test DB does not inherit pg_trgm from the configured DB.
TEST_RUNNER = "tests.runner.ImportPipelineTestRunner"


AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en"
LANGUAGES = [("en", _("English")), ("ar", _("Arabic"))]
LOCALE_PATHS = [BASE_DIR / "locale"]
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
_static_root = os.environ.get("CREDVAULT_STATIC_ROOT", "").strip()
if IS_PRODUCTION:
    if not _static_root:
        raise ImproperlyConfigured(
            "Production requires CREDVAULT_STATIC_ROOT for collectstatic output."
        )
    _static_root_path = Path(_static_root).expanduser()
    if not _static_root_path.is_absolute():
        raise ImproperlyConfigured(
            "CREDVAULT_STATIC_ROOT must be an absolute directory path."
        )
    STATIC_ROOT = str(_static_root_path)
else:
    STATIC_ROOT = str(Path(_static_root).expanduser()) if _static_root else str(
        BASE_DIR / "staticfiles"
    )
MEDIA_URL = "/media/"
MEDIA_ROOT = os.path.join(BASE_DIR, "media")

LOGIN_URL = "vault:login"
LOGIN_REDIRECT_URL = "vault:dashboard"
LOGOUT_REDIRECT_URL = "vault:login"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "metrics": {"format": "%(asctime)s %(name)s %(message)s"},
    },
    "handlers": {
        "import_metrics": {
            "class": "logging.StreamHandler",
            "formatter": "metrics",
        },
    },
    "loggers": {
        "vault.import.metrics": {
            "handlers": [] if IS_TEST else ["import_metrics"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
