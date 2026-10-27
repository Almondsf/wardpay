# Security controls

This page lists the controls in place and what's still to do before go-live.
It is updated with every change that affects security.

## Identity and access
- No public sign-up. Platform admins create schools and their first proprietor;
  proprietors invite staff by email. People choose their own passwords
  (argon2-hashed; 10+ characters, not common, not their name or email).
- Role-based **capabilities**: every endpoint checks a capability server-side,
  never a role name. Nobody can change their own role or deactivate themselves,
  a school always keeps an active proprietor, and only the three school roles
  can be assigned (no escalation to platform admin).
- **Two-factor sign-in (TOTP)** is required for proprietors, bursars, school admins
  and platform admins (`ENFORCE_MFA`): everyone who can move money or change access. Codes can't be replayed (the last used time step is
  stored and updated atomically). Recovery codes are single-use and stored
  hashed. The TOTP secret is encrypted at rest. Enrolling needs the password;
  turning MFA off needs the password and a code.
- **Sessions are server-side.** Access tokens (15 min) carry a session id that
  is checked on every request, so sign-out, deactivation, role change,
  password change/reset, MFA reset and school suspension take effect
  immediately. Refresh tokens are httpOnly, `Secure`, `SameSite=Strict` cookies
  scoped to `/api/v1/auth`. They rotate on every use, and a reused (stolen)
  refresh token revokes its session.
- Tokens are typed (`access`, `mfa`, `password`), so an MFA ticket or reset link
  can't be used as an access token. Reset/invite links are bound to the current
  password hash, so they work once. The token sits in the URL fragment, so it
  never reaches server logs.
- Failed sign-ins look identical whether or not the email exists, and take
  the same time (a dummy hash is checked). "Forgot password" always answers 202.
- **Rate limits**: sign-in per IP and per account, the MFA step per ticket,
  password reset per IP and per email, refresh per IP, MFA changes and invites
  per user/school. Limits use shared Redis storage in production.

## Parents
- Phone sign-in codes: 6 digits from a CSPRNG, stored only as a keyed HMAC, valid
  for 5 minutes, 5 attempts, replaced by any newer code, and rate limited per
  phone and per IP. Requesting a code answers the same for unknown numbers. The
  code travels to the SMS task encrypted and never appears in logs.
- Parent sessions are server-side and bound to one guardian in one school.
  Tokens have their own type, rotating refresh cookies detect replay, and
  suspended schools lock parents out.
- Parents only ever see children linked to them: every portal lookup goes
  through the guardian's links (tested for a non-linked child in the same school
  and for children at other schools).

## Tenant isolation
- Every school-owned table has a non-null `school_id`. Scoping is applied
  automatically to every ORM query in a school-scoped session, and unscoped
  sessions that touch tenant data fail closed (`app/core/tenancy.py`).
- `school_id` on new rows comes from the session's scope, never from request
  bodies, and can't be changed afterwards.
- Primary keys are random UUIDs, so IDs in URLs can't be guessed or enumerated.
- Staff users aren't a tenant table (sign-in finds them before the school is
  known), so the staff service pins `school_id` explicitly. Tests prove a school
  can't list, view, edit, deactivate or reset another school's staff by ID.
- Platform admins have no school capabilities, and school staff have no platform ones.
- **School groups** (approved by the owner 2026-10-27): head-office staff (`users.group_id`, proprietor or
  bursar only, enforced by a check constraint) act in one branch at a time, the one their session chose.
  Every request re-checks that the branch is still in their group and active; otherwise the session has no
  tenant scope and school endpoints answer 403 `branch_required`. Switching to anything outside the group
  gets one answer (400) whether the school exists or not. Branch staff can't switch. Only platform admins
  create groups, add or remove branches and invite head-office staff; all of it is audited, and taking a
  branch out or deactivating someone takes effect on their next request. Tests: `tests/test_school_groups.py`.
- **All branches** (`GET /reports/group`) is the only endpoint where a school user reads several schools. Its
  dependency (`require_head_office`) refuses anyone without `group_id`, so branch staff and platform admins get
  403; the session is platform-wide there, so every query filters to the group's active branches by id, and
  it returns figures only (no names, phones or payment details). The Excel download is audited and rate
  limited. Tests: `tests/test_group_figures.py`.
- IDs in request bodies (arm, term, guardian) are loaded through the same scoped
  session, so another school's IDs are simply "not found". Tests cover each one.

## Platform
- Safe defaults: `ENVIRONMENT=production`, `DEBUG=false`, empty CORS allowlist.
  In production the app refuses to start with debug on, eager background tasks,
  wildcard CORS or per-process (in-memory) rate limits.
- Security headers on every API response: `nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`, a deny-all CSP, `Cache-Control: no-store`,
  and HSTS in production. nginx adds a CSP for the web app.
- Request bodies over `MAX_REQUEST_BODY_BYTES` (10 MB by default) get a 413,
  including chunked uploads without a Content-Length.
- Client IPs come from `X-Forwarded-For` only for the `NUM_PROXIES` hops we run
  behind, so they can't be spoofed.
- Errors never echo submitted values or internals. Unexpected exceptions return
  a generic 500.
- API docs (`/api/docs`) are not served in production.

## Data
- Sensitive fields are encrypted at rest with Fernet (`FIELD_ENCRYPTION_KEYS`,
  rotatable by prepending a new key). Lookups on encrypted values use a keyed
  HMAC **blind index** derived from `SECRET_KEY`. Rotating `SECRET_KEY` means
  recomputing blind indexes as well as signing everyone out.
- Logs pass through a redaction filter that masks passwords, OTPs, tokens,
  BVNs and account/card numbers. One list of sensitive keys
  (`app/core/redaction.py`) covers logs, audit entries and stored gateway payloads.
- In production a failed database statement is logged without its values
  (SQLAlchemy `hide_parameters`), so an error can't put a parent's email or phone
  in the logs. Development keeps them for debugging.
- A rejected setting at startup is reported by name only, never its value
  (`hide_input_in_errors`): database and Redis addresses carry passwords.

## Money
- All amounts are integer kobo in the database and API; the frontend parses
  naira text without floating point.
- Invoice lines are append-only (database trigger) and an invoice can't total
  below zero (deferred constraint trigger). Corrections are new lines with a
  reason, and generation, adjustments and voids are audited.
- Balances are computed from the ledger, never stored, so they can't be edited
  or drift.
- Marking which fees a payment was for (at payment time or afterwards) is append-only:
  a payment can be marked once and never re-marked, needs `payments.record`, is audited,
  and only changes which fees show as paid, never amounts or balances.
- Invoice numbers are gapless per school (row-locked counter).

## Payments
- The platform never holds school money: payments settle to each school's own
  account via a gateway subaccount, and the platform fee is taken by the split.
- Card data never touches the system (hosted checkout). Stored gateway payloads
  are redacted, and outbound gateway calls are logged without keys, card
  authorisations or account numbers. SMS providers' replies are logged without
  the recipients' phone numbers (Africa's Talking's `number` is redacted).
- Switching the payment or SMS provider is for platform admins only, offers only
  providers with keys set, and is audited (`platform.provider_changed`).
- Webhooks are accepted only with a valid signature (Paystack HMAC-SHA512 over
  the raw body, Flutterwave secret hash, compared in constant time). Even then
  the outcome is **re-verified with the gateway API** before anything is
  recorded. Webhook processing is idempotent (unique event key; unique gateway
  reference per payment). Forged webhooks get 401 and are not stored.
- The checkout redirect URL is fixed server-side; `/payments/verify` takes only a
  reference of our own format, is rate limited, and reads the outcome from the gateway.
- Cash, teller and POS payments need **maker-checker** approval: the approver
  must be a different person (service rule and database constraint).
- **Expenses:** recorded by one person and approved by another (constraint), never deleted
  (trigger; a mistake is voided with a reason), and cash spent comes off what should be in the
  drawer. Audited (`expenses.recorded`, `.approved`, `.rejected`, `.voided`).
- **Cash book:** banking recorded by one person is checked against the bank statement by
  another (constraint), banking entries can't be deleted and day closes can't be changed
  (triggers), and cash recorded for a closed day afterwards is flagged. Every step is audited.
- Payments and allocations are append-only. Reversals are new negative entries,
  and only bursars and proprietors can make them. Database triggers stop a
  payment being over-allocated or an invoice overpaid.
- Changing where a school's money settles is proprietor-only, confirmed against
  the bank, audited, and blocked once students have account numbers (support
  must handle it).
- The fake gateway and its development endpoint can't be enabled in production.
- **Switching the payment provider** is platform-admin only (two-factor), audited, and
  limited to providers configured with keys on the server. Bachs webhooks are verified
  (HMAC-SHA256 over `timestamp.body`, constant-time, 5-minute window, rotation-aware
  `X-Bachs-Signature-V2`), and every Bachs payment is re-read from its API; a payment
  id that answers with another checkout's reference is parked for review.
- **Pay cards** carry a child's name, class, account number and what's owed, on paper that
  goes home with the child; no payment link or code. Printing is limited to
  `payments.record`, rate limited and recorded in the activity log.
- **Family payment links** are bearer tokens (192 bits). They're stored as a keyed
  hash (lookup) and encrypted (to resend), expire after `FAMILY_LINK_DAYS`, can be
  replaced by staff (audited) and end when the parent is erased or the school is
  suspended. The page shows first names, classes, amounts and account numbers only;
  unknown, expired and replaced links get the same 404; lookups are rate limited per
  IP and checkouts per link. Card shares are checked server-side (only this parent's
  current children, at most what each owes) and a gateway total that doesn't match
  is parked for review. Responses are `no-store` and `no-referrer`.

## Receipts
- Receipts are numbered per school with no gaps, issued in the payment's own
  transaction, and immutable (database trigger). A reversed payment shows its
  receipt as reversed rather than changing it.
- The public verification page is reached through a random 128-bit code, not
  the receipt number, so receipts can't be enumerated. It's rate limited and
  shows no personal data beyond the student's initials.

## Input handling
- Spreadsheet imports accept only `.xlsx` and `.csv`, at most 5,000 rows, within
  the request body limit, and are rate limited per school. Workbooks are read
  values-only (no formulas or macros run). Every cell is validated before
  anything is saved.

## Audit
- Sign-ins (including failures), session reuse, password/MFA changes, staff and
  school changes are recorded with actor, IP, user agent and before/after values.
  The audit entry commits in the same transaction as the change.
- The audit tables are append-only **in the database**: triggers refuse UPDATE,
  DELETE and TRUNCATE.

## Database
- Migrations run as the schema owner (`MIGRATION_DATABASE_URL`) under an
  advisory lock. The API and workers connect as a DML-only role
  (`wardpay_app`, see `deploy/postgres-init.sh`), so a compromised API can't
  alter the schema.

## Supply chain
- Python dependencies are locked with hashes (`uv.lock`, `requirements.lock`);
  images install only the lock. CI fails if the lock is stale and runs
  `pip-audit`; the frontend runs `npm audit`.
- Images run as a non-root user (UID 10001) with a read-only filesystem in Compose.

## Messages to parents
- Message sending is idempotent: each message has a per-school dedupe key, so a
  retried task or a repeated trigger can't message a parent twice.
- Spam limits are enforced on the server: quiet hours, a school-set cap per
  bill (at most 10) with a minimum gap, one manual reminder per student per
  day, and a per-school rate limit on manual reminders.
- Opt-outs and "is it still owed?" are re-checked at sending time, not just
  when the message is queued.
- School wording can only use a fixed list of placeholders (checked on save),
  so a template can't reach other data. WhatsApp only sends Meta-approved
  templates.
- Phone calls: the provider's callback (`POST /voice/africastalking`) only answers
  for a call we placed (looked up by the provider's session id) and only when the
  number dialled is that parent's; anything else gets `<Reject/>` and no details.
  It is rate limited per IP. Calls are capped per bill, made only 9:00 to 19:00,
  and switched on per school.
- Console backends log masked numbers or email domains, never message text.
  The message log is visible only to staff with `messaging.manage` in that
  school.

## Sign-up and the platform console
- Sign-up gives the same answer whether or not an email is registered (the owner gets an "already have an account" email instead), is rate limited per IP, needs the terms accepted, and has a honeypot field. The school stays pending, and nobody can sign in to it, until the proprietor proves the email address by setting a password. `SIGNUP_ENABLED=false` closes it.
- Platform endpoints need a platform admin (who must use two-factor sign-in). Support lookups show students as initials only, are rate limited, and every search is recorded in the platform audit trail. Plan changes, billing runs, payments and voids are audited too.
- Helping locked-out staff (owner, 2026-10-22): a platform admin can email a school staff member their invite again or a password-reset link (to the staff member's own address, rate limited; the admin never sees a password), and turn off a staff member's two-factor sign-in after a lost phone, which signs them out everywhere; roles that need two-factor must enrol again at their next sign-in. Only active staff of the school in the URL; both recorded in the school's activity log and the platform's trail. Platform admins can't change a staff member's email or password themselves.
- Subscription invoice lines and payments are append-only (database trigger); a payment can't exceed what's owed; a live bill per school per term is enforced by a unique index. A discount is a new negative line (only `discount` lines may be negative, by constraint), never more than what's still owed, platform admins only, audited.
- How a family's one amount is shared between children is the school's money rule: only the proprietor (`payments.setup`) changes it, every change is audited, and the parent sees the split before paying. A share never exceeds what a child owes.
- Card payment of a subscription bill: only staff with `payments.record`, only their own school's bill (scoped session), rate limited; the money settles to the platform's own balance (no school subaccount); the result comes from the gateway's verify call, never the browser; a card payment is unique on its gateway reference and a payment that doesn't fit the bill is parked for review, never forced onto it.

## Unusual activity
- `GET /reports/unusual-activity` needs `staff.manage` (proprietor only): it names the staff who recorded
  reversals, late cash, bill reductions and the like. Names come only from the school's own staff.

## Writing off debt
- Anyone handling money can ask; only the proprietor (`debts.write_off`) approves, so a bursar and a school
  admin together can't clear a debt they took cash for. Requests, approvals and rejections are audited;
  requests are never deleted, and the write-off itself is an append-only bill line.

## Closing the books
- A closed term can't take anything dated in it (payments by hand, spending, banking, drawer counts, bills,
  voids, its dates), enforced in the services under a row lock on the school. Only the proprietor
  (`books.reopen`) can re-open, with a reason; closings and re-openings are append-only and audited.

## Payment notifications
- Webhooks are limited to 3,000 a minute per address (raised from 600 after the load test: Paystack sends a
  busy morning's transfers from a few addresses). Each is signature-checked and re-verified with the gateway;
  the limit only stops floods.

## Reports and downloads
- Background-built files (journal, end-of-session pack) can be fetched only by the staff member who asked
  (anyone else gets 404), expire after a day and are deleted daily.
- Reports need `reports.view` (proprietor, bursar and school admin, all on two-factor
  sign-in).
- Every download is recorded in the activity log (who, which report,
  format, row count, scope; never the contents), rate limited per school
  (60 an hour), capped at 5,000 rows, and sent `Cache-Control: no-store`.
- Spreadsheet formula injection: text starting with `= + - @` (e.g. a parent
  name from an import) is written to Excel with a leading apostrophe, so it's
  shown as text and never run.
- Report queries are scoped to the school like every other query; isolation
  is tested, including another school's term and class IDs.

## Background tasks
- Tasks go through a transactional outbox: a rolled-back request never sends
  its email, and a broker outage delays tasks instead of losing them.
  Outbox payloads hold IDs only; links and tokens are generated at send time.

## Operations
- Development helpers (`/dev/fake-gateway`, `/dev/sms`) aren't mounted in production.
- Error monitoring (optional Sentry) scrubs every event: no bodies, cookies, headers, query strings or user details, and strings are redacted like the logs.
- Backups (`deploy/backup.sh`) are encrypted with `age` before they touch the disk, checksummed, and copied off the server; the private key lives elsewhere. `deploy/restore.sh` refuses a non-empty database.

## Before go-live
- Independent penetration test of the deployed system, following [PENTEST_CHECKLIST.md](PENTEST_CHECKLIST.md).
- Back up the encryption keys separately from database backups, and test a restore.
- Create the first platform admin with `python -m app.cli create-platform-admin`
  and deliver the printed link privately. Never run `seed-demo` against
  production (it refuses to).
