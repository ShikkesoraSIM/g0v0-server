"""add idx_score_ended_at

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-06-05 00:00:01.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "f6a7b8c9d0e1"
down_revision: str | Sequence[str] | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_NAME = "scores"
INDEX_NAME = "idx_score_ended_at"


def upgrade() -> None:
    # Standalone index on scores.ended_at. The server-pulse snapshot
    # (plays_last_minute / plays_last_5min, sparkline, recent_plays) filters
    # Score purely on ended_at, but the only pre-existing ended_at index is
    # the composite (user_id, gamemode, ended_at, id) where ended_at is not a
    # leading column - so those range scans fell back to full table scans
    # (~200-500 ms per pulse recompute). A dedicated index turns them into
    # index range scans. Idempotent so a re-run is a no-op.
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if TABLE_NAME not in set(inspector.get_table_names()):
        return

    indexes = {index["name"] for index in inspector.get_indexes(TABLE_NAME)}
    if INDEX_NAME not in indexes:
        op.create_index(INDEX_NAME, TABLE_NAME, ["ended_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if TABLE_NAME not in set(inspector.get_table_names()):
        return

    indexes = {index["name"] for index in inspector.get_indexes(TABLE_NAME)}
    if INDEX_NAME in indexes:
        op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
