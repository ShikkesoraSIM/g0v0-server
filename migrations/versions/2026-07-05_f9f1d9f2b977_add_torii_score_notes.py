"""add torii_score_notes (notas de score con imagen opcional)

Revision ID: f9f1d9f2b977
Revises: 8a43b2e5093d
Create Date: 2026-07-05
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f9f1d9f2b977"
down_revision: str | Sequence[str] | None = "8a43b2e5093d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "torii_score_notes"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE in insp.get_table_names():
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("score_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.VARCHAR(32), nullable=False),
        sa.Column("text", sa.VARCHAR(280), nullable=False),
        sa.Column("has_image", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_torii_score_notes_score_id", _TABLE, ["score_id"], unique=True)
    op.create_index("ix_torii_score_notes_user_id", _TABLE, ["user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE in insp.get_table_names():
        op.drop_table(_TABLE)
