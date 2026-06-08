"""add torii cosmetic store config (admin curation)

A tiny key-value table the cosmetic store reads to know which catalog items
are sellable. Currently one key, ``disabled``, whose value is a JSON array of
catalog ids an admin has pulled from the store pool. A KV row leaves room for
future store config (featured queue, price overrides) without a schema change.

Idempotent so a re-run / interrupted migration is safe.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-09 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "torii_store_config" not in tables:
        op.create_table(
            "torii_store_config",
            sa.Column("config_key", sa.String(64), primary_key=True),
            sa.Column("value", sa.Text, nullable=False),
            sa.Column("updated_by", sa.BigInteger, nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "torii_store_config" in tables:
        op.drop_table("torii_store_config")
