"""POST/GET /api/v2/torii/replay-render — render a saved replay to video via o!rdr.

The client (results screen) sends a score_id; we read that score's stored
``.osr`` from disk and submit it to o!rdr's render API (apis.issou.best).
o!rdr renders on its own infrastructure and hosts the resulting MP4, so this
costs us zero video storage — only the transient ``.osr`` we already have on
disk is forwarded. The returned ``videoUrl`` embeds directly in Discord and
can be downloaded for a local file.

Key handling
------------
The o!rdr verification key lives only in server config
(``settings.ordr_verification_key``, the public dev key by default) and is
NEVER shipped to the client — the client talks to us, we talk to o!rdr. So a
single contributor key can power every Torii render without being extractable
from the desktop binary.

Premium gating
--------------
Resolutions above 720p and motion blur are an o!rdr supporter/contributor
perk (o!rdr only honours them when the sender key carries the tier). We also
refuse to *request* them for non-supporters so the client UI never shows a
misleading "4K" option to someone who can't use it.

Surfaces
--------
``POST /api/v2/torii/replay-render/{score_id}``   submit a render, returns renderID
``GET  /api/v2/torii/replay-render/{render_id}``   poll status (progress + videoUrl)
"""

from __future__ import annotations

from typing import Annotated, Any

import httpx
from fastapi import Depends, HTTPException, Query, Security
from fastapi_limiter.depends import RateLimiter
from sqlmodel import select

from app.config import settings
from app.database import Score, User
from app.dependencies.database import Database
from app.dependencies.storage import StorageService
from app.dependencies.user import get_current_user
from app.log import logger
from app.models.torii_groups import is_currently_supporting

from .router import router

# o!rdr public API base. Render submission is multipart/form-data to /renders;
# status is GET /renders?renderID=.
_ORDR_BASE = "https://apis.issou.best/ordr"

# Sender name attributed on o!rdr for every Torii-originated render.
_ORDR_SENDER = "Torii"

# o!rdr requires a skin field. "default" is danser's built-in default skin.
_DEFAULT_SKIN = "default"

# 720p is allowed for everyone; higher resolutions need the sender key's
# supporter/contributor perk tier, so we gate them behind supporter status.
_FREE_RESOLUTIONS = {"1280x720"}
_SUPPORTER_RESOLUTIONS = {"1920x1080", "2560x1440", "3840x2160"}
_ALL_RESOLUTIONS = _FREE_RESOLUTIONS | _SUPPORTER_RESOLUTIONS


@router.post(
    "/torii/replay-render/{score_id}",
    tags=["Torii"],
    name="Render a replay to video via o!rdr",
    description=(
        "Submit a stored score's replay to o!rdr for video rendering. Returns "
        "a renderID to poll. Resolutions above 720p and motion blur require "
        "supporter status."
    ),
    dependencies=[Depends(RateLimiter(times=5, minutes=10))],
)
async def submit_replay_render(
    score_id: int,
    db: Database,
    storage: StorageService,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    resolution: str = Query(default="1280x720"),
    skin: str = Query(default=_DEFAULT_SKIN),
    motion_blur: bool = Query(default=False),
) -> dict[str, Any]:
    score = (await db.exec(select(Score).where(Score.id == score_id))).first()
    if score is None:
        raise HTTPException(status_code=404, detail="Score not found")
    if not score.replay_filename:
        raise HTTPException(status_code=404, detail="This score has no replay to render")

    if resolution not in _ALL_RESOLUTIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported resolution: {resolution}")

    supporter = is_currently_supporting(current_user)
    if not supporter and (resolution in _SUPPORTER_RESOLUTIONS or motion_blur):
        raise HTTPException(
            status_code=403,
            detail="Higher resolutions and motion blur are a supporter perk.",
        )

    try:
        replay_bytes = await storage.read_file(score.replay_filename)
    except Exception as e:
        logger.warning(f"[ReplayRender] failed to read replay for score {score_id}: {e}")
        raise HTTPException(status_code=404, detail="Replay file not found")

    form = {
        "username": _ORDR_SENDER,
        "resolution": resolution,
        "skin": skin,
        "verificationKey": settings.ordr_verification_key,
        "motionBlur960fps": "true" if motion_blur else "false",
    }
    files = {"replayFile": (f"{score_id}.osr", replay_bytes, "application/octet-stream")}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{_ORDR_BASE}/renders", data=form, files=files)
    except httpx.HTTPError as e:
        logger.warning(f"[ReplayRender] o!rdr submit failed for score {score_id}: {e}")
        raise HTTPException(status_code=502, detail="Could not reach the render service")

    payload = _safe_json(resp)
    if resp.status_code not in (200, 201) or payload.get("errorCode", 0) not in (0, None):
        raise HTTPException(
            status_code=502,
            detail=payload.get("message")
            or f"Render service rejected the replay (HTTP {resp.status_code})",
        )

    return {
        "render_id": payload.get("renderID"),
        "message": payload.get("message", "Render queued"),
    }


@router.get(
    "/torii/replay-render/{render_id}",
    tags=["Torii"],
    name="Poll an o!rdr replay render",
    description="Fetch the status of a previously-submitted render (progress + final video URL).",
)
async def get_replay_render(
    render_id: int,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],  # noqa: ARG001
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{_ORDR_BASE}/renders", params={"renderID": render_id})
    except httpx.HTTPError as e:
        logger.warning(f"[ReplayRender] o!rdr status fetch failed for {render_id}: {e}")
        raise HTTPException(status_code=502, detail="Could not reach the render service")

    payload = _safe_json(resp)
    renders = payload.get("renders") or []
    if not renders:
        raise HTTPException(status_code=404, detail="Render not found")

    r = renders[0]
    return {
        "render_id": r.get("renderID"),
        "progress": r.get("progress"),
        "video_url": r.get("videoUrl") or None,
        "removed": bool(r.get("removed", False)),
        "error_code": r.get("errorCode", 0),
        "error_message": r.get("errorMessage"),
        "resolution": r.get("resolution"),
        "motion_blur": bool(r.get("motionBlur960fps", False)),
    }


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
