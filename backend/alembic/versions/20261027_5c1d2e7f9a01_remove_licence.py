"""remove the licence fee completely

WardPay charges a set-up fee, a pupil fee each term and a fee per instalment plan; there is no licence fee
(owner, 2026-10-23 and 2026-10-27: "there is nothing like that anymore"). Licence lines were only ever made by
test bills before 23 Oct, so they're removed and the kind is taken out of the type.

Revision ID: 5c1d2e7f9a01
Revises: 121df501f9e4
Create Date: 2026-10-27

"""

from collections.abc import Sequence

from alembic import op

revision: str = "5c1d2e7f9a01"
down_revision: str | Sequence[str] | None = "121df501f9e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIGN = (
    "quantity >= 0 AND CASE WHEN kind = 'discount' THEN amount_kobo < 0 ELSE amount_kobo >= 0 END"
)


def _retype(values: str) -> None:
    op.drop_constraint(op.f("ck_subscription_lines_amount_sign"), "subscription_lines", type_="check")
    op.execute("ALTER TYPE subscription_line_kind RENAME TO subscription_line_kind_old")
    op.execute(f"CREATE TYPE subscription_line_kind AS ENUM ({values})")
    op.execute(
        "ALTER TABLE subscription_lines ALTER COLUMN kind TYPE subscription_line_kind "
        "USING kind::text::subscription_line_kind"
    )
    op.execute("DROP TYPE subscription_line_kind_old")
    op.create_check_constraint(op.f("ck_subscription_lines_amount_sign"), "subscription_lines", SIGN)


def upgrade() -> None:
    # Lines are append-only: the protection is paused for this one clean-up only
    op.execute("ALTER TABLE subscription_lines DISABLE TRIGGER subscription_lines_append_only")
    op.execute("DELETE FROM subscription_lines WHERE kind = 'licence'")
    op.execute("ALTER TABLE subscription_lines ENABLE TRIGGER subscription_lines_append_only")
    _retype("'setup', 'pupils', 'instalment_plans', 'discount'")


def downgrade() -> None:
    _retype("'setup', 'licence', 'pupils', 'instalment_plans', 'discount'")
