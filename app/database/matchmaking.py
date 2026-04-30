from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from app.models.model import UTCBaseModel
from app.models.mods import APIMod

from sqlalchemy import Column, DateTime, Enum as SAEnum, Float, ForeignKey, Index, SmallInteger, Text
from sqlmodel import (
    JSON,
    BigInteger,
    Field,
    Relationship,
    SQLModel,
    func,
)

if TYPE_CHECKING:
    from .beatmap import Beatmap
    from .user import User


class MatchmakingPoolType(str, Enum):
    """Mirrors the spectator C# `matchmaking_pool_type`. The pool's `type`
    column drives the controller branch in `ServerMultiplayerRoom`:
    `quick_play` → `MatchmakingMatchController`, `ranked_play` →
    `RankedPlayMatchController`. MySQL ENUM stores the canonical lower-case
    form; reads round-trip case-insensitively so the C# enum reader matches.
    """

    QUICK_PLAY = "quick_play"
    RANKED_PLAY = "ranked_play"


class MatchmakingRoomResult(str, Enum):
    """Per-side outcome enum recorded into `matchmaking_user_elo_history` at
    end of every match. Spectator writes one row per (winner, loser) pair."""

    WIN = "win"
    LOSS = "loss"
    DRAW = "draw"


class MatchmakingUserStatsBase(SQLModel, UTCBaseModel):
    user_id: int = Field(
        default=None,
        sa_column=Column(BigInteger, ForeignKey("lazer_users.id"), primary_key=True),
    )
    pool_id: int = Field(
        default=None,
        sa_column=Column(ForeignKey("matchmaking_pools.id"), primary_key=True, nullable=True),
    )
    first_placements: int = Field(default=0, ge=0)
    total_points: int = Field(default=0, ge=0)
    # Torii: the spectator's MatchmakingQueueBackgroundService reads `rating`
    # to seed the per-pool rating-distribution buckets and bumps `plays` once
    # per finished match. `rating` is the OpenSkill mu (μ) we expose; the
    # variance (σ) is kept inside `elo_data` JSON so we don't have to touch
    # this row on every micro-update.
    rating: int = Field(default=1500)
    plays: int = Field(default=0, ge=0)
    elo_data: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), server_default=func.now()),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now()),
    )


class MatchmakingUserStats(MatchmakingUserStatsBase, table=True):
    __tablename__: str = "matchmaking_user_stats"
    __table_args__ = (
        Index("matchmaking_user_stats_pool_first_idx", "pool_id", "first_placements"),
        Index("matchmaking_user_stats_pool_points_idx", "pool_id", "total_points"),
        # Covers the spectator's `WHERE pool_id = ? AND plays > 0` rating
        # distribution query at queue-service startup.
        Index("matchmaking_user_stats_pool_plays_idx", "pool_id", "plays"),
    )

    user: "User" = Relationship(back_populates="matchmaking_stats", sa_relationship_kwargs={"lazy": "joined"})
    pool: "MatchmakingPool" = Relationship()


class MatchmakingPoolBase(SQLModel, UTCBaseModel):
    id: int | None = Field(default=None, primary_key=True)
    ruleset_id: int = Field(
        default=0,
        sa_column=Column(SmallInteger, nullable=False),
    )
    name: str = Field(max_length=255)
    # Admin-editable blurb shown on the public ranking page under the pool
    # name. Kept TEXT (no length cap) so admins can drop a paragraph,
    # link to an event, or paste tournament rules without fighting a
    # tight character limit.
    description: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    active: bool = Field(default=True)
    # Discriminator the spectator uses to pick MatchmakingMatchController vs
    # RankedPlayMatchController. Defaults to quick_play so existing rows
    # keep behaving like the original quick-play pools when this column was
    # introduced.
    type: MatchmakingPoolType = Field(
        default=MatchmakingPoolType.QUICK_PLAY,
        sa_column=Column(
            SAEnum(MatchmakingPoolType, name="matchmakingpooltype", values_callable=lambda x: [e.value for e in x]),
            nullable=False,
            server_default=MatchmakingPoolType.QUICK_PLAY.value,
        ),
    )
    lobby_size: int = Field(default=8)
    # Initial rating window the queue searches against; doubles every
    # `rating_search_radius_exp` seconds until it hits the max.
    rating_search_radius: int = Field(default=20)
    rating_search_radius_max: int = Field(default=9999)
    rating_search_radius_exp: int = Field(default=15)
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), server_default=func.now()),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now()),
    )


class MatchmakingPool(MatchmakingPoolBase, table=True):
    __tablename__: str = "matchmaking_pools"
    __table_args__ = (Index("matchmaking_pools_ruleset_active_idx", "ruleset_id", "active"),)

    beatmaps: list["MatchmakingPoolBeatmap"] = Relationship(
        back_populates="pool",
        # sa_relationship_kwargs={
        #     "lazy": "selectin",
        # },
    )


class MatchmakingPoolBeatmapBase(SQLModel, UTCBaseModel):
    id: int | None = Field(default=None, primary_key=True)
    pool_id: int = Field(
        default=None,
        sa_column=Column(ForeignKey("matchmaking_pools.id"), nullable=False, index=True),
    )
    beatmap_id: int = Field(
        default=None,
        sa_column=Column(ForeignKey("beatmaps.id"), nullable=False),
    )
    mods: list[APIMod] | None = Field(default=None, sa_column=Column(JSON))
    rating: int | None = Field(default=1500)
    # OpenSkill σ — pair to `rating` (μ). Spectator INSERT/UPSERTs both on
    # every rating update via `UpdateMatchmakingPoolBeatmapRatingAsync`.
    rating_sig: float = Field(default=150.0, sa_column=Column(Float, nullable=False, server_default="150"))
    selection_count: int = Field(default=0)


class MatchmakingPoolBeatmap(MatchmakingPoolBeatmapBase, table=True):
    __tablename__: str = "matchmaking_pool_beatmaps"

    pool: MatchmakingPool = Relationship(back_populates="beatmaps")
    beatmap: Optional["Beatmap"] = Relationship(
        # sa_relationship_kwargs={"lazy": "joined"},
    )


class MatchmakingUserEloHistoryBase(SQLModel, UTCBaseModel):
    """Per-match audit trail. Append-only; spectator INSERTs one row per
    (winner, loser) pair after every finalised match. Used for rolling-back
    broken matches and for plotting a user's elo curve over time."""

    id: int | None = Field(default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True))
    room_id: int = Field(sa_column=Column(BigInteger, nullable=False))
    pool_id: int = Field(
        sa_column=Column(ForeignKey("matchmaking_pools.id"), nullable=False),
    )
    user_id: int = Field(sa_column=Column(BigInteger, nullable=False))
    opponent_id: int = Field(sa_column=Column(BigInteger, nullable=False))
    result: MatchmakingRoomResult = Field(
        sa_column=Column(
            SAEnum(MatchmakingRoomResult, name="matchmakingroomresult", values_callable=lambda x: [e.value for e in x]),
            nullable=False,
        ),
    )
    elo_before: int = Field()
    elo_after: int = Field()
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False),
    )


class MatchmakingUserEloHistory(MatchmakingUserEloHistoryBase, table=True):
    __tablename__: str = "matchmaking_user_elo_history"
    __table_args__ = (
        # `(user_id, pool_id, id)` clusters rows so "latest N for user in pool"
        # is a contiguous index range scan.
        Index("matchmaking_user_elo_history_user_pool_idx", "user_id", "pool_id", "id"),
        Index("matchmaking_user_elo_history_room_idx", "room_id"),
    )


class MatchmakingRoomEventBase(SQLModel, UTCBaseModel):
    """Matchmaking-mode parallel of `multiplayer_events`. Distinct table so
    per-mode retention/analytics queries don't have to filter on `rooms.type`
    on every read — matchmaking rooms churn at much higher rate than
    friend-list multi rooms."""

    id: int | None = Field(default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True))
    room_id: int = Field(sa_column=Column(BigInteger, nullable=False))
    event_type: str = Field(max_length=64)
    playlist_item_id: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    user_id: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    event_detail: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False),
    )


class MatchmakingRoomEvent(MatchmakingRoomEventBase, table=True):
    __tablename__: str = "matchmaking_room_events"
    __table_args__ = (Index("matchmaking_room_events_room_type_idx", "room_id", "event_type"),)
