"""NFR01 - Performance checks for analytics, budgets, alerts, reports, the whole
dashboard and several users working at the same time.

test_performance.py covers summary, history, search and filtering. This file
covers the remaining acceptance criteria of issue #13.
"""
import http.cookiejar
import json
import math
import os
import secrets
import sys
import threading
import time
import unittest
from collections import defaultdict
from contextlib import ExitStack, nullcontext
from copy import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener
from unittest.mock import patch

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

import app

ROWS_PER_USER = 5000
MAX_SECONDS = 1.0            # one request
MAX_DASHBOARD_SECONDS = 3.0  # all dashboard requests together
CONCURRENT_USERS = 10
MAX_CONCURRENT_SECONDS = 10.0
DASHBOARD_PATHS = ("/api/categories", "/api/summary", "/api/expenses", "/api/budgets", "/api/analytics", "/api/alerts", "/api/notifications")


def client_with_independent_cookies(cookie_jar):
    """Build a separate opener carrying the same authenticated cookie values."""
    isolated_jar = http.cookiejar.CookieJar()
    for cookie in cookie_jar:
        isolated_jar.set_cookie(copy(cookie))
    return build_opener(HTTPCookieProcessor(isolated_jar))


def percentile(values, fraction):
    """Return an interpolated percentile for a non-empty sequence of timings."""
    ordered = sorted(values)
    rank = (len(ordered) - 1) * fraction
    lower = math.floor(rank)
    upper = math.ceil(rank)
    weight = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


class _ProfiledCursor:
    """Measure client-side result-fetch calls while delegating cursor behavior."""

    def __init__(self, cursor, profiler):
        self._cursor = cursor
        self._profiler = profiler

    def _fetch(self, method, *args, **kwargs):
        started = time.perf_counter()
        try:
            return getattr(self._cursor, method)(*args, **kwargs)
        finally:
            self._profiler.record(f"{method}_seconds", time.perf_counter() - started)

    def fetchone(self, *args, **kwargs):
        return self._fetch("fetchone", *args, **kwargs)

    def fetchall(self, *args, **kwargs):
        return self._fetch("fetchall", *args, **kwargs)

    def fetchmany(self, *args, **kwargs):
        return self._fetch("fetchmany", *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _DatabaseProfiler:
    """Temporary, request-scoped PostgreSQL timing collector for this test."""

    def __init__(self):
        self._local = threading.local()
        self._lock = threading.Lock()
        self._routes = defaultdict(self._empty_route)
        self.report_summary = ""

    @staticmethod
    def _empty_request(route):
        return {
            "route": route,
            "connection_attempts": 0,
            "connection_successes": 0,
            "connection_failures": 0,
            "timings": defaultdict(list),
        }

    @staticmethod
    def _empty_route():
        return {
            "requests": 0,
            "connection_attempts": 0,
            "connection_successes": 0,
            "connection_failures": 0,
            "timings": defaultdict(list),
        }

    def record(self, metric, seconds):
        request = getattr(self._local, "request", None)
        if request is not None:
            request["timings"][metric].append(seconds)

    def _finish_request(self, request):
        route = request["route"]
        with self._lock:
            aggregate = self._routes[route]
            aggregate["requests"] += 1
            for key in ("connection_attempts", "connection_successes", "connection_failures"):
                aggregate[key] += request[key]
            for metric, samples in request["timings"].items():
                aggregate["timings"][metric].extend(samples)

    def wrap_get_handler(self, original_handler):
        def profiled_handler(handler):
            previous = getattr(self._local, "request", None)
            request = self._empty_request(urlsplit(handler.path).path)
            self._local.request = request
            try:
                return original_handler(handler)
            finally:
                self._local.request = previous
                self._finish_request(request)

        return profiled_handler

    def wrap_connect(self, original_connect):
        def profiled_connect(*args, **kwargs):
            request = getattr(self._local, "request", None)
            if request is not None:
                request["connection_attempts"] += 1
            started = time.perf_counter()
            try:
                connection = original_connect(*args, **kwargs)
            except Exception:
                if request is not None:
                    request["connection_failures"] += 1
                    self.record("connection_failure_seconds", time.perf_counter() - started)
                raise
            if request is not None:
                request["connection_successes"] += 1
                self.record("connection_success_seconds", time.perf_counter() - started)
            return connection

        return profiled_connect

    def wrap_execute(self, original_execute):
        def profiled_execute(connection, query, params=()):
            started = time.perf_counter()
            try:
                cursor = original_execute(connection, query, params)
            finally:
                self.record("execute_seconds", time.perf_counter() - started)
            return _ProfiledCursor(cursor, self)

        return profiled_execute

    def _timing_summary(self, samples):
        if not samples:
            return "n=0"
        return (
            f"n={len(samples)}, total={sum(samples):.3f}s, "
            f"avg={sum(samples) / len(samples):.3f}s, "
            f"p95={percentile(samples, 0.95):.3f}s, max={max(samples):.3f}s"
        )

    def report(self):
        with self._lock:
            routes = {route: data for route, data in self._routes.items()}
        lines = [
            "NFR05 profile (client-side connection, execute, and fetch timings; no SQL or parameters):"
        ]
        for route in sorted(routes):
            data = routes[route]
            timings = data["timings"]
            lines.append(
                f"{route}: requests={data['requests']}, "
                f"connections attempted={data['connection_attempts']}, "
                f"opened={data['connection_successes']}, failed={data['connection_failures']}"
            )
            for metric in (
                "connection_success_seconds",
                "connection_failure_seconds",
                "execute_seconds",
                "fetchone_seconds",
                "fetchall_seconds",
                "fetchmany_seconds",
            ):
                if metric in timings:
                    lines.append(f"  {metric}: {self._timing_summary(timings[metric])}")
        opened = sum(data["connection_successes"] for data in routes.values())
        attempts = sum(data["connection_attempts"] for data in routes.values())
        failures = sum(data["connection_failures"] for data in routes.values())
        lines.append(
            f"NFR05 profile totals: connections attempted={attempts}, "
            f"opened={opened}, failed={failures}"
        )
        summary = "\n".join(lines)
        print(summary, file=sys.stderr, flush=True)
        return summary

    def installed(self):
        profiler = self

        class InstalledProfile:
            def __enter__(self):
                self.stack = ExitStack()
                try:
                    self.stack.enter_context(patch.object(
                        app.Handler,
                        "do_GET",
                        profiler.wrap_get_handler(app.Handler.do_GET),
                    ))
                    self.stack.enter_context(patch.object(
                        app.psycopg,
                        "connect",
                        profiler.wrap_connect(app.psycopg.connect),
                    ))
                    self.stack.enter_context(patch.object(
                        app.DatabaseConnection,
                        "execute",
                        profiler.wrap_execute(app.DatabaseConnection.execute),
                    ))
                except BaseException:
                    self.stack.close()
                    raise
                return profiler

            def __exit__(self, exc_type, exc_value, traceback):
                try:
                    return self.stack.__exit__(exc_type, exc_value, traceback)
                finally:
                    profiler.report_summary = profiler.report()

        return InstalledProfile()


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
        cls.clients = []
        cls.cookie_jars = []
        for user_number in range(CONCURRENT_USERS):
            cookie_jar = http.cookiejar.CookieJar()
            client = build_opener(HTTPCookieProcessor(cookie_jar))
            email = "dash@example.com" if user_number == 0 else f"dash{user_number}@example.com"
            body = json.dumps({
                "fullName": f"Perf Dash {user_number}",
                "email": email,
                "password": "correct-horse-battery",
            }).encode()
            request = Request(cls.base + "/api/register", data=body, headers={"Content-Type": "application/json"})
            response = client.open(request)
            response.read()
            cls.clients.append(client)
            cls.cookie_jars.append(cookie_jar)
        # Keep the original single-user client for the existing per-route tests.
        cls.client = cls.clients[0]
        today = date.today()
        now = datetime.now(timezone.utc).isoformat()
        with app.db() as conn:
            categories = app.PREDEFINED_CATEGORIES
            for user_number in range(CONCURRENT_USERS):
                email = "dash@example.com" if user_number == 0 else f"dash{user_number}@example.com"
                user_id = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]
                expense_rows = [
                    (
                        user_id,
                        100 + i,
                        categories[i % len(categories)],
                        f"{today.year}-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}",
                        "UPI",
                        f"item {i}",
                        now,
                    )
                    for i in range(ROWS_PER_USER)
                ]
                with conn.connection.cursor() as cursor:
                    cursor.executemany(
                        "INSERT INTO expenses(user_id,amount_cents,category,spent_on,payment_method,description,created_at) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                        expense_rows,
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

    def timed_get(self, path, client=None):
        start = time.perf_counter()
        response = (client or self.client).open(Request(self.base + path))
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
        request_start_time = []

        def mark_requests_started():
            request_start_time.append(time.perf_counter())

        start_barrier = threading.Barrier(
            CONCURRENT_USERS + 1,
            action=mark_requests_started,
        )

        def one_user(user_number):
            phase = "setup"
            try:
                route_clients = {
                    path: client_with_independent_cookies(self.cookie_jars[user_number])
                    for path in DASHBOARD_PATHS
                }
                phase = "start barrier"
                start_barrier.wait(timeout=30)
                phase = "concurrent dashboard requests"
                with ThreadPoolExecutor(max_workers=len(DASHBOARD_PATHS)) as route_pool:
                    route_futures = {
                        path: route_pool.submit(self.timed_get, path, route_clients[path])
                        for path in DASHBOARD_PATHS
                    }
                    route_timings = {}
                    route_failures = []
                    for path, future in route_futures.items():
                        try:
                            route_timings[path] = future.result()
                        except Exception as error:
                            route_failures.append(
                                f"{path}: {type(error).__name__}: {error}"
                            )
                if route_failures:
                    raise RuntimeError("route request failure(s): " + "; ".join(route_failures))
                return route_timings
            except Exception as error:
                # Release workers still waiting at the barrier if this worker
                # fails before reaching it. After release, this is harmless.
                start_barrier.abort()
                raise RuntimeError(
                    f"Concurrent worker {user_number} failed during {phase} "
                    f"({type(error).__name__}): {error}"
                ) from error

        def collect_worker_results(futures):
            worker_timings = []
            failures = []
            for user_number, future in enumerate(futures):
                try:
                    worker_timings.append(future.result())
                except Exception as error:
                    failures.append(f"worker {user_number}: {type(error).__name__}: {error}")
            return worker_timings, failures

        profiler_enabled = os.environ.get("NFR05_DB_PROFILE") == "1"
        profiler = _DatabaseProfiler() if profiler_enabled else None
        profile_context = profiler.installed() if profiler is not None else nullcontext()
        with profile_context:
            with ThreadPoolExecutor(max_workers=CONCURRENT_USERS) as pool:
                futures = [pool.submit(one_user, user_number) for user_number in range(CONCURRENT_USERS)]
                try:
                    start_barrier.wait(timeout=30)
                except threading.BrokenBarrierError as error:
                    start_barrier.abort()
                    _, failures = collect_worker_results(futures)
                    detail = "; ".join(failures) or "no worker exception was reported"
                    self.fail(f"Concurrent workers failed to reach the start barrier: {detail} ({error})")
                worker_timings, failures = collect_worker_results(futures)
                elapsed = time.perf_counter() - request_start_time[0]
                if failures:
                    self.fail("Concurrent dashboard worker failure(s): " + "; ".join(failures))

        route_summaries = []
        for path in DASHBOARD_PATHS:
            timings = sorted(worker[path] for worker in worker_timings)
            p95 = percentile(timings, 0.95)
            route_summaries.append(f"{path}: max={timings[-1]:.3f}s, p95={p95:.3f}s")
        diagnostic = (
            f"Concurrent dashboard workload took {elapsed:.3f}s "
            f"(limit {MAX_CONCURRENT_SECONDS:.1f}s); per-route timings: "
            + "; ".join(route_summaries)
        )
        if profiler is not None and profiler.report_summary:
            diagnostic += "\n" + profiler.report_summary
        self.assertLess(elapsed, MAX_CONCURRENT_SECONDS, diagnostic)
        self.assertLess(self.timed_get("/api/summary"), MAX_SECONDS)  # still quick afterwards


if __name__ == "__main__":
    unittest.main()
