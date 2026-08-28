"""el saludo de bienvenida queda pendiente hasta que entren por el cliente

Revision ID: c4e7a1b9d206
Revises: b6d1f4a8c3e7
Create Date: 2026-08-29

Arranca en 0 para TODOS a proposito. Las cuentas que ya existen no tienen nada
pendiente: es un saludo de bienvenida, no un anuncio, y prenderselo a los que ya
juegan hace meses seria un mensaje masivo disfrazado. Lo prende el registro de acá
en adelante.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e7a1b9d206"
down_revision: str | Sequence[str] | None = "b6d1f4a8c3e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "lazer_users"
_COLUMN = "torii_welcome_pending"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if _COLUMN not in {c["name"] for c in insp.get_columns(_TABLE)}:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.Boolean(), nullable=False, server_default=sa.text("0")))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if _COLUMN in {c["name"] for c in insp.get_columns(_TABLE)}:
        op.drop_column(_TABLE, _COLUMN)
