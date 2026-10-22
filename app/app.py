"""Dependency-free Smart Expense and Budget Management web application."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import ssl
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from email.message import EmailMessage
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
EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", f"http://{HOST}:{PORT}/auth/google/callback")
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USERNAME)
APP_BASE_URL = os.environ.get("APP_BASE_URL", f"http://{HOST}:{PORT}").rstrip("/")
EMAIL_CHANGE_TTL = 60 * 60


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _valid_email(value: str) -> bool:
    if not EMAIL_RE.fullmatch(value) or ".." in value:
        return False
    local, domain = value.rsplit("@", 1)
    return (
        len(local) <= 64
        and not local.startswith(".")
        and not local.endswith(".")
        and len(domain.rsplit(".", 1)[-1]) >= 2
    )


def _send_email_change_verification(address: str, token: str) -> None:
    if not SMTP_HOST or not SMTP_FROM or bool(SMTP_USERNAME) != bool(SMTP_PASSWORD):
        raise RuntimeError("Email delivery is not configured.")
    link = f"{APP_BASE_URL}/auth/email/verify?{urlencode({'token': token})}"
    message = EmailMessage()
    message["Subject"] = "Verify your email address"
    message["From"] = SMTP_FROM
    message["To"] = address
    message.set_content(
        "A request was made to change the email address on your Smart Expense account.\n\n"
        f"Verify the new address by opening this one-time link within 1 hour:\n{link}\n\n"
        "If you did not request this change, you can ignore this message."
    )
    context = ssl.create_default_context()
    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15, context=context) as server:
            if SMTP_USERNAME:
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(message)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls(context=context)
            if SMTP_USERNAME:
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(message)


def initialize() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                mobile TEXT NOT NULL DEFAULT '',
                currency_code TEXT NOT NULL DEFAULT 'INR',
                google_sub TEXT,
                avatar_url TEXT NOT NULL DEFAULT '',
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
                deleted_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS expenses_user_date ON expenses(user_id, spent_on DESC);
            CREATE TABLE IF NOT EXISTS login_attempts (
                email TEXT PRIMARY KEY COLLATE NOCASE,
                failures INTEGER NOT NULL DEFAULT 0,
                blocked_until INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS email_change_requests (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                new_email TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "full_name" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN full_name TEXT NOT NULL DEFAULT ''")
        if "mobile" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN mobile TEXT NOT NULL DEFAULT ''")
        if "currency_code" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN currency_code TEXT NOT NULL DEFAULT 'INR'")
        if "google_sub" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN google_sub TEXT")
        if "avatar_url" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN avatar_url TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_google_sub ON users(google_sub) WHERE google_sub IS NOT NULL")
        expense_columns = {row["name"] for row in conn.execute("PRAGMA table_info(expenses)")}
        if "deleted_at" not in expense_columns:
            conn.execute("ALTER TABLE expenses ADD COLUMN deleted_at TEXT")


def _password_hash(password: str, salt: bytes | None = None) -> bytes:
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return salt + derived


def _password_matches(password: str, stored: bytes) -> bool:
    return len(stored) == 48 and hmac.compare_digest(_password_hash(password, stored[:16]), stored)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _google_avatar_url(value: object) -> str:
    candidate = str(value or "")[:2048]
    parsed = urlparse(candidate)
    host = parsed.hostname or ""
    if parsed.scheme == "https" and re.fullmatch(r"lh\d+\.googleusercontent\.com", host) and parsed.netloc.lower() == host.lower():
        return candidate
    return ""


def _same_origin_request(handler: BaseHTTPRequestHandler) -> bool:
    origin = handler.headers.get("Origin")
    if not origin:
        return True
    parsed = urlparse(origin)
    return parsed.scheme in ("http", "https") and parsed.netloc.lower() == handler.headers.get("Host", "").lower()


class Handler(BaseHTTPRequestHandler):
    server_version = "SmartExpense/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        # Authentication callbacks carry one-time secrets in their query strings.
        if urlparse(self.path).path in ("/auth/google/callback", "/auth/email/verify"):
            super().log_message("Authentication callback query redacted")
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
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data: https://*.googleusercontent.com; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
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
            avatar_url = _google_avatar_url(identity.get("picture"))
            email_verified = identity.get("email_verified") is True or identity.get("email_verified") == "true"
            if not email_verified or not _valid_email(email) or not google_sub:
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
                        conn.execute("UPDATE users SET google_sub=?,avatar_url=? WHERE id=?", (google_sub, avatar_url, user["id"]))
                    else:
                        cursor = conn.execute(
                            "INSERT INTO users(full_name,email,google_sub,avatar_url,password_hash,created_at) VALUES(?,?,?,?,?,?)",
                            (full_name, email, google_sub, avatar_url, _password_hash(secrets.token_urlsafe(48)), datetime.now(timezone.utc).isoformat()),
                        )
                        user = {"id": cursor.lastrowid, "email": email}
                else:
                    conn.execute("UPDATE users SET avatar_url=? WHERE id=?", (avatar_url, user["id"]))
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
                "SELECT s.user_id, s.csrf_token, u.email, u.full_name, u.mobile, u.currency_code, u.google_sub, u.avatar_url, u.created_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?",
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
        if path.path == "/auth/email/verify":
            token = parse_qs(path.query).get("token", [""])[0]
            if not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", token):
                self._redirect("/?email_error=invalid")
                return
            now = int(time.time())
            outcome = "invalid"
            with db() as conn:
                request = conn.execute(
                    "SELECT user_id,new_email,expires_at FROM email_change_requests WHERE token_hash=?",
                    (self._token_hash(token),),
                ).fetchone()
                if request is not None and request["expires_at"] < now:
                    conn.execute("DELETE FROM email_change_requests WHERE user_id=?", (request["user_id"],))
                    outcome = "expired"
                elif request is not None:
                    try:
                        conn.execute("UPDATE users SET email=? WHERE id=?", (request["new_email"], request["user_id"]))
                    except sqlite3.IntegrityError:
                        outcome = "already_used"
                    else:
                        outcome = "verified"
                    conn.execute("DELETE FROM email_change_requests WHERE user_id=?", (request["user_id"],))
            self._redirect("/?email_verified=1" if outcome == "verified" else f"/?email_error={outcome}")
            return
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
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data: https://*.googleusercontent.com; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
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
            with db() as conn:
                pending = conn.execute(
                    "SELECT new_email FROM email_change_requests WHERE user_id=? AND expires_at>?",
                    (session["user_id"], int(time.time())),
                ).fetchone()
            self._send(HTTPStatus.OK, {
                "fullName": session["full_name"], "email": session["email"], "mobile": session["mobile"],
                "currencyCode": session["currency_code"],
                "avatarUrl": session["avatar_url"], "loginMethod": "Google" if session["google_sub"] else "Email and password",
                "createdAt": session["created_at"], "pendingEmail": pending["new_email"] if pending else "",
            })
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
                rows = conn.execute(f"SELECT id,amount_cents,category,spent_on,payment_method,description,deleted_at FROM expenses WHERE {' AND '.join(clauses)} ORDER BY spent_on DESC,id DESC LIMIT 500", params).fetchall()
            self._send(HTTPStatus.OK, {"expenses": [dict(r) for r in rows]})
            return
        if path.path == "/api/summary":
            session = self._require_session()
            if session is None:
                return
            month = date.today().strftime("%Y-%m")
            with db() as conn:
                summary = conn.execute("SELECT COALESCE(SUM(amount_cents),0) AS spent, COUNT(*) AS count FROM expenses WHERE user_id=? AND deleted_at IS NULL AND substr(spent_on,1,7)=?", (session["user_id"], month)).fetchone()
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
            if not _valid_email(email) or len(password) < 10 or len(password) > 128:
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
                        # Persist the failed attempt before replying. The client may
                        # retry as soon as it receives this response, and the
                        # connection context would otherwise commit only after the
                        # response has already been sent.
                        conn.commit()
                        self._send(HTTPStatus.UNAUTHORIZED, {"error": "Email or password is incorrect."})
                        return
                    user_id = user["id"]
                    conn.execute("DELETE FROM login_attempts WHERE email=?", (email,))
            token, csrf = self._new_session(int(user_id))
            with db() as conn:
                profile = conn.execute("SELECT full_name FROM users WHERE id=?", (user_id,)).fetchone()
            self._send(HTTPStatus.OK, {"email": email, "fullName": profile["full_name"], "csrfToken": csrf}, {"Set-Cookie": self._cookie_header(token)})
            return
        if path == "/api/password":
            session = self._require_session(write=True)
            if session is None:
                return
            current_password = str(body.get("currentPassword", ""))
            new_password = str(body.get("newPassword", ""))
            confirm_password = str(body.get("confirmPassword", ""))
            if len(new_password) < 10 or len(new_password) > 128:
                self._send(HTTPStatus.BAD_REQUEST, {"error": "Use a new password between 10 and 128 characters."})
                return
            if new_password != confirm_password:
                self._send(HTTPStatus.BAD_REQUEST, {"error": "The new passwords do not match."})
                return
            with db() as conn:
                user = conn.execute("SELECT password_hash,google_sub FROM users WHERE id=?", (session["user_id"],)).fetchone()
                if user is None:
                    self._send(HTTPStatus.NOT_FOUND, {"error": "Account not found."})
                    return
                if user["google_sub"]:
                    self._send(HTTPStatus.BAD_REQUEST, {"error": "This account uses Google sign-in. Manage its password with Google."})
                    return
                if not _password_matches(current_password, user["password_hash"]):
                    self._send(HTTPStatus.UNAUTHORIZED, {"error": "Your current password is incorrect."})
                    return
                conn.execute("UPDATE users SET password_hash=? WHERE id=?", (_password_hash(new_password), session["user_id"]))
                current_token = self._cookie()
                current_hash = self._token_hash(current_token) if current_token else ""
                conn.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (session["user_id"], current_hash))
            self._send(HTTPStatus.OK, {"ok": True})
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
            raw_name = str(body.get("fullName", "")).strip()
            raw_email = str(body.get("email", "")).strip()
            raw_mobile = str(body.get("mobile", "")).strip()
            full_name = raw_name[:80]
            email = raw_email.lower()[:254]
            mobile = re.sub(r"[^0-9+() -]", "", raw_mobile)[:30].strip()
            currency_code = str(body.get("currencyCode", session["currency_code"])).strip().upper()
            if not full_name or len(raw_name) > 80 or len(raw_email) > 254 or not _valid_email(email):
                self._send(HTTPStatus.BAD_REQUEST, {"error": "Enter a name and a valid email address."})
                return
            if raw_mobile and (mobile != raw_mobile or not re.fullmatch(r"\+?[0-9][0-9 ()-]{5,28}", mobile) or not 10 <= len(re.sub(r"\D", "", mobile)) <= 15):
                self._send(HTTPStatus.BAD_REQUEST, {"error": "Enter a valid mobile number with 10–15 digits, or leave it empty."})
                return
            if currency_code not in {"INR", "USD", "EUR", "GBP"}:
                self._send(HTTPStatus.BAD_REQUEST, {"error": "Choose a supported currency."})
                return
            email_changed = email != session["email"].lower()
            pending_email = ""
            if email_changed:
                with db() as conn:
                    existing = conn.execute("SELECT id FROM users WHERE email=? COLLATE NOCASE AND id<>?", (email, session["user_id"])).fetchone()
                    if existing:
                        self._send(HTTPStatus.CONFLICT, {"error": "That email address is already in use."})
                        return
                    token = secrets.token_urlsafe(32)
                    token_hash = self._token_hash(token)
                    conn.execute("DELETE FROM email_change_requests WHERE user_id=?", (session["user_id"],))
                    conn.execute(
                        "INSERT INTO email_change_requests(user_id,new_email,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)",
                        (session["user_id"], email, token_hash, int(time.time()) + EMAIL_CHANGE_TTL, int(time.time())),
                    )
                try:
                    _send_email_change_verification(email, token)
                except Exception:
                    with db() as conn:
                        conn.execute("DELETE FROM email_change_requests WHERE user_id=? AND token_hash=?", (session["user_id"], token_hash))
                    self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "We couldn't send the verification email. Check the app's SMTP configuration and try again."})
                    return
                pending_email = email
            else:
                with db() as conn:
                    conn.execute("DELETE FROM email_change_requests WHERE user_id=?", (session["user_id"],))
            try:
                with db() as conn:
                    result = conn.execute("UPDATE users SET full_name=?,mobile=?,currency_code=? WHERE id=?", (full_name, mobile, currency_code, session["user_id"]))
            except sqlite3.IntegrityError:
                self._send(HTTPStatus.CONFLICT, {"error": "Your profile could not be saved."})
                return
            if result.rowcount == 0:
                self._send(HTTPStatus.NOT_FOUND, {"error": "Profile not found."})
                return
            self._send(HTTPStatus.OK, {
                "fullName": full_name, "email": session["email"], "mobile": mobile, "currencyCode": currency_code,
                "emailVerificationPending": bool(pending_email), "pendingEmail": pending_email,
            })
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
            result = conn.execute("UPDATE expenses SET amount_cents=?,category=?,spent_on=?,payment_method=?,description=? WHERE id=? AND user_id=? AND deleted_at IS NULL", (amount, category, spent_on, str(body.get("paymentMethod", "")).strip()[:40], str(body.get("description", "")).strip()[:240], int(match.group(1)), session["user_id"]))
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
            result = conn.execute("UPDATE expenses SET deleted_at=? WHERE id=? AND user_id=? AND deleted_at IS NULL", (datetime.now(timezone.utc).isoformat(), int(match.group(1)), session["user_id"]))
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
