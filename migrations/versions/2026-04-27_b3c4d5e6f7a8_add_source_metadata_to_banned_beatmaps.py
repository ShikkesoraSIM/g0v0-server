"""add source metadata to banned beatmaps

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-04-27 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "b3c4d5e6f7a8"
down_revision: str | Sequence[str] | None = "a2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "banned_beatmaps",
        sa.Column("source", sa.String(length=32), nullable=False, server_default="manual"),
    )
    op.add_column(
        "banned_beatmaps",
        sa.Column("reason", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "banned_beatmaps",
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        op.f("ix_banned_beatmaps_source"),
        "banned_beatmaps",
        ["source"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_banned_beatmaps_source"), table_name="banned_beatmaps")
    op.drop_column("banned_beatmaps", "created_at")
    op.drop_column("banned_beatmaps", "reason")
    op.drop_column("banned_beatmaps", "source")
