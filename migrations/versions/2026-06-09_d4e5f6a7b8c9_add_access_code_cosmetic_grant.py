"""add cosmetic grant to access codes

Access codes can now unlock a cosmetic (trail / name colour / aura) on redeem,
not just points. The reward is stored as a JSON array of catalog ids in a new
torii_access_codes.grant_cosmetics column. Null/empty = points only.

Idempotent so a re-run / interrupted migration is safe.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-09 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "torii_access_codes" in tables:
        columns = {c["name"] for c in inspector.get_columns("torii_access_codes")}
        if "grant_cosmetics" not in columns:
            op.add_column(
                "torii_access_codes",
                sa.Column("grant_cosmetics", sa.String(1024), nullable=True),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "torii_access_codes" in tables:
        columns = {c["name"] for c in inspector.get_columns("torii_access_codes")}
        if "grant_cosmetics" in columns:
            op.drop_column("torii_access_codes", "grant_cosmetics")
