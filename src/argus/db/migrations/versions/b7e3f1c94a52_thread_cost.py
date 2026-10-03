"""Running model cost per thread, in USD.

Revision ID: b7e3f1c94a52
Revises: a31d9e8f6200
"""

import sqlalchemy as sa
from alembic import op

revision = "b7e3f1c94a52"
down_revision = "a31d9e8f6200"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "threads",
        sa.Column("cost_usd", sa.Numeric(14, 8), nullable=False, server_default="0"),
    )


def downgrade():
    op.drop_column("threads", "cost_usd")
