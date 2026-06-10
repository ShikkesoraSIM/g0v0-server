"""merge supporter/aura and matchmaking heads

Revision ID: f8a9b0c1d2e3
Revises: f7a8b9c0d1e2, e8f9a0b1c2d3
Create Date: 2026-04-30 15:55:00.000000

"""

from collections.abc import Sequence


revision: str = "f8a9b0c1d2e3"
down_revision: str | Sequence[str] | None = ("f7a8b9c0d1e2", "e8f9a0b1c2d3")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
