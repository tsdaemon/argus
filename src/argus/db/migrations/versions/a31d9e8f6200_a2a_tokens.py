"""API tokens and owned A2A contexts/tasks.

Revision ID: a31d9e8f6200
Revises: f2a6d8c41b93
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a31d9e8f6200"
down_revision = "f2a6d8c41b93"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "api_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("secret_hash", sa.Text(), nullable=False),
        sa.Column("interface", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("last_used_at", sa.TIMESTAMP(timezone=True)),
    )
    op.create_table(
        "a2a_contexts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "token_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("api_tokens.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "thread_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    op.create_table(
        "a2a_tasks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "context_id",
            sa.Text(),
            sa.ForeignKey("a2a_contexts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )


def downgrade():
    op.drop_table("a2a_tasks")
    op.drop_table("a2a_contexts")
    op.drop_table("api_tokens")
