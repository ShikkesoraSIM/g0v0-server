"""add is_local columns for beatmaps and beatmapsets

Revision ID: f4a2c9d1e6b7
Revises: c5472f592d13
Create Date: 2026-02-21 12:35:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "f4a2c9d1e6b7"
down_revision: str | Sequence[str] | None = "c5472f592d13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    beatmapset_columns = {col["name"] for col in inspector.get_columns("beatmapsets")}
    if "is_local" not in beatmapset_columns:
        op.add_column(
            "beatmapsets",
            sa.Column("is_local", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        )

    beatmap_columns = {col["name"] for col in inspector.get_columns("beatmaps")}
    if "is_local" not in beatmap_columns:
        op.add_column(
            "beatmaps",
            sa.Column("is_local", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        )

    beatmap_indexes = {idx["name"] for idx in inspector.get_indexes("beatmaps")}
    if "idx_beatmaps_is_local" not in beatmap_indexes:
        op.create_index("idx_beatmaps_is_local", "beatmaps", ["is_local"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    beatmap_indexes = {idx["name"] for idx in inspector.get_indexes("beatmaps")}
    if "idx_beatmaps_is_local" in beatmap_indexes:
        op.drop_index("idx_beatmaps_is_local", table_name="beatmaps")

    beatmap_columns = {col["name"] for col in inspector.get_columns("beatmaps")}
    if "is_local" in beatmap_columns:
        op.drop_column("beatmaps", "is_local")

    beatmapset_columns = {col["name"] for col in inspector.get_columns("beatmapsets")}
    if "is_local" in beatmapset_columns:
        op.drop_column("beatmapsets", "is_local")
