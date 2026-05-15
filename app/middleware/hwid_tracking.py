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


async def _record_safely(token: str, hwid: str) -> None:
    try:
        user_id = await _resolve_user_id(token)
        if not user_id:
            return
        await hwid_tracker.record(get_redis(), hwid, user_id)
    except Exception as e:
        logger.debug(f"hwid record failed: {e}")


class HwidTrackingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.method not in _RECORDED_METHODS:
            return await call_next(request)

        hwid = request.headers.get("X-Torii-HWID")
        if hwid and hwid_tracker.is_valid(hwid):
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                token = auth[7:]
                # store on request.state so endpoints that want it (score
                # submit, anti-cheat) can pick it up without re-reading
                # the header.
                request.state.torii_hwid = hwid
                # fire and forget so the request never waits on Redis
                asyncio.create_task(_record_safely(token, hwid))

        return await call_next(request)
