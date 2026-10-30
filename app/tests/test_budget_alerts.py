"""FR10 - Budget alerts."""
import calendar
import http.client
import http.cookiejar
import json
import os
import secrets
import threading
import unittest
from unittest.mock import patch
from datetime import date
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

import app


class BudgetAlertTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_database_url = os.environ.get("TEST_DATABASE_URL", "").strip()
        if not cls.test_database_url:
            raise RuntimeError("Set TEST_DATABASE_URL to a dedicated PostgreSQL test database.")
        cls.schema = f"app_alerts_{secrets.token_hex(6)}"
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
            conn.execute("DELETE FROM notifications")
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

    def register(self, email="alerts@example.com"):
        status, body = self.request("/api/register", "POST", {"fullName": "Alert User", "email": email, "password": "correct-horse-123"})
        self.assertEqual(status, 200, body)
        self.csrf = body["csrfToken"]

    def add_expense(self, amount):
        status, _ = self.request("/api/expenses", "POST", {"amount": str(amount), "category": "Food", "date": date.today().isoformat()})
        self.assertEqual(status, 201)

    def add_budget(self, amount, start, end, budget_type="monthly"):
        status, body = self.request("/api/budgets", "POST", {"budgetType": budget_type, "amount": str(amount), "startDate": start.isoformat(), "endDate": end.isoformat()})
        self.assertEqual(status, 201, body)

    def month_range(self):
        today = date.today()
        return today.replace(day=1), today.replace(day=calendar.monthrange(today.year, today.month)[1])

    def alerts(self):
        status, data = self.request("/api/alerts")
        self.assertEqual(status, 200)
        return data["alerts"]

    def notifications(self, authenticated=True):
        status, data = self.request("/api/notifications", authenticated=authenticated)
        if authenticated:
            self.assertEqual(status, 200)
            return data["notifications"]
        return status, data

    def test_alerts_require_login(self):
        status, _ = self.request("/api/alerts", authenticated=False)
        self.assertEqual(status, 401)

    def test_no_budget_means_no_alerts(self):
        self.register()
        self.add_expense(500)
        self.assertEqual(self.alerts(), [])

    def test_no_alert_below_threshold(self):
        self.register()
        self.add_budget(1000, *self.month_range())
        self.add_expense(790)
        self.assertEqual(self.alerts(), [])

    def test_warning_when_threshold_is_reached(self):
        self.register()
        self.add_budget(1000, *self.month_range())
        self.add_expense(800)
        alerts = self.alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["level"], "warning")
        self.assertEqual(alerts[0]["percentUsed"], 80.0)
        self.assertEqual(alerts[0]["remainingCents"], 20000)

    def test_spending_exactly_the_budget_is_still_a_warning(self):
        self.register()
        self.add_budget(1000, *self.month_range())
        self.add_expense(1000)
        self.assertEqual(self.alerts()[0]["level"], "warning")

    def test_alert_when_budget_is_exceeded(self):
        self.register()
        self.add_budget(1000, *self.month_range())
        self.add_expense(1200)
        alert = self.alerts()[0]
        self.assertEqual(alert["level"], "exceeded")
        self.assertEqual(alert["remainingCents"], -20000)
        self.assertEqual(alert["spentCents"], 120000)

    def test_expired_and_future_budgets_do_not_alert(self):
        self.register()
        today = date.today()
        self.add_budget(100, date(today.year - 1, 1, 1), date(today.year - 1, 1, 31))
        self.add_budget(100, date(today.year + 1, 1, 1), date(today.year + 1, 1, 31))
        self.add_expense(500)
        self.assertEqual(self.alerts(), [])
        self.assertEqual(self.notifications(), [])

    def test_each_budget_is_checked_separately(self):
        self.register()
        today = date.today()
        self.add_budget(1000, *self.month_range())
        self.add_budget(5000, date(today.year, 1, 1), date(today.year, 12, 31), "yearly")
        self.add_expense(900)
        alerts = self.alerts()
        self.assertEqual([a["budgetType"] for a in alerts], ["monthly"])

    def test_alert_clears_when_the_expense_is_deleted(self):
        self.register()
        self.add_budget(1000, *self.month_range())
        self.add_expense(1200)
        self.assertEqual(self.alerts()[0]["level"], "exceeded")
        _, history = self.request("/api/expenses")
        status, _ = self.request(f"/api/expenses/{history['expenses'][0]['id']}", "DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(self.alerts(), [])
        self.assertEqual([item["event_type"] for item in self.notifications()], ["exceeded"])

    def test_users_only_get_their_own_alerts(self):
        self.register("first@example.com")
        self.add_budget(1000, *self.month_range())
        self.add_expense(1200)
        self.client = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.register("second@example.com")
        self.assertEqual(self.alerts(), [])

    def test_persistent_notifications_reuse_warning_and_exceeded_alerts(self):
        self.register()
        first, last = self.month_range()
        self.add_budget(1000, first, last)
        self.add_expense(800)
        notifications = self.notifications()
        self.assertEqual(len(notifications), 1)
        warning = notifications[0]
        self.assertEqual(warning["event_type"], "warning")
        self.assertEqual(warning["budget_type"], "monthly")
        self.assertEqual(warning["start_date"], first.isoformat())
        self.assertEqual(warning["end_date"], last.isoformat())
        self.assertEqual(warning["budget_amount_cents"], 100000)
        self.assertEqual(warning["spent_cents"], 80000)
        self.assertEqual(warning["remaining_cents"], 20000)
        self.assertEqual(warning["percent_used"], 80.0)

        self.add_expense(50)
        self.assertEqual([item["event_type"] for item in self.notifications()], ["warning"])
        self.add_expense(200)
        notifications = self.notifications()
        self.assertEqual({item["event_type"] for item in notifications}, {"warning", "exceeded"})
        self.assertEqual(len(notifications), 2)
        self.add_expense(25)
        self.assertEqual(len(self.notifications()), 2)

    def test_expense_updates_generate_events_and_deletion_preserves_history(self):
        self.register()
        first, last = self.month_range()
        self.add_budget(1000, first, last)
        self.add_expense(700)
        self.assertEqual(self.notifications(), [])
        _, history = self.request("/api/expenses")
        expense_id = history["expenses"][0]["id"]

        status, _ = self.request(
            f"/api/expenses/{expense_id}",
            "PATCH",
            {"amount": "850", "category": "Food", "date": date.today().isoformat(), "description": "", "paymentMethod": ""},
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["event_type"] for item in self.notifications()], ["warning"])
        status, _ = self.request(
            f"/api/expenses/{expense_id}",
            "PATCH",
            {"amount": "1100", "category": "Food", "date": date.today().isoformat(), "description": "", "paymentMethod": ""},
        )
        self.assertEqual(status, 200)
        self.assertEqual({item["event_type"] for item in self.notifications()}, {"warning", "exceeded"})

        status, _ = self.request(f"/api/expenses/{expense_id}", "DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(self.alerts(), [])
        self.assertEqual({item["event_type"] for item in self.notifications()}, {"warning", "exceeded"})

    def test_budget_create_update_and_delete_generate_and_keep_notifications(self):
        self.register()
        first, last = self.month_range()
        self.add_expense(850)
        self.add_budget(1000, first, last)
        self.assertEqual([item["event_type"] for item in self.notifications()], ["warning"])

        _, budgets = self.request("/api/budgets")
        budget_id = budgets["budgets"][0]["id"]
        status, _ = self.request(
            f"/api/budgets?id={budget_id}",
            "PATCH",
            {"budgetType": "monthly", "amount": "800", "startDate": first.isoformat(), "endDate": last.isoformat()},
        )
        self.assertEqual(status, 200)
        self.assertEqual({item["event_type"] for item in self.notifications()}, {"warning", "exceeded"})

        status, _ = self.request(f"/api/budgets?id={budget_id}", "DELETE")
        self.assertEqual(status, 200)
        self.assertEqual({item["event_type"] for item in self.notifications()}, {"warning", "exceeded"})

    def test_notification_inbox_persists_existing_active_alerts(self):
        self.register()
        first, last = self.month_range()
        with app.db() as conn:
            user_id = conn.execute(
                "SELECT id FROM users WHERE email=?", ("alerts@example.com",)
            ).fetchone()["id"]
            budget_id = conn.execute(
                """
                INSERT INTO budgets(user_id,budget_type,amount_cents,start_date,end_date,created_at)
                VALUES(?,?,?,?,?,?) RETURNING id
                """,
                (user_id, "monthly", 100000, first.isoformat(), last.isoformat(), "test"),
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO expenses(user_id,amount_cents,category,spent_on,created_at) VALUES(?,?,?,?,?)",
                (user_id, 85000, "Food", date.today().isoformat(), "test"),
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS total FROM notifications").fetchone()["total"],
                0,
            )

        status, live_alerts = self.request("/api/alerts")
        self.assertEqual(status, 200)
        self.assertEqual(live_alerts["alerts"][0]["budgetId"], budget_id)
        self.assertEqual([item["event_type"] for item in self.notifications()], ["warning"])
        self.assertEqual([item["event_type"] for item in self.notifications()], ["warning"])

    def test_notifications_require_authentication(self):
        status, _ = self.notifications(authenticated=False)
        self.assertEqual(status, 401)

    def test_notifications_are_isolated_between_users(self):
        self.register("first@example.com")
        self.add_budget(1000, *self.month_range())
        self.add_expense(850)
        self.assertEqual(len(self.notifications()), 1)
        self.client = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.register("second@example.com")
        self.assertEqual(self.notifications(), [])

    def test_notification_storage_failure_rolls_back_expense(self):
        self.register()
        self.add_budget(1000, *self.month_range())
        with patch("app._persist_budget_alerts", side_effect=app.NotificationPersistenceError):
            status, body = self.request(
                "/api/expenses",
                "POST",
                {"amount": "850", "category": "Food", "date": date.today().isoformat()},
            )
        self.assertEqual(status, 503)
        self.assertIn("budget alert could not be saved", body["error"])
        with app.db() as conn:
            expense_count = conn.execute("SELECT COUNT(*) AS total FROM expenses").fetchone()["total"]
            notification_count = conn.execute("SELECT COUNT(*) AS total FROM notifications").fetchone()["total"]
        self.assertEqual(expense_count, 0)
        self.assertEqual(notification_count, 0)

    def test_saved_notification_survives_inbox_response_failure(self):
        self.register()
        self.add_budget(1000, *self.month_range())
        self.add_expense(850)
        original_send = app.Handler._send

        def fail_notification_response(handler, status, payload, headers=None):
            if handler.path == "/api/notifications":
                raise ConnectionError("simulated inbox response failure")
            return original_send(handler, status, payload, headers)

        with patch.object(app.Handler, "_send", fail_notification_response):
            with patch.object(self.server, "handle_error", lambda request, client_address: None):
                with self.assertRaises((OSError, http.client.HTTPException)):
                    self.request("/api/notifications")
        self.assertEqual([item["event_type"] for item in self.notifications()], ["warning"])


if __name__ == "__main__":
    unittest.main()
