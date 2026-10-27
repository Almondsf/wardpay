"""who pays the provider's charge on online checkouts (each school chooses), and the charge on each checkout

Revision ID: 89fe09a15270
Revises: 1f9782001c99
Create Date: 2026-10-27

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "89fe09a15270"
down_revision: str | Sequence[str] | None = "1f9782001c99"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PAYER = postgresql.ENUM("school", "parent", name="checkout_charge_payer", create_type=False)


def upgrade() -> None:
    PAYER.create(op.get_bind(), checkfirst=True)
    op.add_column("schools", sa.Column("checkout_charge_payer", PAYER, server_default="school", nullable=False))
    op.add_column("payment_intents", sa.Column("charge_kobo", sa.BigInteger(), server_default="0", nullable=False))
    op.create_check_constraint(op.f("ck_payment_intents_charge_not_negative"), "payment_intents", "charge_kobo >= 0")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_payment_intents_charge_not_negative"), "payment_intents", type_="check")
    op.drop_column("payment_intents", "charge_kobo")
    op.drop_column("schools", "checkout_charge_payer")
    PAYER.drop(op.get_bind(), checkfirst=True)
