
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class DatabaseIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "tracker.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_initialize_can_run_multiple_times(self):
        with patch.object(app, "DB_PATH", self.db_path):
            app.initialize()
            app.initialize()

            with app.db() as conn:
                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }

        self.assertTrue(
            {"users", "sessions", "expenses", "user_categories"}.issubset(tables)
        )

    def test_user_and_expense_persist_after_reopening_connection(self):
        with patch.object(app, "DB_PATH", self.db_path):
            app.initialize()

            with app.db() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO users
                        (full_name, email, password_hash, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    ("Test User", "db-test@example.com", b"test-hash", "2026-01-01"),
                )
                user_id = cursor.lastrowid

                conn.execute(
                    """
                    INSERT INTO expenses
                        (user_id, amount_cents, category, spent_on, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (user_id, 1250, "Food", "2026-01-02", "2026-01-02"),
                )

            # The first connection has closed. Read through a new connection.
            with app.db() as conn:
                user = conn.execute(
                    "SELECT email FROM users WHERE id = ?", (user_id,)
                ).fetchone()
                expense = conn.execute(
                    "SELECT amount_cents, category FROM expenses WHERE user_id = ?",
                    (user_id,),
                ).fetchone()

        self.assertEqual(user["email"], "db-test@example.com")
        self.assertEqual(expense["amount_cents"], 1250)
        self.assertEqual(expense["category"], "Food")

    def test_failed_transaction_rolls_back_changes(self):
        with patch.object(app, "DB_PATH", self.db_path):
            app.initialize()

            with self.assertRaises(sqlite3.IntegrityError):
                with app.db() as conn:
                    conn.execute(
                        """
                        INSERT INTO users
                            (full_name, email, password_hash, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        ("Test User", "rollback@example.com", b"test-hash", "now"),
                    )
                    # Duplicate email violates the UNIQUE constraint.
                    conn.execute(
                        """
                        INSERT INTO users
                            (full_name, email, password_hash, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        ("Duplicate User", "rollback@example.com", b"other-hash", "now"),
                    )

            with app.db() as conn:
                result = conn.execute(
                    "SELECT COUNT(*) AS total FROM users WHERE email = ?",
                    ("rollback@example.com",),
                ).fetchone()

        self.assertEqual(result["total"], 0)

    def test_foreign_keys_are_enforced_on_each_connection(self):
        with patch.object(app, "DB_PATH", self.db_path):
            app.initialize()

            with app.db() as conn:
                foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
                self.assertEqual(foreign_keys, 1)

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO expenses
                            (user_id, amount_cents, category, spent_on, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (999999, 100, "Food", "2026-01-02", "now"),
                    )


if __name__ == "__main__":
    unittest.main()
