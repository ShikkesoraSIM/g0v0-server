"""add ai flag to beatmapsets (generado con mapperatorinator)

Revision ID: a9c2e5b1f7d3
Revises: f3b8c1d0a2e4
Create Date: 2026-08-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9c2e5b1f7d3"
down_revision: str | Sequence[str] | None = "f3b8c1d0a2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "beatmapsets"
_COLUMN = "ai"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if _COLUMN not in {c["name"] for c in insp.get_columns(_TABLE)}:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.Boolean(), nullable=False, server_default=sa.text("0")))

    if "ix_beatmapsets_ai" not in {i["name"] for i in insp.get_indexes(_TABLE)}:
        op.create_index("ix_beatmapsets_ai", _TABLE, [_COLUMN])

    # lo que ya se puede saber sin abrir archivos: el tag. Los sets viejos cuyo tag se
    # perdio (o que nunca lo tuvieron a nivel set) se marcan aparte leyendo el .osz.
    op.execute("UPDATE beatmapsets SET ai = 1 WHERE tags LIKE '%mapperatorinator%'")


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if "ix_beatmapsets_ai" in {i["name"] for i in insp.get_indexes(_TABLE)}:
        op.drop_index("ix_beatmapsets_ai", table_name=_TABLE)

    if _COLUMN in {c["name"] for c in insp.get_columns(_TABLE)}:
        op.drop_column(_TABLE, _COLUMN)
