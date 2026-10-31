import os
import secrets
import unittest
import json
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from unittest.mock import patch
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import app
import db_migrations


class DatabaseIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.test_database_url = os.environ.get("TEST_DATABASE_URL", "").strip()
        if not self.test_database_url:
            raise RuntimeError("Set TEST_DATABASE_URL to a dedicated PostgreSQL test database.")

        self.schema = f"db_test_{secrets.token_hex(6)}"
        with psycopg.connect(self.test_database_url, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))

        self.original_database_url = app.DATABASE_URL
        app.DATABASE_URL = make_conninfo(
            self.test_database_url,
            options=f"-c search_path={self.schema}",
        )
        app.initialize()

    def tearDown(self):
        app.close_database_pool()
        with psycopg.connect(self.test_database_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema))
            )
        app.DATABASE_URL = self.original_database_url

    def test_initialize_can_run_multiple_times(self):
        app.initialize()
        app.initialize()

        with app.db() as conn:
            tables = {
                row["table_name"]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = current_schema()"
                )
            }

        self.assertTrue(
            {"users", "sessions", "expenses", "budgets", "notifications", "user_categories"}.issubset(tables)
        )

    def _recreate_current_schema(self):
        with psycopg.connect(self.test_database_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(self.schema))
            )
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        app.DATABASE_URL = make_conninfo(
            self.test_database_url,
            options=f"-c search_path={self.schema}",
        )

    def test_fresh_database_applies_baseline_migration(self):
        self._recreate_current_schema()

        app.initialize()

        with app.db() as conn:
            tables = {
                row["table_name"]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema=current_schema()"
                )
            }
            migrations = conn.execute(
                "SELECT version,name FROM schema_migrations ORDER BY version"
            ).fetchall()

        self.assertTrue(
            {
                "users", "sessions", "expenses", "budgets", "notifications",
                "user_categories", "login_attempts", "email_change_requests",
                "system_activity", "schema_migrations",
            }.issubset(tables)
        )
        self.assertEqual(
            migrations,
            [{"version": 1, "name": "initial_schema"}],
        )

    def test_applied_migration_is_not_executed_again(self):
        app.initialize()

        def fail_if_reapplied(conn):
            raise AssertionError("An applied migration was executed again.")

        already_applied = db_migrations.Migration(
            version=1,
            name="initial_schema",
            apply=fail_if_reapplied,
        )
        with patch.object(db_migrations, "MIGRATIONS", (already_applied,)):
            app.initialize()

        with app.db() as conn:
            migration_count = conn.execute(
                "SELECT COUNT(*) AS count FROM schema_migrations WHERE version=1"
            ).fetchone()["count"]
        self.assertEqual(migration_count, 1)

    def test_pending_migration_updates_existing_database(self):
        with app.db() as conn:
            user_id = conn.execute(
                "INSERT INTO users(full_name,email,password_hash,created_at) "
                "VALUES(?,?,?,?) RETURNING id",
                ("Existing User", "migration-existing@example.com", b"test-hash", "2026-01-01"),
            ).fetchone()["id"]

        def add_test_marker(conn):
            conn.execute("CREATE TABLE migration_test_marker(id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO migration_test_marker(id) VALUES(1)")

        pending = db_migrations.Migration(
            version=2,
            name="test_pending_marker",
            apply=add_test_marker,
        )
        with patch.object(
            db_migrations,
            "MIGRATIONS",
            db_migrations.MIGRATIONS + (pending,),
        ):
            app.initialize()

        with app.db() as conn:
            user = conn.execute(
                "SELECT email FROM users WHERE id=?",
                (user_id,),
            ).fetchone()
            marker = conn.execute("SELECT id FROM migration_test_marker").fetchone()
            applied = conn.execute(
                "SELECT name FROM schema_migrations WHERE version=2"
            ).fetchone()

        self.assertEqual(user["email"], "migration-existing@example.com")
        self.assertEqual(marker["id"], 1)
        self.assertEqual(applied["name"], "test_pending_marker")

    def test_failed_migration_is_not_recorded_and_is_rolled_back(self):
        def fail_after_schema_change(conn):
            conn.execute("CREATE TABLE migration_failure_marker(id INTEGER PRIMARY KEY)")
            raise RuntimeError("intentional migration failure")

        failing = db_migrations.Migration(
            version=2,
            name="test_failing_migration",
            apply=fail_after_schema_change,
        )
        with patch.object(
            db_migrations,
            "MIGRATIONS",
            db_migrations.MIGRATIONS + (failing,),
        ):
            with self.assertRaisesRegex(RuntimeError, "intentional migration failure"):
                app.initialize()

        with app.db() as conn:
            migration = conn.execute(
                "SELECT version FROM schema_migrations WHERE version=2"
            ).fetchone()
            table = conn.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema=current_schema() AND table_name='migration_failure_marker'"
            ).fetchone()

        self.assertIsNone(migration)
        self.assertIsNone(table)

    def test_user_and_expense_persist_after_reopening_connection(self):
        with app.db() as conn:
            user_id = conn.execute(
                "INSERT INTO users(full_name,email,password_hash,created_at) "
                "VALUES(?,?,?,?) RETURNING id",
                ("Test User", "db-test@example.com", b"test-hash", "2026-01-01"),
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO expenses(user_id,amount_cents,category,spent_on,created_at) "
                "VALUES(?,?,?,?,?)",
                (user_id, 1250, "Food", "2026-01-02", "2026-01-02"),
            )

        with app.db() as conn:
            user = conn.execute(
                "SELECT email FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            expense = conn.execute(
                "SELECT amount_cents,category FROM expenses WHERE user_id = ?", (user_id,)
            ).fetchone()

        self.assertEqual(user["email"], "db-test@example.com")
        self.assertEqual(expense["amount_cents"], 1250)
        self.assertEqual(expense["category"], "Food")

    def test_failed_transaction_rolls_back_changes(self):
        with self.assertRaises(psycopg.IntegrityError):
            with app.db() as conn:
                conn.execute(
                    "INSERT INTO users(full_name,email,password_hash,created_at) "
                    "VALUES(?,?,?,?)",
                    ("Test User", "rollback@example.com", b"test-hash", "now"),
                )
                conn.execute(
                    "INSERT INTO users(full_name,email,password_hash,created_at) "
                    "VALUES(?,?,?,?)",
                    ("Duplicate User", "rollback@example.com", b"other-hash", "now"),
                )

        with app.db() as conn:
            result = conn.execute(
                "SELECT COUNT(*) AS total FROM users WHERE email = ?",
                ("rollback@example.com",),
            ).fetchone()

        self.assertEqual(result["total"], 0)

    def test_foreign_keys_reject_expenses_without_a_user(self):
        with self.assertRaises(psycopg.IntegrityError):
            with app.db() as conn:
                conn.execute(
                    "INSERT INTO expenses(user_id,amount_cents,category,spent_on,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (999999, 100, "Food", "2026-01-02", "now"),
                )

    def test_budget_persists_after_reopening_connection(self):
        with app.db() as conn:
            user_id = conn.execute(
                "INSERT INTO users(full_name,email,password_hash,created_at) "
                "VALUES(?,?,?,?) RETURNING id",
                ("Budget User", "budget-test@example.com", b"test-hash", "2026-01-01"),
            ).fetchone()["id"]

            budget_id = conn.execute(
                "INSERT INTO budgets(user_id,budget_type,amount_cents,start_date,end_date,created_at) "
                "VALUES(?,?,?,?,?,?) RETURNING id",
                (user_id, "monthly", 500000, "2026-01-01", "2026-01-31", "2026-01-01"),
            ).fetchone()["id"]

        # Open a new connection and retrieve the committed budget.
        with app.db() as conn:
            budget = conn.execute(
                "SELECT id,budget_type,amount_cents,start_date,end_date "
                "FROM budgets WHERE id=? AND user_id=?",
                (budget_id, user_id),
            ).fetchone()

        self.assertIsNotNone(budget)
        self.assertEqual(budget["budget_type"], "monthly")
        self.assertEqual(budget["amount_cents"], 500000)
        self.assertEqual(budget["start_date"], "2026-01-01")
        self.assertEqual(budget["end_date"], "2026-01-31")

    def test_user_expense_and_budget_can_all_be_retrieved(self):
        with app.db() as conn:
            user_id = conn.execute(
                "INSERT INTO users(full_name,email,password_hash,created_at) "
                "VALUES(?,?,?,?) RETURNING id",
                ("Recovery User", "recovery-test@example.com", b"test-hash", "2026-01-01"),
            ).fetchone()["id"]

            conn.execute(
                "INSERT INTO expenses(user_id,amount_cents,category,spent_on,created_at) "
                "VALUES(?,?,?,?,?)",
                (user_id, 2500, "Travel", "2026-01-10", "2026-01-10"),
            )

            conn.execute(
                "INSERT INTO budgets(user_id,budget_type,amount_cents,start_date,end_date,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (user_id, "monthly", 100000, "2026-01-01", "2026-01-31", "2026-01-01"),
            )

        with app.db() as conn:
            user = conn.execute(
                "SELECT email FROM users WHERE id=?", (user_id,)
            ).fetchone()

            expense = conn.execute(
                "SELECT amount_cents,category FROM expenses WHERE user_id=?",
                (user_id,),
            ).fetchone()

            budget = conn.execute(
                "SELECT amount_cents,budget_type FROM budgets WHERE user_id=?",
                (user_id,),
            ).fetchone()

        self.assertEqual(user["email"], "recovery-test@example.com")
        self.assertEqual(expense["amount_cents"], 2500)
        self.assertEqual(expense["category"], "Travel")
        self.assertEqual(budget["amount_cents"], 100000)
        self.assertEqual(budget["budget_type"], "monthly")

    def test_failed_budget_transaction_rolls_back_user_and_budget(self):
        with self.assertRaises(psycopg.IntegrityError):
            with app.db() as conn:
                user_id = conn.execute(
                    "INSERT INTO users(full_name,email,password_hash,created_at) "
                    "VALUES(?,?,?,?) RETURNING id",
                    ("Rollback Budget User", "budget-rollback@example.com", b"test-hash", "now"),
                ).fetchone()["id"]

                budget_data = (
                    user_id, "monthly", 500000,
                    "2026-01-01", "2026-01-31", "now",
                )

                conn.execute(
                    "INSERT INTO budgets(user_id,budget_type,amount_cents,start_date,end_date,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    budget_data,
                )

                # Duplicate budget violates the unique constraint.
                conn.execute(
                    "INSERT INTO budgets(user_id,budget_type,amount_cents,start_date,end_date,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    budget_data,
                )

        with app.db() as conn:
            user_count = conn.execute(
                "SELECT COUNT(*) AS total FROM users WHERE email=?",
                ("budget-rollback@example.com",),
            ).fetchone()["total"]

            budget_count = conn.execute(
                "SELECT COUNT(*) AS total FROM budgets WHERE user_id IN "
                "(SELECT id FROM users WHERE email=?)",
                ("budget-rollback@example.com",),
            ).fetchone()["total"]

        self.assertEqual(user_count, 0)
        self.assertEqual(budget_count, 0)

    def test_database_pool_reuses_connections(self):
        backend_pids = set()
        for _ in range(8):
            with app.db() as conn:
                backend_pids.add(
                    conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
                )

        self.assertGreater(len(backend_pids), 0)
        self.assertLessEqual(len(backend_pids), app.DB_POOL_MIN_SIZE)

    def test_database_pool_uses_distinct_connections_concurrently(self):
        start_together = threading.Barrier(2)

        def read_backend_pid():
            with app.db() as conn:
                start_together.wait(timeout=5)
                return conn.execute(
                    "SELECT pg_backend_pid() AS pid"
                ).fetchone()["pid"]

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: read_backend_pid(), range(2)))

        self.assertEqual(len(set(results)), 2)

    def test_database_pool_rolls_back_and_returns_connection_after_error(self):
        with app.db() as conn:
            conn.execute(
                "CREATE TABLE pool_rollback_probe (id INTEGER PRIMARY KEY)"
            )

        with self.assertRaisesRegex(RuntimeError, "rollback probe"):
            with app.db() as conn:
                conn.execute("INSERT INTO pool_rollback_probe(id) VALUES(?)", (1,))
                raise RuntimeError("rollback probe")

        with app.db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS total FROM pool_rollback_probe"
            ).fetchone()["total"]
            conn.execute("DROP TABLE pool_rollback_probe")

        self.assertEqual(count, 0)

    def test_database_pool_can_be_closed_and_recreated(self):
        original_pool = app._DATABASE_POOL
        self.assertIsNotNone(original_pool)

        with patch.object(original_pool, "close", wraps=original_pool.close) as close:
            app.close_database_pool()
            close.assert_called_once_with()

        self.assertIsNone(app._DATABASE_POOL)
        with app.db() as conn:
            conn.execute("SELECT 1 AS available").fetchone()
        self.assertIsNot(app._DATABASE_POOL, original_pool)

    def test_database_connection_failure_is_reported(self):
        original_database_url = app.DATABASE_URL
        try:
            app.DATABASE_URL = "postgresql://invalid:invalid@127.0.0.1:1/invalid"

            with self.assertRaises(psycopg.OperationalError):
                app.db()
        finally:
            app.DATABASE_URL = original_database_url

    def test_http_returns_503_when_database_is_unavailable(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(
            target=server.serve_forever,
            daemon=True,
        )
        thread.start()

        try:
            url = f"http://127.0.0.1:{server.server_port}/api/register"

            request = Request(
                url,
                data = json.dumps({
                    "fullName": "Test User",
                    "email": "database-error@example.com",
                    "password": "TestPassword123!",
                }).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with patch(
                "app.db",
                side_effect=psycopg.OperationalError("Database unavailable"),
            ):
                try:
                    urlopen(request, timeout=3)
                    self.fail("Expected HTTP 503 response.")
                except HTTPError as error:
                    self.assertEqual(error.code, 503)

                    response = json.loads(error.read().decode())

                    self.assertEqual(
                        response["error"],
                        "Database temporarily unavailable. Please try again.",
                    )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

if __name__ == "__main__":
    unittest.main()
