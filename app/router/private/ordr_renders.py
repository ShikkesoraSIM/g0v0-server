"""Endpoints privados para que ToriiHalo anuncie renders de o!rdr en vivo.

Flujo estilo yuna: apenas un render compartido entra en la cola, el bot postea
un mensaje ("X queued a replay...") y lo VA EDITANDO con el progreso (%, host,
etc.) hasta que termina, en vez de postear uno nuevo por update. Para eso el
server guarda el discord_message_id del mensaje que posteo el bot.

Contrato:
- GET  /ordr-renders/active            renders compartidos aun no finalizados
                                        (queued/rendering/done sin dispatch), con
                                        su discord_message_id si ya se posteo.
- POST /ordr-renders/{id}/message      el bot guarda el id del mensaje que posteo.
- POST /ordr-renders/{id}/dispatch     el bot marca finalizado (done + posteado);
                                        deja de aparecer en /active.

dispatched_at = "finalizado y mensaje final ya editado". La dedup vive aca; el
bot solo mantiene en memoria el ultimo estado que dibujo, para no re-editar.

Auth: el mismo header X-Torii-Mod-Alert-Token que mod-alerts.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any

from sqlmodel import col, or_, select

from app.config import settings
from app.database import ToriiReplayRender
from app.dependencies.database import Database
from app.utils import utcnow

from fastapi import Header, HTTPException, Query

from .router import router


def _validate_token(token: str | None) -> None:
    expected = (settings.moderation_alert_token or "").strip()
    provided = (token or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="moderation alert token is not configured")
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid moderation alert token")


def _serialize_render(r: ToriiReplayRender) -> dict[str, Any]:
    return {
        "id": r.id,
        "ordr_render_id": r.ordr_render_id,
        "user_id": r.user_id,
        "score_id": r.score_id,
        "username": r.username,
        "player_username": r.player_username,
        "player_user_id": r.player_user_id,
        "beatmap_title": r.beatmap_title,
        "beatmap_online_id": r.beatmap_online_id,
        "beatmapset_id": r.beatmapset_id,
        "gamemode": r.gamemode,
        "resolution": r.resolution,
        "status": r.status,
        "progress": r.progress,
        "renderer": r.renderer,
        "video_url": r.video_url,
        "error_message": r.error_message,
        "discord_message_id": r.discord_message_id,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
    }


@router.get("/ordr-renders/active", tags=["Replay Renders"])
async def get_active_ordr_renders(
    session: Database,
    x_torii_mod_alert_token: Annotated[str | None, Header(alias="X-Torii-Mod-Alert-Token")] = None,
    limit: int = 10,
):
    """Renders compartidos aun no finalizados: para postear/editar el status en vivo.

    Incluye:
    - los que estan en cola / rendereando (para postear el mensaje y editarlo), y
    - los que ya terminaron (done/failed) PERO todavia no fueron dispatched (para
      que el bot haga la edicion final y recien ahi marque dispatch).
    """
    _validate_token(x_torii_mod_alert_token)
    renders = (
        await session.exec(
            select(ToriiReplayRender)
            .where(
                col(ToriiReplayRender.share).is_(True),
                col(ToriiReplayRender.dispatched_at).is_(None),
                or_(
                    col(ToriiReplayRender.status).in_(["queued", "rendering", "done"]),
                    # failed compartido: tambien lo mostramos para editar/cerrar el mensaje
                    col(ToriiReplayRender.status) == "failed",
                ),
            )
            .order_by(col(ToriiReplayRender.created_at))
            .limit(min(max(limit, 1), 25))
        )
    ).all()
    return {"renders": [_serialize_render(r) for r in renders]}


@router.post("/ordr-renders/{record_id}/message", tags=["Replay Renders"])
async def set_ordr_render_message(
    record_id: int,
    session: Database,
    message_id: int = Query(...),
    x_torii_mod_alert_token: Annotated[str | None, Header(alias="X-Torii-Mod-Alert-Token")] = None,
):
    """El bot guarda el id del mensaje de discord que posteo, para editarlo luego."""
    _validate_token(x_torii_mod_alert_token)
    record = await session.get(ToriiReplayRender, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="render not found")
    record.discord_message_id = message_id
    session.add(record)
    await session.commit()
    return {"ok": True}


@router.post("/ordr-renders/{record_id}/dispatch", tags=["Replay Renders"])
async def mark_ordr_render_dispatched(
    record_id: int,
    session: Database,
    x_torii_mod_alert_token: Annotated[str | None, Header(alias="X-Torii-Mod-Alert-Token")] = None,
):
    _validate_token(x_torii_mod_alert_token)
    record = await session.get(ToriiReplayRender, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="render not found")
    if record.dispatched_at is None:
        record.dispatched_at = utcnow()
        session.add(record)
        await session.commit()
    return {"ok": True}


# compat: el flujo viejo (postear solo el video terminado). se deja por si algo
# lo usa, pero el bot nuevo usa /active.
@router.get("/ordr-renders/pending", tags=["Replay Renders"])
async def get_pending_ordr_renders(
    session: Database,
    x_torii_mod_alert_token: Annotated[str | None, Header(alias="X-Torii-Mod-Alert-Token")] = None,
    limit: int = 5,
):
    _validate_token(x_torii_mod_alert_token)
    renders = (
        await session.exec(
            select(ToriiReplayRender)
            .where(
                ToriiReplayRender.status == "done",
                col(ToriiReplayRender.share).is_(True),
                col(ToriiReplayRender.dispatched_at).is_(None),
                col(ToriiReplayRender.video_url).is_not(None),
            )
            .order_by(col(ToriiReplayRender.finished_at))
            .limit(min(max(limit, 1), 20))
        )
    ).all()
    return {"renders": [_serialize_render(r) for r in renders]}
