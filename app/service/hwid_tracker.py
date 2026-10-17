"""Redis-backed rolling map between the X-Torii-HWID header value and
the user ids that have used it. Tiny lookup helpers used downstream;
the interpretation of the data lives outside this module.

Redis (not MySQL) because writes happen on the hot path: sets are
O(1) per add, MySQL would add a round trip we don't need.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis

# 32 hex chars, lowercase. Anything else is rejected silently so a
# malformed or spoofed header can't poison the Redis keyspace.
_HWID_RE = re.compile(r"^[0-9a-f]{16,64}$")

_TTL_SECONDS = 60 * 24 * 60 * 60  # 60 days


def is_valid(hwid: str | None) -> bool:
    if not hwid:
        return False
    return bool(_HWID_RE.match(hwid))


async def record(redis: "Redis", hwid: str, user_id: int) -> None:
    """Idempotent. Adds (hwid, user_id) to both indexes with TTL refresh.
    Failures are swallowed (logged by caller if it cares)."""
    if not is_valid(hwid) or not user_id:
        return
    pipe = redis.pipeline()
    pipe.sadd(f"hwid:hash:{hwid}", user_id)
    pipe.expire(f"hwid:hash:{hwid}", _TTL_SECONDS)
    pipe.sadd(f"hwid:user:{user_id}", hwid)
    pipe.expire(f"hwid:user:{user_id}", _TTL_SECONDS)
    await pipe.execute()


async def users_for(redis: "Redis", hwid: str) -> list[int]:
    """Return the user-id list this HWID has been seen under."""
    if not is_valid(hwid):
        return []
    raw = await redis.smembers(f"hwid:hash:{hwid}")
    out = []
    for v in raw or []:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return sorted(out)


async def hwids_for(redis: "Redis", user_id: int) -> list[str]:
    if not user_id:
        return []
    raw = await redis.smembers(f"hwid:user:{user_id}")
    return sorted([h for h in (raw or []) if is_valid(h)])


async def is_whitelisted(redis: "Redis", hwid: str) -> bool:
    """Check if a HWID is whitelisted to bypass the 2-account registration limit."""
    if not is_valid(hwid):
        return False
    return bool(await redis.sismember("hwid:whitelist", hwid))


async def add_whitelist(redis: "Redis", hwid: str) -> None:
    """Add a HWID to the whitelist so more accounts can be registered from this device."""
    if is_valid(hwid):
        await redis.sadd("hwid:whitelist", hwid)


async def remove_whitelist(redis: "Redis", hwid: str) -> None:
    """Remove a HWID from the whitelist."""
    if is_valid(hwid):
        await redis.srem("hwid:whitelist", hwid)


async def record_v2(redis: "Redis", hwid_v2: str, user_id: int) -> None:
    """Records deep hardware entropy hash (v2) against user_id."""
    if not is_valid(hwid_v2) or not user_id:
        return
    pipe = redis.pipeline()
    pipe.sadd(f"hwid_v2:hash:{hwid_v2}", user_id)
    pipe.expire(f"hwid_v2:hash:{hwid_v2}", _TTL_SECONDS)
    pipe.sadd(f"hwid_v2:user:{user_id}", hwid_v2)
    pipe.expire(f"hwid_v2:user:{user_id}", _TTL_SECONDS)
    await pipe.execute()


async def users_for_v2(redis: "Redis", hwid_v2: str) -> list[int]:
    if not is_valid(hwid_v2):
        return []
    raw = await redis.smembers(f"hwid_v2:hash:{hwid_v2}")
    out = []
    for v in raw or []:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return sorted(out)


async def v2_for_user(redis: "Redis", user_id: int) -> list[str]:
    if not user_id:
        return []
    raw = await redis.smembers(f"hwid_v2:user:{user_id}")
    return sorted([h for h in (raw or []) if is_valid(h)])


async def record_token(redis: "Redis", token: str, user_id: int) -> None:
    """Records persistent device breadcrumb token against user_id."""
    if not is_valid(token) or not user_id:
        return
    pipe = redis.pipeline()
    pipe.sadd(f"token:hash:{token}", user_id)
    pipe.expire(f"token:hash:{token}", _TTL_SECONDS)
    pipe.sadd(f"token:user:{user_id}", token)
    pipe.expire(f"token:user:{user_id}", _TTL_SECONDS)
    await pipe.execute()


async def users_for_token(redis: "Redis", token: str) -> list[int]:
    if not is_valid(token):
        return []
    raw = await redis.smembers(f"token:hash:{token}")
    out = []
    for v in raw or []:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return sorted(out)


async def tokens_for_user(redis: "Redis", user_id: int) -> list[str]:
    if not user_id:
        return []
    raw = await redis.smembers(f"token:user:{user_id}")
    return sorted([t for t in (raw or []) if is_valid(t)])


async def record_env(redis: "Redis", env_str: str, user_id: int) -> None:
    """Saves the user's latest environment & peripherals telemetry string."""
    if not env_str or not user_id:
        return
    # Strip to reasonable size (max 512 chars)
    clean_env = env_str.strip()[:512]
    await redis.setex(f"user:env:{user_id}", _TTL_SECONDS, clean_env)


async def env_for_user(redis: "Redis", user_id: int) -> str | None:
    if not user_id:
        return None
    raw = await redis.get(f"user:env:{user_id}")
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="ignore")
    return raw


