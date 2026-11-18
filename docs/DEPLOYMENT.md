# NOVALYN — Deployment

> The Dockerfiles and `docker-compose.yml` were written on a machine without
> Docker and have not been built yet. The application, the migrations, Row-Level
> Security and the backup scripts were run against PostgreSQL 18, and CI runs
> them on every push. Build the images in a staging environment before relying
> on them.

## Components

| Component | Image | Notes |
|---|---|---|
| API | `backend/Dockerfile` | `uvicorn`, port 8000, health at `/health/ready` |
| Worker | same image | `celery -A app.workers.celery_app worker` |
| Scheduler | same image | `celery -A app.workers.celery_app beat`. Run exactly one |
| Web | `frontend/Dockerfile` | nginx serving the built app and proxying `/api` to the API |
| PostgreSQL | managed service recommended | version 15 or newer (tested on 18) |
| ClamAV | `clamav/clamav` | clamd on port 3310, for `MALWARE_SCANNER=clamav`. About 1.5 GB of memory |
| Redis | managed service recommended | version 7 or newer |

Serve the web app and the API from the same site (for example
`app.example.com` with `/api` proxied), so the refresh cookie is first-party.

## Database role

The application must connect as an **ordinary role that owns its tables**, not
as a superuser and not as a role with BYPASSRLS: those skip the Row-Level
Security that keeps organizations apart, and production refuses to start with
one. On a managed service, create the role once with the administrator account:

```sql
CREATE ROLE novalyn_app LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB;
CREATE DATABASE novalyn OWNER novalyn_app;
```

Run migrations as `novalyn_app` too, so it owns every table. (Docker Compose does
this with `docker/postgres-init.sh`.)

## Required configuration

Production refuses to start unless these are set correctly.

| Variable | Value |
|---|---|
| `ENVIRONMENT` | `production` |
| `SECRET_KEY` | Random, at least 32 characters. `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `DATABASE_URL` | `postgresql://novalyn_app:password@host:5432/novalyn` (the ordinary role above) |
| `REDIS_URL` | `redis://host:6379/0` |
| `CELERY_TASK_ALWAYS_EAGER` | `false` |
| `COOKIE_SECURE` | `true` |
| `FRONTEND_URL` | `https://app.example.com` |
| `CORS_ORIGINS` | `https://app.example.com` |
| `EMAIL_BACKEND` | `smtp` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` | Your email provider |
| `EMAIL_FROM` | A sender on a domain with SPF and DKIM set up |
| `STORAGE_BACKEND` | `s3` |
| `S3_BUCKET`, `S3_REGION` | The document bucket. Keep it private, with versioning and encryption at rest enabled |
| `S3_ENDPOINT_URL` | Only for S3-compatible services other than AWS |
| `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | Credentials limited to that bucket; leave unset to use the instance role |
| `AI_PROVIDER` | `anthropic` for real reading, or `none` |
| `ANTHROPIC_API_KEY` | From your secret manager |
| `LOG_JSON` | `true` |
| `FORWARDED_ALLOW_IPS` | The address of your reverse proxy, so rate limits see real client addresses |
| `MALWARE_SCANNER`, `CLAMAV_HOST`, `CLAMAV_PORT` | `clamav` and the clamd address. Strongly recommended; uploads are refused while it is unreachable |
| `PASSWORD_BREACH_CHECK` | `true` to refuse leaked passwords (needs outbound HTTPS to api.pwnedpasswords.com) |

Optional: `ORGANIZATION_DELETION_GRACE_DAYS` (30) and the `RETENTION_*_DAYS`
settings in `.env.example`. Startup logs a warning for each hardening setting
that is off.

Keep secrets in your platform's secret manager. Do not bake them into images
or commit them.

### Payments (optional)

Online payment is off until a provider is configured; until then the billing
page tells customers to contact `SALES_EMAIL`, and plans can be set with
`python -m app.cli set-plan <organization-slug> <plan>`.

| Variable | Value |
|---|---|
| `DEFAULT_PLAN` | Plan for new organizations, normally `free` |
| `SALES_EMAIL` | Where customers ask about Business plans and plan changes |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | From the Stripe dashboard. Both are needed to enable Stripe |
| `STRIPE_PRICE_{STARTER,GROWTH}_{MONTH,YEAR}` | Recurring price IDs (USD) created in Stripe |
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` | From the Razorpay dashboard. All three enable Razorpay |
| `RAZORPAY_PLAN_{STARTER,GROWTH}_{MONTH,YEAR}` | Plan IDs (INR) created in Razorpay |

Webhook endpoints to register with the providers:

- Stripe: `https://<api host>/api/v1/billing/webhooks/stripe`, events
  `checkout.session.completed`, `customer.subscription.created`,
  `customer.subscription.updated`, `customer.subscription.deleted`,
  `invoice.paid`, `invoice.payment_failed`.
- Razorpay: `https://<api host>/api/v1/billing/webhooks/razorpay`, the
  `subscription.*` events.

The prices shown on the plans page come from `app/core/plans.py`; keep them in
step with the prices in the provider dashboards. Before taking real payments,
run a purchase, a renewal, a failed payment and a cancellation in each
provider's test mode.

## Release steps

1. Build and push the API and web images.
2. Run migrations once, before starting the new API version:
   `alembic upgrade head` (the `migrate` service in Compose does this).
3. Start the API and worker; wait for `/health/ready`.
4. Start or update the web container.

Write migrations so that the previous application version keeps working
against the new schema (add first, remove in a later release).

## TLS and proxy

Terminate TLS at a load balancer or reverse proxy in front of the web
container. Redirect HTTP to HTTPS and enable HSTS there (the API also sends HSTS
in production). Forward `X-Forwarded-For` and `X-Forwarded-Proto`.

The web container's nginx adds the app's Content-Security-Policy and other
headers to the app's pages (`security-headers.conf`) and leaves `/api`
responses with the API's own headers. Do not add security headers for `/api`
at the proxy: a second Content-Security-Policy would block the document preview.
If the API is served from another origin (`VITE_API_URL`), add that origin to
`connect-src` and `frame-src`.

## Environments

| Environment | `ENVIRONMENT` | Database | Email | Tasks |
|---|---|---|---|---|
| Development | `development` | SQLite or local PostgreSQL | file or console | inline |
| Testing | `testing` | temporary SQLite, or `TEST_DATABASE_URL` | memory | inline |
| Production | `production` | PostgreSQL | SMTP | Celery with Redis |

## Health checks

- Liveness: `GET /health`
- Readiness: `GET /health/ready` (checks the database)

## Scheduled jobs

Celery Beat runs: the expiry scan hourly, the notification sweep every minute,
and **nightly maintenance** at 03:15 UTC (erase organizations past their
deletion date, then remove expired records). Run exactly one Beat process.
By hand: `python -m app.cli maintenance`.

## Backup and recovery

- **Database, managed service:** enable daily backups and point-in-time
  recovery.
- **Database, logical copies** (also a second line of defence on a managed
  service): `scripts/backup.sh` writes a compressed `pg_dump` and can copy it to
  S3 (`BACKUP_S3_URI`). Keep the backup bucket versioned, encrypted, and with a
  lifecycle rule for retention (for example 35 daily copies). The scripts dump
  and restore with row security on, in the system scope; plain `pg_dump`
  refuses to dump tables with forced row security.
- **Restore** into an empty database with `scripts/restore.sh`, as the
  application's role, then point a staging copy of the app at it and sign in.
  CI does a full backup and restore on every push; repeat a real restore
  before launch and every quarter.
- **Files:** enable versioning on the document bucket (and replication to a
  second region if you can). Organization deletion removes files; versioning
  keeps them recoverable for the bucket's noncurrent-version lifetime, so set
  that to match your promise to customers.
- Redis holds only queued tasks and rate-limit counters and needs no backup.
- Record recovery time and recovery point objectives once there are customers
  to agree them with.

## Before the first production deployment

- [x] Run the backend test suite against PostgreSQL (CI does this on every push).
- [ ] Check that CI passes on GitHub (both repositories).
- [ ] Create the database role as above; confirm the startup log shows no row-security warning.
- [ ] Build both images and run `docker compose up` end to end.
- [ ] Turn on `MALWARE_SCANNER=clamav` and `PASSWORD_BREACH_CHECK=true`; upload the EICAR test file
      and confirm it is refused.
- [ ] Take a backup with `scripts/backup.sh` and restore it with `scripts/restore.sh`.
- [ ] Send a real email through the SMTP provider.
- [ ] Confirm the refresh cookie is `Secure` and the site is HTTPS only.
- [ ] Confirm rate limits see real client addresses behind the proxy.
- [ ] Set up log collection and an alert on `5xx` responses and readiness failures.
