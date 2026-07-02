"""add pause_count a scores

cantidad de pausas en medio de la play (el cliente manda los timestamps en el
submission). se usa para el nerf de pp por pausar. los scores viejos quedan en
0 (sin penalizacion). idempotente.

Revision ID: d7e8f9a0b1c2
Revises: b1c2d3e4f5a6
Create Date: 2026-07-02 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "d7e8f9a0b1c2"
down_revision: str | Sequence[str] | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("scores")}
    if "pause_count" not in cols:
        op.add_column(
            "scores",
            sa.Column("pause_count", sa.SmallInteger(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("scores")}
    if "pause_count" in cols:
        op.drop_column("scores", "pause_count")
