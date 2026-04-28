"""add equipped_aura column to lazer_users

Stores each user's chosen aura preset. Nullable: NULL means "no preference,
use the group-default mapping" (the same as the sentinel value "default").

The column is small and indexed only by the user PK; no need for a
separate index since lookups are always `WHERE id = ?`.

Revision ID: d5e6f7a8b9c0
Revises: b3c4d5e6f7a8
Create Date: 2026-04-28 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "d5e6f7a8b9c0"
down_revision: str | Sequence[str] | None = "b3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "lazer_users",
        sa.Column(
            "equipped_aura",
            sa.String(length=64),
            nullable=True,
            comment=(
                "Aura the user has chosen to broadcast. Values: NULL or 'default' "
                "= use the default aura derived from their groups; 'none' = explicitly "
                "no aura; otherwise an entry from app.models.torii_auras.TORII_AURAS."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("lazer_users", "equipped_aura")
