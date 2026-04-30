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

from app.database.beatmap import Beatmap
from app.database.matchmaking import (
    MatchmakingPool,
    MatchmakingPoolBeatmap,
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
from sqlmodel import col, desc, func, select


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


# ───────────────────── admin (full pool + beatmap CRUD) ────────────────────


def _require_admin(current_user: User) -> None:
    """Privilege gate shared by every mutation in the admin section."""
    if not getattr(current_user, "is_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can mutate matchmaking pools.",
        )


class MatchmakingPoolCreate(BaseModel):
    """Request body for POST /matchmaking/pools."""

    ruleset_id: int = Field(ge=0, le=7, description="0=osu, 1=taiko, 2=catch, 3=mania, 4-7=RX/AP variants.")
    name: str = Field(min_length=1, max_length=255)
    type: MatchmakingPoolType = MatchmakingPoolType.QUICK_PLAY
    active: bool = False
    lobby_size: int = Field(default=8, ge=2, le=64)
    rating_search_radius: int = Field(default=200, ge=10, le=9999)
    rating_search_radius_max: int = Field(default=9999, ge=10, le=9999)
    rating_search_radius_exp: int = Field(default=15, ge=1, le=600)


class MatchmakingPoolUpdate(BaseModel):
    """Request body for PUT /matchmaking/pools/{id}.

    All fields are optional; only fields actually provided are written.
    Use this for the in-place edit form on the admin UI.
    """

    name: str | None = Field(default=None, min_length=1, max_length=255)
    type: MatchmakingPoolType | None = None
    active: bool | None = None
    lobby_size: int | None = Field(default=None, ge=2, le=64)
    rating_search_radius: int | None = Field(default=None, ge=10, le=9999)
    rating_search_radius_max: int | None = Field(default=None, ge=10, le=9999)
    rating_search_radius_exp: int | None = Field(default=None, ge=1, le=600)


class MatchmakingPoolBeatmapResponse(BaseModel):
    """Single beatmap row inside a pool, joined with the parent
    `beatmaps` table so the admin UI can show the title without a
    second round trip."""

    id: int
    pool_id: int
    beatmap_id: int
    rating: int
    rating_sig: float
    selection_count: int
    # Light beatmap snippet (only what the admin UI shows in a row).
    mode: str | None = None
    version: str | None = None
    artist: str | None = None
    title: str | None = None
    difficulty_rating: float | None = None
    total_length: int | None = None


class BulkBeatmapAddRequest(BaseModel):
    """Pool seed-by-IDs request. Admins paste a textarea full of beatmap
    ids (newline / comma / space separated) into the UI; the frontend
    parses them client-side and POSTs the list here."""

    beatmap_ids: list[int] = Field(min_length=1, max_length=500)
    initial_rating: int = Field(default=1500, ge=0, le=5000)
    initial_rating_sig: float = Field(default=150.0, ge=1, le=1000)


class BulkBeatmapAddResponse(BaseModel):
    added: list[int] = Field(description="Beatmap ids that landed in the pool.")
    skipped_already_in_pool: list[int] = Field(default_factory=list)
    skipped_not_found: list[int] = Field(default_factory=list)
    skipped_wrong_mode: list[int] = Field(
        default_factory=list,
        description=(
            "Beatmap exists but its mode doesn't match the pool's ruleset "
            "(e.g. mania map being added to an osu pool). Skipped to keep "
            "the pool consistent — the queue's rating distribution is "
            "per-mode."
        ),
    )


@router.post(
    "/matchmaking/pools",
    response_model=MatchmakingPoolResponse,
    status_code=status.HTTP_201_CREATED,
    description="Admin-only: create a new matchmaking pool.",
    tags=["Matchmaking"],
)
async def create_matchmaking_pool(
    db: Database,
    payload: MatchmakingPoolCreate,
    current_user: Annotated[User, Security(get_current_user, scopes=["*"])],
) -> MatchmakingPoolResponse:
    _require_admin(current_user)

    pool = MatchmakingPool(**payload.model_dump())
    db.add(pool)
    await db.commit()
    await db.refresh(pool)
    return MatchmakingPoolResponse.model_validate(pool, from_attributes=True)


@router.put(
    "/matchmaking/pools/{pool_id}",
    response_model=MatchmakingPoolResponse,
    description="Admin-only: edit pool config in-place. Pass any subset of fields.",
    tags=["Matchmaking"],
)
async def update_matchmaking_pool(
    db: Database,
    pool_id: Annotated[int, Path(description="Pool id")],
    payload: MatchmakingPoolUpdate,
    current_user: Annotated[User, Security(get_current_user, scopes=["*"])],
) -> MatchmakingPoolResponse:
    _require_admin(current_user)

    pool = await db.get(MatchmakingPool, pool_id)
    if pool is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Pool {pool_id} not found")

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(pool, key, value)
    db.add(pool)
    await db.commit()
    await db.refresh(pool)
    return MatchmakingPoolResponse.model_validate(pool, from_attributes=True)


@router.delete(
    "/matchmaking/pools/{pool_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    description=(
        "Admin-only: delete a pool. Cascades to its `matchmaking_pool_beatmaps` "
        "rows. Refuses to delete a pool that has user_stats / elo_history "
        "associated with it (tells the operator to deactivate it instead) "
        "so the audit trail stays intact."
    ),
    tags=["Matchmaking"],
)
async def delete_matchmaking_pool(
    db: Database,
    pool_id: Annotated[int, Path(description="Pool id")],
    current_user: Annotated[User, Security(get_current_user, scopes=["*"])],
) -> None:
    _require_admin(current_user)

    pool = await db.get(MatchmakingPool, pool_id)
    if pool is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Pool {pool_id} not found")

    # Block deletes when there's history. We don't want to drop elo
    # rows silently — that would corrupt rolling rank graphs.
    history_count = (
        await db.exec(
            select(func.count())
            .select_from(MatchmakingUserEloHistory)
            .where(MatchmakingUserEloHistory.pool_id == pool_id)
        )
    ).one()
    stats_count = (
        await db.exec(
            select(func.count())
            .select_from(MatchmakingUserStats)
            .where(MatchmakingUserStats.pool_id == pool_id)
        )
    ).one()
    if (history_count or 0) > 0 or (stats_count or 0) > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Pool {pool_id} has {history_count} elo-history rows + {stats_count} "
                "user-stats rows. Deactivate it instead (PATCH active=false)."
            ),
        )

    # Drop the beatmap rows first (no FK from beatmaps→pool keeps this trivial).
    pool_beatmaps = (
        await db.exec(select(MatchmakingPoolBeatmap).where(MatchmakingPoolBeatmap.pool_id == pool_id))
    ).all()
    for pb in pool_beatmaps:
        await db.delete(pb)
    await db.delete(pool)
    await db.commit()


@router.get(
    "/matchmaking/pools/{pool_id}/beatmaps",
    response_model=list[MatchmakingPoolBeatmapResponse],
    description="List the beatmaps currently in a pool (paginated).",
    tags=["Matchmaking"],
)
async def list_matchmaking_pool_beatmaps(
    db: Database,
    pool_id: Annotated[int, Path(description="Pool id")],
    cursor: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[MatchmakingPoolBeatmapResponse]:
    pool = await db.get(MatchmakingPool, pool_id)
    if pool is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Pool {pool_id} not found")

    rows = (
        await db.exec(
            select(MatchmakingPoolBeatmap)
            .where(MatchmakingPoolBeatmap.pool_id == pool_id)
            .order_by(MatchmakingPoolBeatmap.id)
            .offset(cursor)
            .limit(limit)
        )
    ).all()

    if not rows:
        return []

    # Single round-trip to fetch every parent beatmap (and beatmapset for
    # artist/title). Manual join to avoid the relationship's lazy-loader
    # which would issue 60 individual SELECTs from a hot path.
    bids = list({r.beatmap_id for r in rows})
    beatmap_lookup: dict[int, dict[str, Any]] = {}
    if bids:
        bm_rows = (
            await db.exec(
                select(
                    Beatmap.id,
                    Beatmap.mode,
                    Beatmap.version,
                    Beatmap.difficulty_rating,
                    Beatmap.total_length,
                    Beatmap.beatmapset_id,
                ).where(col(Beatmap.id).in_(bids))
            )
        ).all()
        # Beatmapset for artist/title — fetched cheaply by id.
        from app.database.beatmap import Beatmapset

        set_ids = list({r[5] for r in bm_rows})
        set_lookup: dict[int, tuple[str, str]] = {}
        if set_ids:
            set_rows = (
                await db.exec(
                    select(Beatmapset.id, Beatmapset.artist, Beatmapset.title).where(col(Beatmapset.id).in_(set_ids))
                )
            ).all()
            set_lookup = {sid: (artist or "", title or "") for sid, artist, title in set_rows}

        for bid, mode, version, diff, length, set_id in bm_rows:
            artist, title = set_lookup.get(set_id, ("", ""))
            beatmap_lookup[bid] = {
                "mode": str(mode) if mode is not None else None,
                "version": version,
                "difficulty_rating": float(diff) if diff is not None else None,
                "total_length": int(length) if length is not None else None,
                "artist": artist,
                "title": title,
            }

    return [
        MatchmakingPoolBeatmapResponse(
            id=r.id or 0,
            pool_id=r.pool_id,
            beatmap_id=r.beatmap_id,
            rating=int(r.rating or 1500),
            rating_sig=float(r.rating_sig or 150.0),
            selection_count=r.selection_count,
            **(beatmap_lookup.get(r.beatmap_id, {})),
        )
        for r in rows
    ]


# Map ruleset id -> the canonical lower-case `mode` value g0v0 stores.
_RULESET_TO_MODE: dict[int, str] = {
    0: "osu",
    1: "taiko",
    2: "fruits",
    3: "mania",
    4: "osurx",
    5: "osuap",
    6: "taikorx",
    7: "fruitsrx",
}


@router.post(
    "/matchmaking/pools/{pool_id}/beatmaps",
    response_model=BulkBeatmapAddResponse,
    description=(
        "Admin-only: bulk-add beatmaps to a pool by id. Skips duplicates, "
        "missing beatmaps, and beatmaps whose mode doesn't match the pool's "
        "ruleset — the response itemises each skip reason so the admin UI "
        "can surface a concise validation summary."
    ),
    tags=["Matchmaking"],
)
async def bulk_add_pool_beatmaps(
    db: Database,
    pool_id: Annotated[int, Path(description="Pool id")],
    payload: BulkBeatmapAddRequest,
    current_user: Annotated[User, Security(get_current_user, scopes=["*"])],
) -> BulkBeatmapAddResponse:
    _require_admin(current_user)

    pool = await db.get(MatchmakingPool, pool_id)
    if pool is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Pool {pool_id} not found")

    requested = list(dict.fromkeys(payload.beatmap_ids))  # de-dupe, preserve order

    # Existing beatmaps in the pool — used to skip dup adds without
    # incurring an INSERT-then-rollback cost.
    existing_in_pool = {
        bid
        for bid, in (
            await db.exec(
                select(MatchmakingPoolBeatmap.beatmap_id).where(MatchmakingPoolBeatmap.pool_id == pool_id)
            )
        ).all()
    }

    # Validate each requested id against the beatmaps table in one shot.
    valid_rows = (
        await db.exec(
            select(Beatmap.id, Beatmap.mode).where(col(Beatmap.id).in_(requested))
        )
    ).all()
    valid_lookup: dict[int, str] = {bid: str(mode) for bid, mode in valid_rows}

    pool_mode = _RULESET_TO_MODE.get(pool.ruleset_id, "osu")

    added: list[int] = []
    skipped_dup: list[int] = []
    skipped_missing: list[int] = []
    skipped_wrong_mode: list[int] = []

    for bid in requested:
        if bid in existing_in_pool:
            skipped_dup.append(bid)
            continue
        bmap_mode = valid_lookup.get(bid)
        if bmap_mode is None:
            skipped_missing.append(bid)
            continue
        # Mode mismatch — keep the pool homogeneous.
        if bmap_mode.lower() != pool_mode:
            skipped_wrong_mode.append(bid)
            continue

        db.add(
            MatchmakingPoolBeatmap(
                pool_id=pool_id,
                beatmap_id=bid,
                mods=[],
                rating=payload.initial_rating,
                rating_sig=payload.initial_rating_sig,
                selection_count=0,
            )
        )
        added.append(bid)

    if added:
        await db.commit()

    return BulkBeatmapAddResponse(
        added=added,
        skipped_already_in_pool=skipped_dup,
        skipped_not_found=skipped_missing,
        skipped_wrong_mode=skipped_wrong_mode,
    )


@router.delete(
    "/matchmaking/pools/{pool_id}/beatmaps/{beatmap_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    description="Admin-only: remove a beatmap from a pool.",
    tags=["Matchmaking"],
)
async def delete_pool_beatmap(
    db: Database,
    pool_id: Annotated[int, Path(description="Pool id")],
    beatmap_id: Annotated[int, Path(description="Beatmap id to remove from this pool")],
    current_user: Annotated[User, Security(get_current_user, scopes=["*"])],
) -> None:
    _require_admin(current_user)

    row = (
        await db.exec(
            select(MatchmakingPoolBeatmap)
            .where(MatchmakingPoolBeatmap.pool_id == pool_id)
            .where(MatchmakingPoolBeatmap.beatmap_id == beatmap_id)
        )
    ).first()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Beatmap {beatmap_id} is not in pool {pool_id}",
        )

    await db.delete(row)
    await db.commit()


class BulkBeatmapsetAddRequest(BaseModel):
    """Pool seed-by-mapset-IDs.

    Admin pastes mapset ids (the number after `/beatmapsets/` on the
    osu.ppy.sh URL) and we expand each one into all its difficulties
    that match the pool's mode AND fit a difficulty / length window.
    Saves the operator from copying every individual diff id.
    """

    beatmapset_ids: list[int] = Field(min_length=1, max_length=200)
    initial_rating: int = Field(default=1500, ge=0, le=5000)
    initial_rating_sig: float = Field(default=150.0, ge=1, le=1000)
    # Optional difficulty window — defaults to a sensible "intermediate"
    # cut so an admin can paste a "best of" beatmapset list and not get
    # 8★ insanes flooded into a 3★ quick-play pool.
    min_sr: float = Field(default=2.5, ge=0.0, le=15.0)
    max_sr: float = Field(default=6.5, ge=0.0, le=15.0)
    min_length_seconds: int = Field(default=60, ge=1, le=3600)
    max_length_seconds: int = Field(default=300, ge=1, le=3600)


class BulkBeatmapsetAddResponse(BaseModel):
    """Per-mapset breakdown of what landed in the pool.

    `added` is a flat list of beatmap ids (the actual rows that got
    written), to make it trivial for the admin UI to refresh the
    rotation table without round-tripping the pool.
    """

    added: list[int] = Field(description="Beatmap ids that landed in the pool.")
    skipped_already_in_pool: list[int] = Field(default_factory=list)
    skipped_outside_window: list[int] = Field(
        default_factory=list,
        description=(
            "Beatmaps that exist in the set but fall outside the requested "
            "SR / length window. Surface these so the operator can widen "
            "the window if they wanted everything."
        ),
    )
    skipped_wrong_mode: list[int] = Field(default_factory=list)
    mapsets_not_found: list[int] = Field(
        default_factory=list,
        description=(
            "Mapset ids that resolved zero rows — either the set isn't on "
            "this server's beatmaps cache yet, or every diff in it was a "
            "different mode. The admin UI surfaces these for re-input."
        ),
    )


@router.post(
    "/matchmaking/pools/{pool_id}/beatmapsets",
    response_model=BulkBeatmapsetAddResponse,
    description=(
        "Admin-only: bulk-add every difficulty from each mapset that fits "
        "the pool's mode and the request's SR / length window. The "
        "common operator workflow is: copy mapset ids from osu.ppy.sh "
        "search results, paste them, leave the defaults, click add."
    ),
    tags=["Matchmaking"],
)
async def bulk_add_pool_beatmapsets(
    db: Database,
    pool_id: Annotated[int, Path(description="Pool id")],
    payload: BulkBeatmapsetAddRequest,
    current_user: Annotated[User, Security(get_current_user, scopes=["*"])],
) -> BulkBeatmapsetAddResponse:
    _require_admin(current_user)

    pool = await db.get(MatchmakingPool, pool_id)
    if pool is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Pool {pool_id} not found")

    # De-dupe mapset ids preserving order.
    requested = list(dict.fromkeys(payload.beatmapset_ids))
    pool_mode = _RULESET_TO_MODE.get(pool.ruleset_id, "osu")

    # Existing beatmaps in pool — used to skip duplicates without an
    # INSERT-rollback round-trip.
    existing_in_pool = {
        bid
        for bid, in (
            await db.exec(
                select(MatchmakingPoolBeatmap.beatmap_id).where(MatchmakingPoolBeatmap.pool_id == pool_id)
            )
        ).all()
    }

    # Resolve ALL diffs of every requested mapset in one SELECT. Matching
    # the pool's mode happens in this query so we don't have to filter
    # downstream — `skipped_wrong_mode` only collects diffs whose mapset
    # had at least one matching diff (else we report the whole mapset as
    # not_found).
    rows = (
        await db.exec(
            select(
                Beatmap.id,
                Beatmap.beatmapset_id,
                Beatmap.mode,
                Beatmap.difficulty_rating,
                Beatmap.total_length,
                Beatmap.beatmap_status,
                Beatmap.deleted_at,
            ).where(col(Beatmap.beatmapset_id).in_(requested))
        )
    ).all()

    # Bucket per mapset for the response breakdown.
    by_set: dict[int, list[tuple[int, str, float, int, str, Any]]] = {}
    for row in rows:
        bid, set_id, mode, sr, length, beatmap_status, deleted_at = row
        by_set.setdefault(int(set_id), []).append(
            (int(bid), str(mode), float(sr or 0.0), int(length or 0), str(beatmap_status), deleted_at)
        )

    added: list[int] = []
    skipped_dup: list[int] = []
    skipped_outside: list[int] = []
    skipped_wrong_mode: list[int] = []
    mapsets_not_found: list[int] = []

    for set_id in requested:
        diffs = by_set.get(set_id, [])
        if not diffs:
            mapsets_not_found.append(set_id)
            continue

        any_kept = False
        for bid, mode, sr, length, beatmap_status, deleted_at in diffs:
            # Hard filters that must pass regardless of window.
            if deleted_at is not None:
                continue  # deleted maps never get re-added
            if beatmap_status not in ("RANKED", "APPROVED"):
                # Don't pollute pools with graveyarded / qualified mid-air
                # changes. The operator can drop the filter explicitly via
                # the per-id endpoint if they really want one.
                continue
            if mode.lower() != pool_mode:
                skipped_wrong_mode.append(bid)
                continue
            if not (payload.min_sr <= sr <= payload.max_sr):
                skipped_outside.append(bid)
                continue
            if not (payload.min_length_seconds <= length <= payload.max_length_seconds):
                skipped_outside.append(bid)
                continue
            if bid in existing_in_pool:
                skipped_dup.append(bid)
                any_kept = True
                continue

            db.add(
                MatchmakingPoolBeatmap(
                    pool_id=pool_id,
                    beatmap_id=bid,
                    mods=[],
                    rating=payload.initial_rating,
                    rating_sig=payload.initial_rating_sig,
                    selection_count=0,
                )
            )
            added.append(bid)
            existing_in_pool.add(bid)
            any_kept = True

        if not any_kept:
            # Every diff in this mapset was filtered out. Report the
            # mapset itself as "not found in this pool's window" so the
            # admin UI can prompt to widen filters or skip.
            mapsets_not_found.append(set_id)

    if added:
        await db.commit()

    return BulkBeatmapsetAddResponse(
        added=added,
        skipped_already_in_pool=skipped_dup,
        skipped_outside_window=skipped_outside,
        skipped_wrong_mode=skipped_wrong_mode,
        mapsets_not_found=mapsets_not_found,
    )
