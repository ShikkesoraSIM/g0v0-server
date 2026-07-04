"""Endpoints privados para que ToriiHalo anuncie renders de o!rdr terminados.

Mismo patron poll+dispatch que mod-alerts: el bot polea /pending, postea el
video en discord, y marca /dispatch para no repetir. La dedup vive aca
(dispatched_at); el bot es stateless. Solo salen renders con share=True
(opt-in del user en el panel del cliente).

Auth: el mismo header X-Torii-Mod-Alert-Token que ya usa el bot para
mod-alerts y el schedule del daily challenge.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any

from sqlmodel import col, select

from app.config import settings
from app.database import ToriiReplayRender
from app.dependencies.database import Database
from app.utils import utcnow

from fastapi import Header, HTTPException

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
        "beatmap_title": r.beatmap_title,
        "resolution": r.resolution,
        "video_url": r.video_url,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
    }


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
