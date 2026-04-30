"""matchmaking: ranked-play complete schema

Brings the matchmaking schema up to the surface the rebased Torii spectator
expects. Previously only the bare quick-play tables existed (pools, pool
beatmaps, user stats); spectator's ranked-play + elo flows additionally need:

  - per-pool lobby/search-radius config (lobby_size, rating_search_radius,
    rating_search_radius_max, rating_search_radius_exp) — without these the
    MatchmakingQueueBackgroundService can't decide when to ship a lobby
  - per-pool `type` discriminator (quick_play vs ranked_play) — drives the
    controller pick in ServerMultiplayerRoom (MatchmakingMatchController vs
    RankedPlayMatchController)
  - matchmaking_user_stats.rating + plays — the matchmaker reads these to
    seed the rating-distribution buckets
  - matchmaking_pool_beatmaps.rating_sig — the OpenSkill rating uncertainty
    that gets written back after every match
  - matchmaking_user_elo_history — append-only audit per (room, pool, pair)
    that the spectator writes via InsertUserEloHistoryEntry at end-of-match
  - matchmaking_room_events — the matchmaking-side LogRoomEventAsync sink
    (parallel to multiplayer_events for non-matchmaking rooms)

Revision ID: d6e7f8a9b0c1
Revises: a2b3c4d5e6f7
Create Date: 2026-04-30 02:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql


# revision identifiers, used by Alembic.
revision: str = "d6e7f8a9b0c1"
down_revision: str | Sequence[str] | None = "a2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # --- matchmaking_pools: pool config the queue-service needs ---
    op.add_column(
        "matchmaking_pools",
        sa.Column(
            "type",
            mysql.ENUM("quick_play", "ranked_play", name="matchmakingpooltype"),
            nullable=False,
            server_default="quick_play",
        ),
    )
    op.add_column(
        "matchmaking_pools",
        sa.Column("lobby_size", sa.Integer(), nullable=False, server_default="8"),
    )
    op.add_column(
        "matchmaking_pools",
        sa.Column("rating_search_radius", sa.Integer(), nullable=False, server_default="20"),
    )
    op.add_column(
        "matchmaking_pools",
        sa.Column("rating_search_radius_max", sa.Integer(), nullable=False, server_default="9999"),
    )
    op.add_column(
        "matchmaking_pools",
        sa.Column("rating_search_radius_exp", sa.Integer(), nullable=False, server_default="15"),
    )

    # --- matchmaking_user_stats: per-(user, pool) Elo + activity ---
    op.add_column(
        "matchmaking_user_stats",
        sa.Column("rating", sa.Integer(), nullable=False, server_default="1500"),
    )
    op.add_column(
        "matchmaking_user_stats",
        sa.Column("plays", sa.Integer(), nullable=False, server_default="0"),
    )
    # Spectator does `WHERE pool_id = ? AND plays > 0` for rating-distribution
    # — the existing pool_id index is composite (with first_placements /
    # total_points), neither of which is `plays`. Add a covering index so the
    # MatchmakingQueueBackgroundService startup query stays cheap.
    op.create_index(
        "matchmaking_user_stats_pool_plays_idx",
        "matchmaking_user_stats",
        ["pool_id", "plays"],
        unique=False,
    )

    # --- matchmaking_pool_beatmaps: rating_sig (OpenSkill σ) ---
    op.add_column(
        "matchmaking_pool_beatmaps",
        sa.Column("rating_sig", sa.Float(), nullable=False, server_default="150"),
    )

    # --- matchmaking_user_elo_history: per-match audit trail ---
    # Spectator INSERTs (no UPDATE / DELETE expected) at end-of-match per
    # (winner, loser) pair. Used for rolling-back broken matches and for
    # building per-user elo graphs. No FK to lazer_users on (user_id, opponent_id)
    # to keep INSERT throughput high under burst load — soft references only.
    op.create_table(
        "matchmaking_user_elo_history",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("room_id", sa.BigInteger(), nullable=False),
        sa.Column("pool_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("opponent_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "result",
            mysql.ENUM("win", "loss", "draw", name="matchmakingroomresult"),
            nullable=False,
        ),
        sa.Column("elo_before", sa.Integer(), nullable=False),
        sa.Column("elo_after", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["pool_id"], ["matchmaking_pools.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "matchmaking_user_elo_history_user_pool_idx",
        "matchmaking_user_elo_history",
        ["user_id", "pool_id", "id"],  # last id col makes this a primary clustering key for "latest N"
        unique=False,
    )
    op.create_index(
        "matchmaking_user_elo_history_room_idx",
        "matchmaking_user_elo_history",
        ["room_id"],
        unique=False,
    )

    # --- matchmaking_room_events: matchmaking-mode LogRoomEventAsync sink ---
    # Parallel to `multiplayer_events` for non-matchmaking rooms. Distinct
    # table so per-mode retention/analytics queries don't have to filter on
    # rooms.type all the time (matchmaking rooms churn at a much higher rate
    # than friend-list multi rooms).
    op.create_table(
        "matchmaking_room_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("room_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("playlist_item_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("event_detail", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "matchmaking_room_events_room_type_idx",
        "matchmaking_room_events",
        ["room_id", "event_type"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("matchmaking_room_events_room_type_idx", table_name="matchmaking_room_events")
    op.drop_table("matchmaking_room_events")

    op.drop_index("matchmaking_user_elo_history_room_idx", table_name="matchmaking_user_elo_history")
    op.drop_index("matchmaking_user_elo_history_user_pool_idx", table_name="matchmaking_user_elo_history")
    op.drop_table("matchmaking_user_elo_history")

    op.drop_column("matchmaking_pool_beatmaps", "rating_sig")

    op.drop_index("matchmaking_user_stats_pool_plays_idx", table_name="matchmaking_user_stats")
    op.drop_column("matchmaking_user_stats", "plays")
    op.drop_column("matchmaking_user_stats", "rating")

    op.drop_column("matchmaking_pools", "rating_search_radius_exp")
    op.drop_column("matchmaking_pools", "rating_search_radius_max")
    op.drop_column("matchmaking_pools", "rating_search_radius")
    op.drop_column("matchmaking_pools", "lobby_size")
    op.drop_column("matchmaking_pools", "type")
