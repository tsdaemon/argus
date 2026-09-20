"""Break-glass requests and the single admin account, moved from sqlite.

Revision ID: f2a6d8c41b93
Revises: e5b7c9a31d20
"""

import sqlalchemy as sa
from alembic import op

revision = "f2a6d8c41b93"
down_revision = "e5b7c9a31d20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "breakglass_requests",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("target_host", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("proposed_objective", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "breakglass_requests_status_idx", "breakglass_requests", ["status", "created_at"]
    )
    op.create_table(
        "admin_account",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("session_secret", sa.Text(), nullable=False),
        sa.CheckConstraint("id = 1", name="admin_account_single_row"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("admin_account")
    op.drop_index("breakglass_requests_status_idx", table_name="breakglass_requests")
    op.drop_table("breakglass_requests")
