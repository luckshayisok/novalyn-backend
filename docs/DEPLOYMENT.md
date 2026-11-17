# NOVALYN — Deployment

> The Dockerfiles and `docker-compose.yml` in this repository were written on a
> machine without Docker and have not been run yet. Build and run them in a
> staging environment before relying on them. Phase 6 hardens this document.

## Components

| Component | Image | Notes |
|---|---|---|
| API | `backend/Dockerfile` | `uvicorn`, port 8000, health at `/health/ready` |
| Worker | same image | `celery -A app.workers.celery_app worker` |
| Scheduler | same image | `celery -A app.workers.celery_app beat`. Run exactly one |
| Web | `frontend/Dockerfile` | nginx serving the built app and proxying `/api` to the API |
| PostgreSQL | managed service recommended | version 15 or newer |
| Redis | managed service recommended | version 7 or newer |

Serve the web app and the API from the same site (for example
`app.example.com` with `/api` proxied), so the refresh cookie is first-party.

## Required configuration

Production refuses to start unless these are set correctly.

| Variable | Value |
|---|---|
| `ENVIRONMENT` | `production` |
| `SECRET_KEY` | Random, at least 32 characters. `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `DATABASE_URL` | `postgresql://user:password@host:5432/novalyn` |
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

Keep secrets in your platform's secret manager. Do not bake them into images
or commit them.

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
container. Redirect HTTP to HTTPS and enable HSTS there. Forward
`X-Forwarded-For` and `X-Forwarded-Proto`.

## Environments

| Environment | `ENVIRONMENT` | Database | Email | Tasks |
|---|---|---|---|---|
| Development | `development` | SQLite or local PostgreSQL | file or console | inline |
| Testing | `testing` | temporary SQLite, or `TEST_DATABASE_URL` | memory | inline |
| Production | `production` | PostgreSQL | SMTP | Celery with Redis |

## Health checks

- Liveness: `GET /health`
- Readiness: `GET /health/ready` (checks the database)

## Backup and recovery (outline)

- PostgreSQL: daily full backups and point-in-time recovery from your managed
  service; test a restore before launch and on a schedule after.
- Object storage (from Phase 2): enable versioning on the document bucket.
- Redis holds only queued tasks and rate-limit counters and needs no backup.
- Record recovery time and recovery point objectives once there are customers
  to agree them with.

## Before the first production deployment

- [ ] Run the backend test suite against PostgreSQL (`TEST_DATABASE_URL`).
- [ ] Build both images and run `docker compose up` end to end.
- [ ] Send a real email through the SMTP provider.
- [ ] Confirm the refresh cookie is `Secure` and the site is HTTPS only.
- [ ] Confirm rate limits see real client addresses behind the proxy.
- [ ] Set up log collection and an alert on `5xx` responses and readiness failures.
