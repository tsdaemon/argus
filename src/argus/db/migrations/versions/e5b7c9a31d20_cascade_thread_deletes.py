"""Cascade thread deletes to every dependent row.

Revision ID: e5b7c9a31d20
Revises: c81a9a2d4f10
"""

from alembic import op

revision = "e5b7c9a31d20"
down_revision = "c81a9a2d4f10"
branch_labels = None
depends_on = None

# (table, column, referred table). PostgreSQL's default constraint name is used.
_FOREIGN_KEYS = [
    ("runs", "thread_id", "threads"),
    ("messages", "thread_id", "threads"),
    ("messages", "run_id", "runs"),
    ("tool_calls", "thread_id", "threads"),
    ("tool_calls", "run_id", "runs"),
    ("approvals", "tool_call_id", "tool_calls"),
]


def _recreate(ondelete: str | None) -> None:
    for table, column, referred in _FOREIGN_KEYS:
        name = f"{table}_{column}_fkey"
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(name, table, referred, [column], ["id"], ondelete=ondelete)


def upgrade() -> None:
    _recreate("CASCADE")


def downgrade() -> None:
    _recreate(None)
