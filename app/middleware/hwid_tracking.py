"""Reads the X-Torii-HWID header and records it against the
authenticated user. Never blocks the request — recording is a
background task. Only runs on write methods to keep read endpoints
cheap.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable

from app.auth import get_token_by_access_token
from app.dependencies.database import get_redis, with_db
from app.dependencies.geoip import get_client_ip
from app.log import log
from app.service import hwid_tracker

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = log("HwidMiddleware")

_TOKEN_CACHE_TTL = 300  # 5 minutes
_RECORDED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _token_digest(token: str) -> str:
    return hashlib.blake2b(token.encode("utf-8"), digest_size=16).hexdigest()


async def _resolve_user_id(token: str) -> int | None:
    """Token -> user_id, cached. Returns None for unknown/expired tokens."""
    redis = get_redis()
    cache_key = f"hwid:tu:{_token_digest(token)}"
    cached = await redis.get(cache_key)
    if cached:
        try:
            return int(cached)
        except (TypeError, ValueError):
            pass

    async with with_db() as db:
        rec = await get_token_by_access_token(db, token)
        if not rec:
            return None
        await redis.setex(cache_key, _TOKEN_CACHE_TTL, str(rec.user_id))
        return rec.user_id


async def _record_safely(
    token: str,
    hwid: str | None = None,
    hwid_v2: str | None = None,
    device_token: str | None = None,
    env_str: str | None = None,
) -> None:
    try:
        user_id = await _resolve_user_id(token)
        if not user_id:
            return
        redis = get_redis()
        if hwid and hwid_tracker.is_valid(hwid):
            await hwid_tracker.record(redis, hwid, user_id)
        if hwid_v2 and hwid_tracker.is_valid(hwid_v2):
            await hwid_tracker.record_v2(redis, hwid_v2, user_id)
        if device_token and hwid_tracker.is_valid(device_token):
            await hwid_tracker.record_token(redis, device_token, user_id)
        if env_str:
            await hwid_tracker.record_env(redis, env_str, user_id)

        # Multi-accounting check: alert moderators if any identifier is shared with other accounts
        primary_id = hwid or hwid_v2 or device_token
        if not primary_id:
            return

        alert_cache_key = f"hwid:alerted:{user_id}:{primary_id}"
        if not await redis.get(alert_cache_key):
            peers_set: set[int] = set()
            if hwid and hwid_tracker.is_valid(hwid):
                for p in await hwid_tracker.users_for(redis, hwid):
                    if p != user_id:
                        peers_set.add(p)
            if hwid_v2 and hwid_tracker.is_valid(hwid_v2):
                for p in await hwid_tracker.users_for_v2(redis, hwid_v2):
                    if p != user_id:
                        peers_set.add(p)
            if device_token and hwid_tracker.is_valid(device_token):
                for p in await hwid_tracker.users_for_token(redis, device_token):
                    if p != user_id:
                        peers_set.add(p)

            peers = sorted(peers_set)
            if peers:
                # Debounce in Redis for 24 hours so we don't spam per play
                await redis.setex(alert_cache_key, 86400, "1")
                if not (hwid and await hwid_tracker.is_whitelisted(redis, hwid)):
                    from app.service.suspicious_alert_service import SuspiciousAlertService

                    asyncio.create_task(
                        SuspiciousAlertService.alert_hwid_activity(
                            redis=redis,
                            user_id=user_id,
                            hwid=primary_id,
                            peer_ids=peers,
                        )
                    )
    except Exception as e:
        logger.debug(f"hwid record failed: {e}")


class HwidTrackingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        try:
            client_ip = get_client_ip(request)
            if client_ip:
                redis = get_redis()
                if await redis.sismember("ip:blacklist", client_ip):
                    logger.warning(f"Blocked request from blacklisted IP: {client_ip} ({request.method} {request.url.path})")
                    return Response(content="Forbidden", status_code=403)
        except Exception:
            pass

        if request.method not in _RECORDED_METHODS:
            return await call_next(request)

        hwid = request.headers.get("X-Torii-HWID")
        hwid_v2 = request.headers.get("X-Torii-HWID-V2")
        device_token = request.headers.get("X-Torii-Device-Token")
        env_str = request.headers.get("X-Torii-Env")

        valid_hwid = hwid if (hwid and hwid_tracker.is_valid(hwid)) else None
        valid_hwid_v2 = hwid_v2 if (hwid_v2 and hwid_tracker.is_valid(hwid_v2)) else None
        valid_token = device_token if (device_token and hwid_tracker.is_valid(device_token)) else None

        if valid_hwid:
            request.state.torii_hwid = valid_hwid
        if valid_hwid_v2:
            request.state.torii_hwid_v2 = valid_hwid_v2
        if valid_token:
            request.state.torii_device_token = valid_token
        if env_str:
            request.state.torii_env = env_str

        if valid_hwid or valid_hwid_v2 or valid_token:
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                token = auth[7:]
                # fire and forget so the request never waits on Redis
                asyncio.create_task(
                    _record_safely(
                        token=token,
                        hwid=valid_hwid,
                        hwid_v2=valid_hwid_v2,
                        device_token=valid_token,
                        env_str=env_str,
                    )
                )

        return await call_next(request)
