from __future__ import annotations

import secrets
from typing import Annotated, Any

from app.config import settings
from app.dependencies.database import Database
from app.service.suspicious_alert_service import SuspiciousAlertService

from fastapi import Header, HTTPException

from .router import router


def _validate_mod_alert_token(token: str | None) -> None:
    expected = (settings.moderation_alert_token or "").strip()
    provided = (token or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="moderation alert token is not configured")
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid moderation alert token")


def _serialize_alert(alert) -> dict[str, Any]:
    return {
        "id": alert.id,
        "kind": alert.kind,
        "severity": alert.severity,
        "user_id": alert.user_id,
        "score_id": alert.score_id,
        "beatmap_id": alert.beatmap_id,
        "title": alert.title,
        "body": alert.body,
        "metadata": alert.payload,
        "created_at": alert.created_at.isoformat(),
    }


@router.get("/mod-alerts/pending", tags=["Moderation Alerts"])
async def get_pending_mod_alerts(
    session: Database,
    x_torii_mod_alert_token: Annotated[str | None, Header(alias="X-Torii-Mod-Alert-Token")] = None,
    limit: int = 10,
):
    _validate_mod_alert_token(x_torii_mod_alert_token)
    alerts = await SuspiciousAlertService.get_pending_alerts(session, limit=limit)
    return {"alerts": [_serialize_alert(alert) for alert in alerts]}


@router.post("/mod-alerts/{alert_id}/dispatch", tags=["Moderation Alerts"])
async def mark_mod_alert_dispatched(
    alert_id: int,
    session: Database,
    x_torii_mod_alert_token: Annotated[str | None, Header(alias="X-Torii-Mod-Alert-Token")] = None,
):
    _validate_mod_alert_token(x_torii_mod_alert_token)
    ok = await SuspiciousAlertService.mark_alert_dispatched(session, alert_id)
    if not ok:
        raise HTTPException(status_code=404, detail="alert not found")
    return {"ok": True}


async def _get_alert(session, alert_id: int):
    from app.database.suspicious_alert import SuspiciousAlert

    alert = await session.get(SuspiciousAlert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="alert not found")
    return alert


@router.post("/mod-alerts/{alert_id}/whitelist-user", tags=["Moderation Alerts"])
async def whitelist_alert_user(
    alert_id: int,
    session: Database,
    x_torii_mod_alert_token: Annotated[str | None, Header(alias="X-Torii-Mod-Alert-Token")] = None,
    moderator_id: int | None = None,
    reason: str | None = None,
):
    _validate_mod_alert_token(x_torii_mod_alert_token)
    from app.database.high_pp_whitelist import HighPpWhitelist
    from app.utils import utcnow

    alert = await _get_alert(session, alert_id)
    if alert.user_id is None:
        raise HTTPException(status_code=400, detail="alert has no user")

    if await session.get(HighPpWhitelist, alert.user_id) is None:
        session.add(HighPpWhitelist(user_id=alert.user_id, added_by_id=moderator_id, reason=reason))

    alert.resolved_at = utcnow()
    session.add(alert)
    await session.commit()
    return {"ok": True, "user_id": alert.user_id}


@router.post("/mod-alerts/{alert_id}/ban-beatmapset", tags=["Moderation Alerts"])
async def ban_alert_beatmapset(
    alert_id: int,
    session: Database,
    x_torii_mod_alert_token: Annotated[str | None, Header(alias="X-Torii-Mod-Alert-Token")] = None,
    reason: str | None = None,
):
    _validate_mod_alert_token(x_torii_mod_alert_token)
    from app.database.beatmap import BannedBeatmaps, Beatmap
    from app.utils import bg_tasks, utcnow

    from sqlmodel import col, select

    alert = await _get_alert(session, alert_id)
    beatmapset_id = (alert.payload or {}).get("beatmapset_id")
    if beatmapset_id is None:
        raise HTTPException(status_code=400, detail="alert has no beatmapset")

    diffs = list(
        (await session.exec(select(Beatmap.id).where(col(Beatmap.beatmapset_id) == beatmapset_id))).all()
    )
    if not diffs:
        raise HTTPException(status_code=404, detail="beatmapset has no beatmaps")

    ya = set(
        (await session.exec(select(BannedBeatmaps.beatmap_id).where(col(BannedBeatmaps.beatmap_id).in_(diffs)))).all()
    )
    nuevos = [d for d in diffs if d not in ya]
    for beatmap_id in nuevos:
        session.add(
            BannedBeatmaps(
                beatmap_id=beatmap_id,
                source="mod_alert",
                reason=(reason or f"banned from alert {alert_id}")[:255],
            )
        )

    alert.resolved_at = utcnow()
    session.add(alert)
    await session.commit()

    # la tarea horaria detecta los nuevos baneos y recalcula TODOS los scores del mapa;
    # se dispara ya para no esperar hasta la proxima corrida
    from app.tasks.recalculate_banned_beatmap import recalculate_banned_beatmap

    bg_tasks.add_task(recalculate_banned_beatmap)

    return {"ok": True, "beatmapset_id": beatmapset_id, "banned": len(nuevos), "already_banned": len(ya)}


@router.post("/mod-alerts/{alert_id}/resolve", tags=["Moderation Alerts"])
async def resolve_alert(
    alert_id: int,
    session: Database,
    x_torii_mod_alert_token: Annotated[str | None, Header(alias="X-Torii-Mod-Alert-Token")] = None,
):
    _validate_mod_alert_token(x_torii_mod_alert_token)
    from app.utils import utcnow

    alert = await _get_alert(session, alert_id)
    alert.resolved_at = utcnow()
    session.add(alert)
    await session.commit()
    return {"ok": True}


@router.post("/mod-alerts/backfill-high-pp", tags=["Moderation Alerts"])
async def backfill_high_pp_alerts(
    session: Database,
    x_torii_mod_alert_token: Annotated[str | None, Header(alias="X-Torii-Mod-Alert-Token")] = None,
    threshold: float | None = None,
    limit: int = 100,
    dry_run: bool = True,
):
    """Barrido historico: UNA alerta por jugador, su play mas alta arriba del umbral."""
    _validate_mod_alert_token(x_torii_mod_alert_token)
    from app.database.best_scores import BestScore
    from app.database.high_pp_whitelist import HighPpWhitelist
    from app.database.score import Score
    from app.database.user import User
    from app.service.suspicious_alert_service import SuspiciousAlertService

    from sqlmodel import col, select

    umbral = float(threshold if threshold is not None else settings.high_pp_backfill_threshold)

    filas = (
        await session.exec(
            select(BestScore).where(col(BestScore.pp) >= umbral).order_by(col(BestScore.pp).desc())
        )
    ).all()

    whitelisted = set((await session.exec(select(HighPpWhitelist.user_id))).all())

    creadas = 0
    saltadas = 0
    detalle = []
    vistos: set[int] = set()
    for best in filas:
        if best.user_id in vistos or len(vistos) >= limit:
            continue
        vistos.add(best.user_id)

        if best.user_id in whitelisted:
            saltadas += 1
            continue

        score = await session.get(Score, best.score_id)
        user = await session.get(User, best.user_id)
        if score is None or user is None:
            continue

        detalle.append({"user_id": best.user_id, "username": user.username, "pp": round(float(best.pp), 2)})
        if dry_run:
            continue

        resultado = await SuspiciousAlertService.maybe_record_high_pp_alert(session, score, user, threshold=umbral, backfill=True)
        if resultado is not None and resultado.created:
            creadas += 1

    if not dry_run:
        await session.commit()

    return {
        "threshold": umbral,
        "dry_run": dry_run,
        "players": len(detalle),
        "created": creadas,
        "skipped_whitelisted": saltadas,
        "detail": detalle[:50],
    }


@router.get("/mod-alerts/user/{user_id}/high-pp", tags=["Moderation Alerts"])
async def user_high_pp_plays(
    user_id: int,
    session: Database,
    x_torii_mod_alert_token: Annotated[str | None, Header(alias="X-Torii-Mod-Alert-Token")] = None,
    threshold: float | None = None,
    limit: int = 25,
):
    _validate_mod_alert_token(x_torii_mod_alert_token)
    from app.database.beatmap import Beatmap
    from app.database.beatmapset import Beatmapset
    from app.database.best_scores import BestScore
    from app.database.score import Score
    from app.database.user import User
    from app.service.suspicious_alert_service import SuspiciousAlertService

    from sqlmodel import col, select

    umbral = float(threshold if threshold is not None else settings.high_pp_backfill_threshold)
    user = await session.get(User, user_id)

    filas = (
        await session.exec(
            select(BestScore)
            .where(col(BestScore.user_id) == user_id, col(BestScore.pp) >= umbral)
            .order_by(col(BestScore.pp).desc())
            .limit(max(1, min(limit, 100)))
        )
    ).all()

    plays = []
    for best in filas:
        score = await session.get(Score, best.score_id)
        if score is None:
            continue
        beatmap = await session.get(Beatmap, score.beatmap_id)
        beatmapset = await session.get(Beatmapset, beatmap.beatmapset_id) if beatmap else None
        plays.append(
            {
                "score_id": score.id,
                "pp": round(float(best.pp), 2),
                "accuracy": round(float(score.accuracy or 0) * 100, 2),
                "mods": SuspiciousAlertService._format_mods(score.mods or []),
                "gamemode": str(score.gamemode),
                "beatmap_id": score.beatmap_id,
                "beatmap_name": (
                    f"{beatmapset.artist} - {beatmapset.title} [{beatmap.version}]"
                    if beatmapset and beatmap
                    else str(score.beatmap_id)
                ),
                "ended_at": score.ended_at.isoformat() if score.ended_at else None,
            }
        )

    return {
        "user_id": user_id,
        "username": user.username if user else None,
        "threshold": umbral,
        "plays": plays,
    }
