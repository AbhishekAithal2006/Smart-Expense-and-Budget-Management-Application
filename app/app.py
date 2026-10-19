"""Dependency-free Smart Expense and Budget Management web application."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request as UrlRequest, urlopen


ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("EXPENSE_DB", ROOT / "data" / "tracker.sqlite3"))
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
SESSION_TTL = 60 * 60 * 24 * 7
SECRET = os.environ.get("SESSION_SECRET", "").encode("utf-8") or b"local-development-secret-change-before-deployment"
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", f"http://{HOST}:{PORT}/auth/google/callback")


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def initialize() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                mobile TEXT NOT NULL DEFAULT '',
                google_sub TEXT,
                password_hash BLOB NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                csrf_token TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                category TEXT NOT NULL,
                spent_on TEXT NOT NULL,
                payment_method TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS expenses_user_date ON expenses(user_id, spent_on DESC);
            CREATE TABLE IF NOT EXISTS login_attempts (
                email TEXT PRIMARY KEY COLLATE NOCASE,
                failures INTEGER NOT NULL DEFAULT 0,
                blocked_until INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "full_name" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN full_name TEXT NOT NULL DEFAULT ''")
        if "mobile" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN mobile TEXT NOT NULL DEFAULT ''")
        if "google_sub" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN google_sub TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_google_sub ON users(google_sub) WHERE google_sub IS NOT NULL")


def _password_hash(password: str, salt: bytes | None = None) -> bytes:
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return salt + derived


def _password_matches(password: str, stored: bytes) -> bool:
    return len(stored) == 48 and hmac.compare_digest(_password_hash(password, stored[:16]), stored)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _same_origin_request(handler: BaseHTTPRequestHandler) -> bool:
    origin = handler.headers.get("Origin")
    if not origin:
        return True
    parsed = urlparse(origin)
    return parsed.scheme in ("http", "https") and parsed.netloc.lower() == handler.headers.get("Host", "").lower()


class Handler(BaseHTTPRequestHandler):
    server_version = "SmartExpense/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        # OAuth callbacks carry one-time codes in their query string; never log them.
        if urlparse(self.path).path == "/auth/google/callback":
            super().log_message("OAuth callback query redacted")
            return
        # Do not log request bodies, credentials, or session identifiers.
        super().log_message(fmt, *args)

    def _send(self, status: int, payload: object, headers: dict[str, str] | None = None) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 16_384)
            data = json.loads(self.rfile.read(length) or b"{}")
            return data if isinstance(data, dict) else {}
        except (ValueError, json.JSONDecodeError):
            raise ValueError("Send a valid JSON object.")

    def _cookie(self) -> str | None:
        return self._cookie_named("session")

    def _cookie_named(self, name: str) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            return cookie[name].value if name in cookie else None
        except Exception:
            return None

    def _redirect(self, location: str, cookies: list[str] | None = None) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _google_state_cookie(self, state: str, clear: bool = False) -> str:
        secure = "; Secure" if os.environ.get("SECURE_COOKIE", "0") == "1" else ""
        if clear:
            return f"google_oauth_state=; Path=/auth/google; HttpOnly; SameSite=Lax; Max-Age=0{secure}"
        return f"google_oauth_state={state}; Path=/auth/google; HttpOnly; SameSite=Lax; Max-Age=600{secure}"

    def _start_google_auth(self) -> None:
        if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
            self._redirect("/?auth_error=google_not_configured")
            return
        state = secrets.token_urlsafe(32)
        query = urlencode({
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "prompt": "select_account",
        })
        self._redirect(f"https://accounts.google.com/o/oauth2/v2/auth?{query}", [self._google_state_cookie(state)])

    def _finish_google_auth(self, query: dict[str, list[str]]) -> None:
        state_cookie = self._cookie_named("google_oauth_state") or ""
        state = query.get("state", [""])[0]
        clear_state = self._google_state_cookie("", clear=True)
        if query.get("error"):
            self._redirect("/?auth_error=google_cancelled", [clear_state])
            return
        code = query.get("code", [""])[0]
        if not state or not state_cookie or not hmac.compare_digest(state, state_cookie) or not code:
            self._redirect("/?auth_error=google_state", [clear_state])
            return
        if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
            self._redirect("/?auth_error=google_not_configured", [clear_state])
            return

        try:
            token_request = UrlRequest(
                "https://oauth2.googleapis.com/token",
                data=urlencode({
                    "code": code,
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "redirect_uri": GOOGLE_REDIRECT_URI,
                    "grant_type": "authorization_code",
                }).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with urlopen(token_request, timeout=10) as response:
                token_payload = json.loads(response.read())
            access_token = token_payload.get("access_token")
            if not access_token:
                raise ValueError("Google did not return an access token")
            userinfo_request = UrlRequest(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            with urlopen(userinfo_request, timeout=10) as response:
                identity = json.loads(response.read())
            email = str(identity.get("email", "")).strip().lower()[:254]
            google_sub = str(identity.get("sub", ""))[:255]
            email_verified = identity.get("email_verified") is True or identity.get("email_verified") == "true"
            if not email_verified or not EMAIL_RE.fullmatch(email) or not google_sub:
                raise ValueError("Google identity was missing a verified email")
            full_name = str(identity.get("name", "")).strip()[:80] or email.split("@", 1)[0]
            with db() as conn:
                user = conn.execute("SELECT id,email,google_sub FROM users WHERE google_sub=?", (google_sub,)).fetchone()
                if user is None:
                    user = conn.execute("SELECT id,email,google_sub FROM users WHERE email=?", (email,)).fetchone()
                    if user:
                        if user["google_sub"] and user["google_sub"] != google_sub:
                            self._redirect("/?auth_error=google_account_conflict", [clear_state])
                            return
                        conn.execute("UPDATE users SET google_sub=? WHERE id=?", (google_sub, user["id"]))
                    else:
                        cursor = conn.execute(
                            "INSERT INTO users(full_name,email,google_sub,password_hash,created_at) VALUES(?,?,?,?,?)",
                            (full_name, email, google_sub, _password_hash(secrets.token_urlsafe(48)), datetime.now(timezone.utc).isoformat()),
                        )
                        user = {"id": cursor.lastrowid, "email": email}
                user_id = int(user["id"])
            token, _csrf = self._new_session(user_id)
            self._redirect("/", [clear_state, self._cookie_header(token)])
        except Exception:
            # Keep provider responses and tokens out of logs and user-visible errors.
            self._redirect("/?auth_error=google_failed", [clear_state])

    def _token_hash(self, token: str) -> str:
        return hmac.new(SECRET, token.encode("utf-8"), hashlib.sha256).hexdigest()

    def _session(self) -> sqlite3.Row | None:
        token = self._cookie()
        if not token or len(token) > 200:
            return None
        with db() as conn:
            return conn.execute(
                "SELECT s.user_id, s.csrf_token, u.email, u.full_name, u.mobile FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?",
                (self._token_hash(token), int(time.time())),
            ).fetchone()

    def _require_session(self, write: bool = False) -> sqlite3.Row | None:
        session = self._session()
        if not session:
            self._send(HTTPStatus.UNAUTHORIZED, {"error": "Please log in to continue."})
            return None
        if write and not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrf_token"]):
            self._send(HTTPStatus.FORBIDDEN, {"error": "Your session token is invalid. Refresh and try again."})
            return None
        return session

    def _new_session(self, user_id: int) -> tuple[str, str]:
        raw_token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))
            conn.execute(
                "INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at) VALUES(?,?,?,?)",
                (self._token_hash(raw_token), user_id, csrf, int(time.time()) + SESSION_TTL),
            )
        return raw_token, csrf

    def _cookie_header(self, token: str, clear: bool = False) -> str:
        secure = "; Secure" if os.environ.get("SECURE_COOKIE", "0") == "1" else ""
        if clear:
            return f"session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{secure}"
        return f"session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}{secure}"

    def do_GET(self) -> None:
        path = urlparse(self.path)
        if path.path == "/auth/google":
            self._start_google_auth()
            return
        if path.path == "/auth/google/callback":
            self._finish_google_auth(parse_qs(path.query))
            return
        if path.path == "/" or path.path == "/index.html":
            try:
                data = (ROOT / "static" / "index.html").read_bytes()
            except OSError:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
            return
        if path.path in ("/app.js", "/styles.css"):
            try:
                filename = "app.js" if path.path == "/app.js" else "styles.css"
                data = (ROOT / "static" / filename).read_bytes()
            except OSError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/javascript; charset=utf-8" if path.path == "/app.js" else "text/css; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
            return
        if path.path == "/api/me":
            session = self._session()
            self._send(HTTPStatus.OK, {"authenticated": bool(session), "email": session["email"] if session else None, "csrfToken": session["csrf_token"] if session else None})
            return
        if path.path == "/api/profile":
            session = self._require_session()
            if session is None:
                return
            self._send(HTTPStatus.OK, {"fullName": session["full_name"], "email": session["email"], "mobile": session["mobile"]})
            return
        if path.path == "/api/expenses":
            session = self._require_session()
            if session is None:
                return
            query = parse_qs(path.query)
            clauses = ["user_id=?"]
            params: list[object] = [session["user_id"]]
            category = (query.get("category", [""])[0]).strip()[:60]
            text = (query.get("q", [""])[0]).strip()[:100]
            month = (query.get("month", [""])[0]).strip()
            if category:
                clauses.append("category = ?")
                params.append(category)
            if text:
                clauses.append("(category LIKE ? OR description LIKE ? OR payment_method LIKE ?)")
                pattern = f"%{text}%"
                params.extend((pattern, pattern, pattern))
            if month and re.fullmatch(r"\d{4}-\d{2}", month):
                clauses.append("substr(spent_on,1,7)=?")
                params.append(month)
            with db() as conn:
                rows = conn.execute(f"SELECT id,amount_cents,category,spent_on,payment_method,description FROM expenses WHERE {' AND '.join(clauses)} ORDER BY spent_on DESC,id DESC LIMIT 500", params).fetchall()
            self._send(HTTPStatus.OK, {"expenses": [dict(r) for r in rows]})
            return
        if path.path == "/api/summary":
            session = self._require_session()
            if session is None:
                return
            month = date.today().strftime("%Y-%m")
            with db() as conn:
                summary = conn.execute("SELECT COALESCE(SUM(amount_cents),0) AS spent, COUNT(*) AS count FROM expenses WHERE user_id=? AND substr(spent_on,1,7)=?", (session["user_id"], month)).fetchone()
            self._send(HTTPStatus.OK, {"month": month, "spentCents": summary["spent"], "expenseCount": summary["count"]})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not _same_origin_request(self):
            self._send(HTTPStatus.FORBIDDEN, {"error": "Cross-origin requests are not allowed."})
            return
        try:
            body = self._body()
        except ValueError as error:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path in ("/api/register", "/api/login"):
            full_name = str(body.get("fullName", "")).strip()[:80]
            email = str(body.get("email", "")).strip().lower()[:254]
            password = str(body.get("password", ""))
            if not EMAIL_RE.fullmatch(email) or len(password) < 10 or len(password) > 128:
                self._send(HTTPStatus.BAD_REQUEST, {"error": "Enter a valid email and a password of at least 10 characters."})
                return
            if path == "/api/register" and not full_name:
                self._send(HTTPStatus.BAD_REQUEST, {"error": "Enter your name to create an account."})
                return
            with db() as conn:
                attempt = conn.execute("SELECT failures,blocked_until FROM login_attempts WHERE email=?", (email,)).fetchone()
                now = int(time.time())
                if path == "/api/login" and attempt and attempt["blocked_until"] > now:
                    self._send(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Too many attempts. Wait 15 minutes and try again."})
                    return
                user = conn.execute("SELECT id,password_hash FROM users WHERE email=?", (email,)).fetchone()
                if path == "/api/register":
                    if user:
                        self._send(HTTPStatus.CONFLICT, {"error": "An account with that email already exists."})
                        return
                    cursor = conn.execute("INSERT INTO users(full_name,email,password_hash,created_at) VALUES(?,?,?,?)", (full_name, email, _password_hash(password), datetime.now(timezone.utc).isoformat()))
                    user_id = cursor.lastrowid
                else:
                    if not user or not _password_matches(password, user["password_hash"]):
                        failures = (attempt["failures"] if attempt else 0) + 1
                        blocked_until = now + 900 if failures >= 5 else 0
                        conn.execute("INSERT INTO login_attempts(email,failures,blocked_until) VALUES(?,?,?) ON CONFLICT(email) DO UPDATE SET failures=excluded.failures,blocked_until=excluded.blocked_until", (email, failures, blocked_until))
                        self._send(HTTPStatus.UNAUTHORIZED, {"error": "Email or password is incorrect."})
                        return
                    user_id = user["id"]
                    conn.execute("DELETE FROM login_attempts WHERE email=?", (email,))
            token, csrf = self._new_session(int(user_id))
            with db() as conn:
                profile = conn.execute("SELECT full_name FROM users WHERE id=?", (user_id,)).fetchone()
            self._send(HTTPStatus.OK, {"email": email, "fullName": profile["full_name"], "csrfToken": csrf}, {"Set-Cookie": self._cookie_header(token)})
            return
        if path == "/api/logout":
            session = self._require_session(write=True)
            if session is None:
                return
            token = self._cookie()
            if token:
                with db() as conn:
                    conn.execute("DELETE FROM sessions WHERE token_hash=?", (self._token_hash(token),))
            self._send(HTTPStatus.OK, {"ok": True}, {"Set-Cookie": self._cookie_header("", clear=True)})
            return
        if path == "/api/expenses":
            session = self._require_session(write=True)
            if session is None:
                return
            try:
                amount = round(float(body.get("amount")) * 100)
                spent_on = str(body.get("date", ""))
                if amount <= 0 or amount > 100_000_000_00:
                    raise ValueError
                date.fromisoformat(spent_on)
            except (TypeError, ValueError, OverflowError):
                self._send(HTTPStatus.BAD_REQUEST, {"error": "Enter a valid date and an amount greater than zero."})
                return
            category = str(body.get("category", "")).strip()[:60]
            if not category:
                self._send(HTTPStatus.BAD_REQUEST, {"error": "Choose a category."})
                return
            payment = str(body.get("paymentMethod", "")).strip()[:40]
            description = str(body.get("description", "")).strip()[:240]
            with db() as conn:
                conn.execute("INSERT INTO expenses(user_id,amount_cents,category,spent_on,payment_method,description,created_at) VALUES(?,?,?,?,?,?,?)", (session["user_id"], amount, category, spent_on, payment, description, datetime.now(timezone.utc).isoformat()))
            self._send(HTTPStatus.CREATED, {"ok": True})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_PATCH(self) -> None:
        path = urlparse(self.path).path
        if not _same_origin_request(self):
            self._send(HTTPStatus.FORBIDDEN, {"error": "Cross-origin requests are not allowed."})
            return
        session = self._require_session(write=True)
        if session is None:
            return
        try:
            body = self._body()
        except ValueError as error:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/profile":
            full_name = str(body.get("fullName", "")).strip()[:80]
            email = str(body.get("email", "")).strip().lower()[:254]
            mobile = re.sub(r"[^0-9+() -]", "", str(body.get("mobile", "")))[:30].strip()
            if not full_name or not EMAIL_RE.fullmatch(email):
                self._send(HTTPStatus.BAD_REQUEST, {"error": "Enter a name and a valid email address."})
                return
            if mobile and len(re.sub(r"\D", "", mobile)) < 7:
                self._send(HTTPStatus.BAD_REQUEST, {"error": "Enter a valid phone number or leave it empty."})
                return
            try:
                with db() as conn:
                    result = conn.execute("UPDATE users SET full_name=?,email=?,mobile=? WHERE id=?", (full_name, email, mobile, session["user_id"]))
            except sqlite3.IntegrityError:
                self._send(HTTPStatus.CONFLICT, {"error": "That email address is already in use."})
                return
            if result.rowcount == 0:
                self._send(HTTPStatus.NOT_FOUND, {"error": "Profile not found."})
                return
            self._send(HTTPStatus.OK, {"fullName": full_name, "email": email, "mobile": mobile})
            return
        match = re.fullmatch(r"/api/expenses/(\d+)", path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            amount = round(float(body.get("amount")) * 100)
            spent_on = str(body.get("date", ""))
            date.fromisoformat(spent_on)
            if amount <= 0 or amount > 100_000_000_00:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            self._send(HTTPStatus.BAD_REQUEST, {"error": "Enter a valid date and amount."})
            return
        category = str(body.get("category", "")).strip()[:60]
        if not category:
            self._send(HTTPStatus.BAD_REQUEST, {"error": "Choose a category."})
            return
        with db() as conn:
            result = conn.execute("UPDATE expenses SET amount_cents=?,category=?,spent_on=?,payment_method=?,description=? WHERE id=? AND user_id=?", (amount, category, spent_on, str(body.get("paymentMethod", "")).strip()[:40], str(body.get("description", "")).strip()[:240], int(match.group(1)), session["user_id"]))
        if result.rowcount == 0:
            self._send(HTTPStatus.NOT_FOUND, {"error": "Expense not found."})
            return
        self._send(HTTPStatus.OK, {"ok": True})


    def do_DELETE(self) -> None:
        if not _same_origin_request(self):
            self._send(HTTPStatus.FORBIDDEN, {"error": "Cross-origin requests are not allowed."})
            return
        match = re.fullmatch(r"/api/expenses/(\d+)", urlparse(self.path).path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        session = self._require_session(write=True)
        if session is None:
            return
        with db() as conn:
            result = conn.execute("DELETE FROM expenses WHERE id=? AND user_id=?", (int(match.group(1)), session["user_id"]))
        if result.rowcount == 0:
            self._send(HTTPStatus.NOT_FOUND, {"error": "Expense not found."})
            return
        self._send(HTTPStatus.OK, {"ok": True})


def backup(destination: str) -> None:
    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as source, sqlite3.connect(target) as output:
        source.backup(output)
    print(f"Backup written to {target}")


def main() -> None:
    initialize()
    if len(sys.argv) == 3 and sys.argv[1] == "backup":
        backup(sys.argv[2])
        return
    if HOST not in ("127.0.0.1", "localhost") and not os.environ.get("SESSION_SECRET"):
        raise SystemExit("Set SESSION_SECRET before binding beyond localhost.")
    if HOST not in ("127.0.0.1", "localhost") and os.environ.get("SECURE_COOKIE") != "1":
        raise SystemExit("Set SECURE_COOKIE=1 when binding beyond localhost (HTTPS required).")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Smart Budget and Expense Tracker running at http://{HOST}:{PORT}")
    print("Use Ctrl+C to stop the development server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
