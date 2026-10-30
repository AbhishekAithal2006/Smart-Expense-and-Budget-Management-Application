"""NFR01 - Performance checks for analytics, budgets, alerts, reports, the whole
dashboard and several users working at the same time.

test_performance.py covers summary, history, search and filtering. This file
covers the remaining acceptance criteria of issue #13.
"""
import http.cookiejar
import json
import os
import secrets
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from http.server import ThreadingHTTPServer
from urllib.request import HTTPCookieProcessor, Request, build_opener

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

import app

SEED_ROWS = 5000
MAX_SECONDS = 1.0            # one request
MAX_DASHBOARD_SECONDS = 3.0  # all dashboard requests together
CONCURRENT_USERS = 10
MAX_CONCURRENT_SECONDS = 10.0
DASHBOARD_PATHS = ("/api/categories", "/api/summary", "/api/expenses", "/api/budgets", "/api/analytics", "/api/alerts", "/api/notifications")


class DashboardPerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_database_url = os.environ.get("TEST_DATABASE_URL", "").strip()
        if not cls.test_database_url:
            raise RuntimeError("Set TEST_DATABASE_URL to a dedicated PostgreSQL test database.")
        cls.schema = f"app_perf_dash_{secrets.token_hex(6)}"
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
        body = json.dumps({"fullName": "Perf Dash", "email": "dash@example.com", "password": "correct-horse-battery"}).encode()
        request = Request(cls.base + "/api/register", data=body, headers={"Content-Type": "application/json"})
        cls.client.open(request).read()
        today = date.today()
        now = datetime.now(timezone.utc).isoformat()
        with app.db() as conn:
            user_id = conn.execute("SELECT id FROM users WHERE email=?", ("dash@example.com",)).fetchone()["id"]
            categories = app.PREDEFINED_CATEGORIES
            for i in range(SEED_ROWS):
                conn.execute(
                    "INSERT INTO expenses(user_id,amount_cents,category,spent_on,payment_method,description,created_at) VALUES(?,?,?,?,?,?,?)",
                    (user_id, 100 + i, categories[i % len(categories)], f"{today.year}-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}", "UPI", f"item {i}", now),
                )
            for month in range(1, 13):
                conn.execute(
                    "INSERT INTO budgets(user_id,budget_type,amount_cents,start_date,end_date,created_at) VALUES(?,?,?,?,?,?)",
                    (user_id, "monthly", 100000, f"{today.year}-{month:02d}-01", f"{today.year}-{month:02d}-28", now),
                )
        cls.month = today.strftime("%Y-%m")
        cls.year = str(today.year)

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
        self.assertEqual(response.status, 200, path)
        return elapsed

    def test_analytics_loads_quickly(self):
        self.assertLess(self.timed_get("/api/analytics"), MAX_SECONDS)

    def test_budgets_and_alerts_load_quickly(self):
        self.assertLess(self.timed_get("/api/budgets"), MAX_SECONDS)
        self.assertLess(self.timed_get("/api/alerts"), MAX_SECONDS)

    def test_categories_and_profile_load_quickly(self):
        self.assertLess(self.timed_get("/api/categories"), MAX_SECONDS)
        self.assertLess(self.timed_get("/api/me"), MAX_SECONDS)

    def test_reports_generate_quickly(self):
        self.assertLess(self.timed_get(f"/api/report?type=monthly&period={self.month}"), MAX_SECONDS)
        self.assertLess(self.timed_get(f"/api/report?type=yearly&period={self.year}"), MAX_SECONDS)

    def test_whole_dashboard_loads_quickly(self):
        # The browser requests all of these at the same time when the dashboard opens.
        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=len(DASHBOARD_PATHS)) as pool:
            list(pool.map(self.timed_get, DASHBOARD_PATHS))
        self.assertLess(time.perf_counter() - start, MAX_DASHBOARD_SECONDS)

    def test_system_stays_responsive_with_several_users(self):
        def one_user(_):
            for path in DASHBOARD_PATHS:
                self.timed_get(path)

        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=CONCURRENT_USERS) as pool:
            list(pool.map(one_user, range(CONCURRENT_USERS)))
        self.assertLess(time.perf_counter() - start, MAX_CONCURRENT_SECONDS)
        self.assertLess(self.timed_get("/api/summary"), MAX_SECONDS)  # still quick afterwards


if __name__ == "__main__":
    unittest.main()
