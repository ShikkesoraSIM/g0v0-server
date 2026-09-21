"""matchmaking_user_stats: una fila por jugador Y por pool

Revision ID: 7b2d9e4f1a63
Revises: c4e7a1b9d206
Create Date: 2026-09-21

La tabla nacio con PK (user_id, ruleset_id), la sincronizacion con osu-web la
dejo en (user_id) sola, y la migracion de abril le agrego pool_id, rating y
plays sin tocar la clave. El modelo y el spectator siempre asumieron
(user_id, pool_id), que es ademas lo que usa osu-web.

Con la PK vieja cada jugador podia tener UNA fila en total. El que tenia fila
en un pool (una partida de ranked mania cuando ese pool estuvo abierto) no
podia elegir su star rating comodo en otro: el INSERT de la siembra chocaba, el
POST de comfort-pick daba 500, la transaccion se revertia y la ventana de
dificultad le volvia a aparecer para siempre. Y el upsert del spectator, en vez
de insertar, le habria pisado los numeros de la fila del otro pool.

En prod se aplico a mano el 2026-09-21 (backup antes), por eso el upgrade mira
la clave actual y no hace nada si ya esta bien.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7b2d9e4f1a63"
down_revision: str | Sequence[str] | None = "c4e7a1b9d206"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "matchmaking_user_stats"


def _pk_columns() -> list[str]:
    insp = sa.inspect(op.get_bind())
    return list(insp.get_pk_constraint(_TABLE).get("constrained_columns") or [])


def upgrade() -> None:
    if _pk_columns() == ["user_id", "pool_id"]:
        return

    bind = op.get_bind()
    sin_pool = bind.execute(sa.text(f"SELECT COUNT(*) FROM {_TABLE} WHERE pool_id IS NULL")).scalar() or 0
    if sin_pool:
        # una columna de PK no puede ser nula. esas filas son de antes de que
        # existiera pool_id y no pertenecen a ningun pool: hay que decidir a mano
        # a cual van (o borrarlas) antes de migrar, no adivinarlo aca.
        raise RuntimeError(
            f"{_TABLE} tiene {sin_pool} filas con pool_id NULL; asignarles un pool o borrarlas antes de migrar"
        )

    # todo en un solo ALTER: la FK de user_id necesita un indice que arranque
    # por user_id, y soltando y creando la PK en la misma sentencia nunca se
    # queda sin uno.
    op.execute(
        f"ALTER TABLE {_TABLE} "
        "MODIFY pool_id int NOT NULL, "
        "DROP PRIMARY KEY, "
        "ADD PRIMARY KEY (user_id, pool_id)"
    )


def downgrade() -> None:
    if _pk_columns() == ["user_id"]:
        return

    # solo se puede volver si nadie tiene filas en mas de un pool; si alguien las
    # tiene, mysql corta el ALTER por clave duplicada y no se pierde nada.
    op.execute(
        f"ALTER TABLE {_TABLE} "
        "DROP PRIMARY KEY, "
        "ADD PRIMARY KEY (user_id), "
        "MODIFY pool_id int NULL"
    )
