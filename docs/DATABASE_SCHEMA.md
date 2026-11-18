# NOVALYN — Database Schema

PostgreSQL is the production database. All primary keys are UUIDs. All
timestamps are stored in UTC with time zone. Token columns hold SHA-256 hashes,
never the token itself.

Phase 1 tables are created by migration `0001`, Phase 2 tables by `0002`.
Tables under "Planned" are the design for later phases and may change.

## Implemented (Phase 1)

### users
| Column | Type | Notes |
|---|---|---|
| id | uuid | PK |
| email | varchar(320) | unique, stored lower-case |
| password_hash | varchar(255) | Argon2id |
| full_name | varchar(200) | |
| is_active | boolean | default true |
| email_verified_at | timestamptz | null until verified |
| last_login_at | timestamptz | |
| created_at, updated_at | timestamptz | |

Users are global. They reach tenant data only through `memberships`.

### organizations
| Column | Type | Notes |
|---|---|---|
| id | uuid | PK, the tenant ID |
| name | varchar(200) | |
| slug | varchar(80) | unique, used in URLs |
| industry | varchar(80) | nullable |
| company_size | varchar(32) | nullable |
| timezone | varchar(64) | IANA name, default `UTC` |
| document_categories | json | list of category keys the organization uses |
| onboarding_completed_at | timestamptz | nullable |
| created_by_id | uuid | FK users, set null on delete |
| deletion_requested_at, deletion_scheduled_for | timestamptz | nullable; set while deletion is scheduled (indexed) |
| deletion_requested_by_id | uuid | FK users, set null on delete |
| created_at, updated_at | timestamptz | |

### memberships (tenant-owned)
| Column | Type | Notes |
|---|---|---|
| id | uuid | PK |
| organization_id | uuid | FK organizations, cascade |
| user_id | uuid | FK users, cascade |
| role | varchar(32) | owner, admin, compliance_manager, member, viewer |
| created_at, updated_at | timestamptz | |

Unique on (`organization_id`, `user_id`). Check constraint on `role`.

### invitations (tenant-owned)
| Column | Type | Notes |
|---|---|---|
| id | uuid | PK |
| organization_id | uuid | FK organizations, cascade |
| email | varchar(320) | lower-case |
| role | varchar(32) | role granted on acceptance; never owner |
| token_hash | varchar(64) | unique |
| invited_by_id | uuid | FK users, set null |
| expires_at | timestamptz | |
| last_sent_at | timestamptz | |
| accepted_at | timestamptz | nullable |
| accepted_by_id | uuid | FK users, set null |
| revoked_at | timestamptz | nullable |
| created_at | timestamptz | |

Partial unique index on (`organization_id`, `email`) where the invitation is
neither accepted nor revoked: one open invitation per address per organization.

### refresh_tokens
| Column | Type | Notes |
|---|---|---|
| id | uuid | PK |
| user_id | uuid | FK users, cascade |
| family_id | uuid | one family per login; all rotations share it |
| token_hash | varchar(64) | unique |
| expires_at | timestamptz | |
| rotated_at | timestamptz | set when exchanged for a new token |
| revoked_at | timestamptz | set on logout, reuse detection, password reset |
| user_agent | varchar(400) | |
| ip_address | varchar(64) | |
| created_at | timestamptz | |

### user_tokens
Single-use tokens for email verification and password reset.

| Column | Type | Notes |
|---|---|---|
| id | uuid | PK |
| user_id | uuid | FK users, cascade |
| purpose | varchar(32) | email_verification, password_reset |
| token_hash | varchar(64) | unique |
| expires_at | timestamptz | |
| used_at | timestamptz | nullable |
| created_at | timestamptz | |

### audit_logs (tenant-owned, append-only)
| Column | Type | Notes |
|---|---|---|
| id | uuid | PK |
| organization_id | uuid | FK organizations, cascade |
| actor_user_id | uuid | FK users, set null |
| action | varchar(100) | for example `member.role_changed` |
| resource_type | varchar(50) | |
| resource_id | varchar(64) | nullable |
| metadata | json | never secrets or document contents |
| ip_address | varchar(64) | |
| created_at | timestamptz | indexed with organization_id |

The application has no update or delete path for this table, and the ORM
rejects both.

## Implemented (Phase 2)

### entities (tenant-owned)
| Column | Type | Notes |
|---|---|---|
| id | uuid | PK |
| organization_id | uuid | FK organizations, cascade |
| type | varchar(32) | employee, supplier, equipment, location, department (check constraint) |
| name | varchar(200) | |
| code | varchar(100) | employee ID, supplier code, serial number, site code |
| email, phone, contact_name | varchar | contact details, mainly for employees and suppliers |
| address, notes | text | |
| location_id | uuid | FK entities (a location), set null |
| department_id | uuid | FK entities (a department), set null |
| owner_membership_id | uuid | FK memberships, set null: the responsible person |
| archived_at | timestamptz | archived records are hidden from lists and pickers |
| created_by_id | uuid | FK users, set null |
| created_at, updated_at | timestamptz | |

Index on (`organization_id`, `type`).

### documents (tenant-owned)
| Column | Type | Notes |
|---|---|---|
| id | uuid | PK |
| organization_id | uuid | FK organizations, cascade |
| name | varchar(255) | |
| category | varchar(50) | one of the document categories |
| document_number, issuing_authority, holder_name | varchar | facts read from the document |
| issue_date, expiry_date | date | calendar dates, no time zone |
| notes | text | |
| entity_id | uuid | FK entities, set null: what the document belongs to |
| owner_membership_id | uuid | FK memberships, set null: the renewal owner |
| verification_status | varchar(32) | pending, verified, rejected (check constraint) |
| verified_by_id, verified_at | | who reviewed it and when |
| rejection_reason | varchar(500) | |
| archived_at | timestamptz | archived documents are read-only and hidden |
| created_by_id | uuid | FK users, set null |
| created_at, updated_at | timestamptz | |

Indexes on (`organization_id`, `expiry_date`), (`organization_id`, `category`)
and `entity_id`.

The expiry status (valid, expiring soon, expired) is **not stored**. It is
calculated from `expiry_date` and today's date in the organization's time
zone each time it is needed (`app/services/expiry.py`), so it never goes stale.
Changing the category, number, issuer, holder or either date resets a
verified document to pending.

### document_versions (tenant-owned)
| Column | Type | Notes |
|---|---|---|
| id | uuid | PK |
| organization_id | uuid | FK organizations, cascade |
| document_id | uuid | FK documents, cascade |
| version_number | integer | unique with `document_id`; the highest is current |
| storage_key | varchar(512) | `organizations/{org}/documents/{doc}/{version}.{ext}` |
| original_filename | varchar(255) | cleaned of any directory part |
| content_type | varchar(120) | decided by the server from the checked content |
| size_bytes | bigint | |
| sha256 | varchar(64) | checksum of the file |
| note | varchar(500) | optional, from the uploader |
| uploaded_by_id | uuid | FK users, set null |
| created_at | timestamptz | |

The application has no path that deletes a version. File contents live in
object storage, never in the database.

## Planned

### Phase 2 design notes (superseded by the tables above)
- **entities** — `organization_id`, `type` (employee, supplier, equipment,
  location, department), `name`, `code`, contact fields, `parent_id`
  (department or location), `owner_membership_id`, `notes`, `attributes` json,
  `archived_at`.
- **documents** — `organization_id`, `name`, `category`, `document_number`,
  `issuing_authority`, `holder_name`, `issue_date`, `expiry_date`, `entity_id`,
  `owner_membership_id`, `verification_status`, `lifecycle_status`,
  `current_version_id`, `archived_at`, `created_by_id`, timestamps.
  Indexes on (`organization_id`, `expiry_date`) and
  (`organization_id`, `category`).
- **document_versions** — `organization_id`, `document_id`, `version_number`,
  `storage_key`, `original_filename`, `content_type`, `size_bytes`, `sha256`,
  `issue_date`, `expiry_date`, `uploaded_by_id`, `created_at`. Never deleted by
  the application. File binaries live in object storage, not in the database.

### Phase 3 — extraction
- **document_extractions** — `organization_id`, `document_version_id`,
  `status` (pending, processing, extraction_completed, review_required,
  failed, verified), `provider`, `fields` json (`value`, `confidence` per
  field), `error`, `started_at`, `completed_at`, `reviewed_by_id`,
  `reviewed_at`.

### Phase 4 — lifecycle
- **reminder_policies** — `organization_id`, `category` (nullable for the
  default), `offsets_days` json, `enabled`.
- **renewals** — `organization_id`, `document_id`, `previous_version_id`,
  `new_version_id`, `owner_membership_id`, `deadline`, `status`, `notes`.
- **renewal_events** — activity history per renewal.
- **outbox_events** — `organization_id`, `event_type`, `payload` json,
  `dedupe_key` (unique), `status`, `attempts`, `next_attempt_at`, `last_error`.
- **notifications** — in-app: `organization_id`, `user_id`, `type`, `title`,
  `body`, `resource_type`, `resource_id`, `read_at`.

### Phase 5 — billing (implemented in migration `0005`)
- **subscriptions** (tenant-owned) — `organization_id` (unique), `plan`
  (check: free, starter, growth, business), `status` (check: active, trialing,
  past_due, canceled, incomplete), `billing_interval`, `provider` (stripe,
  razorpay, manual), `provider_customer_id`, `provider_subscription_id`
  (indexed), `pending_plan` (chosen at checkout, applied on confirmed
  payment), `current_period_end`, `cancel_at_period_end`, `custom_limits` json,
  timestamps. Organizations without a row get one on first use with
  `DEFAULT_PLAN`.
- **billing_events** — every verified provider webhook: `provider`,
  `provider_event_id` (unique together), `event_type`,
  `organization_id` (nullable), `payload` json, `processed_at`, `error`.
- **invoices** (tenant-owned) — `provider`, `provider_invoice_id` (unique
  together), `number`, `status` (paid, failed, refunded), `amount` (smallest
  currency unit), `currency`, `description`, `hosted_url`, `issued_at`.

## Row-Level Security (PostgreSQL)

Every tenant-owned table (`TENANT_TABLES` in `app/core/rls.py`) has row security
enabled and forced, with one policy, `tenant_isolation`:

- visible and writable when `organization_id` equals `current_setting('app.tenant_id')`,
  or when `app.rls_bypass = 'on'` (system jobs);
- `memberships` are also visible to their own user (`app.user_id`).

The settings are set per transaction by the application (`app/core/tenancy.py`).
`billing_events` is not tenant-owned (some events have no organization) and is
read only by system jobs. Migration `0006` creates the policies; the test suite
creates the same ones after `create_all`.

## Conventions

- Constraint names follow one naming convention (`pk_`, `fk_`, `uq_`, `ix_`,
  `ck_`) so migrations are deterministic.
- Schema changes go through Alembic only. A test compares the migrated schema
  with the models and fails on drift.
- Deleting an organization cascades to its tenant-owned rows. The retention
  and deletion workflow that will sit in front of that is Phase 6.
