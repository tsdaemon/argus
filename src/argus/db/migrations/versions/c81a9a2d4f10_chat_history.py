"""Conversation ordering and durable AG-UI messages.

Revision ID: c81a9a2d4f10
Revises: dbac6e6f80fb
"""

import sqlalchemy as sa
from alembic import op

revision = "c81a9a2d4f10"
down_revision = "dbac6e6f80fb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "threads",
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.execute("UPDATE threads SET updated_at = created_at")
    op.create_index("threads_updated_at_idx", "threads", ["updated_at"])
    op.add_column("messages", sa.Column("agui_id", sa.Text(), nullable=True))
    op.add_column(
        "messages", sa.Column("chat_order", sa.BigInteger(), sa.Identity(), nullable=False)
    )
    op.create_unique_constraint("messages_thread_agui_key", "messages", ["thread_id", "agui_id"])


def downgrade() -> None:
    op.drop_constraint("messages_thread_agui_key", "messages", type_="unique")
    op.drop_column("messages", "agui_id")
    op.drop_column("messages", "chat_order")
    op.drop_index("threads_updated_at_idx", table_name="threads")
    op.drop_column("threads", "updated_at")
