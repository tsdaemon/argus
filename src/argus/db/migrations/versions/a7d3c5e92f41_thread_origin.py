"""Thread origin: separate A2A-created conversations from the operator's own.

Revision ID: a7d3c5e92f41
Revises: d4c2e8a71b05
"""

import sqlalchemy as sa
from alembic import op

revision = "a7d3c5e92f41"
down_revision = "d4c2e8a71b05"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "threads", sa.Column("origin", sa.Text(), nullable=False, server_default="human")
    )
    # A2A-created threads: ones an A2A context still points at, and ones that idle rotation
    # orphaned (rotation rewrites a2a_contexts.thread_id), recognised by an agent author on
    # the first user message, as `SqlHistory._author` reads it.
    op.execute(
        """
        UPDATE threads SET origin = 'a2a'
        WHERE id IN (SELECT thread_id FROM a2a_contexts)
           OR (
               SELECT m.content -> 'metadata' -> 'argus_author' ->> 'kind'
               FROM messages m
               WHERE m.thread_id = threads.id AND m.role = 'user'
               ORDER BY m.chat_order
               LIMIT 1
           ) = 'agent'
        """
    )


def downgrade():
    op.drop_column("threads", "origin")
