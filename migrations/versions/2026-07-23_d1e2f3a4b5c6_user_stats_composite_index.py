"""lazer_user_statistics: indice compuesto (user_id, mode) — mata la contencion de locks del submit

El UPDATE `SET play_time=? WHERE user_id=? AND mode=?` del submit de scores, sin un indice
compuesto, podia resolverse por el indice de `mode` solo y escanear+X-lockear las ~770 filas
del modo entero. Eso trababa los submits concurrentes hasta 30s (= el timeout del ScoreUploader
del spectator) -> se dropeaban replays y a veces se perdia el score. El indice (user_id, mode)
fuerza el lookup a 1 fila exacta -> 1 lock, sin contencion.

Idempotente: en prod el indice ya se creo en caliente (ALTER ... ALGORITHM=INPLACE, LOCK=NONE),
asi que aca solo se crea si falta.

Revision ID: d1e2f3a4b5c6
Revises: c9d2e3f4a5b6
Create Date: 2026-07-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1e2f3a4b5c6"
down_revision: str | Sequence[str] | None = "c9d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "lazer_user_statistics"
_INDEX = "idx_user_stats_user_mode"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {ix["name"] for ix in insp.get_indexes(_TABLE)}
    if _INDEX not in existing:
        op.create_index(_INDEX, _TABLE, ["user_id", "mode"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {ix["name"] for ix in insp.get_indexes(_TABLE)}
    if _INDEX in existing:
        op.drop_index(_INDEX, table_name=_TABLE)
