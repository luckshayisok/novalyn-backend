# NOVALYN — Tasks

Status as of the end of Phase 2. `[x]` done and verified, `[~]` written but not
verified, `[ ]` not started.

## Phase 1 — Foundation

### Planning
- [x] Repository inspection (fresh project)
- [x] Roadmap, architecture, database schema, security notes

### Backend
- [x] FastAPI app, configuration with production startup checks
- [x] Database layer, portable column types, Alembic migration `0001`
- [x] One error format, request IDs, structured logging, security headers
- [x] Rate limiting (memory and Redis backends)
- [x] Registration, sign in, email confirmation, password reset and change
- [x] Rotating refresh tokens with reuse detection; sign out, sign out everywhere
- [x] Organizations: create, update, onboarding completion
- [x] Members: list, change role, remove, leave, last-owner protection
- [x] Invitations: create, resend, revoke, preview, accept
- [x] Roles and permission matrix
- [x] Tenant isolation: route check, tenant repositories, ORM tenant filter
- [x] Append-only audit log with list endpoint
- [x] Email adapters (file, console, memory, SMTP) and templates
- [x] Celery app and email task
- [x] Seed command
- [x] 134 tests passing on SQLite; lint clean

### Frontend
- [x] Design tokens and UI components
- [x] HTTP client with in-memory access token and refresh-on-401
- [x] Sign in, register, forgot and reset password, confirm email, accept invitation
- [x] Onboarding wizard, steps 1 to 5, each skippable after the first
- [x] App shell: sidebar, organization switcher, account menu, mobile drawer
- [x] Overview, Team, Organization settings, Account, Audit Logs
- [x] Permission-aware navigation and controls
- [x] Type-check and production build pass

### Checked by hand in a browser
- [x] Register, onboarding (all five steps), confirm email, resume setup
- [x] Invite, resend, open invitation as the wrong user, sign out, register as
      the invitee, accept
- [x] Member view hides admin features; audit log page refuses access
- [x] Role change, audit log entries, save organization settings
- [x] Last owner cannot leave (error shown in the dialog)
- [x] Mobile layout of the overview

### Not verified
- [~] Dockerfiles, `docker-compose.yml`, nginx configuration: written, never
      built or run (no Docker on the build machine)
- [~] PostgreSQL: the migration and tests have run on SQLite only
- [~] SMTP sender: not run against a mail server
- [~] Redis rate limiter and a real Celery worker: tested with an in-memory
      fake and inline execution only
- [~] Screens not opened in the browser: forgot and reset password, account
      (profile, change password, sign out everywhere), remove member, revoke
      invitation, create a second organization. Their API calls are covered by
      backend tests

### Left over from Phase 1
- [ ] Onboarding step 6 (first document upload) — needs Phase 2
- [ ] Header global search, notifications and help — need Phases 2 and 4
- [ ] Frontend automated tests and browser end-to-end tests — Phase 6
- [ ] Split the JavaScript bundle (740 kB before compression)
- [ ] Delete unused Vite sample files: `frontend/src/App.css`,
      `frontend/src/assets/`, `frontend/public/icons.svg`
- [ ] Install `httpx2` for the test client when moving off the deprecated
      `httpx` integration (one deprecation warning today)

## Phase 2 — Core product

### Backend
- [x] Storage adapters: local and S3-compatible (S3 tested against a stand-in, `moto`)
- [x] Upload validation: allowed types, content must match the extension, size limit, safe file names
- [x] Documents: upload, details, replacement with version history, verify and reject, archive and restore
- [x] Signed, short-lived file links for preview and download; downloads audited
- [x] Expiry status calculated in the organization's time zone, never stored
- [x] Library: search, filters, sorting, pagination
- [x] Entities: five types, location and department links, responsible person, document counts, archive
- [x] Dashboard figures and global search, all from real records
- [x] Migration `0002`; 220 backend tests pass on SQLite; lint clean

### Frontend
- [x] Document library with filters kept in the URL
- [x] Upload center: drag and drop, several files at once, per-file type and expiry, progress, per-file errors
- [x] Document detail: preview (PDF, images), download, edit details, replace file, version history, verify, reject, archive
- [x] Entities: list with a tab per type, add, detail page with its documents, edit, archive
- [x] Dashboard: five figures, status chart, upcoming expirations by month, by type, by location or department, lists
- [x] Header search across documents and records
- [x] Onboarding step 6: optional first document upload

### Checked by hand in a browser
- [x] Upload three files at once: two accepted, a disguised file refused with a clear message
- [x] Library shows expired and expiring documents in the right order and status
- [x] Detail page loads the PDF preview through a signed link
- [x] Verify; edit details and attach to a location (sends the document back for review, as designed)
- [x] Create a location from the entities page
- [x] Dashboard figures, charts and lists match the data; activity feed describes document events
- [x] Header search finds a record and opens it with Enter

### Not verified
- [~] Not clicked in the browser: replace file, reject, archive and restore, download button,
      entity edit and archive, onboarding step 6, pagination past 25 items.
      All of these are covered by backend tests
- [~] Real S3 or an S3-compatible service (only the `moto` stand-in)
- [~] The new pages were checked as page text; the browser pane was hidden, so they were not looked at visually

### Left over
- [ ] Split the JavaScript bundle; it is now 1.2 MB (357 kB compressed) with the chart library
- [ ] Malware scanning of uploads (Phase 6)
- [ ] "Renewals in progress" figure and "monthly renewal trends" chart need renewals (Phase 4)
- [ ] Notifications and help in the header (Phase 4)

## Phase 3 — Document intelligence

### Backend (done)
- [x] Text extraction: PDF text layer (pypdf), Word (python-docx), bounded by pages, characters and size
- [x] OCR adapter (Tesseract, used for scans and photos when installed)
- [x] Provider interface with two adapters: Claude (`anthropic`) and a rule-based development reader (`heuristic`)
- [x] Claude reads scans and photos directly; answers are constrained to a JSON schema and validated again
- [x] Deterministic checks: date parsing, ambiguous day/month dates, date order, missing expiry, confidence threshold
- [x] Processing states: pending, processing, extraction_completed, review_required, failed, verified, dismissed
- [x] Suggestions stored as drafts; nothing changes a document until a person applies it
- [x] Apply (optionally verifying in the same step), dismiss and retry, all audited
- [x] Works with automatic reading switched off (`AI_PROVIDER=none`)
- [x] Library filter for documents with suggestions waiting
- [x] Migration `0003`; 283 backend tests pass

### Not verified
- [~] The Claude adapter was tested with a stand-in client only; no real API call has been made
- [~] Tesseract is not installed on the build machine; the OCR path is tested with a stand-in
- [~] Not clicked in the browser: dismiss, retry after a failure, the library filter and the dashboard notice
      (all covered by backend tests)

### Frontend
- [x] Suggestions panel on the document page: reading, failed with retry, ready, reviewed
- [x] Review screen: each suggestion next to the current value, confidence, the quote it came from,
      notes on doubtful dates, "keep current" per field, save or save and verify
- [x] "Suggestions" and "Reading…" badges in the library, and a "Suggestions to review" filter
- [x] Dashboard notice when suggestions are waiting; audit log wording for the new events

### Checked by hand in a browser
- [x] Upload a text PDF: suggestions appear with confidence, and the ambiguous issue date is flagged
- [x] Review screen shows suggestion, current value and quote for each field
- [x] Save and verify: seven fields saved, document verified, panel shows who reviewed it

## Phase 4 — Lifecycle automation
- [ ] Expiry service and document statuses
- [ ] Reminder policies, duplicate prevention
- [ ] Outbox, email and in-app notifications, Celery Beat
- [ ] Renewal workflow and version history

## Phase 5 — Business features
- [ ] Compliance calendar
- [ ] Reports with CSV and PDF export
- [ ] Audit log filters by date and user, export
- [ ] Plans, entitlements, usage limits; Razorpay and Stripe adapters

## Phase 6 — Production readiness
- [ ] PostgreSQL Row-Level Security
- [ ] Security review, Content-Security-Policy, breached-password check
- [ ] Performance and tenant-isolation testing on PostgreSQL
- [ ] Browser end-to-end test of the full customer journey
- [ ] Backups, retention and deletion workflows
- [ ] Deployment verified end to end
