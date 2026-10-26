"""NFR01 - Performance checks for dashboard, history, search and filtering."""
import http.cookiejar
import json
import os
import secrets
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from urllib.request import HTTPCookieProcessor, Request, build_opener

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

import app

SEED_ROWS = 5000
MAX_SECONDS = 1.0  # acceptable response time for each request


class PerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_database_url = os.environ.get("TEST_DATABASE_URL", "").strip()
        if not cls.test_database_url:
            raise RuntimeError("Set TEST_DATABASE_URL to a dedicated PostgreSQL test database.")
        cls.schema = f"app_perf_{secrets.token_hex(6)}"
        cls.original_database_url = app.DATABASE_URL
        with psycopg.connect(cls.test_database_url) as connection:
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema)))
        app.DATABASE_URL = make_conninfo(cls.test_database_url, options=f"-c search_path={cls.schema}")
        app.initialize()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.client = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        body = json.dumps({"fullName": "Perf User", "email": "perf@example.com", "password": "correct-horse-battery"}).encode()
        request = Request(cls.base + "/api/register", data=body, headers={"Content-Type": "application/json"})
        cls.client.open(request).read()
        now = datetime.now(timezone.utc).isoformat()
        with app.db() as conn:
            user_id = conn.execute("SELECT id FROM users WHERE email=?", ("perf@example.com",)).fetchone()["id"]
            categories = app.PREDEFINED_CATEGORIES
            for i in range(SEED_ROWS):
                conn.execute(
                    "INSERT INTO expenses(user_id,amount_cents,category,spent_on,payment_method,description,created_at) VALUES(?,?,?,?,?,?,?)",
                    (user_id, 100 + i, categories[i % len(categories)], f"2026-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}", "UPI", f"item {i}", now),
                )

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        with psycopg.connect(cls.test_database_url) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))
        app.DATABASE_URL = cls.original_database_url

    def timed_get(self, path):
        start = time.perf_counter()
        response = self.client.open(Request(self.base + path))
        response.read()
        elapsed = time.perf_counter() - start
        self.assertEqual(response.status, 200)
        return elapsed

    def test_dashboard_summary_is_fast(self):
        self.assertLess(self.timed_get("/api/summary"), MAX_SECONDS)

    def test_history_loads_quickly(self):
        self.assertLess(self.timed_get("/api/expenses"), MAX_SECONDS)

    def test_search_responds_quickly(self):
        self.assertLess(self.timed_get("/api/expenses?q=item"), MAX_SECONDS)

    def test_category_and_month_filter_respond_quickly(self):
        self.assertLess(self.timed_get("/api/expenses?category=Food&month=2026-03"), MAX_SECONDS)


if __name__ == "__main__":
    unittest.main()