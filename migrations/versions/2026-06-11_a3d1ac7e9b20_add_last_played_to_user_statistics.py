"""add last_played to lazer_user_statistics (active-only ranking)

Cached MAX(scores.ended_at) of a passed play per (user, mode). Powers the
active-only leaderboard: within 30d = ranked, 15-30d = greyed, 30d+ = unranked.
Stamped on score submit going forward; backfilled here from existing scores.

Idempotent so a re-run / interrupted migration is safe.

Revision ID: a3d1ac7e9b20
Revises: c010c0a1b2d3
Create Date: 2026-06-11 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "a3d1ac7e9b20"
down_revision: str | Sequence[str] | None = "c010c0a1b2d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = {c["name"] for c in inspector.get_columns("lazer_user_statistics")}
    if "last_played" not in columns:
        op.add_column(
            "lazer_user_statistics",
            sa.Column("last_played", sa.DateTime(timezone=True), nullable=True),
        )

    indexes = {i["name"] for i in inspector.get_indexes("lazer_user_statistics")}
    if "idx_user_stats_mode_lastplayed" not in indexes:
        op.create_index(
            "idx_user_stats_mode_lastplayed",
            "lazer_user_statistics",
            ["mode", "last_played"],
        )

    # Backfill from existing scores: last_played = MAX(ended_at) of a PASSED
    # play in the matching mode. Served by idx_score_user_mode_date so each
    # per-row MAX is an index lookup. Rows with no passed score stay NULL
    # (treated as "never played in this mode" -> unranked).
    op.execute(
        """
        UPDATE lazer_user_statistics s
        SET last_played = (
            SELECT MAX(sc.ended_at)
            FROM scores sc
            WHERE sc.user_id = s.user_id
              AND sc.gamemode = s.mode
              AND sc.passed = 1
        )
        WHERE s.last_played IS NULL
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    indexes = {i["name"] for i in inspector.get_indexes("lazer_user_statistics")}
    if "idx_user_stats_mode_lastplayed" in indexes:
        op.drop_index("idx_user_stats_mode_lastplayed", table_name="lazer_user_statistics")

    columns = {c["name"] for c in inspector.get_columns("lazer_user_statistics")}
    if "last_played" in columns:
        op.drop_column("lazer_user_statistics", "last_played")
