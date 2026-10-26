"""
A load test on a throwaway database: one school with many pupils, billed and paid through the real code.

    DATABASE_URL=postgresql+psycopg://wardpay@localhost:5433/wardpay_load \\
        python -m app.loadtest --pupils 10000

It refuses to run in production or against anything but a local database whose name contains "load".

What it measures:
1. building the school (pupils, parents, account numbers) and billing every pupil with the real generator;
2. taking in one signed payment notification per pupil through the real webhook endpoint (concurrently);
3. turning them into payments the way the background worker does, with one worker and with several,
   with a delay standing in for the gateway's check-back (the fake gateway answers instantly);
4. the busy screens and files at that size: dashboard, debtors, money received, pace and forecast,
   unusual activity, the journal and the end-of-session pack, and the reminder run.

Then it checks the books add up: every notification became a payment, and allocations match payments.
"""

import argparse
import asyncio
import logging
import random
import statistics
import sys
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from app.core.config import get_settings


def _guard() -> None:
    settings = get_settings()
    url = make_url(settings.database_url)
    if settings.is_production or url.host not in ("localhost", "127.0.0.1") or "load" not in (url.database or ""):
        raise SystemExit("The load test only runs locally, against a database whose name contains 'load'.")


class Timer:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str]] = []

    def time(self, label: str, fn: Callable[[], Any], unit: str = "") -> Any:
        start = time.perf_counter()
        result = fn()
        took = time.perf_counter() - start
        self.rows.append((label, f"{took:.2f} s" + (f"  ({unit})" if unit else "")))
        print(f"  {label}: {took:.2f} s", flush=True)
        return result

    def note(self, label: str, value: str) -> None:
        self.rows.append((label, value))
        print(f"  {label}: {value}", flush=True)


def build(pupils: int, timer: Timer) -> dict[str, Any]:
    from app import demo
    from app.core.db import SessionLocal, refresh_statistics
    from app.core.tenancy import scope_platform_wide
    from app.models import load_all_models
    from app.modules.accounts.models import Role, User
    from app.modules.schools.models import School

    load_all_models()
    with SessionLocal(expire_on_commit=False) as session:
        scope_platform_wide(session)
        school = School(id=uuid.uuid4(), name="Load Test College", slug=f"load-{uuid.uuid4().hex[:8]}")
        session.add(school)
        session.flush()
        bursar = User(  # never signs in: it only stands behind the billing
            id=uuid.uuid4(),
            school_id=school.id,
            email=f"bursar@{school.slug}.load.test",
            full_name="Load Test Bursar",
            role=Role.BURSAR,
            password_hash="!",  # noqa: S106  (no password: this account can't sign in)
            mfa_enabled=False,
        )
        session.add(bursar)
        term = demo._calendar(session, school)
        arms = demo._classes(session, school)
        built = timer.time(
            f"Add {pupils:,} pupils and their parents", lambda: demo._students(session, school, term, arms, pupils)
        )
        demo._fees(session, school, term, built.level_ids)
        timer.time(f"Give {pupils:,} pupils an account number", lambda: demo._fake_payments_setup(session, school))
        timer.time("Save it all", session.commit)
        # As a real import does (app.core.db.refresh_statistics): billing usually follows straight away
        refresh_statistics(session, ("students", "enrollments", "guardians", "guardian_students"))
    return {"school": school, "term": term, "bursar": bursar, "students": built.students}


def bill(world: dict[str, Any], timer: Timer) -> int:
    from app.core.db import SessionLocal
    from app.core.http import RequestMeta
    from app.core.tenancy import scope_to_school
    from app.modules.accounts.deps import StaffContext
    from app.modules.fees.invoicing import generate_term_invoices

    school = world["school"]
    with SessionLocal() as session:
        scope_to_school(session, school.id)
        ctx = StaffContext(
            user=world["bursar"], session_id=uuid.uuid4(), meta=RequestMeta(ip="127.0.0.1"), school_id=school.id
        )
        report = timer.time(
            "Generate the term's bills (one click)",
            lambda: generate_term_invoices(
                session, ctx, term_id=world["term"].id, due_date=None, class_level_ids=None, dry_run=False
            ),
        )
        return int(report.invoices)


async def _post_all(bodies: list[tuple[dict[str, str], bytes]], concurrency: int) -> tuple[int, list[float]]:
    from app.main import create_app

    app = create_app()
    accepted, latencies = 0, []
    gate = asyncio.Semaphore(concurrency)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://load.test") as client:

        async def one(headers: dict[str, str], body: bytes) -> None:
            nonlocal accepted
            async with gate:
                start = time.perf_counter()
                response = await client.post("/api/v1/webhooks/fake", headers=headers, content=body)
                latencies.append(time.perf_counter() - start)
                accepted += response.status_code == 200

        await asyncio.gather(*(one(h, b) for h, b in bodies))
    return accepted, latencies


def intake(world: dict[str, Any], payments: int, concurrency: int, timer: Timer) -> None:
    """One transfer per pupil (part or all of what's owed), as signed notifications through the endpoint."""
    from app.core import outbox
    from app.core.db import SessionLocal
    from app.core.ratelimit import get_limiter
    from app.core.tenancy import scope_to_school
    from app.modules.fees.invoicing import account_summaries
    from app.modules.payments.gateways import FakeGateway
    from app.modules.payments.models import VirtualAccount

    school = world["school"]
    with SessionLocal() as session:
        scope_to_school(session, school.id)
        accounts = dict(session.execute(select(VirtualAccount.student_id, VirtualAccount.account_number)).all())
        owed = {sid: s.outstanding_kobo for sid, s in account_summaries(session, list(accounts)).items()}
    rng = random.Random(10_000)  # noqa: S311  (test data)
    fake = FakeGateway.shared()
    chosen = list(accounts.items())[:payments]
    bodies = [
        fake.simulate_transfer(number, max(100_00, owed.get(sid, 0) * rng.choice((25, 50, 100)) // 100))
        for sid, number in chosen
    ]
    # Measured on their own: the worker isn't running, and the per-IP webhook limit is a separate finding
    outbox.dispatch = lambda row_ids: None
    limiter = get_limiter()
    real_hit = limiter.hit
    limiter.hit = lambda *args, **kwargs: None  # type: ignore[method-assign]
    try:
        start = time.perf_counter()
        accepted, latencies = asyncio.run(_post_all(bodies, concurrency))
        took = time.perf_counter() - start
    finally:
        limiter.hit = real_hit  # type: ignore[method-assign]
    timer.note(
        f"Take in {len(bodies):,} payment notifications ({concurrency} at a time)",
        f"{took:.1f} s, {len(bodies) / took:.0f} a second, {accepted:,} accepted, "
        f"median {statistics.median(latencies) * 1000:.0f} ms, slowest {max(latencies) * 1000:.0f} ms",
    )


def process(timer: Timer, *, sample_delay: float, samples: tuple[tuple[int, int], ...]) -> None:
    """
    Turn the stored notifications into payments as the worker does. `samples`: (workers, how many) with the
    gateway's check-back delay; whatever is left is then processed quickly to finish (no delay, 8 at a time).
    """
    from app.core.db import SessionLocal
    from app.core.tenancy import scope_platform_wide
    from app.modules.payments import webhooks
    from app.modules.payments.gateways import FakeGateway
    from app.modules.payments.models import WebhookEvent, WebhookStatus

    with SessionLocal() as session:
        scope_platform_wide(session)
        pending = list(
            session.scalars(
                select(WebhookEvent.id)
                .where(WebhookEvent.status == WebhookStatus.RECEIVED)
                .order_by(WebhookEvent.received_at)
            )
        )
    fake = FakeGateway.shared()
    real_verify = fake.verify
    delay = {"seconds": sample_delay}

    def slow_verify(*args: Any, **kwargs: Any) -> Any:
        if delay["seconds"]:
            time.sleep(delay["seconds"])
        return real_verify(*args, **kwargs)

    fake.verify = slow_verify  # type: ignore[method-assign]
    done = 0
    for workers, count in samples:
        batch = pending[done : done + count]
        done += len(batch)
        if not batch:
            continue
        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(webhooks.process_event, batch))
        took = time.perf_counter() - start
        rate = len(batch) / took
        timer.note(
            f"Record payments, {workers} worker{'s' if workers > 1 else ''}, {sample_delay:.1f} s gateway check",
            f"{rate:.1f} a second: 10,000 would take {10_000 / rate / 60:.0f} min",
        )
    rest = pending[done:]
    delay["seconds"] = 0
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(webhooks.process_event, rest))
    took = time.perf_counter() - start
    if rest:
        timer.note(
            "Record payments, database only (8 workers, no gateway wait)",
            f"{len(rest) / took:.1f} a second ({len(rest):,} in {took:.0f} s)",
        )


def screens(world: dict[str, Any], timer: Timer) -> None:
    from app.core.clock import lagos_today
    from app.core.db import SessionLocal
    from app.core.pagination import PageParams
    from app.core.tenancy import scope_to_school
    from app.modules.accounting import exports, journal, session_pack
    from app.modules.reports import insights, signals
    from app.modules.reports.service import DebtorFilters, collections, debtors, overview

    school = world["school"]
    today = lagos_today()
    with SessionLocal() as session:
        scope_to_school(session, school.id)
        timer.time("Dashboard (term figures)", lambda: overview(session, None))
        timer.time("Debtors, first page", lambda: debtors(session, DebtorFilters(), PageParams(limit=50, offset=0)))
        timer.time("Money received, 30 days", lambda: collections(session, today - timedelta(days=29), today))
        timer.time("Pace and forecast", lambda: insights.insights(session, today))
        timer.time("Unusual activity, 30 days", lambda: signals.signals(session, school.id, 30))
        j = timer.time("Journal for the term (data)", lambda: journal.journal(session, world["term"].start_date, today))
        timer.time(
            f"Journal as Excel ({len(j.entries):,} entries, built in the background)",
            lambda: exports.journal_xlsx("Load", j),
        )
        pack = timer.time("End-of-session pack (data)", lambda: session_pack.session_pack(session, None))
        timer.time(
            "End-of-session pack as Excel (built in the background)", lambda: exports.session_pack_xlsx("Load", pack)
        )


def reminders(timer: Timer) -> None:
    from app.core import outbox
    from app.modules.notifications.service import schedule_reminders

    outbox.dispatch = lambda row_ids: None
    queued = timer.time(
        "Hourly reminder run (who to remind, queued)", lambda: schedule_reminders(datetime.now(UTC).replace(hour=9))
    )
    timer.note("Reminders queued", f"{queued:,}")


def check(world: dict[str, Any], timer: Timer) -> bool:
    from app.core.db import SessionLocal
    from app.core.tenancy import scope_platform_wide
    from app.modules.payments.models import Allocation, Payment, WebhookEvent, WebhookStatus
    from app.modules.receipts.models import Receipt

    with SessionLocal() as session:
        scope_platform_wide(session)
        statuses = dict(session.execute(select(WebhookEvent.status, func.count()).group_by(WebhookEvent.status)).all())
        paid = session.scalar(select(func.coalesce(func.sum(Payment.amount_kobo), 0))) or 0
        allocated = session.scalar(select(func.coalesce(func.sum(Allocation.amount_kobo), 0))) or 0
        payments = session.scalar(select(func.count()).select_from(Payment)) or 0
        receipts = session.scalar(select(func.count()).select_from(Receipt)) or 0
        gaps = session.scalar(select(func.max(Receipt.number) - func.count()).select_from(Receipt)) or 0
    timer.note("Notifications by outcome", ", ".join(f"{k.value}: {v:,}" for k, v in statuses.items()))
    timer.note("Payments and receipts", f"{payments:,} payments, {receipts:,} receipts, receipt-number gaps: {gaps}")
    timer.note("Money in vs applied to bills + credit", f"₦{paid / 100:,.0f} received, ₦{allocated / 100:,.0f} applied")
    return statuses.get(WebhookStatus.PROCESSED, 0) == payments and payments == receipts and gaps == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load test one school on a throwaway database")
    parser.add_argument("--pupils", type=int, default=10_000)
    parser.add_argument("--concurrency", type=int, default=20, help="Notifications sent at once")
    parser.add_argument("--gateway-delay", type=float, default=0.5, help="Seconds the gateway takes to answer a check")
    args = parser.parse_args(argv)
    _guard()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    timer = Timer()
    print(f"Building a school with {args.pupils:,} pupils…", flush=True)
    world = build(args.pupils, timer)
    print("Billing…", flush=True)
    timer.note("Bills made", f"{bill(world, timer):,}")
    print("Payments arriving…", flush=True)
    intake(world, args.pupils, args.concurrency, timer)
    print("Recording payments…", flush=True)
    process(timer, sample_delay=args.gateway_delay, samples=((1, 60), (4, 240), (16, 800)))
    print("Screens and files…", flush=True)
    screens(world, timer)
    reminders(timer)
    ok = check(world, timer)
    print("\nRESULTS")
    for label, value in timer.rows:
        print(f"  {label:<70} {value}")
    print("\nBooks add up" if ok else "\nSOMETHING DOESN'T ADD UP")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
