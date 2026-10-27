# WardPay


School fee collection and parent communication for Nigerian private schools.
Each student gets their own bank account number. When a parent transfers
money, the payment is matched to the right child automatically, a receipt
goes to the parent on WhatsApp, and the bursar sees an up-to-date debtors
list. Nobody has to match bank alerts by hand.

Inspired by ParentPay (UK) and ParentSquare / ClassDojo (US), and built
around how Nigerian schools actually collect fees: bank transfers, terms,
arrears, and parents with several children in the same school.

> Status: in development. See [docs/SYSTEM_OVERVIEW.md](docs/SYSTEM_OVERVIEW.md).

## Stack

- **Backend (`backend/`):** FastAPI, SQLAlchemy 2 + Alembic, PostgreSQL, Celery + Redis
- **Frontend (`frontend/`):** React 18 + TypeScript + Vite (staff console, parent portal, public receipt check)
- **Payments:** Paystack and Flutterwave behind one gateway interface
- **Messaging:** WhatsApp Cloud API, SMS fallback (e.g. Termii), email
- **Deploy:** Docker, GitHub Actions CI

The two apps talk only over the HTTP API. The frontend's API types are
generated from the backend's OpenAPI schema, so they can't drift apart.

## Getting started (development)

You need Python 3.12, [uv](https://docs.astral.sh/uv/), Node 22+ and PostgreSQL.
Redis is optional in development (`CELERY_TASK_ALWAYS_EAGER=true` runs background
tasks in-process).

### Backend (port 8002)

```bash
cd backend
uv sync --locked                       # creates .venv from uv.lock
cp .env.example .env                   # then set SECRET_KEY, DATABASE_URL, FIELD_ENCRYPTION_KEYS
uv run python -m app.core.migrate      # apply migrations (advisory-locked)
uv run uvicorn app.main:app --port 8002 --reload
```

API docs: http://localhost:8002/api/docs (not served in production).

Demo data and the first admin:

```bash
uv run python -m app.cli seed-demo        # "Demo Model College" mid-term: 240 billed students, bills, payments, receipts, messages, spending, cash book, plans, write-offs, payouts, a second branch and its group; password demo-password-123
uv run python -m app.cli seed-demo --bare # the small version: 60 students, fees set, no bills yet (what the browser tests use)
uv run python -m app.cli create-platform-admin --email you@example.com --name "Your Name"   # prints a set-password link
```

Demo accounts: `proprietor@`, `bursar@`, `admin@` and `platform@demo.wardpay.ng`; head office `owner@` and `centralbursar@demo.wardpay.ng`; the Lekki branch `proprietor@` and `bursar@lekki.demo.wardpay.ng`.
The proprietor, bursar and platform admin use two-factor sign-in with the
secret `JBSWY3DPEHPK3PXP` (add it to an authenticator app).

Background worker and scheduler (when not running tasks eagerly):

```bash
uv run celery -A app.workers.celery_app worker -l info
uv run celery -A app.workers.celery_app beat -l info
```

### Frontend (port 5175)

```bash
cd frontend
npm ci
npm run dev          # http://localhost:5175, proxies /api to :8002
```

### Tests and checks

```bash
# Backend: needs a local Postgres database whose name ends in _test
cd backend
export TEST_DATABASE_URL=postgresql+psycopg://USER@localhost:5432/wardpay_test
uv run pytest tests/test_platform.py      # run targeted files; the full suite is slow on Windows
uv run ruff check app tests alembic && uv run ruff format --check app tests alembic
uv run mypy app

# Frontend
cd frontend
npm run typecheck && npm test
npm run e2e          # browser tests: own API (8102) + Vite (5185) on a throwaway DB
                     # (E2E_DATABASE_URL, default wardpay_e2e). On Windows: PW_CHANNEL=msedge npm run e2e
```

The test suite refuses any database that isn't local or whose name doesn't end
in `_test`. Each run migrates a fresh, randomly named schema and drops it afterwards.

### After changing the API

```bash
cd backend && uv run python -m app.openapi > ../frontend/openapi.json
cd ../frontend && npm run gen:api       # regenerates src/api/schema.d.ts
```

CI fails if either file is stale. Each workflow only runs when its own paths change; both can also be started by hand
from the Actions tab (or `gh workflow run Backend --ref <branch>`).

### Dependencies

Backend dependencies are pinned in `pyproject.toml` and locked with hashes in
`uv.lock`. `requirements.lock` (hashed, runtime only) is exported from it:

```bash
uv lock --upgrade-package NAME && uv sync --locked
uv export --locked --no-dev --no-emit-project --format requirements-txt -o requirements.lock
uv run pip-audit -r requirements.lock --require-hashes --disable-pip
```

## Running the full stack with Docker

```bash
cp .env.example .env     # fill in the secrets
docker compose up -d --build
```

The web app and API are at http://localhost:8080 (`/api/v1/health`). See
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Documentation

| Doc | For |
|---|---|
| [docs/SYSTEM_OVERVIEW.md](docs/SYSTEM_OVERVIEW.md) | What the system does and how it's built |
| [docs/USER_STORIES.md](docs/USER_STORIES.md) | What each kind of user needs |
| [docs/SECURITY.md](docs/SECURITY.md) | Security controls |
| [docs/PRIVACY.md](docs/PRIVACY.md) | Children's data and the NDPA |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Running it in production (backups, monitoring) |
| [docs/PENTEST_CHECKLIST.md](docs/PENTEST_CHECKLIST.md) | What a penetration tester should try |
| [OWNER_TODO.md](OWNER_TODO.md) | What's waiting on the owner (accounts, decisions, paperwork) |
