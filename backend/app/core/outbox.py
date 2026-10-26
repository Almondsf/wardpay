"""
Transactional outbox for background tasks.

Calling Celery directly after a commit loses the task if Redis is briefly
down, or runs it for a transaction that later rolled back if called before
the commit. Instead:

    outbox.enqueue(session, "email.send", {"to": ..., "template": ...})
    session.commit()   # the business rows and the outbox row commit together

- After the commit, the task is handed to Celery straight away (task id = the
  outbox row id) and the row is marked dispatched.
- If that hand-off fails, the row stays pending and the `outbox.sweep` beat
  task retries it: rows are claimed with SELECT ... FOR UPDATE SKIP LOCKED,
  backoff doubles per attempt, and rows that keep failing are marked failed
  (dead-lettered) and logged at ERROR.

Delivery to Celery is at-least-once, so tasks must be idempotent. Payloads
hold IDs and non-secret values only; they're stored in plain JSON.
"""

import enum
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, event, func, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, SessionTransaction, mapped_column

from app.core.base import Base, UUIDPrimaryKey, enum_type

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
BASE_BACKOFF_SECONDS = 15
MAX_BACKOFF_SECONDS = 3600
SWEEP_BATCH = 100
_PENDING_KEY = "outbox_pending"


class OutboxStatus(enum.StrEnum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    FAILED = "failed"


class OutboxTask(UUIDPrimaryKey, Base):
    __tablename__ = "outbox_tasks"
    __table_args__ = (Index("ix_outbox_tasks_due", "next_attempt_at", postgresql_where="status = 'pending'"),)

    task_name: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[OutboxStatus] = mapped_column(enum_type(OutboxStatus, "outbox_status"), default=OutboxStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_error: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def enqueue(
    session: Session, task_name: str, payload: dict[str, Any], *, not_before: datetime | None = None
) -> OutboxTask:
    """
    Add a task to the current transaction. It's dispatched only if the transaction
    commits, and not before `not_before` (the sweeper hands it over when it's due).
    """
    row = OutboxTask(id=uuid.uuid4(), task_name=task_name, payload=payload)
    if not_before is not None:
        row.next_attempt_at = not_before
    session.add(row)
    if not_before is None or not_before <= datetime.now(UTC):
        session.info.setdefault(_PENDING_KEY, []).append(row.id)
    return row


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(BASE_BACKOFF_SECONDS * 2 ** max(attempts - 1, 0), MAX_BACKOFF_SECONDS))


def _send(task_name: str, payload: dict[str, Any], task_id: uuid.UUID) -> None:
    # Imported here: the registry imports domain modules, which import this one
    from app.workers import tasks  # noqa: F401  (registers every task in API processes too)
    from app.workers.celery_app import celery_app

    # apply_async (unlike send_task) honours task_always_eager in development and tests
    celery_app.tasks[task_name].apply_async(kwargs=payload, task_id=str(task_id))


def _claim(session: Session, *conditions: Any) -> list[OutboxTask]:
    """Lock up to SWEEP_BATCH pending rows in one query; rows another worker holds are skipped."""
    return list(
        session.scalars(
            select(OutboxTask)
            .where(OutboxTask.status == OutboxStatus.PENDING, *conditions)
            .order_by(OutboxTask.created_at)
            .limit(SWEEP_BATCH)
            .with_for_update(skip_locked=True)
        )
    )


def _hand_off(rows: list[OutboxTask]) -> int:
    """Send each claimed row to Celery, updating the rows in memory only (the caller commits once)."""
    handled = 0
    now = datetime.now(UTC)
    for row in rows:
        row.attempts += 1
        try:
            _send(row.task_name, row.payload, row.id)
        except Exception as exc:
            row.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            if row.attempts >= MAX_ATTEMPTS:
                row.status = OutboxStatus.FAILED
                logger.error("Outbox task %s (%s) failed permanently", row.id, row.task_name)
            else:
                row.next_attempt_at = now + _backoff(row.attempts)
                logger.warning("Outbox task %s (%s) will be retried: %s", row.id, row.task_name, type(exc).__name__)
            continue
        row.status = OutboxStatus.DISPATCHED
        row.dispatched_at = now
        handled += 1
    return handled


def dispatch(row_ids: list[uuid.UUID]) -> None:
    """Hand just-committed rows to Celery now. Anything not handed off stays pending for the sweeper."""
    from app.core.db import SessionLocal

    with SessionLocal() as session:
        _hand_off(_claim(session, OutboxTask.id.in_(row_ids), OutboxTask.next_attempt_at <= func.now()))
        session.commit()


def sweep() -> int:
    """Dispatch pending rows that are due. Safe to run from several workers at once."""
    from app.core.db import SessionLocal

    with SessionLocal() as session:
        handled = _hand_off(_claim(session, OutboxTask.next_attempt_at <= func.now()))
        session.commit()
    return handled


def requeue_failed(task_ids: list[uuid.UUID] | None = None) -> int:
    """Operator tool: put dead-lettered rows back in the queue after fixing the cause."""
    from app.core.db import SessionLocal

    with SessionLocal() as session:
        stmt = update(OutboxTask).where(OutboxTask.status == OutboxStatus.FAILED)
        if task_ids:
            stmt = stmt.where(OutboxTask.id.in_(task_ids))
        result = session.execute(stmt.values(status=OutboxStatus.PENDING, attempts=0, next_attempt_at=func.now()))
        session.commit()
        return int(result.rowcount)  # type: ignore[attr-defined]


_COMMITTED_KEY = "outbox_committed"


@event.listens_for(Session, "after_commit")
def _committed(session: Session) -> None:
    # Not dispatched here: the session still holds its connection during after_commit, and dispatching
    # needs a second one. Under a burst (a load test with 20 notifications at once) every request held one
    # connection and waited for another, and the pool ran dry. So wait until the connection is released.
    committed: list[uuid.UUID] = session.info.pop(_PENDING_KEY, [])
    if committed:
        session.info.setdefault(_COMMITTED_KEY, []).extend(committed)


@event.listens_for(Session, "after_transaction_end")
def _dispatch_after_release(session: Session, transaction: SessionTransaction) -> None:
    if transaction.parent is None:  # the outermost transaction: its connection is back in the pool
        committed: list[uuid.UUID] = session.info.pop(_COMMITTED_KEY, [])
        if committed:
            dispatch(committed)


@event.listens_for(Session, "after_rollback")
def _forget_after_rollback(session: Session) -> None:
    session.info.pop(_PENDING_KEY, None)
