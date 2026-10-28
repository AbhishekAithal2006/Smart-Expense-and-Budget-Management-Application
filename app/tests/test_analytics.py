"""FR11 - Expense and budget analytics."""
import calendar
import http.cookiejar
import json
import os
import secrets
import threading
import unittest
from datetime import date
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

import app


class AnalyticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_database_url = os.environ.get("TEST_DATABASE_URL", "").strip()
        if not cls.test_database_url:
            raise RuntimeError("Set TEST_DATABASE_URL to a dedicated PostgreSQL test database.")
        cls.schema = f"app_analytics_{secrets.token_hex(6)}"
        cls.original_database_url = app.DATABASE_URL
        with psycopg.connect(cls.test_database_url) as connection:
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema)))
        app.DATABASE_URL = make_conninfo(cls.test_database_url, options=f"-c search_path={cls.schema}")
        app.initialize()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        with psycopg.connect(cls.test_database_url) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))
        app.DATABASE_URL = cls.original_database_url

    def setUp(self):
        with app.db() as conn:
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM budgets")
            conn.execute("DELETE FROM expenses")
            conn.execute("DELETE FROM login_attempts")
            conn.execute("DELETE FROM users")
        self.client = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = ""

    def request(self, path, method="GET", payload=None, authenticated=True):
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if method != "GET" and self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        client = self.client if authenticated else build_opener()
        try:
            response = client.open(Request(self.base + path, data=data, headers=headers, method=method), timeout=3)
        except HTTPError as error:
            response = error
        return response.code, json.loads(response.read() or b"{}")

    def register(self, email="analytics@example.com"):
        status, body = self.request("/api/register", "POST", {"fullName": "Analytics User", "email": email, "password": "correct-horse-123"})
        self.assertEqual(status, 200, body)
        self.csrf = body["csrfToken"]

    def add_expense(self, amount, category, spent_on):
        status, _ = self.request("/api/expenses", "POST", {"amount": str(amount), "category": category, "date": spent_on.isoformat()})
        self.assertEqual(status, 201)

    def add_budget(self, amount, start, end, budget_type="monthly"):
        status, body = self.request("/api/budgets", "POST", {"budgetType": budget_type, "amount": str(amount), "startDate": start.isoformat(), "endDate": end.isoformat()})
        self.assertEqual(status, 201, body)

    def month_range(self):
        today = date.today()
        return today.replace(day=1), today.replace(day=calendar.monthrange(today.year, today.month)[1])

    def test_analytics_requires_login(self):
        status, _ = self.request("/api/analytics", authenticated=False)
        self.assertEqual(status, 401)

    def test_new_user_sees_zero_analytics(self):
        self.register()
        status, data = self.request("/api/analytics")
        self.assertEqual(status, 200)
        self.assertEqual(data["totalSpentCents"], 0)
        self.assertEqual(data["monthCount"], 0)
        self.assertEqual(data["monthAverageCents"], 0)
        self.assertEqual(data["categories"], [])
        self.assertEqual(data["budgets"], [])

    def test_totals_statistics_and_category_breakdown(self):
        self.register()
        today = date.today()
        first, _ = self.month_range()
        self.add_expense(100, "Food", today)
        self.add_expense(300, "Travel", today)
        self.add_expense(100, "Food", first)
        self.add_expense(500, "Other", date(first.year - 1, first.month, 1))  # an older year: counts in total only
        status, data = self.request("/api/analytics")
        self.assertEqual(status, 200)
        self.assertEqual(data["totalSpentCents"], 100000)
        self.assertEqual(data["totalCount"], 4)
        self.assertEqual(data["monthSpentCents"], 50000)
        self.assertEqual(data["monthCount"], 3)
        self.assertEqual(data["monthAverageCents"], 16667)
        self.assertEqual(data["monthLargestCents"], 30000)
        self.assertEqual([c["category"] for c in data["categories"]], ["Travel", "Food"])
        self.assertEqual(data["categories"][0]["percent"], 60.0)
        self.assertEqual(data["categories"][1]["spentCents"], 20000)

    def test_budget_comparison_and_status(self):
        self.register()
        first, last = self.month_range()
        self.add_budget(1000, first, last)
        self.add_expense(700, "Food", date.today())
        status, data = self.request("/api/analytics")
        budget = data["budgets"][0]
        self.assertEqual(budget["spentCents"], 70000)
        self.assertEqual(budget["remainingCents"], 30000)
        self.assertEqual(budget["percentUsed"], 70.0)
        self.assertEqual(budget["status"], "ok")
        self.assertTrue(budget["active"])
        self.add_expense(150, "Food", date.today())
        _, data = self.request("/api/analytics")
        self.assertEqual(data["budgets"][0]["status"], "near")
        self.add_expense(300, "Food", date.today())
        _, data = self.request("/api/analytics")
        self.assertEqual(data["budgets"][0]["status"], "over")
        self.assertEqual(data["budgets"][0]["remainingCents"], -15000)

    def test_expenses_outside_the_budget_period_are_ignored(self):
        self.register()
        first, last = self.month_range()
        self.add_budget(1000, first, last)
        self.add_expense(400, "Food", date(first.year - 1, first.month, 1))
        _, data = self.request("/api/analytics")
        self.assertEqual(data["budgets"][0]["spentCents"], 0)

    def test_analytics_update_when_transactions_change(self):
        self.register()
        today = date.today()
        self.add_expense(250, "Food", today)
        _, data = self.request("/api/analytics")
        self.assertEqual(data["totalSpentCents"], 25000)
        _, history = self.request("/api/expenses")
        expense_id = history["expenses"][0]["id"]
        status, _ = self.request(f"/api/expenses/{expense_id}", "PATCH", {"amount": "400", "category": "Food", "date": today.isoformat(), "description": "", "paymentMethod": ""})
        self.assertEqual(status, 200)
        _, data = self.request("/api/analytics")
        self.assertEqual(data["totalSpentCents"], 40000)
        status, _ = self.request(f"/api/expenses/{expense_id}", "DELETE")
        self.assertEqual(status, 200)
        _, data = self.request("/api/analytics")
        self.assertEqual(data["totalSpentCents"], 0)
        self.assertEqual(data["totalCount"], 0)

    def test_users_only_see_their_own_analytics(self):
        self.register("first@example.com")
        self.add_expense(900, "Food", date.today())
        self.client = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.register("second@example.com")
        _, data = self.request("/api/analytics")
        self.assertEqual(data["totalSpentCents"], 0)


if __name__ == "__main__":
    unittest.main()
