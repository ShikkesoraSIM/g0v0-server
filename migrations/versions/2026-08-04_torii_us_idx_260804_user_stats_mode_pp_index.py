"""lazer_user_statistics: indice (mode, pp) — el rango que cuenta el ranking

get_rank() era un ROW_NUMBER() OVER (ORDER BY pp DESC) sobre todos los rankeados
del modo, del que despues se sacaba una sola fila: para saber el puesto de UNO se
ordenaba a los 10.786. Medido en prod: 1057 llamadas a 1,1 ms en una tanda de
cargas de perfil.

Ahora cuenta cuantos elegibles tienen mas pp. Con el indice de pp suelto habria que
filtrar el modo despues; con (mode, pp) es un rango directo.

OJO con el id: NO inventar uno "que parezca" del estilo abcdef123456. Ya paso hoy
(e5f6a7b8c9d0 existia desde junio, duplicarlo le armo un ciclo al grafo y el server
quedo en restart loop). Grepear antes de elegir.

Idempotente: en prod el indice ya se creo en caliente.

Revision ID: torii_us_idx_260804
Revises: torii_st_idx_260804
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "torii_us_idx_260804"
down_revision: str | Sequence[str] | None = "torii_st_idx_260804"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "lazer_user_statistics"
_INDEX = "idx_user_stats_mode_pp"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {ix["name"] for ix in insp.get_indexes(_TABLE)}
    if _INDEX not in existing:
        op.create_index(_INDEX, _TABLE, ["mode", "pp"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {ix["name"] for ix in insp.get_indexes(_TABLE)}
    if _INDEX in existing:
        op.drop_index(_INDEX, table_name=_TABLE)
