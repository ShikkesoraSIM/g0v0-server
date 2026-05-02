from __future__ import annotations
from datetime import datetime, timedelta
from typing import Annotated, cast, Any

from app.auth import invalidate_user_tokens
from app.database.auth import OAuthToken
from app.database.beatmap import Beatmap, BannedBeatmaps
from app.database.beatmapset import Beatmapset
from app.database.chat import ChannelType, ChatChannel, ChatMessage, ChatMessageModel, MessageType
from app.database.score import Score
from app.database.score_token import ScoreToken
from app.database.statistics import UserStatistics
from app.database.user_login_log import UserLoginLog
from app.database.daily_challenge_model import DailyChallenge, DailyChallengeCreate, DailyChallengeUpdate, DailyChallengeResponse
from app.database.team import Team, TeamMember
from app.database.user import User
from app.database.user_account_history import UserAccountHistory, UserAccountHistoryType
from app.database.user_badge import UserBadge, UserBadgeCreate, UserBadgeUpdate, UserBadgeResponse
from app.database.verification import LoginSession, LoginSessionResp, TrustedDevice, TrustedDeviceResp
from app.const import BANCHOBOT_ID
from app.dependencies.database import Database, Redis, get_redis
from app.dependencies.client_verification import ClientVerificationService
from app.dependencies.geoip import GeoIPService
from app.dependencies.storage import StorageService
from app.dependencies.user import UserAndToken, get_client_user_and_token
from app.log import log
from app.models.mods import APIMod, get_available_mods
from app.models.score import GameMode
from app.models.notification import ChannelMessage, GlobalAnnouncement
from app.router.notification.server import server
from app.service.ranking_cache_service import get_ranking_cache_service
from app.tasks.daily_challenge import create_daily_challenge_room
from app.utils import check_image, utcnow

from .router import router

import json
import httpx
import hashlib
from fastapi import File, Form, HTTPException, Query, Security
from pydantic import BaseModel, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy import or_ as sql_or
from sqlmodel import col, func, select

logger = log("AdminRouter")

def _parse_mods_raw(raw: str | None) -> list[APIMod]:
    """Parse mods from a JSON string into a list of APIMod dicts.

    The admin frontend stores mods as a JSON array of acronym strings
    (e.g. ``["HD","NF"]``).  The rest of the server (cron jobs, room
    creation) expects APIMod dicts (``[{"acronym": "HD"}, ...]``).
    This helper normalises both formats and silently falls back to []
    on parse errors so a bad payload never crashes an endpoint.
    """
    try:
        parsed = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    result: list[APIMod] = []
    for item in parsed:
        if isinstance(item, str):
            result.append(APIMod(acronym=item))
        elif isinstance(item, dict) and "acronym" in item:
            result.append(cast(APIMod, {k: v for k, v in item.items()}))
    return result


async def _evict_user_from_live_services(session: Database, redis: Redis, user: User) -> None:
    user_id = user.id
    if user_id is None:
        return

    revoked_tokens = await invalidate_user_tokens(session, user_id)
    logger.info(f"Revoked {revoked_tokens} tokens for restricted user {user_id}")

    sockets = list(server.connect_client.get(user_id, set()))
    for websocket in sockets:
        try:
            await websocket.close(code=4003, reason="Account restricted")
        except Exception:
            pass

    try:
        await server.disconnect(user, session)
    except Exception as e:
        logger.debug(f"Failed to disconnect restricted user {user_id} from notification server: {e}")

    try:
        await redis.delete(f"metadata:online:{user_id}")
        await redis.srem("metadata:online_users_set", user_id)
        await redis.publish("osu-channel:user:invalidate", json.dumps({"user_id": user_id}))
    except Exception as e:
        logger.debug(f"Failed to clear online presence for restricted user {user_id}: {e}")


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

    user_dict["torii_titles"] = user.torii_titles or []

    # Handle badges - serialize datetime to ISO string
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
        user_badges_list = (
            await session.exec(
                select(UserBadge).where(UserBadge.user_id == user.id).order_by(col(UserBadge.awarded_at).desc())
            )
        ).all()
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

        # Combine both, preferring DB badges.
        user_dict["badges"] = db_badges + legacy_badges
    except Exception:
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


class AdminLoginLogItemResp(BaseModel):
    id: int
    user_id: int
    username: str | None = None
    ip_address: str
    user_agent: str | None = None
    login_time: datetime
    login_success: bool
    login_method: str
    client_label: str | None = None
    client_hash: str | None = None
    notes: str | None = None
    country_code: str | None = None
    country_name: str | None = None
    city_name: str | None = None
    organization: str | None = None


class AdminLoginLogListResp(BaseModel):
    total: int
    page: int
    per_page: int
    logs: list[AdminLoginLogItemResp]


class UnknownClientHashResp(BaseModel):
    hash: str
    count: int
    first_seen: str | None = None
    last_seen: str | None = None
    last_user_id: int | None = None
    last_user_agent: str | None = None
    last_detected_os: str | None = None
    last_source: str | None = None


class UnknownClientHashListResp(BaseModel):
    total: int
    page: int
    per_page: int
    hashes: list[UnknownClientHashResp]


class AssignClientHashReq(BaseModel):
    client_hash: str
    client_name: str
    version: str = ""
    os: str = ""
    remove_from_unknown: bool = True


async def _count_online_users(redis) -> int:
    """Count online users with set-first strategy and SCAN fallback."""
    try:
        online_set_key = "metadata:online_users_set"
        if await redis.exists(online_set_key):
            return int(await redis.scard(online_set_key))
    except Exception:
        pass

    try:
        cursor = 0
        online_count = 0
        max_iterations = 500
        iterations = 0
        while True:
            cursor, keys = await redis.scan(cursor, match="metadata:online:*", count=1000)
            online_count += len(keys)
            iterations += 1
            if cursor == 0 or iterations >= max_iterations:
                break
        return online_count
    except Exception:
        return 0


class UserUpdateRequest(BaseModel):
    username: str | None = None
    country_code: str | None = None
    is_qat: bool | None = None
    is_gmt: bool | None = None
    is_admin: bool | None = None
    # Accept legacy payloads from older frontend builds (dict/str/list)
    badge: dict | str | list[dict] | None = None
    # List of TORII_GROUPS keys to assign as custom titles (replaces the full list)
    torii_titles: list[str] | None = None


class BeatmapBlacklistItem(BaseModel):
    id: int
    beatmapset_id: int
    beatmap_id: int
    source: str = "manual"
    reason: str | None = None
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


class GlobalAnnouncementReq(BaseModel):
    title: str = "Server Announcement"
    message: str
    severity: str = "warning"
    also_send_pm: bool = True
    online_only: bool = True
    sender_username: str | None = None
    sender_user_id: int | None = None


class GlobalAnnouncementResp(BaseModel):
    sent_to: int
    severity: str
    title: str
    online_only: bool
    sender_username: str


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
    online_users = await _count_online_users(redis)

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
    perf_urls = (
        "http://performance-server:8080/health",
        "http://performance-server:8080/",
        "http://localhost:8080/health",
        "http://localhost:8080/",
    )
    try:
        async with httpx.AsyncClient() as client:
            for url in perf_urls:
                try:
                    resp = await client.get(url, timeout=1.5)
                    if resp.status_code < 500:
                        performance_server_status = "online"
                        break
                except Exception:
                    continue
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


@router.get(
    "/admin/login-logs",
    name="Get login history logs",
    tags=["管理", "g0v0 API"],
    response_model=AdminLoginLogListResp,
)
async def get_admin_login_logs(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    search: str = Query(""),
    user_id: int | None = Query(None, ge=0),
    login_success: bool | None = Query(None),
    login_method: str | None = Query(None),
):
    await require_admin(session, user_and_token)

    conditions = []
    search_value = search.strip()

    if user_id is not None:
        conditions.append(col(UserLoginLog.user_id) == user_id)

    if login_success is not None:
        conditions.append(col(UserLoginLog.login_success) == login_success)

    if login_method:
        conditions.append(col(UserLoginLog.login_method).ilike(f"%{login_method.strip()}%"))

    if search_value:
        username_ids = (
            await session.exec(
                select(User.id).where(col(User.username).ilike(f"%{search_value}%")).limit(500)
            )
        ).all()

        text_condition = sql_or(
            col(UserLoginLog.ip_address).ilike(f"%{search_value}%"),
            col(UserLoginLog.user_agent).ilike(f"%{search_value}%"),
            col(UserLoginLog.client_label).ilike(f"%{search_value}%"),
            col(UserLoginLog.client_hash).ilike(f"%{search_value}%"),
            col(UserLoginLog.notes).ilike(f"%{search_value}%"),
            col(UserLoginLog.country_name).ilike(f"%{search_value}%"),
            col(UserLoginLog.city_name).ilike(f"%{search_value}%"),
            col(UserLoginLog.organization).ilike(f"%{search_value}%"),
            col(UserLoginLog.login_method).ilike(f"%{search_value}%"),
        )

        if search_value.isdigit():
            text_condition = sql_or(text_condition, col(UserLoginLog.user_id) == int(search_value))

        if username_ids:
            text_condition = sql_or(text_condition, col(UserLoginLog.user_id).in_(username_ids))

        conditions.append(text_condition)

    count_stmt = select(func.count()).select_from(UserLoginLog)
    data_stmt = select(UserLoginLog)
    if conditions:
        count_stmt = count_stmt.where(*conditions)
        data_stmt = data_stmt.where(*conditions)

    total = (await session.exec(count_stmt)).one()
    rows = (
        await session.exec(
            data_stmt.order_by(col(UserLoginLog.login_time).desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
    ).all()

    user_ids = sorted({row.user_id for row in rows if row.user_id > 0})
    username_map: dict[int, str] = {}
    if user_ids:
        users = (
            await session.exec(
                select(User.id, User.username).where(col(User.id).in_(user_ids))
            )
        ).all()
        username_map = {uid: uname for uid, uname in users}

    logs = [
        AdminLoginLogItemResp(
            id=row.id or 0,
            user_id=row.user_id,
            username=username_map.get(row.user_id),
            ip_address=row.ip_address,
            user_agent=row.user_agent,
            login_time=row.login_time,
            login_success=row.login_success,
            login_method=row.login_method,
            client_label=row.client_label,
            client_hash=row.client_hash,
            notes=row.notes,
            country_code=row.country_code,
            country_name=row.country_name,
            city_name=row.city_name,
            organization=row.organization,
        )
        for row in rows
    ]

    return AdminLoginLogListResp(total=total, page=page, per_page=per_page, logs=logs)


@router.get(
    "/admin/client-hashes/unknown",
    name="Get unknown client hashes",
    tags=["管理", "g0v0 API"],
    response_model=UnknownClientHashListResp,
)
async def get_unknown_client_hashes(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    verification_service: ClientVerificationService,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    search: str = Query(""),
):
    await require_admin(session, user_and_token)

    unknown = await verification_service.get_unknown_hashes()
    items: list[UnknownClientHashResp] = []
    search_value = search.strip().lower()

    for hash_value, data in unknown.items():
        entry = UnknownClientHashResp(
            hash=hash_value,
            count=int(data.get("count", 0) or 0),
            first_seen=str(data.get("first_seen")) if data.get("first_seen") else None,
            last_seen=str(data.get("last_seen")) if data.get("last_seen") else None,
            last_user_id=int(data["last_user_id"]) if data.get("last_user_id") is not None else None,
            last_user_agent=str(data.get("last_user_agent")) if data.get("last_user_agent") else None,
            last_detected_os=str(data.get("last_detected_os")) if data.get("last_detected_os") else None,
            last_source=str(data.get("last_source")) if data.get("last_source") else None,
        )
        if search_value:
            search_blob = " ".join(
                [
                    entry.hash,
                    entry.last_user_agent or "",
                    entry.last_source or "",
                    str(entry.last_user_id or ""),
                ]
            ).lower()
            if search_value not in search_blob:
                continue
        items.append(entry)

    items.sort(key=lambda x: (x.last_seen or "", x.count), reverse=True)
    total = len(items)
    start = (page - 1) * per_page
    end = start + per_page
    return UnknownClientHashListResp(total=total, page=page, per_page=per_page, hashes=items[start:end])


@router.post(
    "/admin/client-hashes/assign",
    name="Assign unknown client hash",
    tags=["管理", "g0v0 API"],
)
async def assign_unknown_client_hash(
    req: AssignClientHashReq,
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    verification_service: ClientVerificationService,
):
    await require_admin(session, user_and_token)
    input_hash = req.client_hash.strip().lower()
    normalized_hash, ambiguous = await verification_service.resolve_hash_input(input_hash)
    if ambiguous:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Hash prefix is ambiguous; provide a longer hash.",
                "input_hash": input_hash,
                "candidates": ambiguous[:10],
            },
        )

    try:
        await verification_service.assign_hash_override(
            normalized_hash,
            client_name=req.client_name,
            version=req.version,
            os_name=req.os,
            remove_from_unknown=req.remove_from_unknown,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    resolved = await verification_service.validate_client_version(normalized_hash)
    resolved_name = (resolved.client_name or "").strip()
    resolved_version = (resolved.version or "").strip()
    resolved_os = (resolved.os or "").strip()
    resolved_label = " ".join(part for part in (resolved_name, resolved_version) if part).strip()
    if resolved_os:
        resolved_label = f"{resolved_label} ({resolved_os})" if resolved_label else resolved_os

    updated_login_logs = 0
    if resolved_label:
        login_rows = (
            await session.exec(
                select(UserLoginLog).where(
                    col(UserLoginLog.client_hash) == normalized_hash,
                )
            )
        ).all()
        for row in login_rows:
            if row.client_label != resolved_label:
                row.client_label = resolved_label
                session.add(row)
                updated_login_logs += 1

    updated_score_tokens = 0
    hash_prefix20 = normalized_hash[:20]
    hash_prefix12 = normalized_hash[:12]
    score_rows = (
        await session.exec(
            select(ScoreToken).where(
                sql_or(
                    col(ScoreToken.client_version).like(f"hash:{hash_prefix20}%"),
                    col(ScoreToken.client_version).like(f"%(hash:{hash_prefix12}%)%"),
                )
            )
        )
    ).all()
    for token_row in score_rows:
        current = (token_row.client_version or "").strip()
        if (
            current.startswith(f"hash:{hash_prefix20}")
            or f"(hash:{hash_prefix12})" in current
        ):
            token_row.client_version = resolved_label or current
            session.add(token_row)
            updated_score_tokens += 1

    if updated_login_logs or updated_score_tokens:
        await session.commit()

    return {
        "ok": True,
        "input_hash": input_hash,
        "hash": normalized_hash,
        "resolved_os": resolved_os or None,
        "updated_login_logs": updated_login_logs,
        "updated_score_tokens": updated_score_tokens,
    }


@router.post(
    "/admin/global-announcement",
    name="å‘é€å…¨æœå…¬å‘Š",
    tags=["ç®¡ç†", "g0v0 API", "é€šçŸ¥"],
    response_model=GlobalAnnouncementResp,
)
async def send_global_announcement(
    session: Database,
    req: GlobalAnnouncementReq,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Send a global in-app announcement, optionally mirrored as PM from a bot/admin account."""
    current_user = await require_admin(session, user_and_token)

    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="message cannot be empty")

    severity = req.severity.lower()
    if severity not in {"info", "warning", "error"}:
        raise HTTPException(status_code=422, detail="severity must be one of: info, warning, error")

    sender: User | None = None
    if req.sender_user_id is not None:
        sender = await session.get(User, req.sender_user_id)
    elif req.sender_username:
        sender = (
            await session.exec(
                select(User).where(col(User.username) == req.sender_username.strip()).limit(1)
            )
        ).first()
    if sender is None:
        sender = await session.get(User, BANCHOBOT_ID)
    if sender is None:
        raise HTTPException(status_code=500, detail="Announcement sender user not found")
    sender_username = sender.username

    if req.online_only:
        connected_user_ids = [uid for uid, sockets in server.connect_client.items() if sockets]
        if not connected_user_ids:
            receivers: list[int] = []
        else:
            receivers = (
                await session.exec(
                    select(User.id).where(
                        col(User.id).in_(connected_user_ids),
                        User.id != BANCHOBOT_ID,
                        User.id != sender.id,
                        ~User.is_restricted_query(col(User.id)),
                    )
                )
            ).all()
    else:
        receivers = (
            await session.exec(
                select(User.id).where(
                    User.id != BANCHOBOT_ID,
                    User.id != sender.id,
                    ~User.is_restricted_query(col(User.id)),
                )
            )
        ).all()

    announcement = GlobalAnnouncement.init(
        source_user_id=current_user.id,
        title=req.title.strip() or "Server Announcement",
        message=message,
        severity=severity,  # pyright: ignore[reportArgumentType]
        receiver_ids=receivers,
    )
    await server.new_private_notification(announcement)

    if req.also_send_pm and receivers:
        targets = (
            await session.exec(
                select(User).where(
                    col(User.id).in_(receivers),
                )
            )
        ).all()

        for target in targets:
            channel = await ChatChannel.get_pm_channel(target.id, sender.id, session)
            if channel is None:
                user_min = min(target.id, sender.id)
                user_max = max(target.id, sender.id)
                channel = ChatChannel(
                    channel_name=f"pm_{user_min}_{user_max}",
                    description="Private message channel",
                    type=ChannelType.PM,
                )
                session.add(channel)
                await session.flush()
                await session.refresh(channel)

            await server.batch_join_channel([target, sender], channel)

            chat_msg = ChatMessage(
                channel_id=channel.channel_id,
                sender_id=sender.id,
                type=MessageType.PLAIN,
                content=f"[{announcement.title}] {message}",
            )
            session.add(chat_msg)
            await session.flush()
            await session.refresh(chat_msg)

            chat_resp = await ChatMessageModel.transform(chat_msg, includes=["sender"])
            await server.send_message_to_channel(chat_resp)
            pm_detail = ChannelMessage.init(
                message=chat_msg,
                user=sender,
                receiver=[target.id],
                channel_type=ChannelType.PM,
            )
            await server.new_private_notification(pm_detail)

        await session.commit()

    return GlobalAnnouncementResp(
        sent_to=len(receivers),
        severity=severity,
        title=announcement.title,
        online_only=req.online_only,
        sender_username=sender_username,
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
        normalized_username = user_data.username.strip()
        if not normalized_username:
            raise HTTPException(status_code=422, detail="username cannot be empty")

        # Avoid uniqueness crashes and return clean validation error.
        existing_user = (
            await session.exec(
                select(User.id).where(
                    col(User.username) == normalized_username,
                    User.id != user_id,
                ).limit(1)
            )
        ).first()
        if existing_user is not None:
            raise HTTPException(status_code=422, detail="username is already in use")

        if normalized_username != user.username:
            user.username = normalized_username

    if user_data.country_code is not None:
        normalized_country = user_data.country_code.strip().upper()
        user.country_code = normalized_country if normalized_country else None

    if user_data.is_qat is not None:
        user.is_qat = user_data.is_qat

    if user_data.is_gmt is not None:
        user.is_gmt = user_data.is_gmt

    if user_data.is_admin is not None:
        user.is_admin = user_data.is_admin

    if user_data.torii_titles is not None:
        from app.models.torii_groups import TORII_GROUPS
        unknown = [k for k in user_data.torii_titles if k not in TORII_GROUPS]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown torii_titles keys: {unknown}. Valid keys: {sorted(TORII_GROUPS)}",
            )
        user.torii_titles = user_data.torii_titles

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
        elif isinstance(user_data.badge, list):
            # Legacy frontend may send a list of badge dicts. Keep only JSON-safe dict entries.
            safe_badges: list[dict[str, Any]] = []
            for entry in user_data.badge:
                if not isinstance(entry, dict):
                    continue
                awarded_at = entry.get("awarded_at")
                if isinstance(awarded_at, datetime):
                    awarded_at = awarded_at.isoformat()
                elif not isinstance(awarded_at, str):
                    awarded_at = datetime.now().isoformat()

                safe_badges.append(
                    {
                        "awarded_at": awarded_at,
                        "description": entry.get("description", ""),
                        "image_url": entry.get("icon_url") or entry.get("image_url", ""),
                        "image@2x_url": entry.get("image@2x_url")
                        or entry.get("icon_url")
                        or entry.get("image_url", ""),
                        "url": entry.get("url", ""),
                    }
                )
            user.badges = cast(Any, safe_badges)
        else:
            user.badges = []

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        error_text = str(exc.orig).lower() if exc.orig else str(exc).lower()
        if "username" in error_text and "duplicate" in error_text:
            raise HTTPException(status_code=422, detail="username is already in use")
        raise HTTPException(status_code=422, detail="invalid user update payload")
    await session.refresh(user)

    # Notify connected lazer clients that this user's public payload has
    # changed (groups / titles flipping in or out, badge awarded, country
    # rename, …). They re-fetch via GetUserRequest and re-render badges +
    # auras + the rest in place — no sign-out / restart required, and no
    # need for the admin to ping the user manually.
    from app.service.user_update_publisher import publish_user_updated
    await publish_user_updated(user.id)

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
    redis: Redis,
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
    await session.refresh(user)
    await _evict_user_from_live_services(session, redis, user)


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
                source=banned_item.source,
                reason=banned_item.reason,
                beatmapset=beatmapset_dict,
            )
        )

    return result


class BeatmapBlacklistRequest(BaseModel):
    beatmapset_id: int | None = None
    beatmap_id: int | None = None

    @model_validator(mode="after")
    def validate_target(self):
        if self.beatmapset_id is None and self.beatmap_id is None:
            raise ValueError("Either beatmapset_id or beatmap_id is required")
        if self.beatmapset_id is not None and self.beatmap_id is not None:
            raise ValueError("Provide only one of beatmapset_id or beatmap_id")
        return self


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
    """Add a beatmap or beatmapset to blacklist (admin only)"""
    await require_admin(session, user_and_token)

    if request.beatmap_id is not None:
        beatmap_id = request.beatmap_id
        beatmap = await session.get(Beatmap, beatmap_id)
        if not beatmap:
            raise HTTPException(status_code=404, detail="Beatmap not found")

        existing_banned = (
            await session.exec(
                select(BannedBeatmaps).where(BannedBeatmaps.beatmap_id == beatmap_id)
            )
        ).first()
        if existing_banned:
            raise HTTPException(status_code=400, detail="Beatmap is already blacklisted")

        session.add(BannedBeatmaps(
            beatmap_id=beatmap_id,
            source="manual",
            reason="manual admin blacklist",
        ))
        await session.commit()
        return {
            "beatmap_id": beatmap_id,
            "beatmapset_id": beatmap.beatmapset_id,
            "message": "Beatmap added to blacklist",
        }

    beatmapset_id = request.beatmapset_id
    if beatmapset_id is None:
        raise HTTPException(status_code=422, detail="beatmapset_id is required")

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
        banned_item = BannedBeatmaps(
            beatmap_id=beatmap.id,
            source="manual",
            reason="manual admin beatmapset blacklist",
        )
        session.add(banned_item)

    await session.commit()

    return {"beatmapset_id": beatmapset_id, "message": "Beatmapset added to blacklist"}


@router.delete(
    "/admin/beatmaps/blacklist/beatmap/{beatmap_id}",
    name="ä»Žé»‘åå•ç§»é™¤å•ä¸ªè°±é¢",
    tags=["ç®¡ç†", "g0v0 API"],
    status_code=204,
)
async def remove_blacklisted_single_beatmap(
    session: Database,
    beatmap_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Remove a single beatmap from blacklist (admin only)"""
    await require_admin(session, user_and_token)

    banned_item = (
        await session.exec(
            select(BannedBeatmaps).where(BannedBeatmaps.beatmap_id == beatmap_id)
        )
    ).first()
    if not banned_item:
        raise HTTPException(status_code=404, detail="Beatmap not in blacklist")

    await session.delete(banned_item)
    await session.commit()


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


@router.post(
    "/admin/beatmaps/blacklist/reverify",
    name="Re-verify banned beatmaps",
    tags=["管理", "g0v0 API"],
)
async def reverify_blacklisted_beatmaps(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """
    Re-verify automatic entries in BannedBeatmaps using the correct .osu fetcher.
    Manual blacklist entries are preserved.
    The next hourly recalculate_banned_beatmap task will restore PP and BestScore
    entries for any maps removed here. Admin only.
    """
    await require_admin(session, user_and_token)
    from app.tasks.recalculate_banned_beatmap import reverify_banned_beatmaps
    result = await reverify_banned_beatmaps()
    return result


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

    try:
        # Join with User to get username
        statement = (
            select(UserBadge, User.username)
            .outerjoin(User, col(UserBadge.user_id) == User.id)
            .order_by(col(UserBadge.id).desc())
        )
        results = (await session.exec(statement)).all()
    except Exception:
        # Keep admin page usable even if table/schema is missing in a partially migrated environment.
        return []

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
    storage: StorageService,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    flag: bytes | None = File(None),
    cover: bytes | None = File(None),
    name: str | None = Form(None, max_length=100),
    short_name: str | None = Form(None, max_length=10),
    leader_id: int | None = Form(None),
    playmode: GameMode | None = Form(None),
    description: str | None = Form(None, max_length=2000),
    website: str | None = Form(None, max_length=255),
):
    """Update team (admin only)."""
    await require_admin(session, user_and_token)

    team = await session.get(Team, team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    if name is not None:
        clean_name = name.strip()
        if not clean_name:
            raise HTTPException(status_code=400, detail="Team name cannot be empty")
        if (
            await session.exec(
                select(exists()).where(
                    Team.name == clean_name,
                    Team.id != team_id,
                )
            )
        ).first():
            raise HTTPException(status_code=409, detail="Name already exists")
        team.name = clean_name

    if short_name is not None:
        clean_short_name = short_name.strip()
        if not clean_short_name:
            raise HTTPException(status_code=400, detail="Team short name cannot be empty")
        if (
            await session.exec(
                select(exists()).where(
                    Team.short_name == clean_short_name,
                    Team.id != team_id,
                )
            )
        ).first():
            raise HTTPException(status_code=409, detail="Short name already exists")
        team.short_name = clean_short_name

    if playmode is not None:
        team.playmode = playmode

    if description is not None:
        clean_description = description.strip()
        team.description = clean_description or None

    if website is not None:
        clean_website = website.strip()
        if clean_website and not (clean_website.startswith("http://") or clean_website.startswith("https://")):
            clean_website = "https://" + clean_website
        team.website = clean_website or None

    if flag is not None:
        fmt = check_image(flag, 2 * 1024 * 1024, 240, 120)
        if old_flag := team.flag_url:
            if path := storage.get_file_name_by_url(old_flag):
                await storage.delete_file(path)
        filehash = hashlib.sha256(flag).hexdigest()
        storage_path = f"team_flag/{team.id}_{filehash}.png"
        if not await storage.is_exists(storage_path):
            await storage.write_file(storage_path, flag, f"image/{fmt}")
        team.flag_url = await storage.get_file_url(storage_path)

    if cover is not None:
        fmt = check_image(cover, 10 * 1024 * 1024, 3000, 2000)
        if old_cover := team.cover_url:
            if path := storage.get_file_name_by_url(old_cover):
                await storage.delete_file(path)
        filehash = hashlib.sha256(cover).hexdigest()
        storage_path = f"team_cover/{team.id}_{filehash}.png"
        if not await storage.is_exists(storage_path):
            await storage.write_file(storage_path, cover, f"image/{fmt}")
        team.cover_url = await storage.get_file_url(storage_path)

    if leader_id is not None:
        if not (await session.exec(select(exists()).where(User.id == leader_id))).first():
            raise HTTPException(status_code=404, detail="Leader not found")
        is_member = (
            await session.exec(
                select(exists()).where(
                    TeamMember.user_id == leader_id,
                    TeamMember.team_id == team_id,
                )
            )
        ).first()
        if not is_member:
            raise HTTPException(status_code=404, detail="Leader is not a member of the team")
        team.leader_id = leader_id

    await session.commit()
    await session.refresh(team)

    redis = get_redis()
    cache_service = get_ranking_cache_service(redis)
    await cache_service.invalidate_team_cache()

    return team


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

    required_mods_list = _parse_mods_raw(challenge_data.required_mods)
    allowed_mods_list = _parse_mods_raw(challenge_data.allowed_mods)

    # Store mods in canonical APIMod format so that the cron job can consume
    # them directly without further conversion.
    required_mods_json = json.dumps(required_mods_list)
    allowed_mods_json = json.dumps(allowed_mods_list)

    # Create new challenge with enhanced fields
    new_challenge = DailyChallenge(
        date=challenge_date,
        beatmap_id=challenge_data.beatmap_id,
        ruleset_id=challenge_data.ruleset_id,
        required_mods=required_mods_json,
        allowed_mods=allowed_mods_json,
        room_id=getattr(challenge_data, 'room_id', None),
        max_attempts=getattr(challenge_data, 'max_attempts', None),
        time_limit=getattr(challenge_data, 'time_limit', None),
    )

    # Sync to Redis (matching tools/add_daily_challenge.py)
    redis = get_redis()
    redis_key = f"daily_challenge:{challenge_date}"

    await redis.hset(
        redis_key,
        mapping={
            "beatmap": new_challenge.beatmap_id,
            "ruleset_id": new_challenge.ruleset_id,
            "required_mods": required_mods_json,
            "allowed_mods": allowed_mods_json,
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
            required_mods=required_mods_list,
            allowed_mods=allowed_mods_list,
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
        # Normalise to APIMod format (frontend sends acronym strings)
        challenge.required_mods = json.dumps(_parse_mods_raw(challenge_data.required_mods))
    if getattr(challenge_data, 'allowed_mods', None) is not None:
        challenge.allowed_mods = json.dumps(_parse_mods_raw(challenge_data.allowed_mods))
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

        required_mods_list = _parse_mods_raw(challenge.required_mods)
        allowed_mods_list = _parse_mods_raw(challenge.allowed_mods)

        room = await create_daily_challenge_room(
            beatmap=challenge.beatmap_id,
            ruleset_id=challenge.ruleset_id,
            duration=duration,
            required_mods=required_mods_list,
            allowed_mods=allowed_mods_list,
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
