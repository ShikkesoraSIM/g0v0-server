"""high_pp_whitelist

Revision ID: torii_highpp_260811
Revises: torii_ucr_ack_260811
"""

from alembic import op
import sqlalchemy as sa

revision = "torii_highpp_260811"
down_revision = "torii_ucr_ack_260811"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "high_pp_whitelist",
        sa.Column("user_id", sa.BigInteger(), primary_key=True),
        sa.Column("added_by_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("high_pp_whitelist")
