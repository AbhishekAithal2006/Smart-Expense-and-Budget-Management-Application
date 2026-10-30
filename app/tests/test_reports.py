"""FR12 - Monthly and yearly report generation."""
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


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_database_url = os.environ.get("TEST_DATABASE_URL", "").strip()
        if not cls.test_database_url:
            raise RuntimeError("Set TEST_DATABASE_URL to a dedicated PostgreSQL test database.")
        cls.schema = f"app_reports_{secrets.token_hex(6)}"
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

    def register(self, email="reports@example.com"):
        status, body = self.request("/api/register", "POST", {"fullName": "Report User", "email": email, "password": "correct-horse-123"})
        self.assertEqual(status, 200, body)
        self.csrf = body["csrfToken"]

    def add_expense(self, amount, category, spent_on, description=""):
        status, _ = self.request("/api/expenses", "POST", {"amount": str(amount), "category": category, "date": spent_on, "description": description})
        self.assertEqual(status, 201)

    def add_budget(self, amount, start, end, budget_type="monthly"):
        status, body = self.request("/api/budgets", "POST", {"budgetType": budget_type, "amount": str(amount), "startDate": start, "endDate": end})
        self.assertEqual(status, 201, body)

    def report(self, report_type, period):
        status, data = self.request(f"/api/report?type={report_type}&period={period}")
        self.assertEqual(status, 200, data)
        return data

    def test_report_requires_login(self):
        status, _ = self.request("/api/report?type=monthly&period=2026-08", authenticated=False)
        self.assertEqual(status, 401)

    def test_invalid_periods_are_rejected(self):
        self.register()
        for query in ("", "type=monthly", "type=weekly&period=2026-08", "type=monthly&period=2026-13",
                      "type=monthly&period=2026", "type=monthly&period=abcd-ef", "type=yearly&period=1999",
                      "type=yearly&period=2026-08", "type=yearly&period=20266", "type=yearly&period=2101"):
            status, body = self.request(f"/api/report?{query}")
            self.assertEqual(status, 400, query)
            self.assertIn("valid", body["error"])

    def test_monthly_report_contains_expense_and_budget_information(self):
        self.register()
        self.add_expense(100, "Food", "2026-08-05", "Groceries")
        self.add_expense(300, "Travel", "2026-08-20")
        self.add_expense(999, "Food", "2026-09-02")
        self.add_budget(1000, "2026-08-01", "2026-08-31")
        data = self.report("monthly", "2026-08")
        self.assertEqual(data["startDate"], "2026-08-01")
        self.assertEqual(data["endDate"], "2026-08-31")
        self.assertEqual(data["totalSpentCents"], 40000)
        self.assertEqual(data["expenseCount"], 2)
        self.assertEqual(data["averageCents"], 20000)
        self.assertEqual(data["largestCents"], 30000)
        self.assertEqual([c["category"] for c in data["categories"]], ["Travel", "Food"])
        self.assertEqual([e["spent_on"] for e in data["expenses"]], ["2026-08-20", "2026-08-05"])
        self.assertEqual(data["months"], [])
        budget = data["budgets"][0]
        self.assertEqual(budget["spentCents"], 40000)
        self.assertEqual(budget["remainingCents"], 60000)
        self.assertEqual(budget["status"], "ok")

    def test_month_boundaries_are_inclusive(self):
        self.register()
        self.add_expense(10, "Food", "2026-08-01")
        self.add_expense(20, "Food", "2026-08-31")
        self.add_expense(40, "Food", "2026-09-01")
        self.add_expense(80, "Food", "2026-07-31")
        self.assertEqual(self.report("monthly", "2026-08")["totalSpentCents"], 3000)
        self.add_expense(5, "Food", "2026-12-31")
        self.assertEqual(self.report("monthly", "2026-12")["totalSpentCents"], 500)

    def test_empty_period_returns_zero_report(self):
        self.register()
        data = self.report("monthly", "2026-09")
        self.assertEqual(data["totalSpentCents"], 0)
        self.assertEqual(data["expenseCount"], 0)
        self.assertEqual(data["averageCents"], 0)
        self.assertEqual(data["categories"], [])
        self.assertEqual(data["expenses"], [])
        self.assertEqual(data["budgets"], [])

    def test_yearly_report_covers_the_whole_year(self):
        self.register()
        self.add_expense(100, "Food", "2026-01-15")
        self.add_expense(200, "Food", "2026-08-10")
        self.add_expense(300, "Home", "2026-12-31")
        self.add_expense(900, "Food", "2025-12-31")
        self.add_expense(900, "Food", "2027-01-01")
        self.add_budget(5000, "2026-01-01", "2026-12-31", "yearly")
        data = self.report("yearly", "2026")
        self.assertEqual(data["totalSpentCents"], 60000)
        self.assertEqual(data["expenseCount"], 3)
        self.assertEqual(len(data["months"]), 12)
        self.assertEqual(data["months"][0], {"month": "2026-01", "spentCents": 10000, "count": 1})
        self.assertEqual(data["months"][1]["count"], 0)
        self.assertEqual(data["months"][11]["spentCents"], 30000)
        self.assertEqual(data["expenses"], [])
        self.assertEqual(data["budgets"][0]["type"], "yearly")
        self.assertEqual(data["budgets"][0]["spentCents"], 60000)

    def test_only_budgets_overlapping_the_period_are_listed(self):
        self.register()
        self.add_budget(1000, "2026-08-01", "2026-08-31")
        self.add_budget(1000, "2026-03-01", "2026-03-31")
        self.assertEqual(len(self.report("monthly", "2026-08")["budgets"]), 1)
        self.assertEqual(len(self.report("yearly", "2026")["budgets"]), 2)
        self.assertEqual(self.report("yearly", "2025")["budgets"], [])

    def test_budget_status_in_report(self):
        self.register()
        self.add_budget(1000, "2026-08-01", "2026-08-31")
        self.add_expense(850, "Food", "2026-08-10")
        self.assertEqual(self.report("monthly", "2026-08")["budgets"][0]["status"], "near")
        self.add_expense(300, "Food", "2026-08-11")
        self.assertEqual(self.report("monthly", "2026-08")["budgets"][0]["status"], "over")

    def test_report_reflects_edited_and_deleted_expenses(self):
        self.register()
        self.add_expense(500, "Food", "2026-08-10")
        self.assertEqual(self.report("monthly", "2026-08")["totalSpentCents"], 50000)
        _, history = self.request("/api/expenses")
        expense_id = history["expenses"][0]["id"]
        status, _ = self.request(f"/api/expenses/{expense_id}", "PATCH", {"amount": "700", "category": "Food", "date": "2026-08-10", "description": "", "paymentMethod": ""})
        self.assertEqual(status, 200)
        self.assertEqual(self.report("monthly", "2026-08")["totalSpentCents"], 70000)
        status, _ = self.request(f"/api/expenses/{expense_id}", "DELETE")
        self.assertEqual(status, 200)
        data = self.report("monthly", "2026-08")
        self.assertEqual(data["totalSpentCents"], 0)
        self.assertEqual(data["expenses"], [])

    def test_users_only_see_their_own_report(self):
        self.register("first@example.com")
        self.add_expense(900, "Food", "2026-08-10")
        self.add_budget(1000, "2026-08-01", "2026-08-31")
        self.client = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.register("second@example.com")
        data = self.report("monthly", "2026-08")
        self.assertEqual(data["totalSpentCents"], 0)
        self.assertEqual(data["budgets"], [])


if __name__ == "__main__":
    unittest.main()
