# NOVALYN — Development

NOVALYN is two repositories. Clone them side by side:

```bash
git clone https://github.com/luckshayisok/novalyn-backend.git
git clone https://github.com/luckshayisok/novalyn-frontend.git
```

Below, `backend` means the `novalyn-backend` folder and `frontend` the `novalyn-frontend` folder.

## Requirements

- Python 3.12 or newer
- Node.js 22 or newer
- Optional: PostgreSQL 15+, Redis 7+, Docker

## Backend

```bash
cd novalyn-backend
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
```

On macOS and Linux the interpreter is `.venv/bin/python`.

Copy `.env.example` to `.env` if you want to change anything; the defaults work
without it. Then create the database and start the API:

```bash
.venv/Scripts/python -m alembic upgrade head
.venv/Scripts/python run_dev.py
```

The API listens on http://127.0.0.1:8000. Interactive reference:
http://127.0.0.1:8000/api/docs.

### Development defaults

| Concern | Default | Meaning |
|---|---|---|
| Database | `sqlite:///./var/novalyn.db` | A file under `backend/var/` |
| Email | `EMAIL_BACKEND=file` | Each message is a JSON file in `backend/var/mail/`. Open the newest file to get a confirmation, reset or invitation link |
| Background tasks | `CELERY_TASK_ALWAYS_EAGER=true` | Tasks run inline; no Redis or worker needed |
| Rate limits | in memory | Reset when the API restarts |
| File storage | `STORAGE_BACKEND=local` | Uploaded files are written under `backend/var/storage/` |

### Using PostgreSQL locally

```
DATABASE_URL=postgresql://novalyn:<password>@localhost:5432/novalyn
```

Create the database and role first, then run `alembic upgrade head`.

### Running a real worker

Set `REDIS_URL`, set `CELERY_TASK_ALWAYS_EAGER=false`, and start:

```bash
.venv/Scripts/python -m celery -A app.workers.celery_app worker --loglevel=INFO
```

### Demo data

```bash
SEED_PASSWORD='<a password of at least 10 characters>' .venv/Scripts/python -m app.cli seed
```

Creates the organization "Northwind Facilities" with one confirmed user per
role (`owner@`, `admin@`, `compliance@`, `member@`, `viewer@northwind.example`),
all with the password you supplied. Refused in production.

### Tests and checks

```bash
.venv/Scripts/python -m pytest
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m ruff format .
```

The suite uses a temporary SQLite file. To run it against PostgreSQL, point
`TEST_DATABASE_URL` at an empty database; tables are dropped and recreated for
every test, so never point it at a database you care about. Connect as an
ordinary role (not a superuser), or the Row-Level Security tests fail on
purpose: superusers skip row security.

```sql
CREATE ROLE novalyn_app LOGIN PASSWORD 'novalyn_app' NOSUPERUSER NOBYPASSRLS;
CREATE DATABASE novalyn_test OWNER novalyn_app;
```

```bash
TEST_DATABASE_URL=postgresql://novalyn_app:novalyn_app@localhost:5432/novalyn_test .venv/Scripts/python -m pytest
```

Code that serves a person's request runs inside their organization. Jobs that
work across organizations (scheduler, workers, CLI) use `system_session()`
from `app/core/database.py`. A new table with `organization_id` needs a
policy: add it to `TENANT_TABLES` in `app/core/rls.py` and write a migration
that calls `enable_row_level_security` (a test checks this).

### Migrations

```bash
.venv/Scripts/python -m alembic revision --autogenerate -m "describe the change"
.venv/Scripts/python -m alembic upgrade head
```

Review generated migrations by hand. A test fails when the migrated schema and
the models differ.

### Environment variables

Documented with defaults in `backend/.env.example`. The ones that matter most:

| Variable | Purpose |
|---|---|
| `ENVIRONMENT` | `development`, `testing` or `production`. Production enables startup checks |
| `DATABASE_URL` | SQLAlchemy URL. `postgresql://` is accepted and uses psycopg 3 |
| `SECRET_KEY` | Signs access tokens. At least 32 characters in production |
| `FRONTEND_URL` | Base URL used in email links |
| `CORS_ORIGINS` | Comma-separated list of allowed origins |
| `COOKIE_SECURE` | Must be `true` in production |
| `REDIS_URL` | Celery broker and shared rate-limit counters |
| `CELERY_TASK_ALWAYS_EAGER` | `true` runs tasks inline |
| `EMAIL_BACKEND` | `file`, `console` or `smtp` |
| `SMTP_*` | SMTP settings when `EMAIL_BACKEND=smtp` |
| `STORAGE_BACKEND` | `local` or `s3` |
| `S3_*` | Bucket, region, endpoint and keys when `STORAGE_BACKEND=s3` |
| `MAX_UPLOAD_SIZE_MB` | Largest accepted file (default 25) |
| `FILE_URL_TTL_SECONDS` | Lifetime of download and preview links (default 120) |
| `EXPIRING_SOON_DAYS` | When a document counts as expiring soon (default 30) |
| `AI_PROVIDER` | `heuristic` (default, no external calls), `anthropic` or `none` |
| `ANTHROPIC_API_KEY` | Key for `AI_PROVIDER=anthropic` |
| `AI_MODEL`, `AI_EFFORT` | Model and effort for Claude (defaults `claude-opus-5-5`, `medium`) |
| `OCR_BACKEND`, `TESSERACT_CMD` | OCR for scans when the reader cannot read files itself |

### Structure

```
backend/app/
  api/deps.py          authentication, organization context, permissions
  api/v1/              routes: auth, organizations, audit
  core/                config, database, security, permissions, tenancy,
                       errors, logging, rate limiting
  models/              SQLAlchemy models
  schemas/             Pydantic schemas
  repositories/        queries; TenantRepository for tenant-owned data
  services/            business logic; services commit
  integrations/email/  email senders and templates
  workers/             Celery app and tasks
  cli.py               seed command
backend/alembic/       migrations
backend/tests/         tests
```

### Conventions

- Routes parse input and call a service. Business rules live in services.
- Tenant-owned models use the `TenantScoped` mixin and are accessed through a
  `TenantRepository` subclass.
- A new organization route takes `ctx: OrgContext = Depends(require_permission(...))`.
- Raise an `AppError` subclass for expected failures; do not return error
  dictionaries.
- Write an audit entry in the same transaction as the change it records.
- Timestamps are timezone-aware UTC (`app.core.time.utcnow`).

## Frontend

```bash
cd novalyn-frontend
npm install
npm run dev
```

Open http://localhost:5173. The dev server proxies `/api` to
`http://127.0.0.1:8000` (change with `VITE_DEV_API_TARGET`), so the browser
sees one origin.

```bash
npm run typecheck
npm run lint
npm run build
```

### Structure

```
frontend/src/
  App.tsx              providers
  routes/              router, guards, paths
  components/ui/       design-system components
  components/layout/   AppShell, AuthLayout
  components/shared/   PageHeader, states, form field, confirm dialog
  features/            auth, onboarding, dashboard, documents, entities, team, settings, audit
  services/            typed API clients and query keys
  lib/                 HTTP client, permissions, utilities
  hooks/  types/
```

### Conventions

- Server data goes through TanStack Query with keys from `services/organizations.ts`.
- Forms use React Hook Form with a Zod schema; server field errors are placed
  on fields with `applyFieldErrors`.
- Every data view handles loading, error and empty states.
- Hide controls with `useOrganization().can(permission)`; the server still decides.
- Colors, radii and fonts come from the tokens in `src/index.css`.

## Docker

From `novalyn-backend`, with `novalyn-frontend` cloned next to it, `docker compose up --build` starts PostgreSQL, Redis, the API, a worker, the
web app (http://localhost:8080) and a development mailbox
(http://localhost:8025).

## Document intelligence

When a file is uploaded, a reading is queued for it. The worker extracts the
text (PDF text layer, Word text, or OCR), passes it to the configured reader,
checks the result with plain code (dates, date order, confidence) and stores
it as suggestions. Nothing changes the document until a person reviews the
suggestions on the document page and saves them.

| `AI_PROVIDER` | What happens |
|---|---|
| `heuristic` | Rule-based development reader. No network calls, modest results, confidence capped at 60% |
| `anthropic` | Claude reads the text, or the file itself for scans and photos (no OCR needed) |
| `none` | No automatic reading; details are entered by hand |

Without a worker (`CELERY_TASK_ALWAYS_EAGER=true`) the reading runs inside
the upload request, so with Claude an upload waits for the answer. Run a
worker in production.

Code: `app/services/extraction.py` (pipeline), `app/services/text_extraction.py`,
`app/services/extraction_rules.py` (deterministic checks),
`app/integrations/ai/` (readers), `app/integrations/ocr/`.

## Reminders and notifications

Production runs a Celery worker and Celery Beat. Beat starts the reminder
scan every hour and sweeps the notification outbox every minute.

Without them (the default for local development) notifications triggered by
an action are still delivered at once, because tasks run inline. Reminders,
which depend on the date, need a scan; run it by hand:

```bash
.venv/Scripts/python -m app.cli scan-expiries
.venv/Scripts/python -m app.cli dispatch
```

With Redis available:

```bash
.venv/Scripts/python -m celery -A app.workers.celery_app worker --loglevel=INFO
.venv/Scripts/python -m celery -A app.workers.celery_app beat --loglevel=INFO
```

Code: `app/services/reminders.py`, `app/services/notifications.py`
(outbox, recipients, wording, dispatcher), `app/services/renewals.py`.

## Plans and payments

New organizations start on `DEFAULT_PLAN` (`free` by default). Free allows 10
documents and one person, so for local work put your organization on a larger
plan:

```bash
.venv/Scripts/python -m app.cli set-plan <organization-slug> growth
```

The change is recorded in the audit log. Plans, limits and features are
defined in `app/core/plans.py`; the checks are in `app/services/plans.py`. The
test suite runs on `business` and the billing tests choose plans themselves.

Online payment is off unless provider keys are set (see `.env.example`). To try
Stripe locally in test mode, set the test keys and price IDs, then forward
webhooks with the Stripe CLI:

```bash
stripe listen --forward-to localhost:8000/api/v1/billing/webhooks/stripe
```

and put the signing secret it prints in `STRIPE_WEBHOOK_SECRET`. Razorpay needs
a public URL for webhooks (a tunnel such as `cloudflared` or `ngrok`).

Code: `app/integrations/payments/` (Stripe and Razorpay adapters, signature
checks), `app/services/billing.py` (checkout, webhooks, invoices),
`app/services/calendar.py`, `app/services/reports.py`.

## End-to-end test

The frontend repository has a browser test of the whole customer journey
(`e2e/journey.spec.ts`). It starts its own API on port 8100 with a fresh
database and serves the production build with the production
Content-Security-Policy on port 4174:

```bash
cd ../novalyn-frontend
npx playwright install chromium   # once; or use an installed browser: E2E_BROWSER=msedge
npm run e2e
```

`BACKEND_DIR` points at this repository if it is not `../novalyn-backend` or `../backend`.

## Security settings in development

`PASSWORD_BREACH_CHECK` and `MALWARE_SCANNER` are off by default. To try the
scanner locally, run ClamAV (`docker compose --profile scan up clamav`) and set
`MALWARE_SCANNER=clamav`; the EICAR test file must then be refused.
