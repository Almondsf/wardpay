"""
How much has been paid on each invoice: the one place balances get their
"paid" side. Every balance (invoice, student, term, debtors) goes through
`paid_subquery()`.
"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.sql import Subquery


def paid_subquery(within: Any = None) -> Subquery:
    """
    invoice_id -> paid_kobo: the sum of payment allocations (reversals are negative allocations). `within`
    (invoice ids, or a select of them) limits it to those bills; see invoicing.totals_subquery.
    """
    from app.modules.payments.models import Allocation  # payments builds on fees, not the other way round

    stmt = select(Allocation.invoice_id.label("invoice_id"), func.sum(Allocation.amount_kobo).label("paid_kobo"))
    if within is not None:
        stmt = stmt.where(Allocation.invoice_id.in_(within))
    return stmt.group_by(Allocation.invoice_id).subquery("invoice_paid")
