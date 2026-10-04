"""Where a thread's title came from: a model or the operator.

Revision ID: d4c2e8a71b05
Revises: b7e3f1c94a52
"""

import sqlalchemy as sa
from alembic import op

revision = "d4c2e8a71b05"
down_revision = "b7e3f1c94a52"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("threads", sa.Column("title_source", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("threads", "title_source")
