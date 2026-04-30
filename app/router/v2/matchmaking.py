"""Matchmaking HTTP API.

The realtime matchmaking flow lives entirely on the spectator's SignalR
hubs (queue/lobby/match are pushed to the client over websocket). What
the *website* and *profile UI* need is a flat HTTP read surface so they
can render leaderboards, profile rating badges, recent-match history,
and the lobby picker.

This router exposes only those read paths plus a tiny admin mutation
for toggling pool active state. Mutations to the live tables
(`matchmaking_user_stats`, `matchmaking_user_elo_history`,
`matchmaking_pool_beatmaps.rating`) belong to the spectator — having
the website touch them would race against the queue background
service.
"""

from datetime import datetime
from typing import Annotated, Any

from app.database.matchmaking import (
    MatchmakingPool,
    MatchmakingPoolType,
    MatchmakingRoomResult,
    MatchmakingUserEloHistory,
    MatchmakingUserStats,
)
from app.database.user import User, UserModel
from app.dependencies.database import Database
from app.dependencies.user import get_current_user, get_optional_user

from .router import router

from fastapi import HTTPException, Path, Query, Security, status
from pydantic import BaseModel, Field
from sqlmodel import col, desc, select


# ───────────────────────────── response models ─────────────────────────────


class MatchmakingPoolResponse(BaseModel):
    id: int
    ruleset_id: int
    name: str
    type: MatchmakingPoolType
    active: bool
    lobby_size: int
    rating_search_radius: int
    rating_search_radius_max: int
    rating_search_radius_exp: int


class MatchmakingLeaderboardEntry(BaseModel):
    user_id: int
    rating: int
    plays: int
    first_placements: int
    total_points: int
    rank: int = Field(description="1-based position within the pool's ranked-by-rating order")
    user: dict[str, Any] | None = Field(default=None, description="Lightweight user payload (id, username, avatar)")


class MatchmakingUserPoolStatsResponse(BaseModel):
    user_id: int
    pool_id: int
    rating: int
    plays: int
    first_placements: int
    total_points: int
    rank: int | None = Field(default=None, description="1-based rank within the pool, null if user has plays=0")
    updated_at: datetime | None


class MatchmakingHistoryEntry(BaseModel):
    id: int
    room_id: int
    pool_id: int
    user_id: int
    opponent_id: int
    result: MatchmakingRoomResult
    elo_before: int
    elo_after: int
    elo_delta: int = Field(description="Convenience: elo_after - elo_before")
    created_at: datetime


# ─────────────────────────── lightweight user hydration ────────────────────


async def _hydrate_users_minimal(session: Database, user_ids: set[int]) -> dict[int, dict[str, Any]]:
    """Fetch (id, username, avatar) tuples for the given user ids.

    Kept minimal because matchmaking leaderboards are paginated to ~50
    rows per request and we don't want to drag the full user-profile
    transformer pipeline (which loads stats, badges, rank history, …).
    """
    if not user_ids:
        return {}
    rows = (
        await session.exec(
            select(User.id, User.username, User.avatar_url).where(col(User.id).in_(user_ids))
        )
    ).all()
    return {
        uid: {
            "id": uid,
            "username": uname or "",
            "avatar_url": avatar_url or UserModel.DEFAULT_AVATAR_URL,
        }
        for uid, uname, avatar_url in rows
    }


# ─────────────────────────────── endpoints ─────────────────────────────────


@router.get(
    "/matchmaking/pools",
    response_model=list[MatchmakingPoolResponse],
    description="List matchmaking pools the user can queue into. By default returns only active pools.",
    tags=["Matchmaking"],
)
async def list_matchmaking_pools(
    db: Database,
    include_inactive: Annotated[
        bool,
        Query(description="Include pools where active=0 (admin clients use this for management)."),
    ] = False,
    ruleset_id: Annotated[
        int | None,
        Query(description="Filter to a single ruleset (0=osu!, 1=taiko, 2=catch, 3=mania)."),
    ] = None,
    type: Annotated[
        MatchmakingPoolType | None,
        Query(description="Filter by pool type (quick_play / ranked_play)."),
    ] = None,
) -> list[MatchmakingPoolResponse]:
    stmt = select(MatchmakingPool)
    if not include_inactive:
        stmt = stmt.where(col(MatchmakingPool.active).is_(True))
    if ruleset_id is not None:
        stmt = stmt.where(MatchmakingPool.ruleset_id == ruleset_id)
    if type is not None:
        stmt = stmt.where(MatchmakingPool.type == type)
    stmt = stmt.order_by(MatchmakingPool.ruleset_id, MatchmakingPool.id)

    rows = (await db.exec(stmt)).all()
    return [MatchmakingPoolResponse.model_validate(r, from_attributes=True) for r in rows]


@router.get(
    "/matchmaking/pools/{pool_id}/leaderboard",
    response_model=list[MatchmakingLeaderboardEntry],
    description="Top users in a pool ranked by rating. Only counts users with plays > 0.",
    tags=["Matchmaking"],
)
async def get_pool_leaderboard(
    db: Database,
    pool_id: Annotated[int, Path(description="Pool id")],
    cursor: Annotated[
        int,
        Query(ge=0, description="Skip the first N rows (use for pagination)."),
    ] = 0,
    limit: Annotated[
        int,
        Query(ge=1, le=200, description="Number of rows to return (1-200)."),
    ] = 50,
) -> list[MatchmakingLeaderboardEntry]:
    pool = await db.get(MatchmakingPool, pool_id)
    if pool is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Pool {pool_id} not found")

    rows = (
        await db.exec(
            select(MatchmakingUserStats)
            .where(MatchmakingUserStats.pool_id == pool_id)
            .where(MatchmakingUserStats.plays > 0)
            .order_by(desc(MatchmakingUserStats.rating), MatchmakingUserStats.user_id)
            .offset(cursor)
            .limit(limit)
        )
    ).all()

    user_payloads = await _hydrate_users_minimal(db, {r.user_id for r in rows})

    return [
        MatchmakingLeaderboardEntry(
            user_id=r.user_id,
            rating=r.rating,
            plays=r.plays,
            first_placements=r.first_placements,
            total_points=r.total_points,
            rank=cursor + i + 1,
            user=user_payloads.get(r.user_id),
        )
        for i, r in enumerate(rows)
    ]


@router.get(
    "/users/{user_id}/matchmaking/stats",
    response_model=list[MatchmakingUserPoolStatsResponse],
    description="Per-pool matchmaking stats for a user. Returns one entry per pool the user has touched.",
    tags=["Matchmaking"],
)
async def get_user_matchmaking_stats(
    db: Database,
    user_id: Annotated[int, Path(description="Target user id")],
    pool_id: Annotated[
        int | None,
        Query(description="Optional pool filter; if omitted returns all pools the user has stats in."),
    ] = None,
) -> list[MatchmakingUserPoolStatsResponse]:
    stmt = select(MatchmakingUserStats).where(MatchmakingUserStats.user_id == user_id)
    if pool_id is not None:
        stmt = stmt.where(MatchmakingUserStats.pool_id == pool_id)
    rows = (await db.exec(stmt)).all()

    out: list[MatchmakingUserPoolStatsResponse] = []
    for r in rows:
        # The user's rank inside the pool: count how many other rows in
        # the same pool have a strictly higher rating, +1. plays=0 rows
        # are skipped from the ranking entirely (matches the leaderboard
        # filter so a "top 50" view + "your rank #N" stay in sync).
        rank_value: int | None = None
        if r.plays > 0:
            count_higher = (
                await db.exec(
                    select(MatchmakingUserStats)
                    .where(MatchmakingUserStats.pool_id == r.pool_id)
                    .where(MatchmakingUserStats.plays > 0)
                    .where(MatchmakingUserStats.rating > r.rating)
                )
            ).all()
            rank_value = len(count_higher) + 1

        out.append(
            MatchmakingUserPoolStatsResponse(
                user_id=r.user_id,
                pool_id=r.pool_id,
                rating=r.rating,
                plays=r.plays,
                first_placements=r.first_placements,
                total_points=r.total_points,
                rank=rank_value,
                updated_at=r.updated_at,
            )
        )
    return out


@router.get(
    "/users/{user_id}/matchmaking/history",
    response_model=list[MatchmakingHistoryEntry],
    description="Most recent matchmaking results for a user (across all pools by default).",
    tags=["Matchmaking"],
)
async def get_user_matchmaking_history(
    db: Database,
    user_id: Annotated[int, Path(description="Target user id")],
    pool_id: Annotated[int | None, Query(description="Restrict to one pool.")] = None,
    cursor: Annotated[int, Query(ge=0, description="Skip the first N rows.")] = 0,
    limit: Annotated[int, Query(ge=1, le=200, description="Page size (1-200).")] = 50,
) -> list[MatchmakingHistoryEntry]:
    stmt = select(MatchmakingUserEloHistory).where(MatchmakingUserEloHistory.user_id == user_id)
    if pool_id is not None:
        stmt = stmt.where(MatchmakingUserEloHistory.pool_id == pool_id)
    # Clustered (user_id, pool_id, id) index makes this a contiguous
    # range scan; ORDER BY id DESC == "newest first".
    stmt = stmt.order_by(desc(MatchmakingUserEloHistory.id)).offset(cursor).limit(limit)

    rows = (await db.exec(stmt)).all()
    return [
        MatchmakingHistoryEntry(
            id=r.id or 0,
            room_id=r.room_id,
            pool_id=r.pool_id,
            user_id=r.user_id,
            opponent_id=r.opponent_id,
            result=r.result,
            elo_before=r.elo_before,
            elo_after=r.elo_after,
            elo_delta=r.elo_after - r.elo_before,
            created_at=r.created_at or datetime.utcnow(),
        )
        for r in rows
    ]


# ─────────────────────────── admin (active toggle) ─────────────────────────


class MatchmakingPoolActivePatch(BaseModel):
    active: bool


@router.patch(
    "/matchmaking/pools/{pool_id}",
    response_model=MatchmakingPoolResponse,
    description=(
        "Admin-only: flip a pool's active flag. Activating an inactive pool makes it visible to "
        "the spectator's queue background service on its next refresh tick (~5 seconds)."
    ),
    tags=["Matchmaking"],
)
async def patch_matchmaking_pool(
    db: Database,
    pool_id: Annotated[int, Path(description="Pool id")],
    patch: MatchmakingPoolActivePatch,
    current_user: Annotated[User, Security(get_current_user, scopes=["*"])],
) -> MatchmakingPoolResponse:
    # Privilege gate. `priv` is g0v0's bitmask + magic-int field; admin
    # is the only privilege that should be touching pool config.
    if not getattr(current_user, "is_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can toggle matchmaking pools.",
        )

    pool = await db.get(MatchmakingPool, pool_id)
    if pool is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Pool {pool_id} not found")

    pool.active = patch.active
    db.add(pool)
    await db.commit()
    await db.refresh(pool)

    return MatchmakingPoolResponse.model_validate(pool, from_attributes=True)
