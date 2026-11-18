# NOVALYN — Security

This describes the controls that exist in the code today (Phases 1 to 6) and the
gaps that remain. It is not a certification and makes no legal compliance claim.

## Implemented controls

### Passwords and accounts
- Argon2id hashing; hashes are upgraded on sign-in when parameters change.
- Minimum length 10 (configurable), maximum 128, not equal to the email.
- With `PASSWORD_BREACH_CHECK=true`, passwords found in known breaches are
  refused (Have I Been Pwned "Pwned Passwords", k-anonymity: only the first
  five characters of the password's SHA-1 hash are sent; responses padded).
  If the service is unreachable the check is skipped and logged.
- Account deletion needs the password and is rate limited.
- Sign-in gives the same error and takes the same time for a wrong password
  and an unknown account.
- Five failed sign-ins for one account in 15 minutes block further attempts
  for that account until the window ends.
- Password reset always answers the same way, so it does not reveal whether
  an address has an account.

### Sessions
- Access token: JWT (HS256), 15 minutes, kept in browser memory only.
- Refresh token: 256-bit random value, stored as a SHA-256 hash, delivered as
  an httpOnly, SameSite=Lax cookie scoped to `/api/v1/auth` (Secure in
  production).
- Refresh tokens rotate on every use. Reusing a rotated token outside a
  10-second window revokes the whole session.
- Every authenticated request checks that its session is still active, so
  sign-out, sign-out-everywhere and password reset take effect at once.
- Password reset ends all sessions; password change ends all other sessions.

### Tokens sent by email
- Confirmation, reset and invitation tokens are 256-bit random values, stored
  hashed, single use and time limited. Resending an invitation or requesting a
  new reset link disables the earlier link.

### Tenant isolation
- Every organization route resolves the caller's membership on the server.
  Non-members get `404`, the same as for an organization that does not exist.
- Tenant data is read and written through repositories bound to one
  organization.
- The ORM adds the tenant filter to every `SELECT` on tenant-owned models once
  the organization is resolved.
- PostgreSQL Row-Level Security on every tenant-owned table, forced for the
  table owner (`app/core/rls.py`, migration `0006`). Every transaction sets
  `app.tenant_id`, `app.user_id` and `app.rls_bypass` with
  `set_config(..., true)`, so the values end with the transaction and never
  travel with a pooled connection. Without an organization a query sees no
  tenant rows; writes must name the current organization. A person can read
  their own memberships in every organization (to choose one), nothing else.
- The system scope (`app.rls_bypass`) is used only by scheduled jobs, workers,
  the CLI, payment webhooks after their signature is verified, and the
  invitation lookup by token (for that one query).
- RLS guards against application bugs: a missing filter, a wrong join, a
  forgotten tenant check. It is not a defence against arbitrary SQL execution:
  code that can run SQL can also set the system scope. SQL injection is
  prevented by using bound parameters everywhere.
- The application must connect as an ordinary role that owns its tables.
  Superusers and BYPASSRLS roles skip row security; production refuses to start
  with one, and other environments log a warning.
- Tests attack this boundary by swapping IDs across organizations
  (`tests/test_tenant_isolation.py`) and, on PostgreSQL, with raw SQL below
  the ORM (`tests/test_rls.py`). A guard test fails if a new table with
  `organization_id` has no policy.

### Authorization
- One role-to-permission matrix on the server; routes declare the permission
  they need. The frontend only hides controls.
- Role-change rules: owners manage everyone; admins manage roles below admin;
  nobody changes their own role; the last owner cannot be removed or demoted.
- Invitations cannot grant ownership and require the inviter to have a
  confirmed email. Accepting requires being signed in with the invited address.

### Web
- CSRF: the only cookie-authenticated endpoints (refresh, sign-out) require a
  custom request header, and the cookie is SameSite=Lax. All other endpoints
  use the bearer token.
- CORS allows only configured origins.
- API responses carry `Content-Security-Policy: default-src 'none';
  frame-ancestors 'none'`, `X-Content-Type-Options`, `X-Frame-Options: DENY`,
  `Referrer-Policy`, `Cross-Origin-Resource-Policy` and `Cache-Control:
  no-store`; production adds HSTS.
- The web app is served with a Content-Security-Policy that allows scripts
  only from its own origin (no inline or injected scripts), frames only from
  its own origin (the document preview), and no plugins
  (`security-headers.conf` in the frontend repository). `npm run preview`
  serves the production build with the same policy, and the end-to-end test
  fails on any violation. Inline styles are allowed because the dialog and
  toast libraries insert style elements.
- All database access uses SQLAlchemy with bound parameters.
- Input is validated with Pydantic; unknown industries, sizes, categories,
  roles and time zones are rejected.
- Post-sign-in redirects accept only paths inside the application.
- Values interpolated into HTML emails are escaped.

### Files
- Only PDF, DOCX, JPG and PNG are accepted, and the content must match the
  extension (checked from the file's first bytes; a DOCX must be a ZIP with
  the Word document parts). The type the browser declares is ignored.
- Uploads are limited to 25 MB (configurable). Oversized requests are refused
  from the `Content-Length` header before the body is read, and the size is
  checked again while reading.
- Storage keys are generated by the server and grouped by organization; a
  key validator and a path check stop any key from leaving the storage root.
  Original filenames are stripped of directory parts and control characters.
- With `MALWARE_SCANNER=clamav`, every upload is scanned by ClamAV before it
  is stored. Infected files are refused; if the scanner cannot be reached,
  uploads are refused (fail closed) rather than stored unscanned.
- Files are never stored in the database. A failed database write removes the
  file that was just stored.
- Downloads and previews use links issued only after a permission check. Each
  link is a signed token naming one file version in one organization and
  expires after 120 seconds. Files are served with `nosniff`, `no-store`, a
  restrictive Content-Security-Policy, and as attachments unless the type is
  safe to show inline (PDF and images).
- Downloads (not previews) are recorded in the audit log.
- Document versions cannot be deleted through the application.

### Document intelligence
- With `AI_PROVIDER=anthropic`, the text of each uploaded document (or, for
  scans and photos, the file) is sent to Anthropic's API. Tell customers in
  your privacy notice and data processing terms. `heuristic` and `none` send
  nothing anywhere.
- Document text is treated as untrusted data: it is fenced in the prompt, the
  system prompt tells the model to ignore instructions inside it, and the
  answer must match a fixed JSON schema.
- The answer is validated again in code, dates are re-read from the
  document's own wording, and the result is only ever a suggestion. A person
  with write permission must apply it; only someone with verify permission
  can verify in the same step. Every apply, dismiss and retry is audited.
- Text extraction is bounded: first 10 pages, 30,000 characters, Word XML up
  to 25 MB, images up to 60 megapixels, and the worker task has a time limit.
- Error messages shown to users never include internal details.

### Notifications and renewals
- Each person can list and mark only their own notifications.
- Recipients are worked out from current memberships when an event is
  delivered, so someone removed from an organization stops receiving its
  notices. Emails go only to confirmed addresses.
- Notification text contains document names and dates, never file contents.
- A renewed file cannot become the current file without a manager with
  verify permission approving it; rejected files are kept for the record.

### Plans and payments
- Plan limits and features are checked by the server on every request that
  would exceed them; the interface only mirrors them.
- NOVALYN never sees card or bank details. People pay on Stripe's or
  Razorpay's own pages.
- A plan changes only when a webhook with a valid signature confirms payment.
  Stripe signatures are timestamped and older than five minutes are refused;
  both providers' signatures are compared in constant time. The plan named in
  a webhook is accepted only if it is a self-serve plan.
- Each provider event is stored once (unique on provider and event ID), so a
  resent or replayed event is not applied twice.
- Webhook endpoints are public but rate limited and limited to 512 KB bodies.
- Without provider keys, checkout says payment is not set up; it never
  simulates a payment.

### Reports
- Reports read only the caller's organization, behind `reports:read`; downloads
  need `reports:export` and are audited.
- CSV cells that start with `=`, `+`, `-`, `@`, tab or carriage return are
  prefixed with `'` so spreadsheets do not run them as formulas.
- Downloads are fetched with the access token and saved from memory; no file
  URL carries a credential.

### Rate limiting
- Fixed-window limits on authentication, invitation and organization-creation
  endpoints (see `API_DOCUMENTATION.md`). Counters are in memory for a single
  process and in Redis when `REDIS_URL` is set.

### Audit log
- Team and settings changes are recorded with actor, resource and metadata in
  the same transaction as the change.
- No update or delete path exists in the API, and the ORM rejects both.
- Metadata keys that look like secrets are rejected.

### Secrets and configuration
- All secrets come from environment variables. `.env` files are git-ignored.
- In production the application refuses to start with the development secret
  key, a non-PostgreSQL database, insecure cookies, inline task execution, a
  development email adapter or a wildcard CORS origin.
- Unhandled errors return a generic message with a request ID; details go to
  the log only. API documentation is disabled in production.

### Deletion and retention
- An owner can delete an organization by typing its name. Deletion waits 30
  days (configurable), owners and admins are emailed, and any owner can cancel.
  After the date the organization is unreachable; the nightly job erases its
  stored files, then its rows (every tenant table cascades from the
  organization). Deletion is refused while a paid subscription would keep
  charging.
- A person can delete their account with their password. Memberships, sessions,
  email links and notifications go with it; records they created stay, shown
  as from a deleted user. The audit log notes that a member left, without their
  name or email. The only owner of a live organization must hand over first.
- Housekeeping removes, nightly: ended sessions and used or expired email links
  (30 days), delivered notification events (90), read notifications (180),
  closed invitations (90), raw payment webhooks (400). Audit logs are kept for
  the life of the organization.

## Known gaps

| Gap | Plan |
|---|---|
| The Redis rate limiter is tested against an in-memory fake, not a real Redis. | Verify with Docker Compose |
| Account emails (confirmation, reset, invitation) are queued after commit and can be lost if the broker is down at that moment; each can be requested again from the UI. Notifications use the outbox and are not affected. | Accepted |
| No multi-factor authentication or single sign-on. | Not scheduled |
| Malware scanning and the leaked-password check are off unless configured. | Turn both on in production; startup logs a warning while they are off |
| A file link can be used by anyone who obtains it during its 120 seconds. | Accepted trade-off so that previews work in an iframe; keep the lifetime short |
| Organizations can be deleted but not exported first. | Data export before deletion |
| Registration reveals that an email is taken (`409`). Accepted for usability; rate limited. | Accepted |
| The client address used for rate limits is the direct peer unless the proxy is configured. | Set `FORWARDED_ALLOW_IPS` in production; see `DEPLOYMENT.md` |
| Payment integrations were tested with signed test events, not against live Stripe or Razorpay accounts. | Run a test-mode purchase, renewal, failed payment and cancellation before taking payments |

## Reporting a vulnerability

Decide on a private contact address before launch and publish it here and in
`/.well-known/security.txt`.
