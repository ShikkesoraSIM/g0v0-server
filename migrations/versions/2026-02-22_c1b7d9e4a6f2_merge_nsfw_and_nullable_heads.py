"""merge nsfw and beatmap nullable heads

Revision ID: c1b7d9e4a6f2
Revises: 8d2f1a4b9c6e, b9f1d8c2a7e4
Create Date: 2026-02-22 00:10:00.000000

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "c1b7d9e4a6f2"
down_revision: str | Sequence[str] | None = ("8d2f1a4b9c6e", "b9f1d8c2a7e4")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

