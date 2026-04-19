"""create daily_challenge table

Revision ID: a1b2c3d4e5f6
Revises: b7c8d9e0f1a2
Create Date: 2026-04-19 03:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "b7c8d9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "daily_challenge",
        sa.Column("beatmap_id", sa.Integer(), nullable=False),
        sa.Column("ruleset_id", sa.Integer(), nullable=False),
        sa.Column("required_mods", sa.String(length=4096), nullable=False),
        sa.Column("allowed_mods", sa.String(length=4096), nullable=False),
        sa.Column("room_id", sa.Integer(), nullable=True),
        sa.Column("max_attempts", sa.Integer(), nullable=True),
        sa.Column("time_limit", sa.Integer(), nullable=True),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("date"),
        sa.Index("ix_daily_challenge_date", "date"),
    )


def downgrade() -> None:
    op.drop_table("daily_challenge")
