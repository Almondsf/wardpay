# Deployment

## Components

| Service | Image | Notes |
|---|---|---|
| `db` | postgres:18 | Data. Back it up (see below). |
| `redis` | redis:7.4 | Celery broker and shared rate-limit counters |
| `migrate` | backend | One-shot: `python -m app.core.migrate` as the schema owner |
| `backend` | backend | API (uvicorn), DML-only database role |
| `worker` | backend | Celery worker |
| `beat` | backend | Celery scheduler (run exactly one) |
| `frontend` | frontend | nginx: serves the SPA and proxies `/api` to `backend` |

## With Docker Compose

```bash
cp .env.example .env          # POSTGRES_PASSWORD, APP_DB_PASSWORD, SECRET_KEY, FIELD_ENCRYPTION_KEYS
docker compose up -d --build
curl http://localhost:8080/api/v1/health
```

Compose binds the web app to `127.0.0.1:8080`. Put a TLS-terminating reverse
proxy (Caddy, nginx, a cloud load balancer) in front of it and set
`FRONTEND_URL` to the public HTTPS URL. With one proxy in front of the
frontend container, `NUM_PROXIES` should be the total count of proxies that
add `X-Forwarded-For` (2 in that case).

## On Render and Netlify (the current hosting)

The live site runs as managed services instead of Compose:

| Piece | Where | Settings |
|---|---|---|
| Database | Render Postgres | Its internal URL is `DATABASE_URL` (the `postgres://` form is accepted) |
| Redis | Render Key Value | Its internal URL is `REDIS_URL` and `RATE_LIMIT_STORAGE_URL` |
| API | Render web service, Docker | Dockerfile `backend/Dockerfile`, build context `backend`, pre-deploy command `python -m app.core.migrate`, health check `/api/v1/health`, `PORT=8002`, `WEB_CONCURRENCY=1` on 512 MB, `FORWARDED_ALLOW_IPS=*` |
| Worker | Render background worker, same image | Command `celery -A app.workers.celery_app worker -B -l info --concurrency 1`. `-B` runs the scheduler too, so run exactly one |
| Web app | Netlify | Base directory `frontend`, build `npm run build`, publish `frontend/dist`, `NODE_VERSION=22`, no `VITE_API_URL` |

The API and worker share one environment (a Render environment group); see
`backend/.env.example` and the Secrets section below.

Netlify forwards `/api/*` to the Render API (`frontend/public/_redirects`), so the
browser sees one address and the SameSite=Strict refresh cookie works. Don't point
the app at the API's own address with `VITE_API_URL`: two different sites can't
share that cookie and everyone would be signed out when their 15-minute access token
ends. `frontend/public/_headers` sets the same security headers as `nginx.conf`.
When the API's address changes, update `_redirects`.

**Database connections.** Each process keeps a pool of up to `DB_POOL_SIZE` + `DB_MAX_OVERFLOW`
(10 + 10) connections. The all-in-one container runs the API and one worker process, so it can use
up to about 40 at peak, well within a small Postgres plan; with separate API and worker services,
add theirs up (and lower the two settings if the database allows few connections).

With Netlify and Render's proxy both in front of the API, `NUM_PROXIES=2`. Check it once:
sign in, then look at the address on that entry in the activity log. It should be yours,
not the same Render address for everyone. `FRONTEND_URL` is the Netlify address (or your
domain), one address only, with no trailing slash; it builds the links in emails and texts.
`CORS_ORIGINS` lists the same address. Never add `localhost` there in production.

Run the CLI commands below from the API service's Shell on Render.

**On Render's free plan** there's no Shell, pre-deploy command or background worker. While
trying it out, set the API's **Docker Command** to

```
sh bin/start-all-in-one.sh
```

`backend/bin/start-all-in-one.sh` runs the migrations, starts the worker with its scheduler
(state in `/tmp`, since the app user can't write its own folders) and then the API on `$PORT`.
Don't paste a long `sh -c "..."` line into that box instead: Render keeps the inner quotes,
and the deploy fails with `sh: 1: python -m app.core.migrate && ...: not found`.

Run CLI commands from a developer machine instead, with `DATABASE_URL` set to the database's
*external* URL for that one terminal (and the live `SECRET_KEY` and `FRONTEND_URL` for commands
that print links). A free service sleeps after 15 idle minutes, so scheduled reminders pause
and gateway webhooks arrive late (the gateway retries); free Postgres expires after 30 days.
Move to paid plans and the separate worker before a school relies on it.

Paste only the address into `REDIS_URL` and `RATE_LIMIT_STORAGE_URL` (`redis://…`, or
`rediss://…` when the provider offers TLS), not the `redis-cli -u …` command some providers
show: the API refuses to start with anything else.

## First platform admin

```bash
docker compose run --rm backend python -m app.cli create-platform-admin --email you@example.com --name "Your Name"
```

It prints a one-time link to set a password (there's nobody to email it from
yet). Send it privately. The admin must turn on two-factor sign-in at first sign-in.

## Email

Set `EMAIL_BACKEND=smtp` with `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`,
`SMTP_PASSWORD` and `EMAIL_FROM`; invites and password resets go out through it.
With `console` they're only logged (subject and recipient domain).

## Platform billing and sign-up

Set `PLATFORM_BANK_NAME`, `PLATFORM_ACCOUNT_NUMBER` and `PLATFORM_ACCOUNT_NAME` to the platform's own bank account: it's printed on every subscription invoice and email. `SUBSCRIPTION_DUE_DAYS` (30) sets the payment terms. Set the plan prices in the console (Plans) before the first billing run; the seeded "Standard" plan has placeholder prices.

Self sign-up is on by default (`SIGNUP_ENABLED`), limited by `SIGNUP_RATE_PER_IP`. Beat must run for unconfirmed sign-ups to be cleaned up (`PENDING_SCHOOL_DAYS`).

## Payment providers

**Paystack is the chosen provider** (owner, 2026-10-20): set `PAYMENT_GATEWAYS=paystack`.
Bachs is built in for later but not configured.

Configure every provider you may use in `PAYMENT_GATEWAYS` (e.g. `paystack,bachs`) with
its keys; which one new checkouts use is switched at runtime by a platform admin
(console: Platform → Providers, or `PUT /api/v1/platform/providers/payment`). With
nothing chosen, the first configured one is used.

- **Paystack:** `PAYSTACK_SECRET_KEY`; webhook URL `https://<api-host>/api/v1/webhooks/paystack`.
  Students' own account numbers need Paystack's "Dedicated NUBAN" enabled on the business
  (ask Paystack support). In test mode a school can be connected with Zenith Bank (057)
  and account number 0000000000.
- **Bachs:** `BACHS_SECRET_KEY` (`sk_sandbox_…` or `sk_live_…`) and `BACHS_WEBHOOK_SECRET`
  (the signing secret of the endpoint `https://<api-host>/api/v1/webhooks/bachs`, created
  in the Bachs developer portal with `event_source` `connect` or `all`, events
  `collection.succeeded`). Production needs a live key. Each school sets itself up with
  "Set up with Bachs" on its Payment settings page.

## Messages to parents

Messages go by **WhatsApp first**, then SMS, then email, whichever works.

**SMS (Termii or Africa's Talking).** Set the keys of either or both:
Termii's `TERMII_API_KEY` and a sender ID registered with Termii
(`TERMII_SENDER_ID`), and/or Africa's Talking's `AFRICASTALKING_USERNAME`,
`AFRICASTALKING_API_KEY` and `AFRICASTALKING_SMS_SENDER` (its registered sender
ID or short code). `SMS_BACKEND` (`termii` or `africastalking`) is the one used
until a platform admin switches in the console (**Platform → Providers → SMS
provider**); only providers with keys are offered, and a switch reaches the worker
within 30 seconds. Parent sign-in codes also need SMS, so the parent portal
doesn't work without one.

**WhatsApp (Meta Cloud API).**
1. In Meta Business Manager, create a WhatsApp Business account and a phone
   number, and a permanent (system-user) access token with
   `whatsapp_business_messaging`.
2. Set `WHATSAPP_BACKEND=cloud`, `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`
   (and `WHATSAPP_LANGUAGE`, default `en`).
3. In WhatsApp Manager, create three **Utility** templates with body
   parameters in exactly this order (names can differ; set them in
   `WHATSAPP_TEMPLATES=receipt=…,fee_notice=…,reminder=…`):

| Template (default name) | Parameters `{{1}}`, `{{2}}`, … |
|---|---|
| `payment_receipt` | parent name, amount, child's name, school name, receipt number, balance, verify link |
| `fee_notice` | parent name, child's name, term, school name, amount, how to pay, the parent's family payment link, what it's for (e.g. ": Tuition ₦130,000, PTA levy ₦5,000") |
| `fee_reminder` | parent name, school name, balance, child's name, how to pay, the parent's family payment link, what it's for |

   For example `payment_receipt`: "Dear {{1}}, we have received {{2}} for {{3}}
   at {{4}}. Receipt no. {{5}}. Outstanding balance: {{6}}. Check this receipt
   at {{7}}". Messages fail over to SMS until the templates are approved.
4. With `WHATSAPP_BACKEND=off`, messages go by SMS and email only.

**Phone calls (Africa's Talking Voice).** Buy a voice number, then set
`VOICE_BACKEND=africastalking`, `AFRICASTALKING_USERNAME`,
`AFRICASTALKING_API_KEY` and `AFRICASTALKING_VOICE_NUMBER`. In the Africa's
Talking dashboard, set the number's callback URL to
`https://<api-host>/api/v1/voice/africastalking`. Each school switches calls on
under Messages; they cost per minute. `CALL_HOURS_START`/`CALL_HOURS_END`
(9, 19) bound when calls are made.

Messages queued longer than `MESSAGE_MAX_AGE_HOURS` (24) are skipped rather
than sent late. Bill notices and reminders are never sent between
`QUIET_HOURS_START` and `QUIET_HOURS_END` (20:00–07:00 Lagos); they wait for
the morning. Beat must be running for automatic reminders.

## Checking a deploy

After every deploy, run the read-only smoke check from a developer machine (it changes nothing):

```bash
cd backend && .venv/Scripts/python -m app.smoke https://wardpay.netlify.app
```

It checks the web app and API answer (waiting up to two minutes for a sleeping free-plan service), the
database and Redis are reachable, the `/api` proxy works, the website prices load, security headers and
the HTTPS redirect are in place, API docs and development-only routes are off, staff pages need sign-in,
forged Paystack webhooks and made-up receipt codes are refused. Add `--api https://<render-host>` to
check the API directly. It exits 1 if anything failed.

## Sizing for big schools

From the load test (`python -m app.loadtest`, see SYSTEM_OVERVIEW section 18): recording a payment is about
100 ms of database work plus the gateway's check (~0.5 s), so throughput is set by worker processes.

| Setup | Payments recorded | 10,000 at once |
|---|---|---|
| Free plan, `--concurrency 1` (today) | ~1.7 a second | ~1.5 hours, and only while awake |
| One worker, `--concurrency 4` (~1 GB RAM) | ~6 a second | ~27 minutes |
| `--concurrency 8` or two workers of 4 | ~12–15 a second | ~12 minutes |

Nothing is lost while the queue drains: notifications are stored first and processed in order. Real schools'
payments spread over days, so for the first pilot schools one paid worker with `--concurrency 4`, a paid
Postgres (not the 30-day free one) and an API instance that doesn't sleep are enough. Billing, the journal and
the end-of-session pack run on the worker too.

## Background workers

Run exactly one `beat` and as many `worker`s as needed. Beat hands stuck outbox
rows to the queue every 30 seconds. It also starts the daily jobs (Lagos time): pending
sign-ups pruned 03:15, NDPA retention 03:45, Paystack payouts fetched and matched
08:20 (`payments.sync_all_settlements`), plus hourly reminders. If Redis is unavailable, emails and other
tasks wait in the `outbox_tasks` table and go out when it's back. Rows that fail
8 times are marked `failed` and logged at ERROR. After fixing the cause, requeue them:

```bash
docker compose run --rm backend python -c "from app.core import outbox; print(outbox.requeue_failed())"
```

## Payment gateways

1. Set `PAYMENT_GATEWAYS` and the keys (`PAYSTACK_SECRET_KEY`;
   `FLUTTERWAVE_SECRET_KEY` and `FLUTTERWAVE_WEBHOOK_HASH`). The app refuses to
   start in production with a gateway enabled but its keys missing, or with
   the fake gateway enabled.
2. In each gateway's dashboard, set the webhook URL:
   - Paystack: `https://<your-domain>/api/v1/webhooks/paystack`
   - Flutterwave: `https://<your-domain>/api/v1/webhooks/flutterwave` (and set the secret hash)
3. Confirm with Paystack that dedicated virtual accounts are enabled for the
   business and which bank to issue them from (`PAYSTACK_PREFERRED_BANK`).
   Test a transfer in the sandbox end to end before onboarding a school
   (PLAN §3.2).
4. Run the worker and beat: webhooks are processed in the background and
   retried every 5 minutes if the gateway is unreachable.

Events that couldn't be matched (`needs_review`) or kept failing (`failed`) are
in the `webhook_events` table for an operator to look at.

## Backups

Run `deploy/backup.sh` daily from the host running Compose (cron or a systemd timer). It dumps the database, encrypts it with [age](https://age-encryption.org) to `BACKUP_AGE_RECIPIENT` (a public key), writes a checksum, copies it off the server with `BACKUP_UPLOAD_CMD` (e.g. rclone to object storage) and keeps `BACKUP_KEEP_DAYS` locally. Keep the age **private** key and the `FIELD_ENCRYPTION_KEYS` off the server and apart from each other: a restore needs both.

```bash
BACKUP_DIR=/var/backups/wardpay BACKUP_AGE_RECIPIENT=age1... \
BACKUP_UPLOAD_CMD='rclone copy "$1" offsite:wardpay-backups/' deploy/backup.sh
```

**Test a restore every month** into an empty database (`deploy/restore.sh` refuses a non-empty one), then start the API against it with production's `FIELD_ENCRYPTION_KEYS` and check `/api/v1/health/ready`, a school's billing summary and a receipt PDF.

## Monitoring

- Health: `/api/v1/health` (liveness) and `/api/v1/health/ready` (database) for the load balancer and an uptime checker.
- Errors: set `SENTRY_DSN` (and `RELEASE` to the deployed commit) for the API and worker; events are scrubbed of personal data before they're sent. `SENTRY_TRACES_SAMPLE_RATE` turns on performance tracing.
- Logs go to stdout as text with secrets redacted; ship them to your log service.
- Watch the outbox: rows marked `failed` are logged at ERROR (see Background workers).

## Data protection

Beat runs the retention job daily (`RECORDS_RETENTION_YEARS`, `MESSAGE_BODY_RETENTION_DAYS`). Set `SUPPORT_EMAIL`: it's shown on the website and privacy notice.

## Database roles

`deploy/postgres-init.sh` runs when the database volume is first created. It
creates `wardpay_app`, which can read and write rows but can't change the
schema. Migrations run as the owner role (`MIGRATION_DATABASE_URL`). On a
managed database, create the same two roles by hand.

## Release order

1. Back up the database.
2. Run migrations (`migrate` service, or `python -m app.core.migrate` as a
   one-off job). Concurrent runs wait on an advisory lock.
3. Roll the API, worker and beat to the new image.

## Secrets

Every backend setting is documented in `backend/.env.example`. Generate:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"                               # SECRET_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # FIELD_ENCRYPTION_KEYS
```

Store `FIELD_ENCRYPTION_KEYS` somewhere other than the database backups: losing
it makes encrypted fields unreadable, and storing it with the backups defeats
the encryption.

## Health checks

- `GET /api/v1/health`: liveness (process up)
- `GET /api/v1/health/ready`: readiness (database and Redis reachable; 503 if not)
