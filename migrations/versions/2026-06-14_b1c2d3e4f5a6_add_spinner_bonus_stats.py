"""add spinner bonus/spin hit-stat columns to scores

SPINNER BONUS (large_bonus) and SPINNER SPIN (small_bonus) were dropped on submit
because the columns did not exist, so the results screen showed 0 for every online
score. Add them (nullable). New submissions store them; old scores stay NULL so the
API omits them and the client shows 0 as before (the data was never captured, a
backfill from replays would be separate).

Idempotent so a re-run / interrupted migration is safe.

Revision ID: b1c2d3e4f5a6
Revises: a3d1ac7e9b20
Create Date: 2026-06-14 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "b1c2d3e4f5a6"
down_revision: str | Sequence[str] | None = "a3d1ac7e9b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("scores")}

    if "nlarge_bonus" not in columns:
        op.add_column("scores", sa.Column("nlarge_bonus", sa.Integer(), nullable=True))
    if "nsmall_bonus" not in columns:
        op.add_column("scores", sa.Column("nsmall_bonus", sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("scores")}

    if "nsmall_bonus" in columns:
        op.drop_column("scores", "nsmall_bonus")
    if "nlarge_bonus" in columns:
        op.drop_column("scores", "nlarge_bonus")
