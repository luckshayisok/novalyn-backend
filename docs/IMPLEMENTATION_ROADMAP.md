# NOVALYN — Implementation Roadmap

NOVALYN is a multi-tenant SaaS platform for document intelligence, certificate
management and renewal tracking. It is built as a modular monolith in six
phases. Each phase ends with tests run, failures fixed, documentation updated
and `TASKS.md` brought up to date.

## MVP scope

The smallest version a business would pay for:

1. Sign up, create an organization, invite a team (Phase 1).
2. Upload documents, record expiry dates, find them again (Phase 2).
3. Get reminded before a document expires, by email and in-app (Phase 4).
4. Replace an expiring document with its renewal and keep the history (Phase 4).

AI extraction (Phase 3), calendar, reports and billing (Phase 5) raise the
value of the product but are not required for the first paying customer. The
phase order below follows the brief; if time to market matters more, Phase 4
can be pulled ahead of Phase 3 without rework, because extraction only fills
in fields that a person can also type.

## Phases

### Phase 1 — Foundation
- Repository inspection, architecture, database design
- Backend: FastAPI app, configuration, database layer, migrations, error
  handling, structured logging, rate limiting
- Authentication: registration, login, email verification, password reset,
  refresh-token rotation with reuse detection, logout
- Organizations: creation, profile, onboarding, members, roles, invitations
- Tenant isolation: tenant-scoped repositories, an ORM-level tenant filter,
  server-side authorization, cross-tenant tests
- Audit log foundation (append-only)
- Frontend: design system, auth screens, onboarding wizard, app shell,
  team management, organization and account settings, audit log

### Phase 2 — Core product
- Storage adapters (local, S3-compatible), signed download URLs
- Document upload (drag and drop, multi-file, progress, content validation)
- Document library: search, filter, sort, pagination
- Document detail, preview, secure download, replacement
- Entities: employees, suppliers, equipment, locations, departments
- Dashboard metrics and charts from real records
- Onboarding step 6 (first document upload) and global search

### Phase 3 — Document intelligence
- Text extraction: PDF parser, DOCX parser, OCR for images and scans
- LLM structured extraction behind a provider abstraction
- Field-level confidence, draft storage, processing states
- Human verification screen; AI never overwrites verified data
- Degraded mode when no AI provider is configured

### Phase 4 — Lifecycle automation
- Central expiry service, timezone-aware
- Reminder policies per organization, duplicate prevention
- Outbox-based notification engine: email and in-app, retries, failure tracking
- Renewal workflow and version history
- Celery Beat schedules

### Phase 5 — Business features
- Compliance calendar (month and week views, filters)
- Audit log filters and export
- Reports with CSV and PDF export
- Subscriptions: plans, entitlements, usage tracking, limit enforcement,
  Razorpay and Stripe adapters

### Phase 6 — Production readiness
- Security review, PostgreSQL Row-Level Security
- Performance and tenant-isolation testing
- End-to-end tests (browser)
- Deployment hardening, backup and recovery, retention and deletion workflows

## Security-sensitive components

| Component | Risk | Control |
|---|---|---|
| Password storage | Credential theft | Argon2id, rehash on login when parameters change |
| Sessions | Token theft and replay | Short-lived access JWT, rotating refresh token in an httpOnly cookie, reuse detection revokes the whole session family |
| Tenant boundary | Cross-tenant data access | Membership check on every organization route, tenant-scoped repositories, ORM tenant filter, RLS in Phase 6 |
| Authorization | Privilege escalation | One permission matrix on the server, role-change rules, last-owner protection |
| Invitations and reset links | Token guessing, replay | 256-bit random tokens, stored hashed, single use, expiry |
| Auth endpoints | Brute force, enumeration | Rate limits per client and per account, uniform responses for password reset |
| File uploads (Phase 2) | Malicious files, path traversal | Content sniffing, size limits, generated storage keys |
| File downloads (Phase 2) | Leaked URLs | Short-lived signed URLs issued after an authorization check |
| AI extraction (Phase 3) | Wrong data trusted | Drafts only, human verification required |
| Audit log | Tampering | Append-only in the application, no secrets or document contents in metadata |
| Secrets | Leakage | Environment variables only, startup validation in production |

## Environment variables

Defined in `backend/.env.example` and `frontend/.env.example`; described in
`DEVELOPMENT.md`.

## Constraints of the build machine

The machine this was first built on had no Docker and no Redis, and its local
PostgreSQL needed a password that was not available. So:

- Models use portable column types. PostgreSQL is the production database;
  SQLite is supported for local development and the test suite.
- Celery tasks run inline (`CELERY_TASK_ALWAYS_EAGER=true`) when no broker is
  configured.
- Dockerfiles and Compose files are provided but were not run there.

`TASKS.md` records what has and has not been verified.
