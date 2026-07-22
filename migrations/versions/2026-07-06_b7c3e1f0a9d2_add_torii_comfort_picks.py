"""add torii_comfort_picks (comfort star-rating pick, once per season)

Revision ID: b7c3e1f0a9d2
Revises: f9f1d9f2b977
Create Date: 2026-07-06
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c3e1f0a9d2"
down_revision: str | Sequence[str] | None = "f9f1d9f2b977"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "torii_comfort_picks"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE in insp.get_table_names():
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("ruleset_id", sa.SmallInteger(), nullable=False),
        sa.Column("season_id", sa.VARCHAR(32), nullable=False),
        sa.Column("picked_star_rating", sa.Float(), nullable=False),
        sa.Column("floor_at_pick", sa.Float(), nullable=False),
        sa.Column("seed_rating", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    # gate "una vez por season" + lookups por (user, ruleset).
    op.create_index("uq_comfort_user_mode_season", _TABLE, ["user_id", "ruleset_id", "season_id"], unique=True)
    op.create_index("ix_torii_comfort_picks_user_id", _TABLE, ["user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE in insp.get_table_names():
        op.drop_table(_TABLE)
