"""make beatmap url columns nullable

Revision ID: 8d2f1a4b9c6e
Revises: f4a2c9d1e6b7
Create Date: 2026-02-21 12:45:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "8d2f1a4b9c6e"
down_revision: str | Sequence[str] | None = "f4a2c9d1e6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    beatmapset_columns = {col["name"]: col for col in inspector.get_columns("beatmapsets")}
    if "preview_url" in beatmapset_columns and not beatmapset_columns["preview_url"]["nullable"]:
        op.alter_column(
            "beatmapsets",
            "preview_url",
            existing_type=sa.String(length=255),
            nullable=True,
        )

    beatmap_columns = {col["name"]: col for col in inspector.get_columns("beatmaps")}
    if "url" in beatmap_columns and not beatmap_columns["url"]["nullable"]:
        op.alter_column(
            "beatmaps",
            "url",
            existing_type=sa.String(length=255),
            nullable=True,
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    beatmap_columns = {col["name"]: col for col in inspector.get_columns("beatmaps")}
    if "url" in beatmap_columns and beatmap_columns["url"]["nullable"]:
        op.alter_column(
            "beatmaps",
            "url",
            existing_type=sa.String(length=255),
            nullable=False,
        )

    beatmapset_columns = {col["name"]: col for col in inspector.get_columns("beatmapsets")}
    if "preview_url" in beatmapset_columns and beatmapset_columns["preview_url"]["nullable"]:
        op.alter_column(
            "beatmapsets",
            "preview_url",
            existing_type=sa.String(length=255),
            nullable=False,
        )
