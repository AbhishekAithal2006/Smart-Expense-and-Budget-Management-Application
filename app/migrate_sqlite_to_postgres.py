"""Merge local SQLite accounts, expenses, and categories into PostgreSQL."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import psycopg

import app


USER_COLUMNS = (
    "full_name", "email", "mobile", "currency_code", "google_sub", "avatar_url",
    "password_hash", "created_at",
)
EXPENSE_COLUMNS = (
    "amount_cents", "category", "spent_on", "payment_method", "description", "deleted_at", "created_at",
)


def _source_rows(source: sqlite3.Connection, table: str, columns: tuple[str, ...]):
    exists = source.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return []
    available = {row[1] for row in source.execute(f"PRAGMA table_info({table})")}
    selected = [column for column in columns if column in available]
    if not selected:
        return []
    return source.execute(f"SELECT id,{','.join(selected)} FROM {table}").fetchall()


def migrate(source_path: Path) -> dict[str, int]:
    if not app.DATABASE_URL:
        raise RuntimeError("Set DATABASE_URL to the target PostgreSQL database first.")
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite source database not found: {source_path}")

    app.initialize()
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    counts = {"users": 0, "expenses": 0, "user_categories": 0}
    try:
        with app.db() as target:
            source_users = _source_rows(source, "users", USER_COLUMNS)
            target_users = target.execute("SELECT id,email,google_sub FROM users").fetchall()
            users_by_email = {row["email"].strip().lower(): row for row in target_users}
            users_by_google = {row["google_sub"]: row for row in target_users if row["google_sub"]}
            user_ids: dict[int, int] = {}

            for old_user in source_users:
                old_email = str(old_user["email"]).strip().lower()
                old_google = old_user["google_sub"] if "google_sub" in old_user.keys() else None
                existing = users_by_email.get(old_email) or (users_by_google.get(old_google) if old_google else None)
                if existing:
                    user_ids[old_user["id"]] = existing["id"]
                    continue

                columns = [column for column in USER_COLUMNS if column in old_user.keys()]
                values = [old_user[column] for column in columns]
                placeholders = ",".join("?" for _ in columns)
                inserted = target.execute(
                    f"INSERT INTO users ({','.join(columns)}) VALUES ({placeholders}) "
                    "ON CONFLICT DO NOTHING RETURNING id",
                    values,
                ).fetchone()
                if inserted:
                    user_id = inserted["id"]
                    counts["users"] += 1
                else:
                    # An email or Google identity may have been created between the lookup and insert.
                    current = target.execute(
                        "SELECT id,email,google_sub FROM users WHERE LOWER(email)=LOWER(?) OR (google_sub IS NOT NULL AND google_sub=?) LIMIT 1",
                        (old_email, old_google),
                    ).fetchone()
                    if not current:
                        raise RuntimeError("A source account conflicted with a PostgreSQL account and could not be matched.")
                    user_id = current["id"]
                user_ids[old_user["id"]] = user_id
                current = target.execute("SELECT id,email,google_sub FROM users WHERE id=?", (user_id,)).fetchone()
                users_by_email[current["email"].strip().lower()] = current
                if current["google_sub"]:
                    users_by_google[current["google_sub"]] = current

            for old_category in _source_rows(source, "user_categories", ("user_id", "name", "created_at")):
                user_id = user_ids.get(old_category["user_id"])
                if user_id is None:
                    continue
                exists = target.execute(
                    "SELECT 1 FROM user_categories WHERE user_id=? AND LOWER(name)=LOWER(?)",
                    (user_id, old_category["name"]),
                ).fetchone()
                if exists:
                    continue
                target.execute(
                    "INSERT INTO user_categories(user_id,name,created_at) VALUES(?,?,?) ON CONFLICT DO NOTHING",
                    (user_id, old_category["name"], old_category["created_at"]),
                )
                counts["user_categories"] += 1

            expense_rows = _source_rows(source, "expenses", ("user_id", *EXPENSE_COLUMNS))
            for old_expense in expense_rows:
                user_id = user_ids.get(old_expense["user_id"])
                if user_id is None:
                    continue
                category = old_expense["category"]
                spent_on = old_expense["spent_on"]
                amount = old_expense["amount_cents"]
                description = old_expense["description"] if "description" in old_expense.keys() else ""
                duplicate = target.execute(
                    "SELECT 1 FROM expenses WHERE user_id=? AND amount_cents=? AND category=? AND spent_on=? "
                    "AND payment_method=? AND description=? AND deleted_at IS NOT DISTINCT FROM ? LIMIT 1",
                    (
                        user_id, amount, category, spent_on,
                        old_expense["payment_method"] if "payment_method" in old_expense.keys() else "",
                        description,
                        old_expense["deleted_at"] if "deleted_at" in old_expense.keys() else None,
                    ),
                ).fetchone()
                if duplicate:
                    continue
                columns = [column for column in EXPENSE_COLUMNS if column in old_expense.keys()]
                values = [old_expense[column] for column in columns]
                columns.insert(0, "user_id")
                values.insert(0, user_id)
                target.execute(
                    f"INSERT INTO expenses ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    values,
                )
                counts["expenses"] += 1
    finally:
        source.close()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sqlite_database", type=Path, help="Path to the existing tracker.sqlite3 file")
    args = parser.parse_args()
    try:
        counts = migrate(args.sqlite_database.expanduser().resolve())
    except (FileNotFoundError, RuntimeError, psycopg.Error, sqlite3.Error) as error:
        print(f"Migration failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print("Merged into PostgreSQL: " + ", ".join(f"{count} {table}" for table, count in counts.items()))
    print("The SQLite source was left unchanged.")


if __name__ == "__main__":
    main()
