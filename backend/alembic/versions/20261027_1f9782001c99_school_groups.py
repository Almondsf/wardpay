"""school groups: branches under one owner, head-office staff, the branch a device works in

Revision ID: 1f9782001c99
Revises: 5c1d2e7f9a01
Create Date: 2026-10-27

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "1f9782001c99"
down_revision: str | Sequence[str] | None = "5c1d2e7f9a01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_ROLE_RULE = (
    "(role = 'platform_admin' AND school_id IS NULL) OR (role <> 'platform_admin' AND school_id IS NOT NULL)"
)
NEW_ROLE_RULE = (
    "(role = 'platform_admin' AND school_id IS NULL AND group_id IS NULL)"
    " OR (role <> 'platform_admin' AND school_id IS NOT NULL AND group_id IS NULL)"
    " OR (role IN ('proprietor', 'bursar') AND school_id IS NULL AND group_id IS NOT NULL)"
)


def upgrade() -> None:
    op.create_table(
        "school_groups",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_school_groups")),
        sa.UniqueConstraint("name", name=op.f("uq_school_groups_name")),
    )
    op.add_column("schools", sa.Column("group_id", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_schools_group_id"), "schools", ["group_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_schools_group_id_school_groups"), "schools", "school_groups", ["group_id"], ["id"], ondelete="RESTRICT"
    )
    op.add_column("users", sa.Column("group_id", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_users_group_id"), "users", ["group_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_users_group_id_school_groups"), "users", "school_groups", ["group_id"], ["id"], ondelete="RESTRICT"
    )
    op.drop_constraint(op.f("ck_users_role_matches_school"), "users", type_="check")
    op.create_check_constraint(op.f("ck_users_role_matches_school"), "users", NEW_ROLE_RULE)
    op.add_column("auth_sessions", sa.Column("branch_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_auth_sessions_branch_id_schools"), "auth_sessions", "schools", ["branch_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_auth_sessions_branch_id_schools"), "auth_sessions", type_="foreignkey")
    op.drop_column("auth_sessions", "branch_id")
    op.execute("DELETE FROM users WHERE group_id IS NOT NULL")
    op.drop_constraint(op.f("ck_users_role_matches_school"), "users", type_="check")
    op.create_check_constraint(op.f("ck_users_role_matches_school"), "users", OLD_ROLE_RULE)
    op.drop_constraint(op.f("fk_users_group_id_school_groups"), "users", type_="foreignkey")
    op.drop_index(op.f("ix_users_group_id"), table_name="users")
    op.drop_column("users", "group_id")
    op.drop_constraint(op.f("fk_schools_group_id_school_groups"), "schools", type_="foreignkey")
    op.drop_index(op.f("ix_schools_group_id"), table_name="schools")
    op.drop_column("schools", "group_id")
    op.drop_table("school_groups")
