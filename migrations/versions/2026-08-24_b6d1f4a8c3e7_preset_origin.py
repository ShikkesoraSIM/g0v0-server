"""presets: de donde salio cada uno (forks)

Revision ID: b6d1f4a8c3e7
Revises: a9c2e5b1f7d3
Create Date: 2026-08-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6d1f4a8c3e7"
down_revision: str | Sequence[str] | None = "a9c2e5b1f7d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "torii_mapperatorinator_presets"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {c["name"] for c in insp.get_columns(_TABLE)}

    if "origin_preset_id" not in existing:
        op.add_column(_TABLE, sa.Column("origin_preset_id", sa.Integer(), nullable=True))
    if "origin_user_id" not in existing:
        op.add_column(_TABLE, sa.Column("origin_user_id", sa.BigInteger(), nullable=True))
    if "origin_username" not in existing:
        op.add_column(_TABLE, sa.Column("origin_username", sa.VARCHAR(32), nullable=True))

    if "ix_torii_mappera_presets_origin" not in {i["name"] for i in insp.get_indexes(_TABLE)}:
        op.create_index("ix_torii_mappera_presets_origin", _TABLE, ["origin_preset_id"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if "ix_torii_mappera_presets_origin" in {i["name"] for i in insp.get_indexes(_TABLE)}:
        op.drop_index("ix_torii_mappera_presets_origin", table_name=_TABLE)

    for column in ("origin_username", "origin_user_id", "origin_preset_id"):
        if column in {c["name"] for c in insp.get_columns(_TABLE)}:
            op.drop_column(_TABLE, column)
