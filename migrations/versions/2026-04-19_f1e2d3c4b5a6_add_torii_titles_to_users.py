"""add torii_titles column to lazer_users

Revision ID: f1e2d3c4b5a6
Revises: e1f2a3b4c5d6
Create Date: 2026-04-19 06:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "f1e2d3c4b5a6"
down_revision: str | Sequence[str] | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "lazer_users",
        sa.Column("torii_titles", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("lazer_users", "torii_titles")
