from __future__ import annotations
from datetime import datetime
from typing import Annotated, cast, Any

from app.database.auth import OAuthToken
from app.database.beatmap import Beatmap, BannedBeatmaps
from app.database.beatmapset import Beatmapset
from app.database.score import Score
from app.database.statistics import UserStatistics
from app.database.daily_challenge_model import DailyChallenge, DailyChallengeCreate, DailyChallengeUpdate, DailyChallengeResponse
from app.database.team import Team
from app.database.user import User
from app.database.user_account_history import UserAccountHistory, UserAccountHistoryType
from app.database.user_badge import UserBadge, UserBadgeCreate, UserBadgeUpdate, UserBadgeResponse
from app.database.verification import LoginSession, LoginSessionResp, TrustedDevice, TrustedDeviceResp
from app.dependencies.database import Database, get_redis
from app.dependencies.geoip import GeoIPService
from app.dependencies.user import UserAndToken, get_client_user_and_token
from app.models.mods import APIMod, get_available_mods
from app.tasks.daily_challenge import create_daily_challenge_room
from app.utils import utcnow

from .router import router

import json
import httpx
from datetime import datetime, timedelta
from fastapi import HTTPException, Query, Security
from pydantic import BaseModel
from sqlalchemy import or_ as sql_or
from sqlmodel import col, func, select


async def require_admin(session: Database, user_and_token: UserAndToken) -> User:
    """Helper function to check if user is admin"""
    current_user, _ = user_and_token
    # is_admin is an OnDemand field, so we need to await it
    is_admin = await current_user.awaitable_attrs.is_admin
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


async def user_to_dict(user: User, session: Database) -> dict:
    """Convert User object to dictionary for API response"""
    # Get basic model dump
    user_dict = user.model_dump(exclude_none=True)

    # Await OnDemand fields that might be needed
    try:
        user_dict["is_admin"] = await user.awaitable_attrs.is_admin
    except Exception:
        user_dict["is_admin"] = False

    try:
        user_dict["is_gmt"] = await user.awaitable_attrs.is_gmt
    except Exception:
        user_dict["is_gmt"] = False

    try:
        user_dict["is_qat"] = await user.awaitable_attrs.is_qat
    except Exception:
        user_dict["is_qat"] = False

    try:
        user_dict["is_restricted"] = await user.is_restricted(session)
    except Exception:
        user_dict["is_restricted"] = False

    # Handle badges if needed - serialize datetime to ISO string
    try:
        # 1. Get badges from JSON field (legacy)
        legacy_badges = []
        json_badges = await user.awaitable_attrs.badges
        if json_badges:
            for badge in json_badges:
                badge_copy = dict(badge)
                if "awarded_at" in badge_copy and isinstance(badge_copy["awarded_at"], datetime):
                    badge_copy["awarded_at"] = badge_copy["awarded_at"].isoformat()
                legacy_badges.append(badge_copy)

        # 2. Get badges from user_badges table (new)
        db_badges = []
        user_badges_list = await user.awaitable_attrs.user_badges
        if user_badges_list:
            for badge in user_badges_list:
                db_badges.append({
                    "id": badge.id,
                    "description": badge.description,
                    "image_url": badge.image_url,
                    "image@2x_url": badge.image_2x_url,
                    "url": badge.url,
                    "awarded_at": badge.awarded_at.isoformat() if isinstance(badge.awarded_at, datetime) else badge.awarded_at,
                    "user_id": badge.user_id
                })

        # Combine both, preferring DB badges (put them first or just combine)
        user_dict["badges"] = db_badges + legacy_badges
    except Exception as e:
        print(f"Error serializing badges for user {user.id}: {e}")
        user_dict["badges"] = []

    return user_dict


class SessionsResp(BaseModel):
    total: int
    current: int = 0
    sessions: list[LoginSessionResp]


class AdminStatsResp(BaseModel):
    total_users: int
    online_users: int
    total_pp: float
    total_plays: int
    total_scores: int
    total_beatmaps: int
    blacklisted_beatmaps: int
    performance_server_status: str
    api_server_status: str


class UserUpdateRequest(BaseModel):
    username: str | None = None
    country_code: str | None = None
    is_qat: bool | None = None
    is_gmt: bool | None = None
    is_admin: bool | None = None
    badge: dict | str | None = None


class BeatmapBlacklistItem(BaseModel):
    id: int
    beatmapset_id: int
    beatmap_id: int
    beatmapset: dict | None = None


class BadgeCreateRequest(BaseModel):
    description: str
    image_url: str
    image_2x_url: str | None = None
    url: str | None = None
    awarded_at: str | None = None  # ISO format string


class BadgeUpdateRequest(BaseModel):
    description: str | None = None
    image_url: str | None = None
    image_2x_url: str | None = None
    url: str | None = None
    awarded_at: str | None = None  # ISO format string


@router.get(
    "/admin/sessions",
    name="获取当前用户的登录会话列表",
    tags=["用户会话", "g0v0 API", "管理"],
    response_model=SessionsResp,
)
async def get_sessions(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    geoip: GeoIPService,
):
    current_user, token = user_and_token
    current = 0

    sessions = (
        await session.exec(
            select(
                LoginSession,
            )
            .where(LoginSession.user_id == current_user.id, col(LoginSession.is_verified).is_(True))
            .order_by(col(LoginSession.created_at).desc())
        )
    ).all()
    resp = []
    for s in sessions:
        resp.append(LoginSessionResp.from_db(s, geoip))
        if s.token_id == token.id:
            current = s.id

    return SessionsResp(
        total=len(sessions),
        current=current,
        sessions=resp,
    )


@router.delete(
    "/admin/sessions/{session_id}",
    name="注销指定的登录会话",
    tags=["用户会话", "g0v0 API", "管理"],
    status_code=204,
)
async def delete_session(
    session: Database,
    session_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    current_user, token = user_and_token
    if session_id == token.id:
        raise HTTPException(status_code=400, detail="Cannot delete the current session")

    db_session = await session.get(LoginSession, session_id)
    if not db_session or db_session.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Session not found")

    await session.delete(db_session)

    token = await session.get(OAuthToken, db_session.token_id or 0)
    if token:
        await session.delete(token)

    await session.commit()
    return


class TrustedDevicesResp(BaseModel):
    total: int
    current: int = 0
    devices: list[TrustedDeviceResp]


@router.get(
    "/admin/trusted-devices",
    name="获取当前用户的受信任设备列表",
    tags=["用户会话", "g0v0 API", "管理"],
    response_model=TrustedDevicesResp,
)
async def get_trusted_devices(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    geoip: GeoIPService,
):
    current_user, token = user_and_token
    devices = (
        await session.exec(
            select(TrustedDevice)
            .where(TrustedDevice.user_id == current_user.id)
            .order_by(col(TrustedDevice.last_used_at).desc())
        )
    ).all()

    current_device_id = (
        await session.exec(
            select(TrustedDevice.id)
            .join(LoginSession, col(LoginSession.device_id) == TrustedDevice.id)
            .where(
                LoginSession.token_id == token.id,
                TrustedDevice.user_id == current_user.id,
            )
            .limit(1)
        )
    ).first()

    return TrustedDevicesResp(
        total=len(devices),
        current=current_device_id or 0,
        devices=[TrustedDeviceResp.from_db(device, geoip) for device in devices],
    )


@router.delete(
    "/admin/trusted-devices/{device_id}",
    name="移除受信任设备",
    tags=["用户会话", "g0v0 API", "管理"],
    status_code=204,
)
async def delete_trusted_device(
    session: Database,
    device_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    current_user, token = user_and_token
    device = await session.get(TrustedDevice, device_id)
    current_device_id = (
        await session.exec(
            select(TrustedDevice.id)
            .join(LoginSession, col(LoginSession.device_id) == TrustedDevice.id)
            .where(
                LoginSession.token_id == token.id,
                TrustedDevice.user_id == current_user.id,
            )
            .limit(1)
        )
    ).first()
    if device_id == current_device_id:
        raise HTTPException(status_code=400, detail="Cannot delete the current trusted device")

    if not device or device.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Trusted device not found")

    await session.delete(device)
    await session.commit()
    return


# ========== Admin Statistics ==========

@router.get(
    "/admin/stats",
    name="获取管理员统计数据",
    tags=["管理", "g0v0 API"],
    response_model=AdminStatsResp,
)
async def get_admin_stats(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Get admin statistics: total users, online users, total pp, total plays, total scores, beatmaps, and blacklisted beatmaps"""
    await require_admin(session, user_and_token)

    # Count total users
    total_users = (await session.exec(select(func.count()).select_from(User))).one()

    # Count online users
    redis = get_redis()
    online_keys = await redis.keys("metadata:online:*")
    online_users = len(online_keys)

    # Sum total PP
    total_pp = (await session.exec(select(func.sum(UserStatistics.pp)))).one() or 0.0

    # Sum total plays
    total_plays = (await session.exec(select(func.sum(UserStatistics.play_count)))).one() or 0

    # Count total scores
    total_scores = (await session.exec(select(func.count()).select_from(Score))).one()

    # Count total beatmaps (non-deleted)
    total_beatmaps = (await session.exec(select(func.count()).select_from(Beatmapset))).one()

    # Count blacklisted beatmaps (unique beatmapsets)
    blacklisted_beatmap_ids = (
        await session.exec(select(BannedBeatmaps.beatmap_id))
    ).all()
    # Get unique beatmapsets from banned beatmaps
    if blacklisted_beatmap_ids:
        unique_beatmapsets = (
            await session.exec(
                select(func.distinct(Beatmap.beatmapset_id))
                .where(col(Beatmap.id).in_(blacklisted_beatmap_ids))
            )
        ).all()
        blacklisted_beatmaps = len(unique_beatmapsets)
    else:
        blacklisted_beatmaps = 0

    # Check server status
    performance_server_status = "offline"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("http://localhost:5223/", timeout=1.0)
            if resp.status_code == 200:
                performance_server_status = "online"
    except Exception:
        pass

    api_server_status = "online"

    return AdminStatsResp(
        total_users=total_users,
        online_users=online_users,
        total_pp=total_pp,
        total_plays=total_plays,
        total_scores=total_scores,
        total_beatmaps=total_beatmaps,
        blacklisted_beatmaps=blacklisted_beatmaps,
        performance_server_status=performance_server_status,
        api_server_status=api_server_status,
    )


# ========== User Management ==========

@router.get(
    "/admin/users",
    name="获取所有用户列表",
    tags=["管理", "g0v0 API"],
)
async def get_all_users(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Get all users (admin only)"""
    await require_admin(session, user_and_token)

    users = (await session.exec(select(User).order_by(col(User.id)))).all()
    return [await user_to_dict(user, session) for user in users]


@router.get(
    "/admin/users/{user_id}",
    name="获取指定用户信息",
    tags=["管理", "g0v0 API"],
)
async def get_user(
    session: Database,
    user_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Get a specific user by ID (admin only)"""
    await require_admin(session, user_and_token)

    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return await user_to_dict(user, session)


@router.patch(
    "/admin/users/{user_id}",
    name="更新用户信息",
    tags=["管理", "g0v0 API"],
)
async def update_user(
    session: Database,
    user_id: int,
    user_data: UserUpdateRequest,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Update user information (admin only)"""
    await require_admin(session, user_and_token)

    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user_data.username is not None:
        user.username = user_data.username

    if user_data.country_code is not None:
        user.country_code = user_data.country_code

    if user_data.is_qat is not None:
        user.is_qat = user_data.is_qat

    if user_data.is_gmt is not None:
        user.is_gmt = user_data.is_gmt

    if user_data.is_admin is not None:
        user.is_admin = user_data.is_admin

    if user_data.badge is not None:
        import json
        # Note: Badges are stored as JSON, so awarded_at must be an ISO string, not datetime
        # We use plain dicts here instead of Badge TypedDict because JSON storage requires strings
        if isinstance(user_data.badge, str):
            try:
                badge_dict = json.loads(user_data.badge)
                # Ensure awarded_at is an ISO string (not datetime) for JSON storage
                if "awarded_at" in badge_dict:
                    if isinstance(badge_dict["awarded_at"], datetime):
                        badge_dict["awarded_at"] = badge_dict["awarded_at"].isoformat()
                    elif not isinstance(badge_dict["awarded_at"], str):
                        badge_dict["awarded_at"] = datetime.now().isoformat()
                else:
                    badge_dict["awarded_at"] = datetime.now().isoformat()

                # Ensure image@2x_url is present (use image_url as fallback)
                if "image@2x_url" not in badge_dict:
                    badge_dict["image@2x_url"] = badge_dict.get("image_url", "")

                # Store as list of badge dicts (JSON-compatible format)
                # Note: We store as dict with string dates for JSON compatibility, not Badge TypedDict with datetime
                user.badges = cast(Any, [badge_dict])
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                # If parsing fails, create a simple badge structure
                # Note: We store as dict with string dates for JSON compatibility
                user.badges = cast(Any, [{
                    "awarded_at": datetime.now().isoformat(),
                    "description": "",
                    "image_url": user_data.badge if user_data.badge.startswith("http") else "",
                    "image@2x_url": user_data.badge if user_data.badge.startswith("http") else "",
                    "url": "",
                }])
        elif isinstance(user_data.badge, dict):
            # Convert awarded_at to ISO string if it's a datetime
            awarded_at_str = datetime.now().isoformat()
            if "awarded_at" in user_data.badge:
                if isinstance(user_data.badge["awarded_at"], str):
                    awarded_at_str = user_data.badge["awarded_at"]
                elif isinstance(user_data.badge["awarded_at"], datetime):
                    awarded_at_str = user_data.badge["awarded_at"].isoformat()
                else:
                    awarded_at_str = datetime.now().isoformat()

            badge_dict = {
                "awarded_at": awarded_at_str,  # Store as ISO string for JSON
                "description": user_data.badge.get("description", ""),
                "image_url": user_data.badge.get("icon_url") or user_data.badge.get("image_url", ""),
                "image@2x_url": user_data.badge.get("image@2x_url") or user_data.badge.get("icon_url") or user_data.badge.get("image_url", ""),
                "url": user_data.badge.get("url", ""),
            }
            # Note: We store as dict with string dates for JSON compatibility, not Badge TypedDict with datetime
            user.badges = cast(Any, [badge_dict])
        else:
            user.badges = []

    await session.commit()
    await session.refresh(user)
    return await user_to_dict(user, session)


@router.post(
    "/admin/users/{user_id}/ban",
    name="封禁用户",
    tags=["管理", "g0v0 API"],
    status_code=204,
)
async def ban_user(
    session: Database,
    user_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Ban a user (admin only)"""
    current_user = await require_admin(session, user_and_token)

    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot ban yourself")

    # Create restriction history
    restriction = UserAccountHistory(
        id=None,  # Will be auto-generated
        user_id=user_id,
        type=UserAccountHistoryType.RESTRICTION,
        description="Account restricted by admin",
        length=0,
        permanent=True,
    )
    session.add(restriction)
    await session.commit()


@router.post(
    "/admin/users/{user_id}/unban",
    name="解封用户",
    tags=["管理", "g0v0 API"],
    status_code=204,
)
async def unban_user(
    session: Database,
    user_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Unban a user (admin only)"""
    await require_admin(session, user_and_token)

    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Remove active restrictions
    restrictions = (
        await session.exec(
            select(UserAccountHistory).where(
                UserAccountHistory.user_id == user_id,
                UserAccountHistory.type == UserAccountHistoryType.RESTRICTION,
            )
        )
    ).all()

    for restriction in restrictions:
        await session.delete(restriction)

    await session.commit()


# ========== Beatmap Blacklist ==========

@router.get(
    "/admin/beatmaps/blacklist",
    name="获取黑名单谱面列表",
    tags=["管理", "g0v0 API"],
)
async def get_blacklisted_beatmaps(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Get all blacklisted beatmaps (admin only)"""
    await require_admin(session, user_and_token)

    # Get all banned beatmaps
    banned_beatmaps = (
        await session.exec(select(BannedBeatmaps))
    ).all()

    result = []
    seen_beatmapsets = set()

    for banned_item in banned_beatmaps:
        # Get the beatmap to find its beatmapset
        beatmap = await session.get(Beatmap, banned_item.beatmap_id)
        if not beatmap:
            continue

        beatmapset_id = beatmap.beatmapset_id

        # Only add each beatmapset once
        if beatmapset_id in seen_beatmapsets:
            continue
        seen_beatmapsets.add(beatmapset_id)

        beatmapset = await session.get(Beatmapset, beatmapset_id)
        beatmapset_dict = None
        if beatmapset:
            beatmapset_dict = {
                "id": beatmapset.id,
                "title": beatmapset.title,
                "artist": beatmapset.artist,
            }
        result.append(
            BeatmapBlacklistItem(
                id=banned_item.id or 0,
                beatmapset_id=beatmapset_id,
                beatmap_id=banned_item.beatmap_id,
                beatmapset=beatmapset_dict,
            )
        )

    return result


class BeatmapBlacklistRequest(BaseModel):
    beatmapset_id: int


@router.post(
    "/admin/beatmaps/blacklist",
    name="添加谱面到黑名单",
    tags=["管理", "g0v0 API"],
    status_code=201,
)
async def add_blacklisted_beatmap(
    session: Database,
    request: BeatmapBlacklistRequest,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Add a beatmapset to blacklist (admin only)"""
    await require_admin(session, user_and_token)

    beatmapset_id = request.beatmapset_id

    # Verify beatmapset exists
    beatmapset = await session.get(Beatmapset, beatmapset_id)
    if not beatmapset:
        raise HTTPException(status_code=404, detail="Beatmapset not found")

    # Get all beatmaps in this beatmapset
    beatmaps = (
        await session.exec(
            select(Beatmap).where(Beatmap.beatmapset_id == beatmapset_id)
        )
    ).all()

    if not beatmaps:
        raise HTTPException(status_code=404, detail="No beatmaps found in this beatmapset")

    # Check if any beatmap in this set is already banned
    beatmap_ids = [b.id for b in beatmaps]
    existing_banned = (
        await session.exec(
            select(BannedBeatmaps).where(col(BannedBeatmaps.beatmap_id).in_(beatmap_ids))
        )
    ).all()

    if existing_banned:
        raise HTTPException(status_code=400, detail="Some beatmaps in this beatmapset are already blacklisted")

    # Ban all beatmaps in the set
    for beatmap in beatmaps:
        banned_item = BannedBeatmaps(beatmap_id=beatmap.id)
        session.add(banned_item)

    await session.commit()

    return {"beatmapset_id": beatmapset_id, "message": "Beatmapset added to blacklist"}


@router.delete(
    "/admin/beatmaps/blacklist/{beatmapset_id}",
    name="从黑名单移除谱面",
    tags=["管理", "g0v0 API"],
    status_code=204,
)
async def remove_blacklisted_beatmap(
    session: Database,
    beatmapset_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Remove a beatmapset from blacklist (admin only)"""
    await require_admin(session, user_and_token)

    # Get all beatmaps in this beatmapset
    beatmaps = (
        await session.exec(
            select(Beatmap).where(Beatmap.beatmapset_id == beatmapset_id)
        )
    ).all()

    if not beatmaps:
        raise HTTPException(status_code=404, detail="Beatmapset not found")

    # Get all banned beatmaps for this beatmapset
    beatmap_ids = [b.id for b in beatmaps]
    banned_items = (
        await session.exec(
            select(BannedBeatmaps).where(col(BannedBeatmaps.beatmap_id).in_(beatmap_ids))
        )
    ).all()

    if not banned_items:
        raise HTTPException(status_code=404, detail="Beatmapset not in blacklist")

    # Remove all banned entries for this beatmapset
    for banned_item in banned_items:
        await session.delete(banned_item)

    await session.commit()


# ========== Beatmap Management ==========

@router.get(
    "/admin/beatmaps",
    name="获取所有谱面",
    tags=["管理", "g0v0 API"],
)
async def get_beatmaps(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    search: str = Query("", description="Search by artist, title, or ID"),
):
    """Get all beatmaps with pagination (admin only)"""
    await require_admin(session, user_and_token)

    offset = (page - 1) * limit

    # Build query with optional search
    query = select(Beatmapset)

    if search:
        search_term = f"%{search}%"
        # Try to parse as ID first
        try:
            search_id = int(search)
            query = query.where(
                sql_or(
                    col(Beatmapset.id) == search_id,
                    col(Beatmapset.title).like(search_term),
                    col(Beatmapset.artist).like(search_term)
                )
            )
        except ValueError:
            # Not a number, search in title and artist
            query = query.where(
                sql_or(
                    col(Beatmapset.title).like(search_term),
                    col(Beatmapset.artist).like(search_term)
                )
            )

    # Get total count
    if search:
        total_count = (await session.exec(select(func.count()).select_from(query.subquery()))).one()
    else:
        total_count = (await session.exec(select(func.count()).select_from(Beatmapset))).one()

    # Get beatmapsets with pagination
    beatmapsets = (
        await session.exec(
            query
            .order_by(col(Beatmapset.id).desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()

    result = []
    for beatmapset in beatmapsets:
        # Get beatmaps for this set
        beatmaps = (
            await session.exec(
                select(Beatmap).where(Beatmap.beatmapset_id == beatmapset.id)
            )
        ).all()

        # Get cover URL
        cover_url = None
        if beatmapset.covers:
            cover_url = beatmapset.covers.get("cover") or beatmapset.covers.get("card")

        beatmapset_dict = {
            "id": beatmapset.id,
            "title": beatmapset.title,
            "artist": beatmapset.artist,
            "creator": beatmapset.creator,
            "rank_status": beatmapset.beatmap_status.name.lower() if beatmapset.beatmap_status else None,
            "covers": beatmapset.covers,
            "cover_url": cover_url,
            "beatmaps": [
                {
                    "id": b.id,
                    "version": b.version,
                    "difficulty_rating": b.difficulty_rating,
                    "mode": b.mode.value if b.mode else None,
                }
                for b in beatmaps
            ],
        }
        result.append(beatmapset_dict)

    return {
        "total": total_count,
        "page": page,
        "limit": limit,
        "total_pages": (total_count + limit - 1) // limit,
        "beatmapsets": result,
    }


@router.get(
    "/admin/beatmaps/{beatmap_id}",
    name="获取谱面详情",
    tags=["管理", "g0v0 API"],
)
async def get_beatmap_details(
    session: Database,
    beatmap_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Get beatmap details (admin only)"""
    await require_admin(session, user_and_token)

    beatmapset = await session.get(Beatmapset, beatmap_id)
    if not beatmapset:
        raise HTTPException(status_code=404, detail="Beatmapset not found")

    # Get all beatmaps in this set
    beatmaps = (
        await session.exec(
            select(Beatmap).where(Beatmap.beatmapset_id == beatmapset.id)
        )
    ).all()

    # Get cover URL
    cover_url = None
    if beatmapset.covers:
        cover_url = beatmapset.covers.get("cover") or beatmapset.covers.get("card")

    return {
        "id": beatmapset.id,
        "title": beatmapset.title,
        "artist": beatmapset.artist,
        "creator": beatmapset.creator,
        "rank_status": beatmapset.beatmap_status.name.lower() if beatmapset.beatmap_status else None,
        "covers": beatmapset.covers,
        "cover_url": cover_url,
        "beatmaps": [
            {
                "id": b.id,
                "version": b.version,
                "difficulty_rating": b.difficulty_rating,
                "mode": b.mode.value if b.mode else None,
            }
            for b in beatmaps
        ],
    }


class RankStatusUpdate(BaseModel):
    status: str


@router.post(
    "/admin/beatmaps/{beatmapset_id}/rank",
    name="更新谱面状态",
    tags=["管理", "g0v0 API"],
)
async def update_beatmap_rank_status(
    session: Database,
    beatmapset_id: int,
    request: RankStatusUpdate,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Update beatmapset rank status (admin only)"""
    await require_admin(session, user_and_token)

    from app.models.beatmap import BeatmapRankStatus

    try:
        new_status = BeatmapRankStatus(request.status)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid rank status: {request.status}")

    beatmapset = await session.get(Beatmapset, beatmapset_id)
    if not beatmapset:
        raise HTTPException(status_code=404, detail="Beatmapset not found")

    beatmapset.beatmap_status = new_status
    await session.commit()
    await session.refresh(beatmapset)

    return {"id": beatmapset.id, "rank_status": beatmapset.beatmap_status.value}


@router.post(
    "/admin/beatmaps/{beatmapset_id}/ban",
    name="封禁谱面",
    tags=["管理", "g0v0 API"],
)
async def ban_beatmapset(
    session: Database,
    beatmapset_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Ban a beatmapset and remove all scores (admin only)"""
    await require_admin(session, user_and_token)

    from app.database.score import Score

    # Get all beatmaps in this set
    beatmaps = (
        await session.exec(
            select(Beatmap).where(Beatmap.beatmapset_id == beatmapset_id)
        )
    ).all()

    if not beatmaps:
        raise HTTPException(status_code=404, detail="Beatmapset not found")

    # Delete all scores for these beatmaps
    for beatmap in beatmaps:
        scores = (
            await session.exec(
                select(Score).where(Score.beatmap_id == beatmap.id)
            )
        ).all()
        for score in scores:
            await session.delete(score)

    # Add all beatmaps to blacklist
    for beatmap in beatmaps:
        # Check if already blacklisted
        existing = (
            await session.exec(
                select(BannedBeatmaps).where(BannedBeatmaps.beatmap_id == beatmap.id)
            )
        ).first()
        if not existing:
            banned_beatmap = BannedBeatmaps(beatmap_id=beatmap.id)
            session.add(banned_beatmap)

    await session.commit()

    return {"beatmapset_id": beatmapset_id, "message": "Beatmapset banned and scores removed"}


# ========== User Wipe ==========

class WipeRequest(BaseModel):
    mode: str  # e.g., "osu", "taiko", "fruits", "mania"


@router.post(
    "/admin/users/{user_id}/wipe",
    name="清除用户数据",
    tags=["管理", "g0v0 API"],
)
async def wipe_user_stats(
    session: Database,
    user_id: int,
    request: WipeRequest,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Wipe user statistics and scores for a specific mode (admin only)"""
    await require_admin(session, user_and_token)

    from app.database.score import Score
    from app.models.score import GameMode

    try:
        mode = GameMode(request.mode)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid game mode: {request.mode}")

    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Delete all scores for this user and mode
    scores = (
        await session.exec(
            select(Score).where(
                Score.user_id == user_id,
                Score.gamemode == mode,
            )
        )
    ).all()

    deleted_count = 0
    for score in scores:
        await session.delete(score)
        deleted_count += 1

    await session.commit()

    return {
        "user_id": user_id,
        "mode": request.mode,
        "deleted_scores": deleted_count,
        "message": f"Wiped {deleted_count} scores for mode {request.mode}",
    }


# ========== Badge Management ==========
# Now using user_badges table instead of JSON in User.badges field

@router.get(
    "/admin/user-badges",
    name="获取所有徽章",
    tags=["管理", "g0v0 API"],
)
async def get_user_badges(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Get all badges from user_badges table (admin only)"""
    await require_admin(session, user_and_token)

    # Join with User to get username
    statement = (
        select(UserBadge, User.username)
        .outerjoin(User, col(UserBadge.user_id) == User.id)
        .order_by(col(UserBadge.id).desc())
    )
    results = (await session.exec(statement)).all()

    badges = []
    for badge, username in results:
        badge_dict = badge.model_dump()
        badge_dict["username"] = username
        # Convert datetime to ISO string
        if badge_dict.get("awarded_at") and isinstance(badge_dict["awarded_at"], datetime):
            badge_dict["awarded_at"] = badge_dict["awarded_at"].isoformat()
        badges.append(badge_dict)

    return badges


@router.post(
    "/admin/user-badges",
    name="创建徽章",
    tags=["管理", "g0v0 API"],
    status_code=201,
)
async def create_user_badge(
    session: Database,
    badge_data: UserBadgeCreate,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Create a badge in user_badges table (admin only)"""
    await require_admin(session, user_and_token)

    # Set default awarded_at if not provided
    awarded_at = badge_data.awarded_at or datetime.now()

    # Create new badge
    new_badge = UserBadge(
        description=badge_data.description,
        image_url=badge_data.image_url,
        image_2x_url=badge_data.image_2x_url or badge_data.image_url,
        url=badge_data.url or "",
        awarded_at=awarded_at,
        user_id=badge_data.user_id,
    )

    session.add(new_badge)
    await session.commit()
    await session.refresh(new_badge)

    return new_badge


@router.patch(
    "/admin/user-badges/{badge_id}",
    name="更新徽章",
    tags=["管理", "g0v0 API"],
)
async def update_user_badge(
    session: Database,
    badge_id: int,
    badge_data: UserBadgeUpdate,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Update a badge in user_badges table (admin only)"""
    await require_admin(session, user_and_token)

    # Get the badge
    badge = await session.get(UserBadge, badge_id)
    if not badge:
        raise HTTPException(status_code=404, detail="Badge not found")

    # Update fields if provided
    if badge_data.description is not None:
        badge.description = badge_data.description
    if badge_data.image_url is not None:
        badge.image_url = badge_data.image_url
    if badge_data.image_2x_url is not None:
        badge.image_2x_url = badge_data.image_2x_url
    if badge_data.url is not None:
        badge.url = badge_data.url
    if badge_data.awarded_at is not None:
        badge.awarded_at = badge_data.awarded_at
    if badge_data.user_id is not None:
        badge.user_id = badge_data.user_id

    await session.commit()
    await session.refresh(badge)

    return badge


@router.delete(
    "/admin/user-badges/{badge_id}",
    name="删除徽章",
    tags=["管理", "g0v0 API"],
    status_code=204,
)
async def delete_user_badge(
    session: Database,
    badge_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Delete a badge from user_badges table (admin only)"""
    await require_admin(session, user_and_token)

    badge = await session.get(UserBadge, badge_id)
    if not badge:
        raise HTTPException(status_code=404, detail="Badge not found")

    await session.delete(badge)
    await session.commit()


# ========== Team Management ==========

@router.get(
    "/admin/teams",
    name="获取所有战队",
    tags=["管理", "g0v0 API"],
)
async def get_all_teams(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Get all teams (admin only)"""
    await require_admin(session, user_and_token)

    teams = (await session.exec(select(Team).order_by(col(Team.created_at).desc()))).all()
    return teams


@router.patch(
    "/admin/teams/{team_id}",
    name="更新战队",
    tags=["管理", "g0v0 API"],
)
async def update_team_admin(
    session: Database,
    team_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Update team (admin only) - delegates to existing team update endpoint"""
    await require_admin(session, user_and_token)

    # This should use the existing team update logic from team.py
    # For now, return a message indicating to use the regular team endpoint
    raise HTTPException(
        status_code=501,
        detail="Use /api/private/team/{team_id} endpoint for team updates. Admin override not yet implemented.",
    )


@router.delete(
    "/admin/teams/{team_id}",
    name="删除战队",
    tags=["管理", "g0v0 API"],
    status_code=204,
)
async def delete_team_admin(
    session: Database,
    team_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Delete a team (admin only)"""
    await require_admin(session, user_and_token)

    team = await session.get(Team, team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    await session.delete(team)
    await session.commit()


# ========== Daily Challenge Statistics ==========

class DailyChallengeStatsResponse(BaseModel):
    user_id: int
    daily_streak_best: int = 0
    daily_streak_current: int = 0
    weekly_streak_best: int = 0
    weekly_streak_current: int = 0
    top_10p_placements: int = 0
    top_50p_placements: int = 0
    playcount: int = 0
    last_update: str | None = None  # ISO format
    last_weekly_streak: str | None = None  # ISO format


@router.get(
    "/admin/daily-challenge/stats/{user_id}",
    name="获取用户每日挑战统计",
    tags=["管理", "g0v0 API"],
    response_model=DailyChallengeStatsResponse,
)
async def get_daily_challenge_stats(
    session: Database,
    user_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Get daily challenge statistics for a user (admin only) - Matches osu.Game APIUserDailyChallengeStatistics"""
    await require_admin(session, user_and_token)

    # For now, return default stats. In a real implementation, this would query user statistics
    # from a dedicated daily challenge statistics table or calculate from scores
    return DailyChallengeStatsResponse(
        user_id=user_id,
        daily_streak_best=0,
        daily_streak_current=0,
        weekly_streak_best=0,
        weekly_streak_current=0,
        top_10p_placements=0,
        top_50p_placements=0,
        playcount=0,
        last_update=None,
        last_weekly_streak=None,
    )


# ========== Daily Challenge Management ==========

class DailyChallengeListResponse(BaseModel):
    total: int
    challenges: list[DailyChallengeResponse]
    page: int = 1
    per_page: int = 50


@router.get(
    "/admin/daily-challenges",
    name="获取每日挑战列表",
    tags=["管理", "g0v0 API"],
    response_model=DailyChallengeListResponse,
)
async def list_daily_challenges(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(50, ge=1, le=100, description="Items per page"),
    date_from: str | None = Query(None, description="Start date (YYYY-MM-DD)"),
    date_to: str | None = Query(None, description="End date (YYYY-MM-DD)"),
):
    """List daily challenges with pagination and optional date filtering - Enhanced for osu.Game compatibility"""
    await require_admin(session, user_and_token)

    # Build query
    query = select(DailyChallenge)

    # Apply date filtering if provided
    if date_from:
        try:
            from_date = datetime.strptime(date_from, "%Y-%m-%d").date()
            query = query.where(col(DailyChallenge.date) >= from_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date_from format. Use YYYY-MM-DD")

    if date_to:
        try:
            to_date = datetime.strptime(date_to, "%Y-%m-%d").date()
            query = query.where(col(DailyChallenge.date) <= to_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date_to format. Use YYYY-MM-DD")

    # Order by date descending (newest first)
    query = query.order_by(col(DailyChallenge.date).desc())

    # Get total count
    total_query = select(func.count()).select_from(DailyChallenge)
    if date_from:
        total_query = total_query.where(col(DailyChallenge.date) >= from_date)
    if date_to:
        total_query = total_query.where(col(DailyChallenge.date) <= to_date)

    total = (await session.exec(total_query)).one()

    # Apply pagination
    offset = (page - 1) * per_page
    query = query.offset(offset).limit(per_page)

    challenges = (await session.exec(query)).all()

    # Add beatmap info to each challenge
    challenges_with_beatmap = []
    for challenge in challenges:
        # Convert to response model to avoid SQLModel/Pydantic validation errors
        challenge_res = DailyChallengeResponse.model_validate(challenge)
        beatmap = await session.get(Beatmap, challenge.beatmap_id)
        if beatmap:
            # Get beatmapset info using awaitable_attrs to avoid MissingGreenlet error
            beatmapset = await beatmap.awaitable_attrs.beatmapset
            challenge_res.beatmap = {
                "id": beatmap.id,
                "title": beatmapset.title if beatmapset else "Unknown",
                "artist": beatmapset.artist if beatmapset else "Unknown",
                "difficulty_rating": beatmap.difficulty_rating,
            }
        challenges_with_beatmap.append(challenge_res)

    return DailyChallengeListResponse(
        total=total,
        challenges=challenges_with_beatmap,
        page=page,
        per_page=per_page,
    )

@router.get(
    "/admin/daily-challenge/{date}",
    name="获取每日挑战",
    tags=["管理", "g0v0 API"],
    response_model=DailyChallengeResponse | None,
)
async def get_daily_challenge(
    session: Database,
    date: str,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Get daily challenge for a specific date (admin only)"""
    await require_admin(session, user_and_token)

    try:
        challenge_date = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    challenge = (
        await session.exec(select(DailyChallenge).where(col(DailyChallenge.date) == challenge_date))
    ).first()

    if not challenge:
        return None

    # Convert to response model to avoid SQLModel/Pydantic validation errors
    challenge_res = DailyChallengeResponse.model_validate(challenge)

    # Try to get beatmap info
    beatmap = await session.get(Beatmap, challenge.beatmap_id)
    if beatmap:
        # Get beatmapset info using awaitable_attrs to avoid MissingGreenlet error
        beatmapset = await beatmap.awaitable_attrs.beatmapset
        challenge_res.beatmap = {
            "id": beatmap.id,
            "title": beatmapset.title if beatmapset else "Unknown",
            "artist": beatmapset.artist if beatmapset else "Unknown",
            "difficulty_rating": beatmap.difficulty_rating,
        }

    return challenge_res


@router.post(
    "/admin/daily-challenge/trigger",
    name="手动触发每日挑战",
    tags=["管理", "g0v0 API"],
)
async def trigger_daily_challenge(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Manually trigger the daily challenge job (admin only)"""
    await require_admin(session, user_and_token)

    from app.tasks.daily_challenge import daily_challenge_job
    await daily_challenge_job()

    return {"message": "Daily challenge job triggered successfully"}


@router.post(
    "/admin/daily-challenge",
    name="创建每日挑战",
    tags=["管理", "g0v0 API"],
    status_code=201,
    response_model=DailyChallengeResponse,
)
async def create_daily_challenge(
    session: Database,
    challenge_data: DailyChallengeCreate,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Create a daily challenge (admin only) - Enhanced to match osu.Game Room structure"""
    await require_admin(session, user_and_token)

    try:
        challenge_date = datetime.strptime(challenge_data.date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    # Check if beatmap exists
    beatmap = await session.get(Beatmap, challenge_data.beatmap_id)
    if not beatmap:
        raise HTTPException(status_code=404, detail="Beatmap not found")

    # Check if challenge already exists for this date
    existing_challenge = (
        await session.exec(select(DailyChallenge).where(col(DailyChallenge.date) == challenge_date))
    ).first()

    if existing_challenge:
        raise HTTPException(status_code=409, detail="Daily challenge already exists for this date")

    # Check if room_id is already used (if provided)
    if hasattr(challenge_data, 'room_id') and challenge_data.room_id is not None:
        existing_room_challenge = (
            await session.exec(select(DailyChallenge).where(col(DailyChallenge.room_id) == challenge_data.room_id))
        ).first()
        if existing_room_challenge:
            raise HTTPException(status_code=409, detail="Room ID already in use by another daily challenge")

    # Create new challenge with enhanced fields
    new_challenge = DailyChallenge(
        date=challenge_date,
        beatmap_id=challenge_data.beatmap_id,
        ruleset_id=challenge_data.ruleset_id,
        required_mods=challenge_data.required_mods,
        allowed_mods=challenge_data.allowed_mods,
        room_id=getattr(challenge_data, 'room_id', None),
        max_attempts=getattr(challenge_data, 'max_attempts', None),
        time_limit=getattr(challenge_data, 'time_limit', None),
    )

    # Sync to Redis (matching tools/add_daily_challenge.py)
    redis = get_redis()
    redis_key = f"daily_challenge:{challenge_date}"

    required_mods_list = json.loads(challenge_data.required_mods)
    allowed_mods_list = json.loads(challenge_data.allowed_mods)

    await redis.hset(
        redis_key,
        mapping={
            "beatmap": new_challenge.beatmap_id,
            "ruleset_id": new_challenge.ruleset_id,
            "required_mods": challenge_data.required_mods,
            "allowed_mods": challenge_data.allowed_mods,
        },
    )

    # Automatically assign room_id if for today and not provided
    if new_challenge.room_id is None and challenge_date == utcnow().date():
        now = utcnow()
        next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        # Duration should be in minutes, not seconds
        duration = int((next_day - now).total_seconds() / 60)

        room = await create_daily_challenge_room(
            beatmap=new_challenge.beatmap_id,
            ruleset_id=new_challenge.ruleset_id,
            duration=duration,
            required_mods=cast(list[APIMod], required_mods_list),
            allowed_mods=cast(list[APIMod], allowed_mods_list),
        )
        new_challenge.room_id = room.id

    session.add(new_challenge)
    await session.commit()
    await session.refresh(new_challenge)

    # Refresh beatmap to avoid MissingGreenlet after commit
    if beatmap:
        await session.refresh(beatmap)

    # Add beatmap info to response
    challenge_res = DailyChallengeResponse.model_validate(new_challenge)
    if beatmap:
        # Re-fetch beatmap to ensure we have a fresh session-attached instance
        beatmap = await session.get(Beatmap, new_challenge.beatmap_id)
        if beatmap:
            # Get beatmapset info using awaitable_attrs to avoid MissingGreenlet error
            beatmapset = await beatmap.awaitable_attrs.beatmapset
            challenge_res.beatmap = {
                "id": beatmap.id,
                "title": beatmapset.title if beatmapset else "Unknown",
                "artist": beatmapset.artist if beatmapset else "Unknown",
                "difficulty_rating": beatmap.difficulty_rating,
            }

    return challenge_res


@router.patch(
    "/admin/daily-challenge/{date}",
    name="更新每日挑战",
    tags=["管理", "g0v0 API"],
    response_model=DailyChallengeResponse,
)
async def update_daily_challenge(
    session: Database,
    date: str,
    challenge_data: DailyChallengeUpdate,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Update a daily challenge (admin only) - Enhanced with new fields"""
    await require_admin(session, user_and_token)

    try:
        challenge_date = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    challenge = (
        await session.exec(select(DailyChallenge).where(col(DailyChallenge.date) == challenge_date))
    ).first()

    if not challenge:
        raise HTTPException(status_code=404, detail="Daily challenge not found")

    # Update fields if provided
    if getattr(challenge_data, 'beatmap_id', None) is not None:
        # Check if new beatmap exists
        beatmap = await session.get(Beatmap, challenge_data.beatmap_id)
        if not beatmap:
            raise HTTPException(status_code=404, detail="Beatmap not found")
        challenge.beatmap_id = challenge_data.beatmap_id

    if getattr(challenge_data, 'ruleset_id', None) is not None:
        challenge.ruleset_id = challenge_data.ruleset_id
    if getattr(challenge_data, 'required_mods', None) is not None:
        challenge.required_mods = challenge_data.required_mods
    if getattr(challenge_data, 'allowed_mods', None) is not None:
        challenge.allowed_mods = challenge_data.allowed_mods
    if getattr(challenge_data, 'room_id', None) is not None:
        # Check if room_id is already used by another challenge
        existing_room_challenge = (
            await session.exec(
                select(DailyChallenge)
                .where(
                    col(DailyChallenge.room_id) == challenge_data.room_id,
                    col(DailyChallenge.date) != challenge_date
                )
            )
        ).first()
        if existing_room_challenge:
            raise HTTPException(status_code=409, detail="Room ID already in use by another daily challenge")
        challenge.room_id = challenge_data.room_id
    if getattr(challenge_data, 'max_attempts', None) is not None:
        challenge.max_attempts = challenge_data.max_attempts
    if getattr(challenge_data, 'time_limit', None) is not None:
        challenge.time_limit = challenge_data.time_limit

    # Sync to Redis (matching tools/add_daily_challenge.py)
    redis = get_redis()
    redis_key = f"daily_challenge:{challenge_date}"

    await redis.hset(
        redis_key,
        mapping={
            "beatmap": challenge.beatmap_id,
            "ruleset_id": challenge.ruleset_id,
            "required_mods": challenge.required_mods,
            "allowed_mods": challenge.allowed_mods,
        },
    )

    # Automatically assign room_id if for today and not provided
    if challenge.room_id is None and challenge_date == utcnow().date():
        now = utcnow()
        next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        # Duration should be in minutes, not seconds
        duration = int((next_day - now).total_seconds() / 60)

        required_mods_list = json.loads(challenge.required_mods)
        allowed_mods_list = json.loads(challenge.allowed_mods)

        room = await create_daily_challenge_room(
            beatmap=challenge.beatmap_id,
            ruleset_id=challenge.ruleset_id,
            duration=duration,
            required_mods=cast(list[APIMod], required_mods_list),
            allowed_mods=cast(list[APIMod], allowed_mods_list),
        )
        challenge.room_id = room.id

    await session.commit()
    await session.refresh(challenge)

    # Refresh beatmap to avoid MissingGreenlet after commit
    beatmap = await session.get(Beatmap, challenge.beatmap_id)
    if beatmap:
        await session.refresh(beatmap)

    # Add beatmap info to response
    challenge_res = DailyChallengeResponse.model_validate(challenge)
    if beatmap:
        # Re-fetch beatmap to ensure we have a fresh session-attached instance
        beatmap = await session.get(Beatmap, challenge.beatmap_id)
        if beatmap:
            # Get beatmapset info using awaitable_attrs to avoid MissingGreenlet error
            beatmapset = await beatmap.awaitable_attrs.beatmapset
            challenge_res.beatmap = {
                "id": beatmap.id,
                "title": beatmapset.title if beatmapset else "Unknown",
                "artist": beatmapset.artist if beatmapset else "Unknown",
                "difficulty_rating": beatmap.difficulty_rating,
            }

    return challenge_res


@router.delete(
    "/admin/daily-challenge/{date}",
    name="删除每日挑战",
    tags=["管理", "g0v0 API"],
    status_code=204,
)
async def delete_daily_challenge(
    session: Database,
    date: str,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Delete a daily challenge (admin only)"""
    await require_admin(session, user_and_token)

    try:
        challenge_date = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    challenge = (
        await session.exec(select(DailyChallenge).where(col(DailyChallenge.date) == challenge_date))
    ).first()

    if not challenge:
        raise HTTPException(status_code=404, detail="Daily challenge not found")

    await session.delete(challenge)
    await session.commit()
