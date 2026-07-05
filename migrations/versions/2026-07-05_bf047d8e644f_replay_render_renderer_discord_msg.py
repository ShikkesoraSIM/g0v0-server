"""add renderer + discord_message_id to torii_replay_renders

Para el status en vivo del bot: renderer = host de o!rdr, discord_message_id =
el mensaje que ToriiHalo edita con el progreso.

Revision ID: bf047d8e644f
Revises: 1502b70ae8f5
Create Date: 2026-07-05
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bf047d8e644f"
down_revision: str | Sequence[str] | None = "1502b70ae8f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "torii_replay_renders"


def _has_column(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, _TABLE, "renderer"):
        op.add_column(_TABLE, sa.Column("renderer", sa.VARCHAR(64), nullable=True))
    if not _has_column(bind, _TABLE, "discord_message_id"):
        op.add_column(_TABLE, sa.Column("discord_message_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, _TABLE, "discord_message_id"):
        op.drop_column(_TABLE, "discord_message_id")
    if _has_column(bind, _TABLE, "renderer"):
        op.drop_column(_TABLE, "renderer")
