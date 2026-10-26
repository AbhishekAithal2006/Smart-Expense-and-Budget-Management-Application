import os
import secrets
import unittest

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

import app


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
            {"users", "sessions", "expenses", "user_categories"}.issubset(tables)
        )

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


if __name__ == "__main__":
    unittest.main()
