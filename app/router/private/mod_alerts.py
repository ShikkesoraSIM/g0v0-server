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

