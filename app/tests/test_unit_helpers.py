"""Focused unit tests for shared validation and infrastructure helpers."""

import io
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg
from psycopg_pool import PoolTimeout

import app


class HelperUnitTests(unittest.TestCase):
    def test_email_validation_rejects_invalid_address_shapes(self):
        for value in (
            "plain-address",
            "user..name@example.com",
            ".user@example.com",
            "user.@example.com",
            "user@example.c",
            f"{'x' * 65}@example.com",
        ):
            with self.subTest(value=value):
                self.assertFalse(app._valid_email(value))

    def test_email_validation_accepts_valid_address(self):
        self.assertTrue(app._valid_email("first.last+tag@example.org"))

    def test_google_avatar_url_only_accepts_google_https_hosts(self):
        self.assertEqual(
            app._google_avatar_url("https://lh3.googleusercontent.com/avatar"),
            "https://lh3.googleusercontent.com/avatar",
        )
        for value in (
            "http://lh3.googleusercontent.com/avatar",
            "https://example.com/avatar",
            "https://lh3.googleusercontent.com.evil.example/avatar",
            "https://lh3.googleusercontent.com:443/avatar",
            "//lh3.googleusercontent.com/avatar",
        ):
            with self.subTest(value=value):
                self.assertEqual(app._google_avatar_url(value), "")

    def test_same_origin_check_allows_absent_origin_and_exact_http_origins(self):
        self.assertTrue(app._same_origin_request(SimpleNamespace(headers={"Host": "app.test"})))
        self.assertTrue(app._same_origin_request(SimpleNamespace(
            headers={"Host": "app.test", "Origin": "https://app.test"}
        )))

    def test_same_origin_check_rejects_foreign_or_malformed_origins(self):
        for origin in ("https://attacker.test", "javascript://app.test", "https://"):
            with self.subTest(origin=origin):
                self.assertFalse(app._same_origin_request(SimpleNamespace(
                    headers={"Host": "app.test", "Origin": origin}
                )))

    def test_custom_category_validation_rejects_reserved_invalid_and_long_names(self):
        self.assertIsNone(app._valid_category_name("  food  "))
        self.assertIsNone(app._valid_category_name("bad/name"))
        self.assertIsNone(app._valid_category_name("x" * 41))
        self.assertIsNone(app._valid_category_name("   "))

    def test_custom_category_validation_trims_and_accepts_supported_names(self):
        self.assertEqual(app._valid_category_name("  Pet Care  "), "Pet Care")

    def test_resolve_category_returns_predefined_canonical_value(self):
        conn = MagicMock()
        self.assertEqual(app._resolve_category(conn, 3, "fOoD"), "Food")
        conn.execute.assert_not_called()

    def test_resolve_category_returns_owned_custom_value_or_none(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {"name": "Pet Care"}
        self.assertEqual(app._resolve_category(conn, 3, "pet care"), "Pet Care")
        conn.execute.return_value.fetchone.return_value = None
        self.assertIsNone(app._resolve_category(conn, 4, "Pet Care"))

    def test_json_body_requires_a_valid_object(self):
        handler = SimpleNamespace(
            headers={"Content-Length": "12"},
            rfile=io.BytesIO(b'{"ok": true}'),
        )
        self.assertEqual(app.Handler._body(handler), {"ok": True})
        handler.headers = {"Content-Length": "2"}
        handler.rfile = io.BytesIO(b"[]")
        self.assertEqual(app.Handler._body(handler), {})
        handler.headers = {"Content-Length": "1"}
        handler.rfile = io.BytesIO(b"{")
        with self.assertRaisesRegex(ValueError, "valid JSON"):
            app.Handler._body(handler)

    def test_named_cookie_handles_present_missing_and_malformed_headers(self):
        handler = SimpleNamespace(headers={"Cookie": "session=abc; theme=dark"})
        self.assertEqual(app.Handler._cookie_named(handler, "session"), "abc")
        self.assertIsNone(app.Handler._cookie_named(handler, "missing"))
        handler.headers = {"Cookie": 'broken="unterminated'}
        self.assertIsNone(app.Handler._cookie_named(handler, "session"))

    def test_google_state_cookie_secure_and_clear_variants(self):
        handler = object.__new__(app.Handler)
        with patch.dict("os.environ", {"SECURE_COOKIE": "1"}):
            state = app.Handler._google_state_cookie(handler, "state-token")
            cleared = app.Handler._google_state_cookie(handler, "", clear=True)
        self.assertIn("Secure", state)
        self.assertIn("Max-Age=600", state)
        self.assertIn("Max-Age=0", cleared)
        self.assertIn("Secure", cleared)
        with patch.dict("os.environ", {"SECURE_COOKIE": "0"}):
            self.assertNotIn("Secure", app.Handler._google_state_cookie(handler, "state-token"))

    def test_budget_alert_level_covers_no_alert_warning_and_exceeded(self):
        self.assertEqual(app._budget_alert_level(0, 10000), (None, 0.0))
        self.assertEqual(app._budget_alert_level(7900, 10000), (None, 79.0))
        self.assertEqual(app._budget_alert_level(8000, 10000), ("warning", 80.0))
        self.assertEqual(app._budget_alert_level(10001, 10000), ("exceeded", 100.0))
        self.assertEqual(app._budget_alert_level(100, 0), ("exceeded", 0))

    def test_alert_persistence_uses_supplied_date_and_writes_active_alerts(self):
        conn = MagicMock()
        with patch.object(app, "_active_budget_alerts", return_value=[{
            "budgetId": 2, "level": "warning", "budgetType": "monthly",
            "startDate": "2026-11-01", "endDate": "2026-11-30",
            "amountCents": 10000, "spentCents": 8000,
            "remainingCents": 2000, "percentUsed": 80.0,
        }]) as active:
            app._persist_budget_alerts(conn, 7, "2026-11-05")
        active.assert_called_once_with(conn, 7, "2026-11-05")
        self.assertEqual(conn.execute.call_count, 2)

    def test_alert_persistence_wraps_database_errors(self):
        conn = MagicMock()
        conn.execute.side_effect = psycopg.OperationalError("temporary failure")
        with self.assertRaises(app.NotificationPersistenceError):
            app._persist_budget_alerts(conn, 7, "2026-11-05")

    def test_database_pool_requires_database_url(self):
        with patch.object(app, "DATABASE_URL", ""):
            with self.assertRaisesRegex(RuntimeError, "DATABASE_URL"):
                app._database_pool()

    def test_database_pool_reuses_the_pool_for_unchanged_url(self):
        cached = object()
        with patch.object(app, "DATABASE_URL", "postgresql://test/db"), \
             patch.object(app, "_DATABASE_POOL", cached), \
             patch.object(app, "_DATABASE_POOL_URL", "postgresql://test/db"):
            self.assertIs(app._database_pool(), cached)

    def test_database_pool_translates_pool_timeout(self):
        pool = MagicMock()
        pool.open.side_effect = PoolTimeout("timed out")
        with patch.object(app, "DATABASE_URL", "postgresql://test/db"), \
             patch.object(app, "_DATABASE_POOL", None), \
             patch.object(app, "_DATABASE_POOL_URL", None), \
             patch.object(app, "ConnectionPool", return_value=pool):
            with self.assertRaises(psycopg.OperationalError):
                app._database_pool()
        pool.close.assert_called_once()

    def test_database_pool_closes_an_old_pool_when_url_changes(self):
        old_pool = MagicMock()
        new_pool = MagicMock()
        new_pool.open.return_value = None
        with patch.object(app, "DATABASE_URL", "postgresql://test/new"), \
             patch.object(app, "_DATABASE_POOL", old_pool), \
             patch.object(app, "_DATABASE_POOL_URL", "postgresql://test/old"), \
             patch.object(app, "ConnectionPool", return_value=new_pool):
            self.assertIs(app._database_pool(), new_pool)
        old_pool.close.assert_called_once()
        new_pool.open.assert_called_once()

    def test_database_pool_closes_and_reraises_non_timeout_open_errors(self):
        pool = MagicMock()
        pool.open.side_effect = psycopg.OperationalError("unavailable")
        with patch.object(app, "DATABASE_URL", "postgresql://test/db"), \
             patch.object(app, "_DATABASE_POOL", None), \
             patch.object(app, "_DATABASE_POOL_URL", None), \
             patch.object(app, "ConnectionPool", return_value=pool):
            with self.assertRaisesRegex(psycopg.OperationalError, "unavailable"):
                app._database_pool()
        pool.close.assert_called_once()

    def test_database_connection_translates_context_pool_timeout(self):
        pooled = MagicMock()
        pooled.__enter__.side_effect = PoolTimeout("timed out")
        with self.assertRaises(psycopg.OperationalError):
            app.DatabaseConnection(pooled).__enter__()

    def test_email_verification_requires_complete_smtp_configuration(self):
        with patch.multiple(app, SMTP_HOST="", SMTP_FROM="", SMTP_USERNAME="", SMTP_PASSWORD=""):
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                app._send_email_change_verification("new@example.com", "token")
        with patch.multiple(app, SMTP_HOST="smtp.test", SMTP_FROM="from@example.com", SMTP_USERNAME="user", SMTP_PASSWORD=""):
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                app._send_email_change_verification("new@example.com", "token")

    def test_email_verification_sends_over_authenticated_starttls(self):
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        with patch.multiple(app, SMTP_HOST="smtp.test", SMTP_PORT=587, SMTP_FROM="from@example.com", SMTP_USERNAME="user", SMTP_PASSWORD="secret", APP_BASE_URL="https://app.test"), \
             patch.object(app.smtplib, "SMTP", return_value=smtp) as client, \
             patch.object(app.ssl, "create_default_context", return_value=object()):
            app._send_email_change_verification("new@example.com", "opaque-token")
        client.assert_called_once()
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("user", "secret")
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "new@example.com")
        self.assertIn("opaque-token", message.get_content())

    def test_email_verification_supports_ssl_without_smtp_login(self):
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        with patch.multiple(app, SMTP_HOST="smtp.test", SMTP_PORT=465, SMTP_FROM="from@example.com", SMTP_USERNAME="", SMTP_PASSWORD="", APP_BASE_URL="https://app.test"), \
             patch.object(app.smtplib, "SMTP_SSL", return_value=smtp) as client, \
             patch.object(app.ssl, "create_default_context", return_value=object()):
            app._send_email_change_verification("new@example.com", "opaque-token")
        client.assert_called_once()
        smtp.send_message.assert_called_once()
        smtp.login.assert_not_called()

    def test_google_oauth_start_handles_unconfigured_and_builds_provider_url(self):
        handler = SimpleNamespace(_redirect=MagicMock(), _google_state_cookie=lambda state: f"cookie={state}")
        with patch.object(app, "GOOGLE_CLIENT_ID", ""), patch.object(app, "GOOGLE_CLIENT_SECRET", ""):
            app.Handler._start_google_auth(handler)
        handler._redirect.assert_called_once_with("/?auth_error=google_not_configured")

        handler._redirect.reset_mock()
        with patch.object(app, "GOOGLE_CLIENT_ID", "client-id"), \
             patch.object(app, "GOOGLE_CLIENT_SECRET", "client-secret"), \
             patch.object(app, "GOOGLE_REDIRECT_URI", "https://app.test/callback"), \
             patch.object(app.secrets, "token_urlsafe", return_value="state-value"):
            app.Handler._start_google_auth(handler)
        location, cookies = handler._redirect.call_args.args
        self.assertTrue(location.startswith("https://accounts.google.com/o/oauth2/v2/auth?"))
        self.assertEqual(cookies, ["cookie=state-value"])
        self.assertIn("state=state-value", location)

    def test_google_oauth_callback_handles_cancel_invalid_state_and_missing_config(self):
        handler = SimpleNamespace(
            _cookie_named=lambda _name: "stored-state",
            _google_state_cookie=lambda _state, clear=False: "clear-state",
            _redirect=MagicMock(),
        )
        app.Handler._finish_google_auth(handler, {"error": ["access_denied"]})
        handler._redirect.assert_called_with("/?auth_error=google_cancelled", ["clear-state"])

        handler._redirect.reset_mock()
        app.Handler._finish_google_auth(handler, {"state": ["wrong"], "code": ["auth-code"]})
        handler._redirect.assert_called_with("/?auth_error=google_state", ["clear-state"])

        handler._redirect.reset_mock()
        with patch.object(app, "GOOGLE_CLIENT_ID", ""), patch.object(app, "GOOGLE_CLIENT_SECRET", ""):
            app.Handler._finish_google_auth(handler, {"state": ["stored-state"], "code": ["auth-code"]})
        handler._redirect.assert_called_with("/?auth_error=google_not_configured", ["clear-state"])

    def test_bootstrap_admin_rejects_invalid_existing_and_suspended_accounts(self):
        with self.assertRaises(ValueError):
            app.bootstrap_admin("not-an-email")

        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {"total": 1}
        with patch.object(app, "db") as database:
            database.return_value.__enter__.return_value = conn
            with self.assertRaisesRegex(RuntimeError, "already provisioned"):
                app.bootstrap_admin("owner@example.com")

        for user in (None, {"id": 5, "is_suspended": True}):
            conn.execute.return_value.fetchone.side_effect = [{"total": 0}, user]
            with patch.object(app, "db") as database:
                database.return_value.__enter__.return_value = conn
                with self.assertRaises(ValueError):
                    app.bootstrap_admin("owner@example.com")

    def test_bootstrap_admin_grants_role_and_records_activity(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.side_effect = [
            {"total": 0}, {"id": 8, "is_suspended": False}
        ]
        with patch.object(app, "db") as database:
            database.return_value.__enter__.return_value = conn
            self.assertEqual(app.bootstrap_admin(" OWNER@EXAMPLE.COM "), 8)
        self.assertEqual(conn.execute.call_count, 5)

    def test_backup_rejects_missing_url_and_invokes_pg_dump_with_connection_env(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "backup.dump"
            with patch.object(app, "DATABASE_URL", ""):
                with self.assertRaisesRegex(RuntimeError, "DATABASE_URL"):
                    app.backup(str(target))

            with patch.object(app, "DATABASE_URL", "postgresql://backup_user:pw@db.test:5544/backup_db?sslmode=require"), \
                 patch.object(app.subprocess, "run") as run:
                app.backup(str(target))
            args, kwargs = run.call_args
            self.assertEqual(args[0][:3], ["pg_dump", "--format=custom", "--file"])
            self.assertEqual(kwargs["env"]["PGDATABASE"], "backup_db")
            self.assertEqual(kwargs["env"]["PGUSER"], "backup_user")
            self.assertEqual(kwargs["env"]["PGPASSWORD"], "pw")
            self.assertEqual(kwargs["env"]["PGSSLMODE"], "require")

    def test_app_startup_rejects_missing_database_url(self):
        with patch.object(app, "DATABASE_URL", ""), patch.object(sys, "argv", ["app.py"]):
            with self.assertRaisesRegex(SystemExit, "DATABASE_URL"):
                app.main()


if __name__ == "__main__":
    unittest.main()
