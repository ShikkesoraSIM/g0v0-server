"""add player + beatmap ids to torii_replay_renders (para links del bot)

Revision ID: 8a43b2e5093d
Revises: bf047d8e644f
Create Date: 2026-07-05
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8a43b2e5093d"
down_revision: str | Sequence[str] | None = "bf047d8e644f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "torii_replay_renders"
_COLS = [
    ("player_username", sa.VARCHAR(32)),
    ("player_user_id", sa.BigInteger()),
    ("beatmap_online_id", sa.BigInteger()),
    ("beatmapset_id", sa.BigInteger()),
    ("gamemode", sa.VARCHAR(16)),
]


def _has_column(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    for name, coltype in _COLS:
        if not _has_column(bind, _TABLE, name):
            op.add_column(_TABLE, sa.Column(name, coltype, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    for name, _ in reversed(_COLS):
        if _has_column(bind, _TABLE, name):
            op.drop_column(_TABLE, name)
