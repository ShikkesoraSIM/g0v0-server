"""matchmaking_flagged_beatmaps (mapas que no matchean la version online -> flag + auto-exclusion)

Revision ID: c9d2e3f4a5b6
Revises: b7c3e1f0a9d2
Create Date: 2026-07-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9d2e3f4a5b6"
down_revision: str | Sequence[str] | None = "b7c3e1f0a9d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "matchmaking_flagged_beatmaps"


def upgrade() -> None:
    # cuando un mapa de la pool no se puede jugar (la copia local no matchea la version
    # online -> "Imported beatmap set doesn't match the online version"), el spectator
    # incrementa flagged_count aca. el selector excluye los mapas con flagged_count sobre
    # el umbral, asi los mapas rotos se sacan solos del pool sin castigar a nadie por un
    # solo downloader lento (el umbral filtra los falsos positivos).
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE in insp.get_table_names():
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("pool_id", sa.Integer(), nullable=False),
        sa.Column("beatmap_id", sa.Integer(), nullable=False),
        sa.Column("flagged_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_flagged_at", sa.DateTime(), nullable=False),
    )
    op.create_index("uq_flagged_pool_beatmap", _TABLE, ["pool_id", "beatmap_id"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE in insp.get_table_names():
        op.drop_table(_TABLE)
