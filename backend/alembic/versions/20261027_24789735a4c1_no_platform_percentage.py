"""no platform percentage: WardPay takes no share of school payments; it earns only from its bills to schools

Owner, 2026-10-27: "remove the WardPay 1%... we'll collect our money strictly by billing the school".
Subaccounts already at Paystack are set to 0% by `python -m app.cli clear-platform-fee`.

Revision ID: 24789735a4c1
Revises: 89fe09a15270
Create Date: 2026-10-27

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "24789735a4c1"
down_revision: str | Sequence[str] | None = "89fe09a15270"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("school_payment_accounts", "platform_fee_bp")


def downgrade() -> None:
    op.add_column(
        "school_payment_accounts",
        sa.Column("platform_fee_bp", sa.SmallInteger(), server_default="0", nullable=False),
    )
