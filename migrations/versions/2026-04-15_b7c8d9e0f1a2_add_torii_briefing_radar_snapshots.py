"""add torii briefing radar snapshots

Revision ID: b7c8d9e0f1a2
Revises: d2c4f7a8b1e0
Create Date: 2026-04-15 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b7c8d9e0f1a2"
down_revision: str | Sequence[str] | None = "d2c4f7a8b1e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_NAME = "torii_briefing_radar_snapshots"
UPDATED_AT_INDEX = "ix_torii_briefing_radar_updated_at"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if TABLE_NAME not in set(inspector.get_table_names()):
        op.create_table(
            TABLE_NAME,
            sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("lazer_users.id"), primary_key=True, nullable=False),
            sa.Column("gamemode", sa.String(length=32), primary_key=True, nullable=False),
            sa.Column("variant", sa.String(length=32), primary_key=True, nullable=False),
            sa.Column("snapshot_data", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )

    indexes = {index["name"] for index in inspector.get_indexes(TABLE_NAME)}
    if UPDATED_AT_INDEX not in indexes:
        op.create_index(UPDATED_AT_INDEX, TABLE_NAME, ["updated_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if TABLE_NAME not in set(inspector.get_table_names()):
        return

    indexes = {index["name"] for index in inspector.get_indexes(TABLE_NAME)}
    if UPDATED_AT_INDEX in indexes:
        op.drop_index(UPDATED_AT_INDEX, table_name=TABLE_NAME)

    op.drop_table(TABLE_NAME)
