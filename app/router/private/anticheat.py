"""Admin endpoints for the anti-cheat audit trail.

These are read-mostly endpoints that let an admin:

- Page through the anti-cheat-generated SuspiciousAlert rows with
  filtering by severity, user, beatmap, score, or kind.
- Inspect the rolling HWID correlation for a specific user (which
  machines they've been seen on and which other accounts share each).
- Reset (resolve) the anti-cheat alerts for a user when manual review
  determines they're legit — e.g. an LAN-party or family-shared PC
  case the HWID detector flagged.

All write-side endpoints require admin auth (same check as the
matchmaking / user-mod admin surfaces). Read endpoints follow the
same convention for consistency, even though the data is not
sensitive in a "user privacy" sense.
"""

from __future__ import annotations

from typing import Annotated, Any

from app.database.suspicious_alert import SuspiciousAlert
from app.database.user import User
from app.dependencies.database import Database, Redis, get_redis
from app.dependencies.user import UserAndToken, get_client_user_and_token
from app.log import log
from app.service import hwid_tracker
from app.utils import utcnow

from fastapi import HTTPException, Query, Security
from sqlmodel import col, func, select

from .admin import require_admin
from .router import router

logger = log("AnticheatAdmin")


def _serialize_alert(alert: SuspiciousAlert) -> dict[str, Any]:
    return {
        "id": alert.id,
        "kind": alert.kind,
        "severity": alert.severity,
        "user_id": alert.user_id,
        "score_id": alert.score_id,
        "beatmap_id": alert.beatmap_id,
        "title": alert.title,
        "body": alert.body,
        "payload": alert.payload,
        "created_at": alert.created_at.isoformat() if alert.created_at else None,
        "dispatched_at": alert.dispatched_at.isoformat() if alert.dispatched_at else None,
        "resolved_at": alert.resolved_at.isoformat() if alert.resolved_at else None,
    }


@router.get("/admin/anticheat/alerts", tags=["Admin Anti-cheat"])
async def admin_search_anticheat_alerts(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    kind: Annotated[
        str | None,
        Query(description="Filter on alert kind. Defaults to anticheat_score; pass blank for all kinds."),
    ] = "anticheat_score",
    severity: str | None = None,
    user_id: int | None = None,
    score_id: int | None = None,
    beatmap_id: int | None = None,
    unresolved_only: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Page through anti-cheat alert rows with filters.

    Default behaviour returns the most recent UNRESOLVED anticheat_score
    rows so the admin lands on actionable alerts. Pass `unresolved_only=
    false` to include resolved ones (useful for audit lookups).
    """
    await require_admin(session, user_and_token)

    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))

    stmt = select(SuspiciousAlert)
    if kind:
        stmt = stmt.where(SuspiciousAlert.kind == kind)
    if severity:
        stmt = stmt.where(SuspiciousAlert.severity == severity)
    if user_id is not None:
        stmt = stmt.where(SuspiciousAlert.user_id == int(user_id))
    if score_id is not None:
        stmt = stmt.where(SuspiciousAlert.score_id == int(score_id))
    if beatmap_id is not None:
        stmt = stmt.where(SuspiciousAlert.beatmap_id == int(beatmap_id))
    if unresolved_only:
        stmt = stmt.where(SuspiciousAlert.resolved_at.is_(None))

    total_stmt = select(func.count()).select_from(stmt.subquery())
    total = int((await session.exec(total_stmt)).first() or 0)

    rows = (
        await session.exec(
            stmt.order_by(col(SuspiciousAlert.created_at).desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "alerts": [_serialize_alert(a) for a in rows],
    }


@router.get("/admin/anticheat/users/{user_id}/summary", tags=["Admin Anti-cheat"])
async def admin_anticheat_user_summary(
    user_id: int,
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    redis: Redis,
) -> dict[str, Any]:
    """Per-user anti-cheat snapshot: alert counts + HWID correlations."""
    await require_admin(session, user_and_token)

    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")

    total_alerts = int(
        (
            await session.exec(
                select(func.count())
                .select_from(SuspiciousAlert)
                .where(SuspiciousAlert.user_id == user_id)
            )
        ).first()
        or 0
    )
    unresolved_alerts = int(
        (
            await session.exec(
                select(func.count())
                .select_from(SuspiciousAlert)
                .where(SuspiciousAlert.user_id == user_id)
                .where(SuspiciousAlert.resolved_at.is_(None))
            )
        ).first()
        or 0
    )

    hwids = await hwid_tracker.hwids_for(redis, user_id)
    hwid_breakdown = []
    correlated: set[int] = set()
    for h in hwids:
        peers = await hwid_tracker.users_for(redis, h)
        peer_others = [u for u in peers if u != user_id]
        correlated.update(peer_others)
        hwid_breakdown.append(
            {
                "hwid": h,
                "peer_count": len(peer_others),
                "peer_user_ids": peer_others[:50],
            }
        )

    return {
        "user_id": user_id,
        "username": user.username,
        "alert_counts": {
            "total": total_alerts,
            "unresolved": unresolved_alerts,
        },
        "hwid": {
            "known_hwids": hwids,
            "breakdown": hwid_breakdown,
            "correlated_account_count": len(correlated),
            "correlated_user_ids": sorted(correlated)[:200],
        },
    }


@router.post("/admin/anticheat/users/{user_id}/reset-alerts", tags=["Admin Anti-cheat"])
async def admin_reset_anticheat_alerts(
    user_id: int,
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    kind: Annotated[
        str | None,
        Query(description="Only resolve alerts of this kind. Default: anticheat_score."),
    ] = "anticheat_score",
) -> dict[str, Any]:
    """Mark all matching alerts for a user as resolved (manual review
    cleared). Does NOT delete rows — the audit trail stays intact, the
    alerts just stop counting as 'open'."""
    admin_user = await require_admin(session, user_and_token)

    stmt = select(SuspiciousAlert).where(SuspiciousAlert.user_id == user_id)
    if kind:
        stmt = stmt.where(SuspiciousAlert.kind == kind)
    stmt = stmt.where(SuspiciousAlert.resolved_at.is_(None))
    rows = (await session.exec(stmt)).all()

    now = utcnow()
    for alert in rows:
        alert.resolved_at = now
        session.add(alert)
    await session.commit()

    logger.info(
        f"admin {admin_user.username}#{admin_user.id} reset {len(rows)} "
        f"anti-cheat alert(s) for user {user_id} (kind={kind or 'any'})"
    )
    return {
        "user_id": user_id,
        "kind": kind,
        "resolved_count": len(rows),
        "resolved_at": now.isoformat(),
    }


@router.delete("/admin/anticheat/users/{user_id}/hwid", tags=["Admin Anti-cheat"])
async def admin_clear_user_hwid(
    user_id: int,
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    redis: Redis,
) -> dict[str, Any]:
    """Forget a user's recorded HWIDs from Redis. Used when a user
    legitimately changes machines or when an admin verifies a flagged
    HWID overlap was a false positive (shared PC, LAN cafe). The
    middleware will start collecting new HWIDs on the next request."""
    admin_user = await require_admin(session, user_and_token)

    hwids = await hwid_tracker.hwids_for(redis, user_id)
    for h in hwids:
        await redis.srem(f"hwid:hash:{h}", user_id)
    await redis.delete(f"hwid:user:{user_id}")

    logger.info(
        f"admin {admin_user.username}#{admin_user.id} cleared "
        f"{len(hwids)} HWID(s) for user {user_id}"
    )
    return {
        "user_id": user_id,
        "cleared_hwids": hwids,
        "count": len(hwids),
    }
