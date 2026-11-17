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
(an action or prefix such as `member.`).

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
