"""Production settings fail closed and do not suppress Django deploy checks."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase


class ProductionSettingsTests(SimpleTestCase):
    def _environment(self, upload_dir):
        env = os.environ.copy()
        env.update({
            "DJANGO_SETTINGS_MODULE": "admin_LogIntel.settings",
            "CREDVAULT_ENV": "production",
            "DJANGO_DEBUG": "false",
            "SECRET_KEY": (
                "test-only-unique-production-profile-secret-"
                "54c5d4019c074fa7907a44b4bb65f8d4"
            ),
            "CREDVAULT_ALLOWED_HOSTS": "vault.example.test",
            "CREDVAULT_CSRF_TRUSTED_ORIGINS": "https://vault.example.test",
            "CREDVAULT_HTTPS_MODE": "proxy",
            "CREDVAULT_TRUSTED_PROXY_CIDRS": "127.0.0.1/32",
            "CREDVAULT_HSTS_SECONDS": "31536000",
            "CREDVAULT_HSTS_INCLUDE_SUBDOMAINS": "true",
            "CREDVAULT_HSTS_PRELOAD": "true",
            "POSTGRES_DB": "credvault_test",
            "POSTGRES_USER": "test-role",
            "POSTGRES_PASSWORD": "test-only-not-a-real-password",
            "POSTGRES_HOST": "127.0.0.1",
            "CREDVAULT_POSTGRES_SSLMODE": "require",
            "CREDVAULT_MAX_UPLOAD_BYTES": "1048576",
            "CREDVAULT_MAX_REQUEST_BYTES": "1114112",
            "CREDVAULT_MAX_UPLOAD_FILES": "3",
            "CREDVAULT_UPLOAD_TMP_DIR": str(upload_dir),
            "CREDVAULT_STATIC_ROOT": str(Path(upload_dir) / "static"),
        })
        return env

    def test_production_deploy_check_has_no_silenced_or_unreviewed_warnings(self):
        with tempfile.TemporaryDirectory(prefix="credvault-settings-") as path:
            Path(path).chmod(0o700)
            env = self._environment(path)
            result = subprocess.run(
                [sys.executable, "manage.py", "check", "--deploy"],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("no issues (0 silenced)", output)
        self.assertNotIn("WARNINGS:", output)

    def test_production_requires_explicit_debug_off(self):
        with tempfile.TemporaryDirectory(prefix="credvault-settings-") as path:
            Path(path).chmod(0o700)
            env = self._environment(path)
            env.pop("DJANGO_DEBUG")
            missing = subprocess.run(
                [sys.executable, "-c", "from django.conf import settings; print(settings.DEBUG)"],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("DJANGO_DEBUG must be explicitly set", missing.stderr)

            env["DJANGO_DEBUG"] = "true"
            enabled = subprocess.run(
                [sys.executable, "-c", "from django.conf import settings; print(settings.DEBUG)"],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        self.assertNotEqual(enabled.returncode, 0)
        self.assertIn("DJANGO_DEBUG must not be enabled", enabled.stderr)

    def test_production_requires_absolute_static_output_path(self):
        with tempfile.TemporaryDirectory(prefix="credvault-settings-") as path:
            Path(path).chmod(0o700)
            env = self._environment(path)
            env["CREDVAULT_STATIC_ROOT"] = "staticfiles"
            result = subprocess.run(
                [
                    sys.executable, "-c",
                    "from django.conf import settings; print(settings.STATIC_ROOT)",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CREDVAULT_STATIC_ROOT must be an absolute", result.stderr)

    def test_hsts_preload_requires_explicit_subdomain_policy(self):
        with tempfile.TemporaryDirectory(prefix="credvault-settings-") as path:
            Path(path).chmod(0o700)
            env = self._environment(path)
            env["CREDVAULT_HSTS_INCLUDE_SUBDOMAINS"] = "false"
            result = subprocess.run(
                [
                    sys.executable, "-c",
                    "from django.conf import settings; print(settings.SECURE_HSTS_PRELOAD)",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CREDVAULT_HSTS_PRELOAD=true requires", result.stderr)
