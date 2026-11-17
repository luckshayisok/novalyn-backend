# NOVALYN — Architecture

## Overview

NOVALYN is a modular monolith: one FastAPI backend, one React single-page
application, one PostgreSQL database, and a Celery worker for background work.
There are no microservices. Modules are separated by package boundaries, not
network boundaries, so the system can be split later if a module needs to
scale on its own.

```
Browser (React SPA)
      |  HTTPS, JSON, /api/v1
      v
FastAPI application  ------------------>  PostgreSQL
  routes -> services -> repositories
      |                                   Object storage (S3-compatible)
      |  enqueue
      v
Redis  -->  Celery worker / Celery Beat  -->  Email, OCR, AI providers
```

## Backend layers

| Layer | Package | Responsibility |
|---|---|---|
| Routes | `app/api/v1/*` | HTTP only: parse input, call a service, shape the response. No business logic. |
| Dependencies | `app/api/deps.py` | Authentication, organization context, permission checks, rate limits |
| Services | `app/services` | Use cases and business rules. Own the transaction and commit. |
| Repositories | `app/repositories` | All database queries. Tenant-owned data goes through `TenantRepository`. |
| Models | `app/models` | SQLAlchemy 2.x mapped classes |
| Schemas | `app/schemas` | Pydantic request and response models |
| Core | `app/core` | Configuration, security primitives, permissions, database, errors, logging, rate limiting |
| Integrations | `app/integrations` | Adapters for email, OCR, AI and payments behind small interfaces |
| Workers | `app/workers` | Celery application and tasks |

Rules:

- A route handler never queries the database directly.
- A service never returns a response object and never reads HTTP headers.
- External providers are reached only through an interface in
  `app/integrations`, with a development adapter when credentials are absent.

## Multi-tenancy

Organizations are tenants. Every tenant-owned table has an `organization_id`
column. Isolation has three layers, with a fourth planned:

1. **Route layer.** Organization routes live under
   `/api/v1/organizations/{organization_id}/...`. The `get_org_context`
   dependency loads the caller's membership for that organization. A caller
   who is not a member gets `404`, the same answer as for an organization that
   does not exist. The organization ID in the URL is never trusted on its own.
2. **Repository layer.** `TenantRepository` takes the organization ID in its
   constructor and adds it to every query, insert check, update and delete.
3. **ORM layer.** Once the organization context is resolved, the session
   carries the tenant ID and a `do_orm_execute` hook adds
   `organization_id = :tenant` to every `SELECT` on a tenant-owned model,
   including relationship loads. A query that forgets its filter still cannot
   read another tenant's rows.
4. **Database layer (Phase 6).** PostgreSQL Row-Level Security policies keyed
   on a per-transaction setting.

## Authorization

Roles: Owner, Admin, Compliance Manager, Member, Viewer. `app/core/permissions.py`
holds the single role-to-permission matrix. Routes declare the permission they
need with `require_permission(...)`. The frontend receives the caller's
permission list from the API and uses it only to hide controls; the server
decides.

Role management rules:

- Only an Owner can grant or remove the Owner or Admin role.
- An Admin can manage Compliance Managers, Members and Viewers.
- Nobody can change their own role.
- An organization always keeps at least one Owner.

## Authentication and sessions

- Passwords are hashed with Argon2id.
- Login returns a 15-minute access token (JWT, HS256) in the response body. The
  frontend keeps it in memory only.
- A refresh token (random, 256-bit, stored as a SHA-256 hash) is set as an
  httpOnly cookie scoped to `/api/v1/auth`.
- Every refresh rotates the token. Presenting an already-rotated token outside
  a short grace window revokes the whole session family.
- Each request checks that the session family behind the access token is still
  active, so logout and password reset take effect immediately.
- Cookie-authenticated endpoints require a custom header, which forces a CORS
  preflight and blocks cross-site form posts.

## Background processing

Celery with Redis as broker. Tasks are idempotent and retried with backoff.
Phase 1 has one task (send email). Phase 3 adds document processing; Phase 4
adds the expiry scan and an outbox dispatcher so that a notification is written
in the same transaction as the event that caused it and cannot be lost.

Without a broker (local development, tests) tasks run inline.

## Lifecycle automation

- **Expiry status** is calculated, not stored (`app/services/expiry.py`).
- **Reminders**: an hourly scan (`app/services/reminders.py`) works out each
  organization's date and emits one event per reminder threshold, with a
  dedupe key so that nothing is sent twice.
- **Outbox**: every event is an `outbox_events` row written in the same
  transaction as the change that caused it. The dispatcher claims due rows,
  creates one `notifications` row per recipient (unique per event and person)
  and sends emails with retries. Delivery is at least once.
- **Renewals** (`app/services/renewals.py`) keep the renewed file as a
  pending version until approval, so the current file never changes without
  an approver.

## Frontend

React, TypeScript, Vite, Tailwind CSS, shadcn/ui components, React Router,
TanStack Query, React Hook Form and Zod.

```
src/
  app/          providers, router
  components/   ui (design system), layout (shell), shared
  features/     auth, onboarding, dashboard, team, settings, audit, ...
  services/     typed API clients, one per backend module
  lib/          HTTP client, utilities
  hooks/  types/  routes/
```

- Server state lives in TanStack Query; there is no global client store.
- The HTTP client attaches the access token, refreshes it once on `401` and
  retries the request.
- Routes under `/o/:orgSlug` are protected and resolve the organization from
  the caller's memberships.

## Decisions

| Decision | Reason |
|---|---|
| Modular monolith | One deployable unit, one transaction boundary, fewer failure modes |
| Synchronous SQLAlchemy | Simpler code, shared by the API and Celery workers; FastAPI runs sync routes in a thread pool |
| Services own commits | A failed commit must be able to change the HTTP response |
| Opaque refresh tokens in the database | Revocable, rotation and reuse detection are possible |
| Hashed single-use tokens for email links | A database leak does not expose usable links |
| Portable column types | The same models run on PostgreSQL and SQLite |
| Organization ID in the URL path | The tenant is explicit in every request and log line |
