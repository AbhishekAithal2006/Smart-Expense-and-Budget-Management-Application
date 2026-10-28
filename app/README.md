# Smart Expense and Budget Management Application

The Sprint 1 working product covers the assigned GitHub items: **#1 User Registration and Login, #7 User Profile Management, #8 Expense Management, #14 Security, and #15 Usability**.

## Run locally

Requirements: Python 3.11 or newer, PostgreSQL 15 or newer, and the Psycopg 3 driver.

```sh
cd app
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
docker compose up -d postgres
set -a
source .env
set +a
python3 app.py
```

The Compose file starts PostgreSQL on `127.0.0.1:5432` with a persistent Docker volume. The sample credentials are for local development only. The app creates its tables and indexes on first launch. Open [http://127.0.0.1:8000](http://127.0.0.1:8000) and create an account. Docker Compose is one local setup option; any PostgreSQL server can be used by setting `DATABASE_URL` to its connection URL.

Do not commit `.env` or put a production database password in source control. For a hosted database, use the provider's TLS connection URL (usually with `sslmode=require`) and keep it in an environment secret.

## Features

- Register with a name, email, and password; sign in and out.
- Sign in or create an account with Google using the OAuth authorization-code flow.
- Update name and optional mobile number in the profile; email changes take effect only after the new address is verified.
- Add, edit, delete, search, and filter personal expenses.
- View a monthly spending summary and transaction history.
- Use a responsive interface with clear form validation and feedback.
- Administrators can review a limited user account listing, suspend or reactivate accounts, and review recent system activity.

## Security and data handling

Passwords are stored as salted scrypt hashes. Sessions use random opaque tokens stored as keyed hashes, HttpOnly/SameSite cookies, CSRF protection, and same-origin checks. Repeated failed logins are throttled. Database statements are parameterized, fields are validated, and all expense reads and changes are scoped to the signed-in owner. The interface avoids rendering user-entered strings as HTML.

Administrator access is not granted during registration. After creating the intended account, an operator with access to the application host and database can provision the first administrator once from an interactive terminal:

```sh
python3 app.py bootstrap-admin admin@example.com
```

The command requires an explicit email-bound confirmation and refuses to run after an administrator already exists. Administrators can view user names, email addresses, registration dates, and account status; they cannot view password hashes or session credentials. An administrator can suspend or reactivate any other account, including another administrator, but cannot suspend their own account. Suspension immediately revokes the account's sessions and blocks login and protected requests until reactivated. The activity view records registration, successful and failed login attempts for known accounts, suspended-account login attempts, logout, password changes, administrator bootstrap, account suspension/reactivation, and expense creation, edits, and deletion. Events contain user IDs, event type, and timestamp only; password values, expense amounts, categories, descriptions, unknown login addresses, session tokens, and request contents are not recorded.

The app stores its data in PostgreSQL. Create a custom-format backup with `pg_dump` installed and database credentials available through `DATABASE_URL`:

```sh
python3 app.py backup backups/tracker-$(date +%F).dump
```

Restore into the intended database with `pg_restore --clean --if-exists --no-owner --dbname "$DATABASE_URL" backups/tracker-YYYY-MM-DD.dump`. Keep backups access-controlled because they contain personal financial information.

### Migrate existing local SQLite data

If you already have `data/tracker.sqlite3`, start PostgreSQL and set `DATABASE_URL` first. The migration command copies supported app tables into an **empty** PostgreSQL database and leaves the SQLite file untouched:

```sh
python3 migrate_sqlite_to_postgres.py data/tracker.sqlite3
```

The script refuses to copy into a PostgreSQL database that already contains app data. Preserve a separate backup of the SQLite file before migrating.

## Google sign-in setup

Google sign-in is optional and requires a Google OAuth **Web application** client. In Google Cloud Console, configure the OAuth consent screen and add this exact authorized redirect URI for local development:

```text
http://127.0.0.1:8000/auth/google/callback
```

Set the client values in the environment before starting the app (keep the secret private and out of Git):

```sh
GOOGLE_CLIENT_ID='your-client-id' \
GOOGLE_CLIENT_SECRET='your-client-secret' \
GOOGLE_REDIRECT_URI='http://127.0.0.1:8000/auth/google/callback' \
python3 app.py
```

The callback URI must match the value registered in Google Cloud Console. For a deployed app, use its HTTPS callback URL and set `SECURE_COOKIE=1`. The app asks Google only for OpenID Connect identity, email, and basic profile scopes. It links a verified Google email to an existing account with that email; Google-only accounts receive a randomly generated local password that is never shown or stored in plaintext.

## Email-change verification setup

Changing the profile email sends a one-time verification link to the new address. The current address remains active until the link is opened. Links expire after one hour. Configure an SMTP provider before enabling email changes:

```sh
SMTP_HOST='smtp.example.com' \
SMTP_PORT='587' \
SMTP_USERNAME='your-smtp-user' \
SMTP_PASSWORD='your-smtp-password' \
SMTP_FROM='Smart Expense <noreply@example.com>' \
APP_BASE_URL='http://127.0.0.1:8000' \
python3 app.py
```

Port `587` uses STARTTLS; port `465` uses SMTP over TLS. For a deployed app, set `APP_BASE_URL` to its public HTTPS origin. Keep SMTP credentials in environment secrets and out of Git. Without SMTP configuration, the app will not change the email and will show an error when a user requests an email change.

## Deployment note

The built-in HTTP server is for local development and demonstrations. Before binding beyond localhost, configure a unique `SESSION_SECRET`, set `SECURE_COOKIE=1`, and place the server behind an HTTPS reverse proxy. Do not deploy the development server directly to the public internet.

## Project references

- [Software Requirements Specification](../documents/SRS_Document.docx)
- [Security Requirements](../documents/Security_Requirements.docx)
- [Test Case Document](../documents/Test_Case_Doc.docx)
- [Component Diagram](../documents/Component_Diagram.svg)

See [Sprint 1 tracking](PROJECT_TRACKING/SPRINT_1.md) and [Sprint 2 tracking](PROJECT_TRACKING/SPRINT_2.md) for scope, demo notes, and burndown inputs.
