"""add torii_mapperatorinator_presets (presets de generacion, guardados online)

Revision ID: f3b8c1d0a2e4
Revises: torii_highpp_260811
Create Date: 2026-08-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3b8c1d0a2e4"
down_revision: str | Sequence[str] | None = "torii_highpp_260811"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "torii_mapperatorinator_presets"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE in insp.get_table_names():
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.VARCHAR(60), nullable=False),
        sa.Column("settings", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_torii_mappera_presets_user_id", _TABLE, ["user_id"])
    op.create_unique_constraint("uq_torii_mappera_preset_user_name", _TABLE, ["user_id", "name"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE in insp.get_table_names():
        op.drop_table(_TABLE)
