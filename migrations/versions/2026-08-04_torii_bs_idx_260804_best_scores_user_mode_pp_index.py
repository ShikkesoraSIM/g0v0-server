"""best_scores: indice (user_id, gamemode, pp) — la consulta mas cara del server

get_best_id() ("que numero de top play es este score") era un ROW_NUMBER() OVER
(PARTITION BY user_id, gamemode) sobre un subquery SIN WHERE: calculaba la ventana
sobre la tabla entera y recien afuera filtraba por score_id. MySQL no puede empujar
ese filtro adentro de la ventana, asi que cada llamada recorria las 44 mil filas.

Medido en prod sobre 8 dias: 1.077.933 llamadas, 57.211 segundos. Casi 16 horas de
CPU y el 8% del tiempo de vida del server en una sola consulta, y como se llama una
vez por play mostrada, era el motivo de que los perfiles cargaran lento.

La funcion ahora cuenta cuantos scores mejores tiene el jugador en ese modo, que con
este indice es un rango de a lo sumo el largo de su top list. La tabla tiene ~44 mil
filas, asi que crear el indice es instantaneo.

Idempotente: si ya se creo en caliente en prod, aca no hace nada.

Revision ID: torii_bs_idx_260804
Revises: d1e2f3a4b5c6
Create Date: 2026-08-04

OJO con el id: NO inventar uno "que parezca" del estilo abcdef123456. e5f6a7b8c9d0 ya
existia desde junio (add_profile_media_reviews) y duplicarlo le armo un ciclo al grafo
de alembic: el server no bootea y queda en restart loop. Antes de elegir uno,
grepear que no exista.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "torii_bs_idx_260804"
down_revision: str | Sequence[str] | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "best_scores"
_INDEX = "idx_best_scores_user_mode_pp"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {ix["name"] for ix in insp.get_indexes(_TABLE)}
    if _INDEX not in existing:
        op.create_index(_INDEX, _TABLE, ["user_id", "gamemode", "pp"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {ix["name"] for ix in insp.get_indexes(_TABLE)}
    if _INDEX in existing:
        op.drop_index(_INDEX, table_name=_TABLE)
