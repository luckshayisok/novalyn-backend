# NOVALYN — API Documentation

Base path: `/api/v1`. JSON in, JSON out. An interactive reference generated
from the code is served at `/api/docs` outside production.

## Conventions

**Authentication.** Send `Authorization: Bearer <access_token>`. Access tokens
last 15 minutes. The refresh token is an httpOnly cookie (`novalyn_refresh`)
scoped to `/api/v1/auth`; call `POST /auth/refresh` to get a new access token.

**Client header.** `POST /auth/refresh` and `POST /auth/logout` require an
`X-Requested-With` header with any non-empty value.

**Errors.** Every error has the same shape:

```json
{
  "error": {
    "code": "validation_error",
    "message": "The submitted data is not valid.",
    "details": [{ "field": "email", "message": "Enter a valid email address." }],
    "request_id": "9f0c1c0e5c0b4e0c8f3b2f6d0a1e7c55"
  }
}
```

| Status | Meaning |
|---|---|
| 400 | Invalid or expired link token (`invalid_token`) |
| 401 | Not authenticated: `not_authenticated`, `invalid_token`, `session_revoked`, `invalid_credentials`, `invalid_refresh_token` |
| 403 | Authenticated but not allowed: `permission_denied` and specific codes below |
| 404 | Not found. Also returned for organizations the caller is not a member of |
| 409 | Conflict with current state |
| 410 | Invitation no longer available |
| 422 | Validation failed |
| 429 | Rate limited; see the `Retry-After` header |

**Request ID.** Every response carries `X-Request-ID`. Send your own value
(8 to 64 ASCII characters) to correlate logs.

**Roles.** `owner`, `admin`, `compliance_manager`, `member`, `viewer`.

## Health

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | Liveness |
| GET | `/health/ready` | Readiness; checks the database. `503` when it fails |

## Authentication

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/auth/register` | none | Create an account and a session. Sends a confirmation email |
| POST | `/auth/login` | none | Sign in |
| POST | `/auth/refresh` | cookie | Rotate the refresh token, return a new access token |
| POST | `/auth/logout` | cookie | End this session. `204` |
| POST | `/auth/logout-all` | bearer | End every session of the user. `204` |
| GET | `/auth/me` | bearer | The user and their organizations with role and permissions |
| PATCH | `/auth/me` | bearer | Update `full_name` |
| POST | `/auth/verify-email` | none | Confirm an email address with the emailed token |
| POST | `/auth/resend-verification` | bearer | Send a new confirmation email. `202` |
| POST | `/auth/forgot-password` | none | Request a reset link. Always `202`, whether or not the account exists |
| POST | `/auth/reset-password` | none | Set a new password with the emailed token. Ends all sessions |
| POST | `/auth/change-password` | bearer | Change password. Ends the user's other sessions |
| DELETE | `/auth/me` | bearer | Delete the account for good. Body `{ "password" }`. `204`. `401 wrong_password`; `409 sole_owner` with `details.organizations` when the caller is the only owner of a live organization |

`POST /auth/register`

```json
{ "email": "ada@example.com", "password": "at least 10 characters", "full_name": "Ada Lovelace" }
```

`201` with a token response (also returned by `login` and `refresh`):

```json
{
  "access_token": "eyJ...",
  "token_type": "bearer",
  "expires_in": 900,
  "user": {
    "id": "2a6c...", "email": "ada@example.com", "full_name": "Ada Lovelace",
    "is_email_verified": false, "created_at": "2026-11-16T17:21:04Z"
  }
}
```

Errors: `409 email_taken`, `422 weak_password`.

Other request bodies:

| Endpoint | Body |
|---|---|
| `/auth/login` | `{ "email", "password" }` — `401 invalid_credentials`; `429` after 5 failures for one account in 15 minutes |
| `/auth/verify-email` | `{ "token" }` — `400 invalid_token`. Repeating a successful call returns `200` |
| `/auth/forgot-password` | `{ "email" }` |
| `/auth/reset-password` | `{ "token", "password" }` — `400 invalid_token`, `422 weak_password` |
| `/auth/change-password` | `{ "current_password", "new_password" }` — `422 incorrect_password`, `422 weak_password` |

## Organizations

| Method | Path | Permission | Description |
|---|---|---|---|
| POST | `/organizations` | signed in | Create an organization; the caller becomes owner |
| GET | `/organizations` | signed in | Organizations the caller belongs to |
| GET | `/organizations/{id}` | `organization:read` | One organization |
| PATCH | `/organizations/{id}` | `organization:update` | Update profile |
| POST | `/organizations/{id}/onboarding/complete` | `organization:update` | Mark setup finished |
| POST | `/organizations/{id}/leave` | member | Leave. `409 last_owner` for the only owner |
| POST | `/organizations/{id}/deletion` | `organization:delete` (owner) | Schedule deletion. Body `{ "confirm_name" }` (the exact name). Emails owners and admins |
| DELETE | `/organizations/{id}/deletion` | `organization:delete` | Cancel a scheduled deletion |

Organizations carry `deletion_scheduled_for` (null, or the time their data is erased). Scheduling
returns `409 cancel_subscription_first` while a paid subscription would keep charging, and
`422 confirmation_mismatch` for a wrong name. After that time the organization answers `404` and
is left out of `GET /organizations`.

Create body: `{ "name", "industry"?, "company_size"?, "timezone"? }`.
Update body, all optional: `name`, `industry`, `company_size`, `timezone`,
`document_categories`. `industry` and `company_size` accept `null` to clear.

Organization object:

```json
{
  "id": "7fc6...", "name": "Acme Facilities", "slug": "acme-facilities",
  "industry": "facility_management", "company_size": "11-50",
  "timezone": "Asia/Kolkata", "document_categories": ["insurance_policy", "permit"],
  "onboarding_completed_at": null, "created_at": "2026-11-16T17:21:30Z",
  "role": "owner",
  "permissions": ["audit:read", "billing:manage", "..."]
}
```

`role` and `permissions` describe the caller, not the organization.

## Members

| Method | Path | Permission | Description |
|---|---|---|---|
| GET | `/organizations/{id}/members` | `members:read` | List members |
| PATCH | `/organizations/{id}/members/{membership_id}` | `members:manage` | Change role: `{ "role" }` |
| DELETE | `/organizations/{id}/members/{membership_id}` | `members:manage` | Remove. `204` |

Rules: only an owner can grant or change the owner and admin roles; an admin
manages the roles below admin; nobody changes their own role
(`403 cannot_change_own_role`); an organization keeps at least one owner
(`409 last_owner`); removing yourself goes through `/leave` (`409 use_leave`).

## Invitations

| Method | Path | Permission | Description |
|---|---|---|---|
| GET | `/organizations/{id}/invitations` | `members:invite` | Open invitations (pending and expired) |
| POST | `/organizations/{id}/invitations` | `members:invite` | Invite: `{ "email", "role" }` |
| POST | `/organizations/{id}/invitations/{invitation_id}/resend` | `members:invite` | New link; the old one stops working |
| DELETE | `/organizations/{id}/invitations/{invitation_id}` | `members:invite` | Revoke. `204` |
| GET | `/invitations/{token}` | none | What the link invites to |
| POST | `/invitations/{token}/accept` | signed in | Accept; the caller's email must match |

Errors: `403 email_not_verified` (the inviter has not confirmed their own
email), `409 already_member`, `409 already_invited`,
`403 invitation_email_mismatch`, `410 invitation_expired`,
`410 invitation_revoked`, `410 invitation_accepted`. Ownership cannot be
granted by invitation.

## Audit log

| Method | Path | Permission | Description |
|---|---|---|---|
| GET | `/organizations/{id}/audit-logs` | `audit:read` | Newest first |

Query: `page` (default 1), `page_size` (default 25, max 100), `action`
(an action or prefix such as `member.`), `actor` (a user ID), `resource_type`,
`start` and `end` (dates, both included, in the organization's time zone;
`422 invalid_range` when the end is before the start).

```json
{
  "items": [{
    "id": "c1d2...", "action": "member.role_changed", "resource_type": "membership",
    "resource_id": "9a8b...", "metadata": { "email": "mia@example.com", "from": "member", "to": "admin" },
    "actor": { "id": "2a6c...", "full_name": "Olivia Owner", "email": "olivia@example.com" },
    "created_at": "2026-11-16T17:24:11Z"
  }],
  "total": 9, "page": 1, "page_size": 25
}
```

Actions: `organization.created`, `organization.settings_changed`,
`organization.onboarding_completed`, `member.invited`,
`member.invitation_resent`, `member.invitation_revoked`, `member.joined`,
`member.role_changed`, `member.removed`, `member.left`.

There are no endpoints to change or delete entries.

## Reference data

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/meta/options` | none | Industries, company sizes, document categories, roles |

## Rate limits

| Endpoint | Limit |
|---|---|
| `/auth/register` | 10 per hour per client address |
| `/auth/login` | 20 per minute per client address; 5 failures per account per 15 minutes |
| `/auth/refresh` | 120 per minute per client address |
| `/auth/forgot-password` | 5 per hour per client address |
| `/auth/reset-password` | 10 per hour per client address |
| `/auth/verify-email` | 30 per hour per client address |
| `/auth/resend-verification` | 3 per hour per user |
| `/auth/change-password` | 10 per hour per user |
| Create organization | 10 per hour per user |
| Invitations (create and resend) | 50 per hour per organization |
| `/invitations/{token}` | 60 per minute per client address |

## Documents

All paths below are under `/organizations/{id}`.

| Method | Path | Permission | Description |
|---|---|---|---|
| POST | `/documents` | `documents:write` | Upload a file and create a document. `multipart/form-data` |
| GET | `/documents` | `documents:read` | Library: search, filter, sort, paginate |
| GET | `/documents/{document_id}` | `documents:read` | One document with its full version history |
| PATCH | `/documents/{document_id}` | `documents:write` | Change details |
| POST | `/documents/{document_id}/versions` | `documents:write` | Upload a replacement file. `multipart/form-data` |
| POST | `/documents/{document_id}/versions/{version_id}/file-url` | `documents:read` | A short-lived link to view or download one version |
| POST | `/documents/{document_id}/verify` | `documents:verify` | Mark as verified |
| POST | `/documents/{document_id}/reject` | `documents:verify` | Reject: `{ "reason" }` |
| POST | `/documents/{document_id}/archive` | `documents:delete` | Hide; read-only until restored |
| POST | `/documents/{document_id}/restore` | `documents:delete` | Bring back |

There is no delete. Archived documents keep their files and history.

**Upload** fields: `file` (required), `category` (required), and optionally
`name` (defaults to the file name), `document_number`, `issuing_authority`,
`holder_name`, `issue_date`, `expiry_date` (`YYYY-MM-DD`), `notes`,
`entity_id`, `owner_membership_id`. Accepted files: PDF, DOCX, JPG, PNG, up to
25 MB, with content that matches the extension. Errors:
`422 unsupported_file_type`, `422 file_content_mismatch`, `422 empty_file`,
`413 file_too_large`.

**Replace file** fields: `file`, and optionally `issue_date`, `expiry_date`,
`note`. The new file becomes the current version, the old one stays in the
history, and the document goes back to pending review.

**List** query: `q` (name, number, issuer or holder), `category`, `status`
(`valid`, `expiring_soon`, `expired`), `verification` (`pending`, `verified`,
`rejected`), `entity_id`, `owner_membership_id`, `archived` (`true` for the
archive), `sort` (`expiry_date`, `name`, `created_at`, `updated_at`, each with
an optional leading `-`; default `expiry_date`, undated documents last),
`page`, `page_size` (max 100).

**Document object**

```json
{
  "id": "dfe8...", "name": "Fire Safety Certificate", "category": "safety_certificate",
  "document_number": "FS-2291", "issuing_authority": null, "holder_name": null,
  "issue_date": null, "expiry_date": "2026-12-01", "notes": null,
  "status": "expiring_soon", "days_until_expiry": 14,
  "verification_status": "pending", "verified_at": null, "verified_by_name": null,
  "rejection_reason": null,
  "entity": { "id": "cb34...", "name": "North Warehouse", "type": "location" },
  "owner": null,
  "current_version": {
    "id": "9090...", "version_number": 1, "original_filename": "Fire Safety Certificate.pdf",
    "content_type": "application/pdf", "size_bytes": 409, "sha256": "...",
    "note": null, "uploaded_by_name": "Olivia Owner", "created_at": "...", "previewable": true
  },
  "version_count": 1, "archived_at": null, "created_at": "...", "updated_at": "..."
}
```

`status` is calculated from the expiry date and today's date in the
organization's time zone: `expired` before today, `expiring_soon` from today
until 30 days ahead, otherwise `valid` (also when there is no expiry date), or
`archived`. The detail endpoint adds `versions`, newest first.

Changing `category`, `document_number`, `issuing_authority`, `holder_name`,
`issue_date` or `expiry_date` on a verified document sets it back to pending.

### Files

Uploads can also be refused with `422 file_infected` (malware found) or
`503 scanner_unavailable` (scanning is on and the scanner cannot be reached).

`POST .../file-url` with `{ "disposition": "inline" | "attachment" }` returns
`{ "url": "/api/v1/files/<token>", "expires_in": 120 }`. `GET` that URL with no
other credentials to receive the file. PDFs and images can be shown inline;
other types are always served as attachments. An expired or altered link
returns `404 file_link_invalid`. Downloads (not previews) are recorded in the
audit log as `document.downloaded`.

## Entities

| Method | Path | Permission | Description |
|---|---|---|---|
| GET | `/entities` | `entities:read` | List with document counts. Query: `type`, `q`, `archived`, `page`, `page_size` |
| GET | `/entities/lookup` | `entities:read` | Up to 50 active names for pickers. Query: `type`, `q` |
| GET | `/entities/counts` | `entities:read` | Number of active records of each type |
| POST | `/entities` | `entities:write` | Create |
| GET | `/entities/{entity_id}` | `entities:read` | One record with document counts |
| PATCH | `/entities/{entity_id}` | `entities:write` | Change details |
| POST | `/entities/{entity_id}/archive` | `entities:write` | Hide from lists and pickers |
| POST | `/entities/{entity_id}/restore` | `entities:write` | Bring back |

Types: `employee`, `supplier`, `equipment`, `location`, `department`. Fields:
`name` (required), `code`, `email`, `phone`, `contact_name`, `address`,
`notes`, `location_id` (must be a location), `department_id` (must be a
department), `owner_membership_id`. A location cannot sit inside another
location, nor a department inside another department. Responses include
`document_count`, `expiring_soon_count` and `expired_count`.

## Dashboard and search

| Method | Path | Permission | Description |
|---|---|---|---|
| GET | `/dashboard` | `documents:read` | Figures for the overview page |
| GET | `/search?q=` | `documents:read` | Up to 6 documents and 6 records matching the text (at least 2 characters) |

The dashboard returns `metrics` (total, valid, expiring_soon, expired,
pending_verification, archived), `by_category`, `upcoming_expirations` (six
calendar months from the current one), `by_group` (counts per location or
department, through the record each document belongs to), `ungrouped_count`,
and three short lists: `expiring_documents` (next 90 days),
`expired_documents` and `recent_documents`. Every figure is computed from the
organization's records at request time.

Audit actions added in Phase 2: `document.uploaded`, `document.updated`,
`document.version_added`, `document.verified`, `document.rejected`,
`document.downloaded`, `document.archived`, `document.restored`,
`entity.created`, `entity.updated`, `entity.archived`, `entity.restored`.

## Document intelligence

Under `/organizations/{id}/documents/{document_id}`.

| Method | Path | Permission | Description |
|---|---|---|---|
| GET | `/extraction` | `documents:read` | `{ "enabled", "extraction" }` for the current file; `extraction` is null when none exists |
| POST | `/extraction/retry` | `documents:write` | Read the current file again. `409 extraction_in_progress` while a reading is running |
| POST | `/extraction/apply` | `documents:write` | Save confirmed values: any of `name`, `category`, `document_number`, `issuing_authority`, `holder_name`, `issue_date`, `expiry_date`, `entity_id`, plus `verify` (needs `documents:verify`) |
| POST | `/extraction/dismiss` | `documents:write` | Close the suggestions without changing the document |

Statuses: `pending`, `processing`, `extraction_completed`, `review_required`,
`failed`, `verified` (reviewed and saved), `dismissed`. Apply and dismiss need
`extraction_completed` or `review_required` (`409 extraction_not_ready`
otherwise). With `AI_PROVIDER=none`, `enabled` is false and retry returns
`409 extraction_disabled`.

Each entry in `fields` is `{ "value", "confidence", "evidence", "note", "raw" }`.
Dates are `YYYY-MM-DD` in `value`, with the wording on the document in `raw`.
`entity_id` is suggested when a name on the document matches a record exactly.
`warnings` lists what needs a careful look (missing or past expiry, ambiguous
or out-of-order dates).

Document objects gain `extraction_status`; the library accepts
`suggestions=true`; the dashboard gains `metrics.suggestions_to_review`.
Audit actions: `document.extraction_applied`, `document.extraction_dismissed`,
`document.extraction_retried`.

## Notifications

Under `/organizations/{id}/notifications`. Every member sees only their own.

| Method | Path | Description |
|---|---|---|
| GET | `` | Newest first. Query: `unread=true`, `page`, `page_size` |
| GET | `/unread-count` | `{ "unread": 3 }` |
| POST | `/{notification_id}/read` | Mark one as read |
| POST | `/read-all` | Mark all as read. `204` |

A notification is `{ id, type, title, body, link, read_at, created_at }`;
`link` is a path inside the organization such as `documents/<id>` or
`renewals/<id>`. Types: `document.expiring`, `document.expired`,
`document.overdue`, `document.verification_required` (in-app only),
`document.rejected`, `renewal.requested`, `renewal.awaiting_approval`,
`renewal.completed`, `renewal.rejected`.

## Renewals

Under `/organizations/{id}/renewals`. Reading needs `renewals:read` (every role).

| Method | Path | Who | Description |
|---|---|---|---|
| GET | `` | any | Query: `status` (`open` default, `overdue`, `closed`, or a status), `mine=true`, `page`, `page_size` |
| POST | `` | manager | `{ document_id, assignee_membership_id?, deadline?, notes?, request }`. `request: true` asks the assignee now |
| GET | `/{renewal_id}` | any | Detail with history and `can` (the actions the caller may take now) |
| PATCH | `/{renewal_id}` | manager | Change `assignee_membership_id`, `deadline`, `notes` |
| POST | `/{renewal_id}/request` | manager | Ask the assignee |
| POST | `/{renewal_id}/start` | assignee or manager | |
| POST | `/{renewal_id}/file` | assignee or manager | Multipart: `file`, optional `issue_date`, `expiry_date`, `note` |
| POST | `/{renewal_id}/submit` | assignee or manager | Send for approval. Needs a file, and a new expiry date if the document had one |
| POST | `/{renewal_id}/approve` | manager with `documents:verify` | The file becomes current, the dates apply, the document is verified |
| POST | `/{renewal_id}/reject` | manager | `{ "reason" }`; the uploaded file is kept, marked not used |
| POST | `/{renewal_id}/reopen` | manager | A rejected renewal goes back to in progress |

"Manager" means owner, admin or compliance manager. Errors:
`409 renewal_exists` (one open renewal per document),
`409 renewal_wrong_status`, `409 renewal_no_file`, `422 renewal_no_expiry`,
and on the document endpoints `409 renewal_in_progress` when a direct file
replacement is attempted during a renewal.

Statuses: `not_started`, `requested`, `in_progress`, `submitted` (file
uploaded), `awaiting_approval`, `completed`, `rejected`. Document versions
have a `state`: `active`, `pending` (waiting for renewal approval) or
`rejected`. Documents carry `renewal` (the open renewal, or null).

Organizations gain `reminders_enabled` and `reminder_offsets` (days before
expiry, 0 to 365, at most 10). The dashboard gains `renewals_in_progress`,
`renewals_overdue`, `renewals_awaiting_approval`, `open_renewals` and
`renewal_trend`.

## Plan limits

Any endpoint can refuse because of the organization's plan, with HTTP 402:

| Code | When | `details` |
|---|---|---|
| `plan_limit_reached` | Uploading or restoring past the document limit; inviting or accepting past the people limit (open invitations count); adding a location past the location limit | `{ "limit": "max_documents", "max": 10, "current": 10 }` |
| `feature_not_in_plan` | Using a feature the plan does not include | `{ "feature": "csv_export", "plan": "growth" }` (the cheapest plan that has it) |

Features: `automated_reminders`, `custom_reminders` (changing `reminder_offsets`), `basic_reports`,
`advanced_reports`, `pdf_export`, `csv_export`, `document_history` (links to earlier file versions),
`entity_management`, `multiple_locations`, `advanced_permissions` (inviting or assigning Compliance Manager
and Viewer), `custom_integrations`, `priority_support`.

## Calendar

| Method | Path | Permission | Description |
|---|---|---|---|
| GET | `/organizations/{id}/calendar` | `documents:read` | Events between `start` and `end` (dates, both included, at most 62 days) |

Optional query: `types` (repeat for several: `expiry`, `renewal_deadline`, `approval`), `category`,
`owner` (a membership ID: the document owner, or the renewal assignee), `location` (an entity ID; matches
documents of the location and of records in it).

```json
{
  "start": "2026-11-01", "end": "2026-11-30", "today": "2026-11-18",
  "events": [{
    "id": "renewal:7c1e...", "type": "renewal_deadline", "date": "2026-11-28",
    "title": "Renewal of Trade licence due", "document_id": "3f2a...", "document_name": "Trade licence",
    "category": "business_license", "status": "requested", "renewal_id": "7c1e...",
    "owner_name": "Mia Member", "entity_name": "Head office"
  }]
}
```

Event types: `expiry`, `expired` (an expiry date already passed), `renewal_deadline`, `approval` (a renewal
awaiting approval, on its deadline). Sorted by date.

## Reports

| Method | Path | Permission | Description |
|---|---|---|---|
| GET | `/organizations/{id}/reports` | `reports:read` | Every report with `allowed` (in the plan), plus `can_export`, `csv_export`, `pdf_export` |
| GET | `/organizations/{id}/reports/{key}` | `reports:read` | Run a report. `start`, `end` optional (defaults per report) |
| GET | `/organizations/{id}/reports/{key}?format=csv` | `reports:export` | CSV download (UTF-8 with BOM, formula-safe), up to 5,000 rows |
| GET | `/organizations/{id}/reports/{key}?format=pdf` | `reports:export` | PDF download, up to 5,000 rows |

Reports: `expiring`, `expired`, `by_category`, `pending_verification` (basic reports); `by_supplier`,
`by_employee`, `renewal_performance`, `activity` (advanced reports). The JSON preview has `columns`, `rows`
(at most 500), `summary`, `start`, `end`, `generated_at` and `truncated`. Exports are recorded in the audit
log as `report.exported`. Periods longer than three years return `422 range_too_long`.

## Billing

| Method | Path | Who | Description |
|---|---|---|---|
| GET | `/billing/plans` | public | Every plan: limits, features, display prices (`monthly_price`, `yearly_price` in the smallest currency unit, by currency; null for Business) |
| GET | `/organizations/{id}/billing` | any member | Subscription, effective features, usage against limits, the last 24 invoices, configured payment providers, the plan catalog |
| POST | `/organizations/{id}/billing/checkout` | `billing:manage` (owner) | `{ "plan": "starter" \| "growth", "interval": "month" \| "year", "provider"?: "stripe" \| "razorpay" }` returns `{ "url" }`, the provider's hosted page |
| POST | `/organizations/{id}/billing/cancel` | `billing:manage` | Stop renewing at the end of the paid period |
| POST | `/billing/webhooks/stripe` | Stripe | Signed with `Stripe-Signature` |
| POST | `/billing/webhooks/razorpay` | Razorpay | Signed with `X-Razorpay-Signature` |

Checkout does not change the plan. It records `pending_plan`; the plan changes when the provider's signed
webhook confirms payment. Errors: `409 payments_not_configured` (no provider keys set: the message gives the
sales address), `409 plan_not_available` (no price configured for that plan and interval),
`409 already_subscribed`, `422 plan_not_self_serve` (Business), `502 payment_provider_error`.

Webhooks answer `{ "message": "processed" | "duplicate" | "ignored" }`. A bad signature, or a Stripe
timestamp more than five minutes old, is `400 invalid_webhook`; an unconfigured provider is `404`. Each
provider event is applied once. Events handled: Stripe `checkout.session.completed`,
`customer.subscription.created|updated|deleted`, `invoice.paid`, `invoice.payment_failed`; Razorpay
`subscription.activated|charged|resumed|updated|pending|halted|paused|cancelled|completed`.

Subscription statuses: `active`, `trialing`, `past_due` (the plan applies while the provider retries),
`canceled` and `incomplete` (Free limits apply).
