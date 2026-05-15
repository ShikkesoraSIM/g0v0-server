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

from app.database.beatmap import Beatmap
from app.database.score import Score
from app.database.score_anticheat_analysis import ScoreAnticheatAnalysis
from app.database.suspicious_alert import SuspiciousAlert
from app.database.user import User
from app.dependencies.database import Database, Redis, get_redis
from app.dependencies.user import UserAndToken, get_client_user_and_token
from app.log import log
from app.service import hwid_tracker
from app.utils import utcnow

from fastapi import HTTPException, Query, Security
from sqlalchemy.orm import joinedload
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


# ─── Replay browser ────────────────────────────────────────────────────────


def _serialize_replay_row(score: Score, beatmap: Beatmap | None, user: User | None,
                          analysis: ScoreAnticheatAnalysis | None) -> dict[str, Any]:
    return {
        "score_id": score.id,
        "user_id": score.user_id,
        "username": user.username if user else None,
        "beatmap_id": score.beatmap_id,
        "beatmap_title": (
            f"{beatmap.artist} - {beatmap.title} [{beatmap.version}]"
            if beatmap else None
        ),
        "gamemode": int(getattr(score, "gamemode", 0) or 0),
        "mods": getattr(score, "mods", None) or [],
        "pp": float(getattr(score, "pp", 0.0) or 0.0),
        "accuracy": float(getattr(score, "accuracy", 0.0) or 0.0),
        "max_combo": int(getattr(score, "max_combo", 0) or 0),
        "rank": getattr(score, "rank", None),
        "passed": bool(getattr(score, "passed", False)),
        "has_replay": bool(getattr(score, "has_replay", False)),
        "ended_at": (
            score.ended_at.isoformat()
            if getattr(score, "ended_at", None) else None
        ),
        "analysis": (
            {
                "verdict": analysis.verdict,
                "confidence": analysis.confidence,
                "trust_factor_applied": analysis.trust_factor_applied,
                "detectors_fired": analysis.detectors_fired,
                "replay_was_available": analysis.replay_was_available,
                "analyzer_version": analysis.analyzer_version,
                "error": analysis.error,
                "analyzed_at": analysis.analyzed_at.isoformat() if analysis.analyzed_at else None,
            }
            if analysis else None
        ),
    }


@router.get("/admin/anticheat/replays", tags=["Admin Anti-cheat"])
async def admin_list_replays(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    user_id: int | None = None,
    beatmap_id: int | None = None,
    gamemode: int | None = None,
    verdict: Annotated[
        str | None,
        Query(description="Filter by cached verdict (ok / low_concern / suspicious / critical / errored). 'unanalyzed' = no row."),
    ] = None,
    has_replay: bool | None = None,
    min_pp: float | None = None,
    passed_only: bool = True,
    sort: Annotated[
        str,
        Query(description="latest | top_pp | low_trust"),
    ] = "latest",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Page through scores with their cached anti-cheat analysis joined.

    Filters compose: pick a verdict, a user, a beatmap, a min pp, etc.
    Sort modes give the common admin views without per-filter UI work
    on the client.
    """
    await require_admin(session, user_and_token)

    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))

    stmt = (
        select(Score, ScoreAnticheatAnalysis)
        .outerjoin(
            ScoreAnticheatAnalysis,
            ScoreAnticheatAnalysis.score_id == Score.id,
        )
        .options(joinedload(Score.beatmap), joinedload(Score.user))
    )
    if user_id is not None:
        stmt = stmt.where(Score.user_id == int(user_id))
    if beatmap_id is not None:
        stmt = stmt.where(Score.beatmap_id == int(beatmap_id))
    if gamemode is not None:
        stmt = stmt.where(Score.gamemode == int(gamemode))
    if has_replay is not None:
        stmt = stmt.where(Score.has_replay == bool(has_replay))
    if min_pp is not None:
        stmt = stmt.where(Score.pp >= float(min_pp))
    if passed_only:
        stmt = stmt.where(Score.passed == True)  # noqa: E712
    if verdict:
        v = verdict.lower()
        if v == "unanalyzed":
            stmt = stmt.where(ScoreAnticheatAnalysis.score_id.is_(None))
        else:
            stmt = stmt.where(ScoreAnticheatAnalysis.verdict == v)

    # Count BEFORE sorting / limit, so the pager has the right total.
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = int((await session.exec(count_stmt)).first() or 0)

    if sort == "top_pp":
        stmt = stmt.order_by(col(Score.pp).desc())
    elif sort == "low_trust":
        stmt = stmt.order_by(col(ScoreAnticheatAnalysis.trust_factor_applied).asc().nulls_last())
    else:
        stmt = stmt.order_by(col(Score.ended_at).desc())

    rows = (await session.exec(stmt.limit(limit).offset(offset))).all()
    out = []
    for score, analysis in rows:
        out.append(_serialize_replay_row(score, score.beatmap, score.user, analysis))

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": out,
    }


@router.get("/admin/anticheat/scores/{score_id}/detail", tags=["Admin Anti-cheat"])
async def admin_replay_detail(
    score_id: int,
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    redis: Redis,
) -> dict[str, Any]:
    """Everything we have about one score: score row, cached verdict
    with full reasons + metrics, beatmap, user, HWID context, and the
    list of OTHER scores this same user has on the same beatmap."""
    await require_admin(session, user_and_token)

    score = (
        await session.exec(
            select(Score)
            .where(Score.id == score_id)
            .options(joinedload(Score.beatmap), joinedload(Score.user))
        )
    ).first()
    if score is None:
        raise HTTPException(status_code=404, detail="score not found")

    analysis = (
        await session.exec(
            select(ScoreAnticheatAnalysis).where(
                ScoreAnticheatAnalysis.score_id == score_id
            )
        )
    ).first()

    # Other scores by the same user on the same beatmap — useful for
    # "is this an outlier on this map or a steady improvement?"
    sibling_rows = (
        await session.exec(
            select(Score)
            .where(Score.user_id == score.user_id)
            .where(Score.beatmap_id == score.beatmap_id)
            .where(Score.id != score_id)
            .order_by(col(Score.ended_at).desc())
            .limit(20)
        )
    ).all()
    siblings = [
        {
            "score_id": s.id,
            "pp": float(getattr(s, "pp", 0.0) or 0.0),
            "accuracy": float(getattr(s, "accuracy", 0.0) or 0.0),
            "max_combo": int(getattr(s, "max_combo", 0) or 0),
            "rank": getattr(s, "rank", None),
            "passed": bool(getattr(s, "passed", False)),
            "ended_at": s.ended_at.isoformat() if getattr(s, "ended_at", None) else None,
        }
        for s in sibling_rows
    ]

    # HWIDs the user has shown up on
    user_hwids = await hwid_tracker.hwids_for(redis, score.user_id)

    # Alerts that reference this score (any kind)
    alert_rows = (
        await session.exec(
            select(SuspiciousAlert)
            .where(SuspiciousAlert.score_id == score_id)
            .order_by(col(SuspiciousAlert.created_at).desc())
        )
    ).all()

    return {
        "score": _serialize_replay_row(score, score.beatmap, score.user, analysis),
        "analysis_full": (
            {
                "verdict": analysis.verdict,
                "confidence": analysis.confidence,
                "trust_factor_applied": analysis.trust_factor_applied,
                "detectors_fired": analysis.detectors_fired,
                "reasons": analysis.reasons,
                "metrics": analysis.metrics,
                "replay_was_available": analysis.replay_was_available,
                "analyzer_version": analysis.analyzer_version,
                "error": analysis.error,
                "analyzed_at": analysis.analyzed_at.isoformat() if analysis.analyzed_at else None,
            }
            if analysis else None
        ),
        "siblings_same_map": siblings,
        "hwid": {
            "known_hwids": user_hwids,
        },
        "alerts": [_serialize_alert(a) for a in alert_rows],
    }


@router.post("/admin/anticheat/scores/{score_id}/reanalyze", tags=["Admin Anti-cheat"])
async def admin_reanalyze_score(
    score_id: int,
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
) -> dict[str, Any]:
    """Force the external detection service to re-evaluate a score.

    Runs synchronously so the admin sees the verdict right away. The
    upsert path inside `_submit_to_anticheat_background` overwrites
    the cached analysis row with the new result.
    """
    admin_user = await require_admin(session, user_and_token)

    score = await session.get(Score, score_id)
    if score is None:
        raise HTTPException(status_code=404, detail="score not found")

    from app.database.score import _submit_to_anticheat_background
    from app.dependencies.database import engine

    try:
        await _submit_to_anticheat_background(engine, score_id)
    except Exception as e:
        logger.warning(f"manual reanalyze failed for score {score_id}: {e}")
        raise HTTPException(status_code=500, detail=f"reanalyze failed: {e}")

    # Re-read the row so we can return the fresh verdict.
    analysis = (
        await session.exec(
            select(ScoreAnticheatAnalysis).where(
                ScoreAnticheatAnalysis.score_id == score_id
            )
        )
    ).first()

    logger.info(
        f"admin {admin_user.username}#{admin_user.id} re-analysed score {score_id}: "
        f"{analysis.verdict if analysis else 'no result'}"
    )

    return {
        "score_id": score_id,
        "analyzed_at": analysis.analyzed_at.isoformat() if analysis and analysis.analyzed_at else None,
        "verdict": analysis.verdict if analysis else None,
        "confidence": analysis.confidence if analysis else None,
        "detectors_fired": analysis.detectors_fired if analysis else [],
        "reasons": analysis.reasons if analysis else [],
        "metrics": analysis.metrics if analysis else {},
        "trust_factor_applied": analysis.trust_factor_applied if analysis else None,
        "replay_was_available": analysis.replay_was_available if analysis else None,
        "error": analysis.error if analysis else None,
    }


# ─── Bulk re-analyze job ───────────────────────────────────────────────────
#
# State lives in Redis under `ac:reanalyze_job:<id>` as a JSON dict. Jobs
# self-expire after 24h. The worker is an asyncio.create_task that walks
# the score table in batches and fires _submit_to_anticheat_background
# for each — same code path the live score-submit hook uses.

import asyncio as _asyncio
import json as _json
import uuid as _uuid


_BULK_JOB_TTL_SECONDS = 24 * 3600


def _bulk_job_key(job_id: str) -> str:
    return f"ac:reanalyze_job:{job_id}"


async def _bulk_job_write(redis, job_id: str, state: dict[str, Any]) -> None:
    await redis.setex(_bulk_job_key(job_id), _BULK_JOB_TTL_SECONDS, _json.dumps(state))


async def _bulk_reanalyze_worker(
    engine,
    redis,
    job_id: str,
    *,
    user_id: int | None,
    beatmap_id: int | None,
    gamemode: int | None,
    only_unanalyzed: bool,
    only_with_replay: bool,
    min_pp: float | None,
    max_count: int,
):
    """Background worker. Walks matching scores in batches and reanalyses
    each. Writes progress to Redis every batch."""
    from sqlmodel.ext.asyncio.session import AsyncSession
    from app.database.score import _submit_to_anticheat_background
    from app.database.score_anticheat_analysis import ScoreAnticheatAnalysis as _SAA

    state = {
        "id": job_id,
        "status": "running",
        "total": 0,
        "processed": 0,
        "errors": 0,
        "started_at": utcnow().isoformat(),
        "finished_at": None,
        "filters": {
            "user_id": user_id,
            "beatmap_id": beatmap_id,
            "gamemode": gamemode,
            "only_unanalyzed": only_unanalyzed,
            "only_with_replay": only_with_replay,
            "min_pp": min_pp,
            "max_count": max_count,
        },
    }
    try:
        async with AsyncSession(engine) as s:
            stmt = (
                select(Score.id)
                .outerjoin(_SAA, _SAA.score_id == Score.id)
                .where(Score.passed == True)  # noqa: E712
            )
            if user_id is not None:
                stmt = stmt.where(Score.user_id == int(user_id))
            if beatmap_id is not None:
                stmt = stmt.where(Score.beatmap_id == int(beatmap_id))
            if gamemode is not None:
                stmt = stmt.where(Score.gamemode == int(gamemode))
            if only_with_replay:
                stmt = stmt.where(Score.has_replay == True)  # noqa: E712
            if only_unanalyzed:
                stmt = stmt.where(_SAA.score_id.is_(None))
            if min_pp is not None:
                stmt = stmt.where(Score.pp >= float(min_pp))
            stmt = stmt.order_by(col(Score.ended_at).desc()).limit(max_count)
            score_ids = list((await s.exec(stmt)).all())

        state["total"] = len(score_ids)
        await _bulk_job_write(redis, job_id, state)

        # Process in small batches so progress updates are responsive.
        BATCH = 5
        for i in range(0, len(score_ids), BATCH):
            chunk = score_ids[i : i + BATCH]
            results = await _asyncio.gather(
                *[_submit_to_anticheat_background(engine, sid) for sid in chunk],
                return_exceptions=True,
            )
            for r in results:
                state["processed"] += 1
                if isinstance(r, Exception):
                    state["errors"] += 1
            await _bulk_job_write(redis, job_id, state)

        state["status"] = "completed"
    except Exception as e:
        state["status"] = "failed"
        state["error"] = str(e)
    finally:
        state["finished_at"] = utcnow().isoformat()
        await _bulk_job_write(redis, job_id, state)


@router.post("/admin/anticheat/reanalyze-bulk", tags=["Admin Anti-cheat"])
async def admin_reanalyze_bulk(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    redis: Redis,
    user_id: int | None = None,
    beatmap_id: int | None = None,
    gamemode: int | None = None,
    only_unanalyzed: bool = True,
    only_with_replay: bool = True,
    min_pp: float | None = None,
    max_count: int = 1000,
) -> dict[str, Any]:
    """Kick off a background job that re-analyses matching scores.

    Defaults to "unanalyzed scores with a replay, capped at 1000" — a
    safe one-button "fill in the cache for everything new" action.
    Returns a job_id you can poll via GET /admin/anticheat/reanalyze-jobs/{job_id}.
    """
    admin_user = await require_admin(session, user_and_token)

    max_count = max(1, min(int(max_count), 50000))

    job_id = _uuid.uuid4().hex
    initial = {
        "id": job_id,
        "status": "queued",
        "total": 0,
        "processed": 0,
        "errors": 0,
        "started_at": utcnow().isoformat(),
        "finished_at": None,
        "filters": {
            "user_id": user_id,
            "beatmap_id": beatmap_id,
            "gamemode": gamemode,
            "only_unanalyzed": only_unanalyzed,
            "only_with_replay": only_with_replay,
            "min_pp": min_pp,
            "max_count": max_count,
        },
    }
    await _bulk_job_write(redis, job_id, initial)

    from app.dependencies.database import engine

    _asyncio.create_task(
        _bulk_reanalyze_worker(
            engine,
            redis,
            job_id,
            user_id=user_id,
            beatmap_id=beatmap_id,
            gamemode=gamemode,
            only_unanalyzed=only_unanalyzed,
            only_with_replay=only_with_replay,
            min_pp=min_pp,
            max_count=max_count,
        )
    )

    logger.info(
        f"admin {admin_user.username}#{admin_user.id} queued bulk reanalyze "
        f"job {job_id} (filters: {initial['filters']})"
    )
    return initial


@router.get("/admin/anticheat/reanalyze-jobs/{job_id}", tags=["Admin Anti-cheat"])
async def admin_get_reanalyze_job(
    job_id: str,
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    redis: Redis,
) -> dict[str, Any]:
    """Poll the state of a bulk re-analyze job."""
    await require_admin(session, user_and_token)
    raw = await redis.get(_bulk_job_key(job_id))
    if not raw:
        raise HTTPException(status_code=404, detail="job not found or expired")
    try:
        return _json.loads(raw)
    except Exception:
        raise HTTPException(status_code=500, detail="job state corrupt")
