"""Server-wide maintenance mode toggle.

Backed by a single Redis hash (``torii:maintenance``) so the flag is
shared across every uvicorn worker without a DB round-trip and can be
flipped from any admin endpoint or out-of-band shell.

State shape stored at ``torii:maintenance`` ::

    enabled         "1" | "0"
    message         human-readable string shown to clients (optional)
    set_at          ISO-8601 UTC timestamp of the last enable/disable
    set_by_user_id  numeric user id of the admin who flipped it
    set_by_username username for nice display in audit logs

Self-lockout posture
  Maintenance mode does NOT block authentication, profile reads, or
  admin endpoints. It only gates score submission. Admins can always
  log in and disable maintenance — there's no way for an admin to
  flip the switch and be unable to flip it back. We deliberately did
  NOT implement upstream's "cannot self-disable" behaviour because
  any rule that prevents an admin from undoing their own action is a
  foot-cannon waiting to fire.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from app.utils import utcnow

if TYPE_CHECKING:
    import redis.asyncio as redis_async


_REDIS_KEY = "torii:maintenance"

# Default message when an admin enables maintenance without specifying
# one. Phrased neutrally — the client banner will already render some
# severity styling around it.
DEFAULT_MAINTENANCE_MESSAGE = (
    "Torii is undergoing maintenance. Score submission is temporarily "
    "disabled and will resume shortly."
)


@dataclass(slots=True, frozen=True)
class MaintenanceState:
    enabled: bool
    message: str | None
    set_at: datetime | None
    set_by_user_id: int | None
    set_by_username: str | None


_DISABLED_STATE = MaintenanceState(
    enabled=False, message=None, set_at=None, set_by_user_id=None, set_by_username=None
)


def _decode(value: bytes | str | None) -> str | None:
    """Redis client may or may not auto-decode depending on the
    connection. Centralise the dance here so callers stay clean."""
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


async def get_state(redis: "redis_async.Redis") -> MaintenanceState:
    """Read the maintenance hash. Returns a clean DISABLED sentinel
    when the hash is missing or has enabled=0."""
    raw = await redis.hgetall(_REDIS_KEY)
    if not raw:
        return _DISABLED_STATE

    # Redis hgetall keys may come back as bytes or str depending on
    # decode_responses; normalise both sides.
    fields: dict[str, str | None] = {
        (_decode(k) or ""): _decode(v) for k, v in raw.items()
    }
    if fields.get("enabled") != "1":
        return _DISABLED_STATE

    set_at: datetime | None = None
    if iso := fields.get("set_at"):
        try:
            set_at = datetime.fromisoformat(iso)
        except ValueError:
            set_at = None

    set_by_user_id: int | None = None
    if uid := fields.get("set_by_user_id"):
        try:
            set_by_user_id = int(uid)
        except ValueError:
            set_by_user_id = None

    return MaintenanceState(
        enabled=True,
        message=fields.get("message") or DEFAULT_MAINTENANCE_MESSAGE,
        set_at=set_at,
        set_by_user_id=set_by_user_id,
        set_by_username=fields.get("set_by_username"),
    )


async def is_active(redis: "redis_async.Redis") -> bool:
    """Hot-path helper for score submission. Single HGET, sub-millisecond.
    Falls open (returns False) on any Redis error so a Redis blip can
    never accidentally enable maintenance and lock everyone out."""
    try:
        value = await redis.hget(_REDIS_KEY, "enabled")
        return _decode(value) == "1"
    except Exception:
        return False


async def enable(
    redis: "redis_async.Redis",
    *,
    message: str | None,
    actor_user_id: int,
    actor_username: str | None,
) -> MaintenanceState:
    """Turn maintenance on. Overwrites any prior state. Returns the
    new state so the calling endpoint can echo it back."""
    text = (message or "").strip() or DEFAULT_MAINTENANCE_MESSAGE
    now = utcnow()

    await redis.hset(
        _REDIS_KEY,
        mapping={
            "enabled": "1",
            "message": text,
            "set_at": now.isoformat(),
            "set_by_user_id": str(actor_user_id),
            "set_by_username": actor_username or "",
        },
    )
    return MaintenanceState(
        enabled=True,
        message=text,
        set_at=now,
        set_by_user_id=actor_user_id,
        set_by_username=actor_username,
    )


async def disable(redis: "redis_async.Redis") -> MaintenanceState:
    """Turn maintenance off. We rewrite the hash with enabled=0
    rather than deleting it, so the audit trail (set_at / set_by) of
    the last toggle survives until the next enable. Easier to read
    in admin UI than "no record at all" after a disable."""
    now = utcnow()
    await redis.hset(
        _REDIS_KEY,
        mapping={
            "enabled": "0",
            "set_at": now.isoformat(),
        },
    )
    return _DISABLED_STATE


def to_public_dict(state: MaintenanceState) -> dict:
    """Public-safe representation for unauthenticated banner endpoints.
    Strips the actor identity — no point exposing who flipped the
    switch to logged-out users."""
    return {
        "maintenance": state.enabled,
        "message": state.message if state.enabled else None,
    }


def to_admin_dict(state: MaintenanceState) -> dict:
    """Full representation for admin UI."""
    return {
        "enabled": state.enabled,
        "message": state.message,
        "set_at": state.set_at.isoformat() if state.set_at else None,
        "set_by_user_id": state.set_by_user_id,
        "set_by_username": state.set_by_username,
    }
