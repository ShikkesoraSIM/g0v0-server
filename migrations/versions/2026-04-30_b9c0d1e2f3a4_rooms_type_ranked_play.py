"""rooms.type: add RANKED_PLAY enum value

The lazer client distinguishes between two server-orchestrated room
types:

* `Matchmaking` — the original "matchmaking" type that ships with
  upstream and uses the `MatchmakingRoomState` shape.
* `RankedPlay` — the newer card / pick / discard 1v1 elo bout that
  uses `RankedPlayRoomState`.

The spectator (`MatchmakingQueueBackgroundService.processBundle`)
sets `Settings.MatchType = pool.type.ToMatchType()`, which serialises
to the JSON string `ranked_play` for ranked-play pools. Without this
enum value the server-side fallback collapses it to MATCHMAKING, the
client tries to cross-cast `MatchmakingRoomState → RankedPlayRoomState`
on `RankedPlayMatchInfo.LoadComplete`, and the whole client crashes
on match start.

Round-trip the value untouched.

Revision ID: b9c0d1e2f3a4
Revises: f8a9b0c1d2e3
Create Date: 2026-04-30 16:25:00.000000

"""

from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "b9c0d1e2f3a4"
down_revision: str | Sequence[str] | None = "f8a9b0c1d2e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE rooms MODIFY COLUMN type "
        "ENUM('PLAYLISTS','HEAD_TO_HEAD','TEAM_VERSUS','MATCHMAKING','RANKED_PLAY') "
        "NOT NULL"
    )


def downgrade() -> None:
    # Collapse any in-flight RANKED_PLAY rooms to MATCHMAKING before
    # dropping the enum value — otherwise the ALTER fails on existing
    # rows.
    op.execute("UPDATE rooms SET type = 'MATCHMAKING' WHERE type = 'RANKED_PLAY'")
    op.execute(
        "ALTER TABLE rooms MODIFY COLUMN type "
        "ENUM('PLAYLISTS','HEAD_TO_HEAD','TEAM_VERSUS','MATCHMAKING') "
        "NOT NULL"
    )
