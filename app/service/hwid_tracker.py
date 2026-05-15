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
