# NOVALYN

**Every document. Every deadline. Under control.**

NOVALYN is a multi-tenant SaaS platform for business document intelligence,
certificate management and renewal tracking. It gives a business one place to
keep licenses, insurance policies, certificates and permits, and makes sure
nobody misses a renewal.

## Status

All six phases (Foundation, Core product, Document intelligence, Lifecycle automation, Business features,
Production readiness) are complete. What works today:

- Accounts: registration, sign in, email confirmation, password reset,
  rotating sessions, sign out everywhere
- Organizations: creation, onboarding wizard, profile settings
- Team: invitations by email, five roles, role changes, removal, leaving
- Tenant isolation between organizations, enforced on the server
- Append-only audit log
- Documents: multi-file upload with content checks, library with search,
  filters and sorting, detail page with preview, secure download,
  file replacement with full version history, review (verify or reject),
  archive and restore
- Entities: employees, suppliers, equipment, locations and departments,
  each with its own page and documents
- Dashboard with real figures and charts, and search across the organization
- Document intelligence: uploaded files are read (PDF text, Word, OCR, or Claude for scans) and their
  details offered as suggestions, with confidence and date checks, applied only after a person reviews them
- Reminders before expiry (configurable), expired and overdue alerts, in-app notifications and email
- Renewal workflow with assignment, deadlines, the renewed file kept aside until approval, and full history
- Compliance calendar of expirations, renewal deadlines and approvals
- Eight reports with CSV and PDF export; audit log filters by area, person and date
- Plans (Free, Starter, Growth, Business) with limits enforced on the server, Stripe and Razorpay checkout through
  signed webhooks, and billing history. Online payment stays off until provider keys are configured

- Production readiness: PostgreSQL Row-Level Security on every tenant table, Content-Security-Policy,
  leaked-password check and malware scanning (when configured), organization and account deletion,
  retention, backup and restore scripts, CI, and a browser end-to-end test of the customer journey

It has not been deployed to a real environment yet. [`TASKS.md`](docs/TASKS.md) lists exactly what is done,
what is not, and what has not been verified. [`IMPLEMENTATION_ROADMAP.md`](docs/IMPLEMENTATION_ROADMAP.md) has the plan.

## Repositories

| Repository | Contents |
|---|---|
| [novalyn-backend](https://github.com/luckshayisok/novalyn-backend) | This repository: the FastAPI API, worker, migrations, tests, and the project documentation in `docs/` |
| [novalyn-frontend](https://github.com/luckshayisok/novalyn-frontend) | The React web application |

## Stack

| Layer | Technology |
|---|---|
| Frontend | React, TypeScript, Vite, Tailwind CSS, shadcn/ui-style components on Radix, React Router, TanStack Query, React Hook Form, Zod |
| Backend | Python, FastAPI, Pydantic, SQLAlchemy 2, Alembic |
| Data | PostgreSQL (SQLite supported for local development and tests) |
| Background work | Celery, Redis |
| Packaging | Docker, Docker Compose |

## Quick start (no Docker)

Requires Python 3.12+ and Node 22+.

```bash
cd novalyn-backend
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # macOS/Linux: .venv/bin/python
.venv/Scripts/python -m alembic upgrade head
.venv/Scripts/python run_dev.py
```

In a second terminal:

```bash
cd novalyn-frontend
npm install
npm run dev
```

Open http://localhost:5173. With the default configuration the backend uses a
SQLite file and writes outgoing email to `backend/var/mail/` as JSON files;
open the newest file to find confirmation and invitation links.

## Quick start (Docker)

Clone `novalyn-frontend` next to this repository, then from here:

```bash
docker compose up --build
```

App at http://localhost:8080, development mailbox at http://localhost:8025.

## Tests

```bash
cd backend && .venv/Scripts/python -m pytest
cd frontend && npm run typecheck && npm run build
```

## Documentation

| File | Contents |
|---|---|
| [`ARCHITECTURE.md`](docs/ARCHITECTURE.md) | System design, layers, multi-tenancy, sessions |
| [`DATABASE_SCHEMA.md`](docs/DATABASE_SCHEMA.md) | Tables, implemented and planned |
| [`API_DOCUMENTATION.md`](docs/API_DOCUMENTATION.md) | HTTP API reference |
| [`SECURITY.md`](docs/SECURITY.md) | Security controls and known gaps |
| [`DEVELOPMENT.md`](docs/DEVELOPMENT.md) | Local setup, configuration, conventions |
| [`DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Production deployment |
| [`IMPLEMENTATION_ROADMAP.md`](docs/IMPLEMENTATION_ROADMAP.md) | Phases and MVP scope |
| [`TASKS.md`](docs/TASKS.md) | Task list with status |

## Repository layout

```
app/        FastAPI application
alembic/    database migrations
tests/      test suite
docs/       project documentation
docker-compose.yml
```
