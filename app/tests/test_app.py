import http.cookiejar
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

import app


class AppFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        app.DB_PATH = Path(cls.temp.name) / "tracker.sqlite3"
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
        cls.temp.cleanup()

    def setUp(self):
        with app.db() as conn:
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM expenses")
            conn.execute("DELETE FROM login_attempts")
            conn.execute("DELETE FROM users")
        self.cookies = http.cookiejar.CookieJar()
        self.client = build_opener(HTTPCookieProcessor(self.cookies))
        self.csrf = ""

    def request(self, path, method="GET", payload=None, csrf=True):
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if method not in ("GET", "HEAD") and csrf and self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            response = self.client.open(request, timeout=3)
        except HTTPError as error:
            response = error
        body = json.loads(response.read() or b"{}")
        return response.code, body

    def register(self, email="owner@example.com"):
        status, body = self.request("/api/register", "POST", {"fullName": "Test User", "email": email, "password": "correct-horse-123"}, csrf=False)
        self.assertEqual(status, 200, body)
        self.csrf = body["csrfToken"]

    def test_registration_profile_expenses_and_owner_filter(self):
        self.register()
        today = app.date.today()
        month = today.strftime("%Y-%m")
        status, profile = self.request("/api/profile")
        self.assertEqual(status, 200)
        self.assertEqual(profile["fullName"], "Test User")
        status, updated = self.request("/api/profile", "PATCH", {"fullName": "Updated User", "email": "owner@example.com", "mobile": "+91 98765 43210"})
        self.assertEqual(status, 200)
        self.assertEqual(updated["mobile"], "+91 98765 43210")
        status, _ = self.request("/api/expenses", "POST", {"amount": "850", "category": "Food", "date": today.isoformat(), "description": "Groceries"})
        self.assertEqual(status, 201)
        status, summary = self.request("/api/summary")
        self.assertEqual(status, 200)
        self.assertEqual(summary["expenseCount"], 1)
        status, history = self.request(f"/api/expenses?q=Groceries&month={month}")
        self.assertEqual(status, 200)
        self.assertEqual(len(history["expenses"]), 1)
        self.assertEqual(history["expenses"][0]["amount_cents"], 85000)

        expense_id = history["expenses"][0]["id"]
        status, _ = self.request(f"/api/expenses/{expense_id}", "PATCH", {"amount": "900", "category": "Food", "date": today.isoformat(), "description": "Updated groceries", "paymentMethod": "UPI"})
        self.assertEqual(status, 200)
        status, history = self.request("/api/expenses?q=Updated")
        self.assertEqual(status, 200)
        self.assertEqual(history["expenses"][0]["amount_cents"], 90000)
        status, _ = self.request(f"/api/expenses/{expense_id}", "DELETE")
        self.assertEqual(status, 200)
        status, history = self.request("/api/expenses")
        self.assertEqual(status, 200)
        self.assertEqual(len(history["expenses"]), 1)
        self.assertIsNotNone(history["expenses"][0]["deleted_at"])
        status, summary = self.request("/api/summary")
        self.assertEqual(status, 200)
        self.assertEqual(summary["expenseCount"], 0)

        self.request("/api/logout", "POST")
        self.csrf = ""
        self.register("other@example.com")
        status, history = self.request("/api/expenses")
        self.assertEqual(status, 200)
        self.assertEqual(history["expenses"], [])

    def test_write_requires_csrf_and_login_throttles(self):
        self.register()
        status, _ = self.request("/api/expenses", "POST", {"amount": 2, "category": "Food", "date": app.date.today().isoformat()}, csrf=False)
        self.assertEqual(status, 403)
        self.request("/api/logout", "POST")
        self.csrf = ""
        for _ in range(5):
            status, _ = self.request("/api/login", "POST", {"email": "owner@example.com", "password": "wrong-password"}, csrf=False)
            self.assertEqual(status, 401)
        status, body = self.request("/api/login", "POST", {"email": "owner@example.com", "password": "correct-horse-123"}, csrf=False)
        self.assertEqual(status, 429)
        self.assertIn("15 minutes", body["error"])


if __name__ == "__main__":
    unittest.main()
