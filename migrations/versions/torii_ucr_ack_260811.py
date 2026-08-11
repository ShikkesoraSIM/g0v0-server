"""ucr: acknowledged_at

Revision ID: torii_ucr_ack_260811
Revises: torii_us_idx_260804
"""

from alembic import op
import sqlalchemy as sa

revision = "torii_ucr_ack_260811"
down_revision = "torii_us_idx_260804"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "username_change_requests",
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("username_change_requests", "acknowledged_at")
