"""
Engine, session factory and the request session dependency.
Services own transactions: they call `session.commit()` themselves.
"""

import logging
from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.core.base import Base
from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

__all__ = ["Base", "SessionLocal", "engine", "get_db"]


def make_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        # A failed statement's error carries its bound values (emails, phone numbers). Keep them out of
        # production logs and error reports; development keeps them for debugging.
        hide_parameters=settings.is_production,
    )


engine = make_engine(get_settings())
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def get_db() -> Iterator[Session]:
    """
    One session per request. It starts unscoped: the auth dependencies scope it
    to the caller's school (or mark it platform-wide) before any tenant data
    can be read. See app.core.tenancy.
    """
    with SessionLocal() as session:
        yield session


BULK_ROWS = 200  # after adding this many rows at once, the planner's statistics are worth refreshing
_ANALYZABLE = frozenset(
    {"students", "enrollments", "guardians", "guardian_students", "invoices", "invoice_lines", "allocations"}
)


def refresh_statistics(session: Session, tables: tuple[str, ...]) -> None:
    """
    ANALYZE tables just filled in bulk. Postgres refreshes its row counts by itself about a minute later; in
    between, queries on those tables can get a bad plan (load test, 2026-10-25: a 3,000-pupil school's billing
    queries took 3 to 4 s each straight after its import, 27 ms once analysed). Skipped quietly where the
    database user isn't allowed to (a runtime role without ownership).
    """
    names = [t for t in tables if t in _ANALYZABLE]  # fixed names only: they're put into the SQL
    if not names:
        return
    try:
        session.execute(text(f"ANALYZE {', '.join(names)}"))
        session.commit()
    except DBAPIError:
        session.rollback()
        logger.info("Couldn't refresh statistics for %s (not allowed for this database user)", names)
