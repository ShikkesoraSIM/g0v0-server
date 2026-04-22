"""merge daily_challenge and suspicious_alerts heads

Revision ID: e1f2a3b4c5d6
Revises: a1b2c3d4e5f6, c3a4e5f6b7d8
Create Date: 2026-04-19 04:45:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: str | Sequence[str] | None = ("a1b2c3d4e5f6", "c3a4e5f6b7d8")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
