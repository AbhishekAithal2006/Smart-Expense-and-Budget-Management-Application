import http.cookiejar
import json
import os
import secrets
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg import sql

import app


class AppFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_database_url = os.environ.get("TEST_DATABASE_URL", "").strip()
        if not cls.test_database_url:
            raise RuntimeError("Set TEST_DATABASE_URL to a dedicated PostgreSQL test database.")
        cls.schema = f"app_test_{secrets.token_hex(6)}"
        cls.original_database_url = app.DATABASE_URL
        with psycopg.connect(cls.test_database_url) as connection:
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema)))
        app.DATABASE_URL = make_conninfo(
            cls.test_database_url,
            options=f"-c search_path={cls.schema}",
        )
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
            conn.execute("DELETE FROM expenses")
            conn.execute("DELETE FROM login_attempts")
            conn.execute("DELETE FROM users")
        self.cookies = http.cookiejar.CookieJar()
        self.client = build_opener(HTTPCookieProcessor(self.cookies))
        self.csrf = ""

    def request(self, path, method="GET", payload=None, csrf=True, csrf_token=None, cookie=None, no_cookie=False):
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if method not in ("GET", "HEAD") and csrf:
            token = self.csrf if csrf_token is None else csrf_token
            if token:
                headers["X-CSRF-Token"] = token
        if cookie is not None:
            headers["Cookie"] = f"session={cookie}"
        request = Request(self.base + path, data=data, headers=headers, method=method)
        client = build_opener() if no_cookie else self.client
        try:
            response = client.open(request, timeout=3)
        except HTTPError as error:
            response = error
        body = json.loads(response.read() or b"{}")
        return response.code, body

    def register(self, email="owner@example.com"):
        status, body = self.request("/api/register", "POST", {"fullName": "Test User", "email": email, "password": "correct-horse-123"}, csrf=False)
        self.assertEqual(status, 200, body)
        self.csrf = body["csrfToken"]

    def test_cross_user_expense_access_is_denied(self):
        self.register()
        today = app.date.today().isoformat()
        status, _ = self.request("/api/expenses", "POST", {"amount": 12, "category": "Food", "date": today, "description": "private expense"})
        self.assertEqual(status, 201)
        with app.db() as conn:
            owner_expense = conn.execute("SELECT id,user_id FROM expenses WHERE description=?", ("private expense",)).fetchone()
            owner_id = owner_expense["user_id"]
        expense_id = owner_expense["id"]

        status, _ = self.request("/api/logout", "POST")
        self.assertEqual(status, 200)
        self.csrf = ""
        self.register("other@example.com")

        status, history = self.request("/api/expenses")
        self.assertEqual(status, 200)
        self.assertEqual(history["expenses"], [])
        status, _ = self.request(
            f"/api/expenses/{expense_id}", "PATCH",
            {"amount": 99, "category": "Food", "date": today, "description": "tampered"},
        )
        self.assertEqual(status, 404)
        status, _ = self.request(f"/api/expenses/{expense_id}", "DELETE")
        self.assertEqual(status, 404)
        with app.db() as conn:
            unchanged = conn.execute("SELECT amount_cents,description,deleted_at FROM expenses WHERE id=? AND user_id=?", (expense_id, owner_id)).fetchone()
        self.assertEqual(unchanged["amount_cents"], 1200)
        self.assertEqual(unchanged["description"], "private expense")
        self.assertIsNone(unchanged["deleted_at"])

    def test_protected_reads_and_writes_require_authentication(self):
        for path in ("/api/profile", "/api/categories", "/api/expenses", "/api/summary"):
            with self.subTest(path=path):
                status, _ = self.request(path, no_cookie=True)
                self.assertEqual(status, 401)
        self.register()
        today = app.date.today().isoformat()
        status, category = self.request("/api/categories", "POST", {"name": "Travel custom"})
        self.assertEqual(status, 201)
        status, _ = self.request("/api/expenses", "POST", {"amount": 2, "category": "Food", "date": today})
        self.assertEqual(status, 201)
        with app.db() as conn:
            expense = conn.execute("SELECT id FROM expenses LIMIT 1").fetchone()

        protected_writes = (
            ("POST", "/api/categories", {"name": "Another category"}),
            ("POST", "/api/password", {"currentPassword": "correct-horse-123", "newPassword": "new-password-123", "confirmPassword": "new-password-123"}),
            ("POST", "/api/logout", {}),
            ("POST", "/api/expenses", {"amount": 2, "category": "Food", "date": today}),
            ("PATCH", "/api/profile", {"fullName": "Updated", "email": "owner@example.com", "mobile": ""}),
            ("PATCH", f"/api/categories/{category['id']}", {"name": "Travel renamed"}),
            ("PATCH", f"/api/expenses/{expense['id']}", {"amount": 3, "category": "Food", "date": today}),
            ("DELETE", f"/api/expenses/{expense['id']}", None),
        )
        for method, path, payload in protected_writes:
            with self.subTest(method=method, path=path):
                status, _ = self.request(path, method, payload, csrf=False, no_cookie=True)
                self.assertEqual(status, 401)

    def test_logout_invalidates_session(self):
        self.register()
        original_token = next(cookie.value for cookie in self.cookies if cookie.name == "session")
        status, _ = self.request("/api/logout", "POST")
        self.assertEqual(status, 200)
        self.csrf = ""
        status, _ = self.request("/api/profile", cookie=original_token)
        self.assertEqual(status, 401)

    def test_expired_session_is_rejected(self):
        self.register()
        with app.db() as conn:
            user = conn.execute("SELECT id FROM users WHERE email=?", ("owner@example.com",)).fetchone()
            conn.execute("UPDATE sessions SET expires_at=0 WHERE user_id=?", (user["id"],))
        status, _ = self.request("/api/profile")
        self.assertEqual(status, 401)
        status, _ = self.request("/api/expenses", "POST", {"amount": 2, "category": "Food", "date": app.date.today().isoformat()})
        self.assertEqual(status, 401)

    def test_database_constraints_reject_invalid_rows(self):
        self.register()
        with app.db() as conn:
            user = conn.execute("SELECT id FROM users WHERE email=?", ("owner@example.com",)).fetchone()
            with self.assertRaises(app.sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO expenses(user_id,amount_cents,category,spent_on,created_at) VALUES(?,?,?,?,?)",
                    (user["id"], 0, "Food", app.date.today().isoformat(), "now"),
                )
            with self.assertRaises(app.sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO expenses(user_id,amount_cents,category,spent_on,created_at) VALUES(?,?,?,?,?)",
                    (user["id"] + 9999, 100, "Food", app.date.today().isoformat(), "now"),
                )
            conn.execute(
                "INSERT INTO user_categories(user_id,name,created_at) VALUES(?,?,?)",
                (user["id"], "Groceries", "now"),
            )
            with self.assertRaises(app.sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO user_categories(user_id,name,created_at) VALUES(?,?,?)",
                    (user["id"], "groceries", "now"),
                )
            with self.assertRaises(app.sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO users(full_name,email,password_hash,created_at) VALUES(?,?,?,?)",
                    ("Duplicate", "OWNER@example.com", b"hash", "now"),
                )
            with self.assertRaises(app.sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at) VALUES(?,?,?,?)",
                    ("orphan-token", user["id"] + 9999, "csrf", 9999999999),
                )
            with self.assertRaises(app.sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO expenses(user_id,amount_cents,category,spent_on,created_at) VALUES(?,?,?,?,?)",
                    (user["id"], 100, None, app.date.today().isoformat(), "now"),
                )
            with self.assertRaises(app.sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO email_change_requests(user_id,new_email,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)",
                    (user["id"] + 9999, "new@example.com", "token", 9999999999, 1),
                )

    def test_database_not_null_constraints_reject_null_values(self):
        self.register()
        with app.db() as conn:
            user = conn.execute("SELECT id FROM users WHERE email=?", ("owner@example.com",)).fetchone()
            cases = (
                ("users.full_name", "INSERT INTO users(full_name,email,mobile,currency_code,avatar_url,password_hash,created_at) VALUES(?,?,?,?,?,?,?)", (None, "null-name@example.com", "", "INR", "", b"hash", "now")),
                ("users.email", "INSERT INTO users(full_name,email,mobile,currency_code,avatar_url,password_hash,created_at) VALUES(?,?,?,?,?,?,?)", ("Null email", None, "", "INR", "", b"hash", "now")),
                ("users.mobile", "INSERT INTO users(full_name,email,mobile,currency_code,avatar_url,password_hash,created_at) VALUES(?,?,?,?,?,?,?)", ("Null mobile", "null-mobile@example.com", None, "INR", "", b"hash", "now")),
                ("users.currency_code", "INSERT INTO users(full_name,email,mobile,currency_code,avatar_url,password_hash,created_at) VALUES(?,?,?,?,?,?,?)", ("Null currency", "null-currency@example.com", "", None, "", b"hash", "now")),
                ("users.avatar_url", "INSERT INTO users(full_name,email,mobile,currency_code,avatar_url,password_hash,created_at) VALUES(?,?,?,?,?,?,?)", ("Null avatar", "null-avatar@example.com", "", "INR", None, b"hash", "now")),
                ("users.password_hash", "INSERT INTO users(full_name,email,password_hash,created_at) VALUES(?,?,?,?)", ("Null hash", "null-hash@example.com", None, "now")),
                ("users.created_at", "INSERT INTO users(full_name,email,password_hash,created_at) VALUES(?,?,?,?)", ("Null created", "null-created@example.com", b"hash", None)),
                ("sessions.user_id", "INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at) VALUES(?,?,?,?)", ("null-session-user", None, "csrf", 9999999999)),
                ("sessions.csrf_token", "INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at) VALUES(?,?,?,?)", ("null-session-csrf", user["id"], None, 9999999999)),
                ("sessions.expires_at", "INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at) VALUES(?,?,?,?)", ("null-session-expiry", user["id"], "csrf", None)),
                ("expenses.user_id", "INSERT INTO expenses(user_id,amount_cents,category,spent_on,created_at) VALUES(?,?,?,?,?)", (None, 100, "Food", app.date.today().isoformat(), "now")),
                ("expenses.amount_cents", "INSERT INTO expenses(user_id,amount_cents,category,spent_on,created_at) VALUES(?,?,?,?,?)", (user["id"], None, "Food", app.date.today().isoformat(), "now")),
                ("expenses.spent_on", "INSERT INTO expenses(user_id,amount_cents,category,spent_on,created_at) VALUES(?,?,?,?,?)", (user["id"], 100, "Food", None, "now")),
                ("expenses.payment_method", "INSERT INTO expenses(user_id,amount_cents,category,spent_on,payment_method,created_at) VALUES(?,?,?,?,?,?)", (user["id"], 100, "Food", app.date.today().isoformat(), None, "now")),
                ("expenses.description", "INSERT INTO expenses(user_id,amount_cents,category,spent_on,description,created_at) VALUES(?,?,?,?,?,?)", (user["id"], 100, "Food", app.date.today().isoformat(), None, "now")),
                ("expenses.created_at", "INSERT INTO expenses(user_id,amount_cents,category,spent_on,created_at) VALUES(?,?,?,?,?)", (user["id"], 100, "Food", app.date.today().isoformat(), None)),
                ("user_categories.user_id", "INSERT INTO user_categories(user_id,name,created_at) VALUES(?,?,?)", (None, "Null owner", "now")),
                ("user_categories.name", "INSERT INTO user_categories(user_id,name,created_at) VALUES(?,?,?)", (user["id"], None, "now")),
                ("user_categories.created_at", "INSERT INTO user_categories(user_id,name,created_at) VALUES(?,?,?)", (user["id"], "Null created", None)),
                ("login_attempts.failures", "INSERT INTO login_attempts(email,failures,blocked_until) VALUES(?,?,?)", ("null-failures@example.com", None, 0)),
                ("login_attempts.blocked_until", "INSERT INTO login_attempts(email,failures,blocked_until) VALUES(?,?,?)", ("null-block@example.com", 0, None)),
                ("email_change_requests.new_email", "INSERT INTO email_change_requests(user_id,new_email,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)", (user["id"], None, "null-email-token", 9999999999, 1)),
                ("email_change_requests.token_hash", "INSERT INTO email_change_requests(user_id,new_email,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)", (user["id"], "new@example.com", None, 9999999999, 1)),
                ("email_change_requests.expires_at", "INSERT INTO email_change_requests(user_id,new_email,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)", (user["id"], "new@example.com", "null-expiry-token", None, 1)),
                ("email_change_requests.created_at", "INSERT INTO email_change_requests(user_id,new_email,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)", (user["id"], "new@example.com", "null-created-token", 9999999999, None)),
            )
            for column, sql, params in cases:
                with self.subTest(column=column):
                    with self.assertRaises(app.sqlite3.IntegrityError):
                        conn.execute(sql, params)

    def test_database_primary_keys_and_email_change_token_are_unique(self):
        self.register()
        with app.db() as conn:
            user = conn.execute("SELECT id FROM users WHERE email=?", ("owner@example.com",)).fetchone()
            session = conn.execute("SELECT token_hash,csrf_token,expires_at FROM sessions WHERE user_id=?", (user["id"],)).fetchone()
            with self.assertRaises(app.sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO users(id,full_name,email,password_hash,created_at) VALUES(?,?,?,?,?)",
                    (user["id"], "Duplicate id", "other-id@example.com", b"hash", "now"),
                )
            with self.assertRaises(app.sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at) VALUES(?,?,?,?)",
                    (session["token_hash"], user["id"], "another-csrf", session["expires_at"]),
                )
            conn.execute(
                "INSERT INTO expenses(id,user_id,amount_cents,category,spent_on,created_at) VALUES(?,?,?,?,?,?)",
                (7001, user["id"], 100, "Food", app.date.today().isoformat(), "now"),
            )
            with self.assertRaises(app.sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO expenses(id,user_id,amount_cents,category,spent_on,created_at) VALUES(?,?,?,?,?,?)",
                    (7001, user["id"], 200, "Food", app.date.today().isoformat(), "now"),
                )
            conn.execute(
                "INSERT INTO user_categories(id,user_id,name,created_at) VALUES(?,?,?,?)",
                (7002, user["id"], "Unique category", "now"),
            )
            with self.assertRaises(app.sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO user_categories(id,user_id,name,created_at) VALUES(?,?,?,?)",
                    (7002, user["id"], "Different category", "now"),
                )
            conn.execute("INSERT INTO login_attempts(email) VALUES(?)", ("attempt@example.com",))
            with self.assertRaises(app.sqlite3.IntegrityError):
                conn.execute("INSERT INTO login_attempts(email) VALUES(?)", ("ATTEMPT@example.com",))

            conn.execute(
                "INSERT INTO email_change_requests(user_id,new_email,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)",
                (user["id"], "first-new@example.com", "shared-token-hash", 9999999999, 1),
            )
            with self.assertRaises(app.sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO email_change_requests(user_id,new_email,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)",
                    (user["id"], "second-new@example.com", "different-token-hash", 9999999999, 2),
                )
            cursor = conn.execute(
                "INSERT INTO users(full_name,email,password_hash,created_at) VALUES(?,?,?,?)",
                ("Second user", "second@example.com", b"hash", "now"),
            )
            second_user_id = cursor.lastrowid
            with self.assertRaises(app.sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO email_change_requests(user_id,new_email,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)",
                    (second_user_id, "second-new@example.com", "shared-token-hash", 9999999999, 2),
                )

    def test_sql_injection_inputs_are_treated_as_data(self):
        self.register()
        today = app.date.today().isoformat()
        payload = "' OR 1=1 --"
        status, _ = self.request(
            "/api/expenses", "POST",
            {"amount": 5, "category": "Food", "date": today, "description": payload},
        )
        self.assertEqual(status, 201)
        status, history = self.request("/api/expenses?q=%27%20OR%201%3D1%20--")
        self.assertEqual(status, 200)
        self.assertEqual(len(history["expenses"]), 1)
        self.assertEqual(history["expenses"][0]["description"], payload)
        status, _ = self.request(
            "/api/expenses", "POST",
            {"amount": 7, "category": "Travel", "date": today, "description": "ordinary expense"},
        )
        self.assertEqual(status, 201)
        status, history = self.request("/api/expenses?category=%27%20OR%201%3D1%20--")
        self.assertEqual(status, 200)
        self.assertEqual(history["expenses"], [])
        status, history = self.request("/api/expenses?q=%27%20OR%201%3D1%20--")
        self.assertEqual(status, 200)
        self.assertEqual([row["description"] for row in history["expenses"]], [payload])
        injection_name = "x' OR 1 LIKE 1 --"
        status, category = self.request("/api/categories", "POST", {"name": injection_name})
        self.assertEqual(status, 201)
        status, categories = self.request("/api/categories")
        self.assertEqual(status, 200)
        self.assertIn(injection_name, [item["name"] for item in categories["custom"]])

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
        today = app.date.today().isoformat()
        status, category = self.request("/api/categories", "POST", {"name": "Trips"})
        self.assertEqual(status, 201)
        status, _ = self.request("/api/expenses", "POST", {"amount": 2, "category": "Food", "date": today})
        self.assertEqual(status, 201)
        with app.db() as conn:
            expense = conn.execute("SELECT id FROM expenses LIMIT 1").fetchone()
        protected_writes = (
            ("POST", "/api/categories", {"name": "Another category"}),
            ("POST", "/api/password", {"currentPassword": "correct-horse-123", "newPassword": "new-password-123", "confirmPassword": "new-password-123"}),
            ("POST", "/api/logout", {}),
            ("POST", "/api/expenses", {"amount": 2, "category": "Food", "date": today}),
            ("PATCH", "/api/profile", {"fullName": "Updated", "email": "owner@example.com", "mobile": ""}),
            ("PATCH", f"/api/categories/{category['id']}", {"name": "Trips renamed"}),
            ("PATCH", f"/api/expenses/{expense['id']}", {"amount": 3, "category": "Food", "date": today}),
            ("DELETE", f"/api/expenses/{expense['id']}", None),
        )
        for method, path, payload in protected_writes:
            for label, csrf_kwargs in (("missing", {"csrf": False}), ("invalid", {"csrf_token": "invalid-csrf-token"})):
                with self.subTest(method=method, path=path, token=label):
                    status, _ = self.request(path, method, payload, **csrf_kwargs)
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
