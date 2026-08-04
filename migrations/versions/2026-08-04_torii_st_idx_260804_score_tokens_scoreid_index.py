"""score_tokens: indice (score_id, created_at) — score_id no tenia ninguno

Dos consultas escaneaban las 370 mil filas enteras cada vez:

    WHERE score_id IS NULL AND created_at >= ? ORDER BY created_at DESC
    WHERE score_id = ?

Medido en prod: 82 ms por llamada y 366.747 filas leidas para devolver un puñado.
Con el compuesto la primera pasa a `type=range` con 4 filas y backward index scan,
y la segunda a un lookup directo porque score_id va primero.

OJO con el id de esta revision: NO inventar uno "que parezca" del estilo
abcdef123456. Ya paso hoy: e5f6a7b8c9d0 existia desde junio, duplicarlo le armo un
ciclo al grafo de alembic y el server quedo en restart loop. Grepear antes de elegir.

Idempotente: en prod el indice ya se creo en caliente
(ALTER ... ALGORITHM=INPLACE, LOCK=NONE), asi que aca solo se crea si falta.

Revision ID: torii_st_idx_260804
Revises: torii_bs_idx_260804
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "torii_st_idx_260804"
down_revision: str | Sequence[str] | None = "torii_bs_idx_260804"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "score_tokens"
_INDEX = "idx_score_tokens_scoreid_created"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {ix["name"] for ix in insp.get_indexes(_TABLE)}
    if _INDEX not in existing:
        op.create_index(_INDEX, _TABLE, ["score_id", "created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {ix["name"] for ix in insp.get_indexes(_TABLE)}
    if _INDEX in existing:
        op.drop_index(_INDEX, table_name=_TABLE)
