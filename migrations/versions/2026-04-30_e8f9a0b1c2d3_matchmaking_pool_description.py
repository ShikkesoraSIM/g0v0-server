"""matchmaking: pool description column

Adds an admin-editable rich-text blurb to each pool. The public ranking
page renders it under the pool's name so a curated pool can introduce
itself ("warm-up pool, 3-5★ classics") and feel like a real product
instead of a row in a list.

Stored as TEXT (no length cap) since pool descriptions might link to
external mappers / tournaments / events and admins shouldn't be fighting
a 255-char wall.

Revision ID: e8f9a0b1c2d3
Revises: d6e7f8a9b0c1
Create Date: 2026-04-30 03:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "e8f9a0b1c2d3"
down_revision: str | Sequence[str] | None = "d6e7f8a9b0c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "matchmaking_pools",
        sa.Column("description", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("matchmaking_pools", "description")
