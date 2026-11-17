import http.cookiejar
import json
import os
import secrets
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, Request, build_opener
from unittest.mock import patch

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
            conn.execute("DELETE FROM system_activity")
            conn.execute("DELETE FROM users")
        self.cookies = http.cookiejar.CookieJar()
        self.client = build_opener(HTTPCookieProcessor(self.cookies))
        self.csrf = ""

    def request(self, path, method="GET", payload=None, csrf=True, csrf_token=None, cookie=None, no_cookie=False, extra_headers=None, raw_data=None):
        data = raw_data if raw_data is not None else (None if payload is None else json.dumps(payload).encode())
        headers = {"Content-Type": "application/json"}
        if method not in ("GET", "HEAD") and csrf:
            token = self.csrf if csrf_token is None else csrf_token
            if token:
                headers["X-CSRF-Token"] = token
        if cookie is not None:
            headers["Cookie"] = f"session={cookie}"
        if extra_headers is not None:
            headers.update(extra_headers)
        request = Request(self.base + path, data=data, headers=headers, method=method)
        client = build_opener() if no_cookie else self.client
        try:
            response = client.open(request, timeout=3)
        except HTTPError as error:
            response = error
        raw_body = response.read() or b"{}"
        try:
            body = json.loads(raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = raw_body.decode("utf-8", errors="replace")
        return response.code, body

    def register(self, email="owner@example.com"):
        status, body = self.request("/api/register", "POST", {"fullName": "Test User", "email": email, "password": "correct-horse-123"}, csrf=False)
        self.assertEqual(status, 200, body)
        self.csrf = body["csrfToken"]

    def login(self, email):
        status, body = self.request("/api/login", "POST", {"email": email, "password": "correct-horse-123"}, csrf=False)
        self.assertEqual(status, 200, body)
        self.csrf = body["csrfToken"]

    def assert_integrity_error(self, statement, params):
        with self.assertRaises(psycopg.IntegrityError):
            with app.db() as conn:
                conn.execute(statement, params)

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
            conn.execute(
                "INSERT INTO user_categories(user_id,name,created_at) VALUES(?,?,?)",
                (user["id"], "Groceries", "now"),
            )
            user_id = user["id"]

        cases = (
            ("INSERT INTO expenses(user_id,amount_cents,category,spent_on,created_at) VALUES(?,?,?,?,?)", (user_id, 0, "Food", app.date.today().isoformat(), "now")),
            ("INSERT INTO expenses(user_id,amount_cents,category,spent_on,created_at) VALUES(?,?,?,?,?)", (user_id + 9999, 100, "Food", app.date.today().isoformat(), "now")),
            ("INSERT INTO user_categories(user_id,name,created_at) VALUES(?,?,?)", (user_id, "groceries", "now")),
            ("INSERT INTO users(full_name,email,password_hash,created_at) VALUES(?,?,?,?)", ("Duplicate", "OWNER@example.com", b"hash", "now")),
            ("INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at) VALUES(?,?,?,?)", ("orphan-token", user_id + 9999, "csrf", 9999999999)),
            ("INSERT INTO expenses(user_id,amount_cents,category,spent_on,created_at) VALUES(?,?,?,?,?)", (user_id, 100, None, app.date.today().isoformat(), "now")),
            ("INSERT INTO email_change_requests(user_id,new_email,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)", (user_id + 9999, "new@example.com", "token", 9999999999, 1)),
        )
        for statement, params in cases:
            with self.subTest(statement=statement, params=params):
                self.assert_integrity_error(statement, params)

    def test_database_not_null_constraints_reject_null_values(self):
        self.register()
        with app.db() as conn:
            user = conn.execute("SELECT id FROM users WHERE email=?", ("owner@example.com",)).fetchone()
            user_id = user["id"]
        cases = (
            ("users.full_name", "INSERT INTO users(full_name,email,mobile,currency_code,avatar_url,password_hash,created_at) VALUES(?,?,?,?,?,?,?)", (None, "null-name@example.com", "", "INR", "", b"hash", "now")),
            ("users.email", "INSERT INTO users(full_name,email,mobile,currency_code,avatar_url,password_hash,created_at) VALUES(?,?,?,?,?,?,?)", ("Null email", None, "", "INR", "", b"hash", "now")),
            ("users.mobile", "INSERT INTO users(full_name,email,mobile,currency_code,avatar_url,password_hash,created_at) VALUES(?,?,?,?,?,?,?)", ("Null mobile", "null-mobile@example.com", None, "INR", "", b"hash", "now")),
            ("users.currency_code", "INSERT INTO users(full_name,email,mobile,currency_code,avatar_url,password_hash,created_at) VALUES(?,?,?,?,?,?,?)", ("Null currency", "null-currency@example.com", "", None, "", b"hash", "now")),
            ("users.avatar_url", "INSERT INTO users(full_name,email,mobile,currency_code,avatar_url,password_hash,created_at) VALUES(?,?,?,?,?,?,?)", ("Null avatar", "null-avatar@example.com", "", "INR", None, b"hash", "now")),
            ("users.password_hash", "INSERT INTO users(full_name,email,password_hash,created_at) VALUES(?,?,?,?)", ("Null hash", "null-hash@example.com", None, "now")),
            ("users.created_at", "INSERT INTO users(full_name,email,password_hash,created_at) VALUES(?,?,?,?)", ("Null created", "null-created@example.com", b"hash", None)),
            ("sessions.user_id", "INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at) VALUES(?,?,?,?)", ("null-session-user", None, "csrf", 9999999999)),
            ("sessions.csrf_token", "INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at) VALUES(?,?,?,?)", ("null-session-csrf", user_id, None, 9999999999)),
            ("sessions.expires_at", "INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at) VALUES(?,?,?,?)", ("null-session-expiry", user_id, "csrf", None)),
            ("expenses.user_id", "INSERT INTO expenses(user_id,amount_cents,category,spent_on,created_at) VALUES(?,?,?,?,?)", (None, 100, "Food", app.date.today().isoformat(), "now")),
            ("expenses.amount_cents", "INSERT INTO expenses(user_id,amount_cents,category,spent_on,created_at) VALUES(?,?,?,?,?)", (user_id, None, "Food", app.date.today().isoformat(), "now")),
            ("expenses.spent_on", "INSERT INTO expenses(user_id,amount_cents,category,spent_on,created_at) VALUES(?,?,?,?,?)", (user_id, 100, "Food", None, "now")),
            ("expenses.payment_method", "INSERT INTO expenses(user_id,amount_cents,category,spent_on,payment_method,created_at) VALUES(?,?,?,?,?,?)", (user_id, 100, "Food", app.date.today().isoformat(), None, "now")),
            ("expenses.description", "INSERT INTO expenses(user_id,amount_cents,category,spent_on,description,created_at) VALUES(?,?,?,?,?,?)", (user_id, 100, "Food", app.date.today().isoformat(), None, "now")),
            ("expenses.created_at", "INSERT INTO expenses(user_id,amount_cents,category,spent_on,created_at) VALUES(?,?,?,?,?)", (user_id, 100, "Food", app.date.today().isoformat(), None)),
            ("user_categories.user_id", "INSERT INTO user_categories(user_id,name,created_at) VALUES(?,?,?)", (None, "Null owner", "now")),
            ("user_categories.name", "INSERT INTO user_categories(user_id,name,created_at) VALUES(?,?,?)", (user_id, None, "now")),
            ("user_categories.created_at", "INSERT INTO user_categories(user_id,name,created_at) VALUES(?,?,?)", (user_id, "Null created", None)),
            ("login_attempts.failures", "INSERT INTO login_attempts(email,failures,blocked_until) VALUES(?,?,?)", ("null-failures@example.com", None, 0)),
            ("login_attempts.blocked_until", "INSERT INTO login_attempts(email,failures,blocked_until) VALUES(?,?,?)", ("null-block@example.com", 0, None)),
            ("email_change_requests.new_email", "INSERT INTO email_change_requests(user_id,new_email,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)", (user_id, None, "null-email-token", 9999999999, 1)),
            ("email_change_requests.token_hash", "INSERT INTO email_change_requests(user_id,new_email,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)", (user_id, "new@example.com", None, 9999999999, 1)),
            ("email_change_requests.expires_at", "INSERT INTO email_change_requests(user_id,new_email,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)", (user_id, "new@example.com", "null-expiry-token", None, 1)),
            ("email_change_requests.created_at", "INSERT INTO email_change_requests(user_id,new_email,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)", (user_id, "new@example.com", "null-created-token", 9999999999, None)),
        )
        for column, statement, params in cases:
            with self.subTest(column=column):
                self.assert_integrity_error(statement, params)

    def test_database_primary_keys_and_email_change_token_are_unique(self):
        self.register()
        with app.db() as conn:
            user = conn.execute("SELECT id FROM users WHERE email=?", ("owner@example.com",)).fetchone()
            session = conn.execute("SELECT token_hash,csrf_token,expires_at FROM sessions WHERE user_id=?", (user["id"],)).fetchone()
            conn.execute(
                "INSERT INTO expenses(id,user_id,amount_cents,category,spent_on,created_at) VALUES(?,?,?,?,?,?)",
                (7001, user["id"], 100, "Food", app.date.today().isoformat(), "now"),
            )
            conn.execute(
                "INSERT INTO user_categories(id,user_id,name,created_at) VALUES(?,?,?,?)",
                (7002, user["id"], "Unique category", "now"),
            )
            conn.execute("INSERT INTO login_attempts(email) VALUES(?)", ("attempt@example.com",))
            conn.execute(
                "INSERT INTO email_change_requests(user_id,new_email,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)",
                (user["id"], "first-new@example.com", "shared-token-hash", 9999999999, 1),
            )
            cursor = conn.execute(
                "INSERT INTO users(full_name,email,password_hash,created_at) VALUES(?,?,?,?) RETURNING id",
                ("Second user", "second@example.com", b"hash", "now"),
            )
            second_user_id = cursor.fetchone()["id"]
            user_id = user["id"]
            token_hash = session["token_hash"]
            csrf_token = session["csrf_token"]
            expires_at = session["expires_at"]

        cases = (
            ("INSERT INTO users(id,full_name,email,password_hash,created_at) VALUES(?,?,?,?,?)", (user_id, "Duplicate id", "other-id@example.com", b"hash", "now")),
            ("INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at) VALUES(?,?,?,?)", (token_hash, user_id, "another-csrf", expires_at)),
            ("INSERT INTO expenses(id,user_id,amount_cents,category,spent_on,created_at) VALUES(?,?,?,?,?,?)", (7001, user_id, 200, "Food", app.date.today().isoformat(), "now")),
            ("INSERT INTO user_categories(id,user_id,name,created_at) VALUES(?,?,?,?)", (7002, user_id, "Different category", "now")),
            ("INSERT INTO login_attempts(email) VALUES(?)", ("ATTEMPT@example.com",)),
            ("INSERT INTO email_change_requests(user_id,new_email,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)", (user_id, "second-new@example.com", "different-token-hash", 9999999999, 2)),
            ("INSERT INTO email_change_requests(user_id,new_email,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)", (second_user_id, "second-new@example.com", "shared-token-hash", 9999999999, 2)),
        )
        for statement, params in cases:
            with self.subTest(statement=statement, params=params):
                self.assert_integrity_error(statement, params)

    def test_admin_views_minimal_user_data_and_activity(self):
        self.register("admin@example.com")
        with app.db() as conn:
            admin_id = conn.execute("SELECT id FROM users WHERE email=?", ("admin@example.com",)).fetchone()["id"]
        self.assertEqual(app.bootstrap_admin("admin@example.com"), admin_id)
        with self.assertRaisesRegex(RuntimeError, "already provisioned"):
            app.bootstrap_admin("admin@example.com")
        status, me = self.request("/api/me")
        self.assertEqual(status, 200)
        self.assertTrue(me["isAdmin"])
        self.request("/api/logout", "POST")
        self.csrf = ""
        self.register("member@example.com")
        with app.db() as conn:
            member_id = conn.execute("SELECT id FROM users WHERE email=?", ("member@example.com",)).fetchone()["id"]
        self.request("/api/logout", "POST")
        self.csrf = ""
        status, _ = self.request("/api/login", "POST", {"email": "member@example.com", "password": "wrong-password"}, csrf=False, no_cookie=True)
        self.assertEqual(status, 401)
        self.login("admin@example.com")
        status, _ = self.request("/api/password", "POST", {
            "currentPassword": "correct-horse-123",
            "newPassword": "new-correct-horse-123",
            "confirmPassword": "new-correct-horse-123",
        })
        self.assertEqual(status, 200)
        status, data = self.request("/api/admin/users")
        self.assertEqual(status, 200, data)
        self.assertEqual(len(data["users"]), 2)
        self.assertEqual(set(data["users"][0]), {"id", "full_name", "email", "created_at", "is_admin", "is_suspended"})
        self.assertIn(member_id, [row["id"] for row in data["users"]])
        today = app.date.today().isoformat()
        status, _ = self.request("/api/expenses", "POST", {"amount": 9, "category": "Food", "date": today, "description": "private audit payload"})
        self.assertEqual(status, 201)
        with app.db() as conn:
            expense_id = conn.execute("SELECT id FROM expenses WHERE user_id=?", (admin_id,)).fetchone()["id"]
        status, _ = self.request(f"/api/expenses/{expense_id}", "PATCH", {"amount": 10, "category": "Food", "date": today, "description": "updated private audit payload"})
        self.assertEqual(status, 200)
        status, _ = self.request(f"/api/expenses/{expense_id}", "DELETE")
        self.assertEqual(status, 200)
        status, data = self.request("/api/admin/activity")
        self.assertEqual(status, 200, data)
        self.assertIn("account_registered", [event["event_type"] for event in data["events"]])
        self.assertIn("admin_bootstrapped", [event["event_type"] for event in data["events"]])
        self.assertIn("login_succeeded", [event["event_type"] for event in data["events"]])
        self.assertIn("login_failed", [event["event_type"] for event in data["events"]])
        self.assertIn("password_changed", [event["event_type"] for event in data["events"]])
        self.assertIn("logout", [event["event_type"] for event in data["events"]])
        for event_type in ("expense_created", "expense_updated", "expense_deleted"):
            self.assertIn(event_type, [event["event_type"] for event in data["events"]])
        self.assertTrue(all(set(event) == {"id", "actor_user_id", "target_user_id", "event_type", "created_at"} for event in data["events"]))
        self.assertNotIn("private audit payload", json.dumps(data["events"]))

    def test_admin_routes_require_authentication_and_admin_role(self):
        paths = (("GET", "/api/admin/users", None), ("GET", "/api/admin/activity", None), ("PATCH", "/api/admin/users/1", {"suspended": True}))
        for method, path, payload in paths:
            with self.subTest(method=method, path=path):
                status, _ = self.request(path, method, payload, csrf=False, no_cookie=True)
                self.assertEqual(status, 401)

        self.register("member@example.com")
        with app.db() as conn:
            user_id = conn.execute("SELECT id FROM users WHERE email=?", ("member@example.com",)).fetchone()["id"]
        self.request("/api/logout", "POST")
        self.csrf = ""
        self.register("other@example.com")
        with app.db() as conn:
            other_user_id = conn.execute("SELECT id FROM users WHERE email=?", ("other@example.com",)).fetchone()["id"]
        self.request("/api/logout", "POST")
        self.csrf = ""
        self.login("member@example.com")
        for method, path, payload in (
            ("GET", "/api/admin/users", None),
            ("GET", "/api/admin/activity", None),
            ("PATCH", f"/api/admin/users/{user_id}", {"suspended": True}),
            ("PATCH", f"/api/admin/users/{other_user_id}", {"suspended": True}),
            ("PATCH", f"/api/admin/users/{user_id}", {"is_admin": True, "suspended": False}),
        ):
            with self.subTest(method=method, path=path, payload=payload):
                status, _ = self.request(path, method, payload)
                self.assertEqual(status, 403)
        with app.db() as conn:
            self.assertFalse(conn.execute("SELECT is_admin FROM users WHERE id=?", (user_id,)).fetchone()["is_admin"])
            self.assertFalse(conn.execute("SELECT is_suspended FROM users WHERE id=?", (user_id,)).fetchone()["is_suspended"])
            self.assertFalse(conn.execute("SELECT is_suspended FROM users WHERE id=?", (other_user_id,)).fetchone()["is_suspended"])

    def test_admin_write_requires_csrf_and_rejects_privilege_changes(self):
        self.register("admin@example.com")
        with app.db() as conn:
            admin_id = conn.execute("SELECT id FROM users WHERE email=?", ("admin@example.com",)).fetchone()["id"]
        app.bootstrap_admin("admin@example.com")
        self.request("/api/logout", "POST")
        self.csrf = ""
        self.register("member@example.com")
        with app.db() as conn:
            target_id = conn.execute("SELECT id FROM users WHERE email=?", ("member@example.com",)).fetchone()["id"]
        self.request("/api/logout", "POST")
        self.csrf = ""
        self.login("admin@example.com")
        for label, kwargs in (("missing", {"csrf": False}), ("invalid", {"csrf_token": "bad-token"})):
            with self.subTest(token=label):
                status, _ = self.request(f"/api/admin/users/{target_id}", "PATCH", {"suspended": True}, **kwargs)
                self.assertEqual(status, 403)
        status, _ = self.request(f"/api/admin/users/{target_id}", "PATCH", {"suspended": True, "is_admin": True})
        self.assertEqual(status, 400)
        status, _ = self.request(f"/api/admin/users/{admin_id}", "PATCH", {"suspended": True})
        self.assertEqual(status, 409)
        with app.db() as conn:
            # Seed a second admin directly to exercise management of an existing
            # admin account without adding any application role-promotion route.
            conn.execute("UPDATE users SET is_admin=TRUE WHERE id=?", (target_id,))
        status, result = self.request(f"/api/admin/users/{target_id}", "PATCH", {"suspended": True})
        self.assertEqual(status, 200, result)
        with app.db() as conn:
            target = conn.execute("SELECT is_admin,is_suspended FROM users WHERE id=?", (target_id,)).fetchone()
        self.assertTrue(target["is_admin"])
        self.assertTrue(target["is_suspended"])
        status, result = self.request(f"/api/admin/users/{target_id}", "PATCH", {"suspended": False})
        self.assertEqual(status, 200, result)
        with app.db() as conn:
            target = conn.execute("SELECT is_admin,is_suspended FROM users WHERE id=?", (target_id,)).fetchone()
        self.assertTrue(target["is_admin"])
        self.assertFalse(target["is_suspended"])

    def test_suspension_revokes_existing_sessions_and_blocks_login(self):
        self.register("admin@example.com")
        with app.db() as conn:
            admin_id = conn.execute("SELECT id FROM users WHERE email=?", ("admin@example.com",)).fetchone()["id"]
        app.bootstrap_admin("admin@example.com")
        self.request("/api/logout", "POST")
        self.csrf = ""
        self.register("member@example.com")
        member_cookie = next(cookie.value for cookie in self.cookies if cookie.name == "session")
        with app.db() as conn:
            member_id = conn.execute("SELECT id FROM users WHERE email=?", ("member@example.com",)).fetchone()["id"]
        # Signing in as the administrator replaces the browser cookie but leaves
        # the captured member session active until the admin suspends that account.
        self.login("admin@example.com")
        status, result = self.request(f"/api/admin/users/{member_id}", "PATCH", {"suspended": True})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["isSuspended"])
        with app.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS total FROM sessions WHERE user_id=?", (member_id,)).fetchone()["total"], 0)
        status, _ = self.request("/api/profile", cookie=member_cookie, no_cookie=True)
        self.assertEqual(status, 401)
        status, _ = self.request("/api/expenses", cookie=member_cookie, no_cookie=True)
        self.assertEqual(status, 401)

        self.request("/api/logout", "POST")
        self.csrf = ""
        status, _ = self.request("/api/login", "POST", {"email": "member@example.com", "password": "correct-horse-123"}, csrf=False, no_cookie=True)
        self.assertEqual(status, 403)
        with app.db() as conn:
            activity = {row["event_type"] for row in conn.execute("SELECT event_type FROM system_activity").fetchall()}
        self.assertIn("account_suspended", activity)
        self.assertIn("login_rejected_suspended", activity)
        self.login("admin@example.com")
        status, result = self.request(f"/api/admin/users/{member_id}", "PATCH", {"suspended": False})
        self.assertEqual(status, 200, result)
        self.request("/api/logout", "POST")
        self.csrf = ""
        status, result = self.request("/api/login", "POST", {"email": "member@example.com", "password": "correct-horse-123"}, csrf=False)
        self.assertEqual(status, 200, result)
        status, me = self.request("/api/me")
        self.assertFalse(me["isAdmin"])

    def test_suspended_google_account_cannot_start_a_session(self):
        with app.db() as conn:
            conn.execute(
                "INSERT INTO users(full_name,email,google_sub,password_hash,created_at,is_suspended) VALUES(?,?,?,?,?,TRUE)",
                ("Suspended Google", "google-suspended@example.com", "google-sub-123", app._password_hash("random-google-password"), "now"),
            )

        class StubResponse:
            def __init__(self, payload):
                self.payload = json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return self.payload

        class NoRedirectHandler(HTTPRedirectHandler):
            def redirect_request(self, request, response, code, message, headers, new_url):
                return None

        original_values = (app.GOOGLE_CLIENT_ID, app.GOOGLE_CLIENT_SECRET)
        app.GOOGLE_CLIENT_ID, app.GOOGLE_CLIENT_SECRET = "test-client", "test-secret"
        try:
            callback = Request(
                self.base + "/auth/google/callback?state=state-value&code=auth-code",
                headers={"Cookie": "google_oauth_state=state-value"},
            )
            client = build_opener(NoRedirectHandler())
            with patch("app.urlopen", side_effect=[
                StubResponse({"access_token": "test-access-token"}),
                StubResponse({"email": "google-suspended@example.com", "email_verified": True, "sub": "google-sub-123"}),
            ]):
                try:
                    client.open(callback, timeout=3)
                    self.fail("Expected the suspended Google account callback to redirect.")
                except HTTPError as response:
                    self.assertEqual(response.code, 302)
                    self.assertEqual(response.headers.get("Location"), "/?auth_error=account_suspended")
        finally:
            app.GOOGLE_CLIENT_ID, app.GOOGLE_CLIENT_SECRET = original_values
        with app.db() as conn:
            user_id = conn.execute("SELECT id FROM users WHERE email=?", ("google-suspended@example.com",)).fetchone()["id"]
            self.assertEqual(conn.execute("SELECT COUNT(*) AS total FROM sessions WHERE user_id=?", (user_id,)).fetchone()["total"], 0)
            self.assertIn("login_rejected_suspended", [row["event_type"] for row in conn.execute("SELECT event_type FROM system_activity WHERE target_user_id=?", (user_id,)).fetchall()])

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

    def test_budget_endpoints_validate_create_update_and_delete(self):
        self.register()
        status, _ = self.request("/api/budgets")
        self.assertEqual(status, 200)
        self.assertEqual([], _["budgets"])

        start = app.date.today()
        weekly = {
            "budgetType": "weekly", "amount": 500,
            "startDate": start.isoformat(), "endDate": (start + app.timedelta(days=6)).isoformat(),
        }
        cases = [
            ({**weekly, "budgetType": "daily"}, "budget type"),
            ({**weekly, "amount": "bad"}, "amount"),
            ({**weekly, "amount": float("inf")}, "positive"),
            ({**weekly, "amount": 0}, "positive"),
            ({**weekly, "startDate": "not-a-date"}, "dates"),
            ({**weekly, "endDate": start.isoformat()}, "7 days"),
            ({**weekly, "endDate": (start - app.timedelta(days=1)).isoformat()}, "on or after"),
        ]
        for payload, _label in cases:
            with self.subTest(case=_label):
                status, _ = self.request("/api/budgets", "POST", payload)
                self.assertEqual(status, 400)

        status, created = self.request("/api/budgets", "POST", weekly)
        self.assertEqual(status, 201, created)
        budget_id = created["budget"]["id"]
        status, duplicate = self.request("/api/budgets", "POST", weekly)
        self.assertEqual(status, 409, duplicate)

        status, _ = self.request("/api/budgets", "PATCH", weekly)
        self.assertEqual(status, 400)
        status, _ = self.request("/api/budgets?id=0", "PATCH", weekly)
        self.assertEqual(status, 400)
        status, _ = self.request("/api/budgets?id=99999", "PATCH", weekly)
        self.assertEqual(status, 404)
        invalid_update = {**weekly, "amount": "bad"}
        status, _ = self.request(f"/api/budgets?id={budget_id}", "PATCH", invalid_update)
        self.assertEqual(status, 400)
        status, _ = self.request(f"/api/budgets?id={budget_id}", "PATCH", {**weekly, "budgetType": "daily"})
        self.assertEqual(status, 400)
        status, updated = self.request(f"/api/budgets?id={budget_id}", "PATCH", {**weekly, "amount": 650})
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated["budget"]["amount_cents"], 65000)

        status, _ = self.request("/api/budgets", "DELETE")
        self.assertEqual(status, 400)
        status, _ = self.request("/api/budgets?id=99999", "DELETE")
        self.assertEqual(status, 404)
        status, deleted = self.request(f"/api/budgets?id={budget_id}", "DELETE")
        self.assertEqual(status, 200, deleted)

    def test_category_mutations_validate_and_rename_existing_expenses(self):
        self.register()
        status, _ = self.request("/api/categories", "POST", {"name": "Food"})
        self.assertEqual(status, 400)
        status, first = self.request("/api/categories", "POST", {"name": "Pet Care"})
        self.assertEqual(status, 201, first)
        status, _ = self.request("/api/categories", "POST", {"name": "pet care"})
        self.assertEqual(status, 409)
        status, _ = self.request(f"/api/categories/99999", "PATCH", {"name": "Renamed"})
        self.assertEqual(status, 404)
        status, _ = self.request(f"/api/categories/{first['id']}", "PATCH", {"name": "Food"})
        self.assertEqual(status, 400)
        status, _ = self.request("/api/categories", "POST", {"name": "Transport"})
        self.assertEqual(status, 201)
        status, _ = self.request(f"/api/categories/{first['id']}", "PATCH", {"name": "transport"})
        self.assertEqual(status, 409)

        status, _ = self.request("/api/expenses", "POST", {
            "amount": 18, "category": "Pet Care", "date": app.date.today().isoformat(),
        })
        self.assertEqual(status, 201)
        status, renamed = self.request(f"/api/categories/{first['id']}", "PATCH", {"name": "Pets"})
        self.assertEqual(status, 200, renamed)
        status, history = self.request("/api/expenses")
        self.assertEqual(status, 200)
        self.assertEqual(history["expenses"][0]["category"], "Pets")

    def test_analytics_and_monthly_yearly_reports_cover_empty_and_populated_periods(self):
        self.register()
        status, analytics = self.request("/api/analytics")
        self.assertEqual(status, 200, analytics)
        self.assertEqual(analytics["monthAverageCents"], 0)
        status, invalid = self.request("/api/report?type=monthly&period=2026-13")
        self.assertEqual(status, 400, invalid)
        status, invalid = self.request("/api/report?type=weekly&period=2026")
        self.assertEqual(status, 400, invalid)
        status, invalid = self.request("/api/report?type=yearly&period=1999")
        self.assertEqual(status, 400, invalid)

        today = app.date.today()
        status, _ = self.request("/api/expenses", "POST", {
            "amount": 42, "category": "Food", "date": today.isoformat(), "description": "Coverage sample",
        })
        self.assertEqual(status, 201)
        status, analytics = self.request("/api/analytics")
        self.assertEqual(status, 200, analytics)
        self.assertEqual(analytics["monthCount"], 1)
        status, monthly = self.request(f"/api/report?type=monthly&period={today:%Y-%m}")
        self.assertEqual(status, 200, monthly)
        self.assertEqual(monthly["expenseCount"], 1)
        status, yearly = self.request(f"/api/report?type=yearly&period={today:%Y}")
        self.assertEqual(status, 200, yearly)
        self.assertEqual(len(yearly["months"]), 12)
        self.assertEqual(yearly["months"][today.month - 1]["count"], 1)

    def test_profile_validation_and_email_change_verification_paths(self):
        self.register()
        valid = {"fullName": "Test User", "email": "owner@example.com", "mobile": "", "currencyCode": "INR"}
        for payload in (
            {**valid, "fullName": ""},
            {**valid, "email": "not-an-email"},
            {**valid, "mobile": "123"},
            {**valid, "currencyCode": "XYZ"},
        ):
            with self.subTest(payload=payload):
                status, _ = self.request("/api/profile", "PATCH", payload)
                self.assertEqual(status, 400)

        with patch.object(app, "_send_email_change_verification", side_effect=RuntimeError("SMTP unavailable")):
            status, _ = self.request("/api/profile", "PATCH", {**valid, "email": "new@example.com"})
            self.assertEqual(status, 503)

        captured = {}
        def capture_email(email, token):
            captured.update(email=email, token=token)
        with patch.object(app, "_send_email_change_verification", side_effect=capture_email):
            status, pending = self.request("/api/profile", "PATCH", {**valid, "email": "new@example.com", "currencyCode": "USD"})
        self.assertEqual(status, 200, pending)
        self.assertTrue(pending["emailVerificationPending"])
        self.assertEqual(pending["pendingEmail"], "new@example.com")
        token = captured["token"]

        status, _ = self.request("/api/profile")
        self.assertEqual(status, 200)
        status, _ = self.request("/auth/email/verify?token=bad")
        self.assertEqual(status, 200)
        status, _ = self.request(f"/auth/email/verify?token={token}")
        self.assertEqual(status, 200)
        with app.db() as conn:
            user = conn.execute("SELECT email FROM users WHERE email=?", ("new@example.com",)).fetchone()
        self.assertIsNotNone(user)

    def test_static_routes_and_unknown_routes(self):
        for path in ("/", "/index.html", "/app.js", "/styles.css"):
            with self.subTest(path=path):
                status, _ = self.request(path)
                self.assertEqual(status, 200)
        self.register()
        for method, path, payload in (
            ("GET", "/unknown-route", None),
            ("POST", "/unknown-route", {}),
            ("PATCH", "/unknown-route", {}),
            ("DELETE", "/unknown-route", None),
        ):
            with self.subTest(method=method):
                status, _ = self.request(path, method, payload)
                self.assertEqual(status, 404)

    def test_auth_password_and_request_validation_branches(self):
        for payload in (
            {"fullName": "User", "email": "bad", "password": "correct-horse-123"},
            {"fullName": "User", "email": "valid@example.com", "password": "short"},
            {"fullName": "", "email": "valid@example.com", "password": "correct-horse-123"},
        ):
            status, _ = self.request("/api/register", "POST", payload, csrf=False)
            self.assertEqual(status, 400)
        status, _ = self.request("/api/register", "POST", {
            "fullName": "Test User", "email": "owner@example.com", "password": "correct-horse-123",
        }, csrf=False)
        self.assertEqual(status, 200)
        self.csrf = ""
        status, _ = self.request("/api/register", "POST", {
            "fullName": "Duplicate", "email": "owner@example.com", "password": "correct-horse-123",
        }, csrf=False)
        self.assertEqual(status, 409)
        status, _ = self.request("/api/login", "POST", {"email": "unknown@example.com", "password": "correct-horse-123"}, csrf=False)
        self.assertEqual(status, 401)
        status, _ = self.request("/api/login", "POST", {"email": "owner@example.com", "password": "wrong-password"}, csrf=False)
        self.assertEqual(status, 401)
        self.login("owner@example.com")
        for payload in (
            {"currentPassword": "correct-horse-123", "newPassword": "short", "confirmPassword": "short"},
            {"currentPassword": "correct-horse-123", "newPassword": "new-password-123", "confirmPassword": "different-password"},
            {"currentPassword": "wrong-password", "newPassword": "new-password-123", "confirmPassword": "new-password-123"},
        ):
            status, _ = self.request("/api/password", "POST", payload)
            self.assertIn(status, (400, 401))

        status, _ = self.request("/api/register", "POST", {}, extra_headers={"Origin": "https://attacker.invalid"}, csrf=False)
        self.assertEqual(status, 403)
        status, _ = self.request("/api/register", "POST", {}, raw_data=b"not json", csrf=False)
        self.assertEqual(status, 400)
        status, _ = self.request("/api/profile", "PATCH", {}, extra_headers={"Origin": "https://attacker.invalid"})
        self.assertEqual(status, 403)
        status, _ = self.request("/api/profile", "PATCH", {}, raw_data=b"not json")
        self.assertEqual(status, 400)
        status, _ = self.request("/api/expenses", "POST", {}, extra_headers={"Origin": "https://attacker.invalid"})
        self.assertEqual(status, 403)
        status, _ = self.request("/api/expenses", "POST", {}, raw_data=b"not json")
        self.assertEqual(status, 400)
        status, _ = self.request("/api/expenses", "POST", {"amount": "bad", "category": "Food", "date": "bad-date"})
        self.assertEqual(status, 400)
        status, _ = self.request("/api/expenses", "POST", {"amount": 10, "category": "not a category", "date": app.date.today().isoformat()})
        self.assertEqual(status, 400)

    def test_expired_and_conflicting_email_change_tokens_are_rejected(self):
        self.register()
        valid = {"fullName": "Test User", "email": "owner@example.com", "mobile": "", "currencyCode": "INR"}
        captured = {}
        def capture_email(email, token):
            captured.update(email=email, token=token)

        with patch.object(app, "_send_email_change_verification", side_effect=capture_email):
            status, _ = self.request("/api/profile", "PATCH", {**valid, "email": "expired@example.com"})
        self.assertEqual(status, 200)
        with app.db() as conn:
            conn.execute("UPDATE email_change_requests SET expires_at=? WHERE new_email=?", (int(app.time.time()) - 1, "expired@example.com"))
        self.request(f"/auth/email/verify?token={captured['token']}")
        with app.db() as conn:
            expired = conn.execute("SELECT 1 FROM email_change_requests WHERE new_email=?", ("expired@example.com",)).fetchone()
        self.assertIsNone(expired)

        with patch.object(app, "_send_email_change_verification", side_effect=capture_email):
            status, _ = self.request("/api/profile", "PATCH", {**valid, "email": "taken@example.com"})
        self.assertEqual(status, 200)
        with app.db() as conn:
            conn.execute(
                "INSERT INTO users(full_name,email,password_hash,created_at) VALUES(?,?,?,?)",
                ("Other", "taken@example.com", app._password_hash("another-password-123"), app.datetime.now(app.timezone.utc).isoformat()),
            )
        self.request(f"/auth/email/verify?token={captured['token']}")
        with app.db() as conn:
            current_email = conn.execute("SELECT email FROM users WHERE email=?", ("owner@example.com",)).fetchone()
            remaining = conn.execute("SELECT 1 FROM email_change_requests WHERE new_email=?", ("taken@example.com",)).fetchone()
        self.assertIsNotNone(current_email)
        self.assertIsNone(remaining)

    def test_static_file_read_errors_are_returned_as_http_errors(self):
        with patch.object(app.Path, "read_bytes", side_effect=OSError("missing static file")):
            status, _ = self.request("/")
            self.assertEqual(status, 500)
            status, _ = self.request("/app.js")
            self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
