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

Rate limiting (requisito del owner de o!rdr)
--------------------------------------------
La key oficial no tiene rate limit en o!rdr, asi que la contencion es NUESTRA:
1 render cada 10 minutos POR USER (cooldown en Redis, ``ordr:cooldown:{uid}``).
Si o!rdr rechaza el submit de entrada (error sincronico), devolvemos el
cooldown para que el user pueda reintentar; si el render falla async, el
cooldown queda (ya consumio un slot de render real).

Registro
--------
Cada render queda en ``torii_replay_renders`` (ver el modelo): el poller de
fondo lo sigue aunque el cliente se cierre, y ToriiHalo anuncia los que tienen
``share=True`` al terminar.

Premium gating
--------------
Resolutions above 720p and motion blur are an o!rdr supporter/contributor
perk. We also refuse to *request* them for non-supporters so the client UI
never shows a misleading option to someone who can't use it.

Surfaces
--------
``POST /api/v2/torii/replay-render/{score_id}``    submit a render, returns renderID
``GET  /api/v2/torii/replay-render/cooldown``      remaining per-user cooldown seconds
``GET  /api/v2/torii/replay-render/{render_id}``   poll status (progress + videoUrl)
"""

from __future__ import annotations

import base64
import io
import re
from typing import Annotated, Any
from urllib.parse import quote

import httpx
from fastapi import HTTPException, Query, Response, Security
from PIL import Image
from sqlmodel import select

from app.config import settings
from app.database import Beatmap, Beatmapset, Score, ToriiReplayRender, User
from app.dependencies.database import Database, Redis
from app.dependencies.storage import StorageService
from app.dependencies.user import get_current_user
from app.log import logger
from app.models.torii_groups import is_currently_supporting
from app.utils import utcnow

from .router import router

# o!rdr public API base. Render submission is multipart/form-data to /renders;
# status is GET /renders?renderID=.
_ORDR_BASE = "https://apis.issou.best/ordr"

# plantilla del preview de una skin en o!rdr (webp). lo proxeamos+convertimos a PNG
# porque el cliente solo puede cargar texturas de *.shikkesora.com / *.ppy.sh
# (TrustedDomainOnlineStore bloquea dl.issou.best directo).
_ORDR_SKIN_PREVIEW = "https://dl.issou.best/ordr/skinpreview/{skin}/low-res.webp"
_SKIN_PREVIEW_CACHE = "ordr:skinpreview:{skin}"  # base64 del PNG en redis, 24h

# Sender name attributed on o!rdr for every Torii-originated render.
_ORDR_SENDER = "Torii"

# o!rdr requires a skin field. "default" is danser's built-in default skin.
_DEFAULT_SKIN = "default"

# 720p is allowed for everyone; higher resolutions need the sender key's
# supporter/contributor perk tier, so we gate them behind supporter status.
# OJO: 1080p ademas requiere permiso del lado de o!rdr para nuestra key
# (error 48 si no lo tenemos) — mantener chico hasta confirmar con MasterIO.
_FREE_RESOLUTIONS = {"960x540", "1280x720"}
_SUPPORTER_RESOLUTIONS = {"1920x1080"}
_ALL_RESOLUTIONS = _FREE_RESOLUTIONS | _SUPPORTER_RESOLUTIONS

# requisito de MasterIO (o!rdr): al menos 1 render cada 10 min por user.
_COOLDOWN_SECONDS = 600
_COOLDOWN_KEY = "ordr:cooldown:{user_id}"

# mensajes amigables para los errorCode sincronicos mas comunes de o!rdr
# (el resto cae al message crudo que mande o!rdr).
_ORDR_ERROR_MESSAGES: dict[int, str] = {
    5: "The replay file looks corrupted and can't be rendered.",
    9: "This map's audio is unavailable on o!rdr (copyright strike).",
    11: "Autoplay replays can't be rendered.",
    13: "This map is longer than 15 minutes — o!rdr's hard limit.",
    26: "This replay's mod combination isn't supported by the renderer.",
    29: "This replay is already being rendered — check back in a bit.",
    30: "This map is above 20 stars — o!rdr's hard limit.",
    33: "This exact replay failed to render less than an hour ago; wait before retrying.",
    38: "That skin doesn't exist on o!rdr — check the name at ordr.issou.best/skins.",
}


def _friendly_ordr_error(payload: dict[str, Any], status_code: int) -> str:
    code = payload.get("errorCode")
    if isinstance(code, int) and code in _ORDR_ERROR_MESSAGES:
        return _ORDR_ERROR_MESSAGES[code]
    return payload.get("message") or f"The render service rejected the replay (HTTP {status_code})"


def _mode_str(value) -> str | None:
    """Normaliza un GameMode enum a 'osu'/'taiko'/'fruits'/'mania' (para el link)."""
    try:
        s = str(getattr(value, "value", value) or "").lower()
    except Exception:
        return None
    return s or None


async def _render_meta_for_score(db, score: Score) -> dict[str, Any]:
    """Denormaliza lo que el bot necesita para el mensaje: titulo, ids del mapa +
    modo (para el link), y el username del jugador (dueño del score). Best-effort."""
    meta: dict[str, Any] = {
        "beatmap_title": "",
        "beatmap_online_id": None,
        "beatmapset_id": None,
        "gamemode": _mode_str(getattr(score, "gamemode", None)),
        "player_username": None,
    }
    try:
        beatmap = await db.get(Beatmap, score.beatmap_id)
        if beatmap is not None:
            meta["beatmap_online_id"] = beatmap.id
            meta["beatmapset_id"] = beatmap.beatmapset_id
            # el modo del BEATMAP es el ruleset base del link (osu/taiko/fruits/mania),
            # mejor que el gamemode del score que puede ser rx/ap.
            bmode = _mode_str(getattr(beatmap, "mode", None))
            if bmode:
                meta["gamemode"] = bmode
            beatmapset = await db.get(Beatmapset, beatmap.beatmapset_id)
            if beatmapset is not None:
                meta["beatmap_title"] = f"{beatmapset.artist} - {beatmapset.title} [{beatmap.version}]"[:250]
        player = await db.get(User, score.user_id)
        if player is not None:
            meta["player_username"] = player.username
    except Exception:
        pass
    return meta


@router.get(
    "/torii/replay-render/cooldown",
    tags=["Torii"],
    name="Remaining replay-render cooldown",
    description="Seconds until the current user can submit another render (0 = ready).",
)
async def get_replay_render_cooldown(
    redis: Redis,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
) -> dict[str, Any]:
    ttl = await redis.ttl(_COOLDOWN_KEY.format(user_id=current_user.id))
    return {"seconds_remaining": max(0, ttl if isinstance(ttl, int) else 0)}


# OJO orden de rutas: estas GET estaticas van ANTES de "/{render_id}" (que es un
# int path param) o FastAPI intenta castear "skins"/"mine" a int y tira 422.
@router.get(
    "/torii/replay-render/skins",
    tags=["Torii"],
    name="Search o!rdr skins",
    description="Proxy a la lista de skins de o!rdr para el selector del cliente (nombre + preview).",
)
async def search_ordr_skins(
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],  # noqa: ARG001
    search: str = Query(default=""),
    page: int = Query(default=1, ge=1),
) -> dict[str, Any]:
    params: dict[str, Any] = {"pageSize": 60, "page": page}
    search = (search or "").strip()[:64]
    if search:
        params["search"] = search
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{_ORDR_BASE}/skins", params=params)
        data = resp.json() if isinstance(resp.json(), dict) else {}
    except Exception as e:
        logger.warning(f"[ReplayRender] o!rdr skins fetch failed: {e}")
        return {"skins": []}

    skins = []
    for s in (data.get("skins") or [])[:60]:
        name = s.get("skin")
        if not name:
            continue
        skins.append(
            {
                "skin": name,
                "name": s.get("presentationName") or name,
                # preview que el cliente PUEDE cargar (proxeada por nosotros a PNG);
                # high_res es el link crudo de o!rdr para abrir en el navegador (ojito).
                "preview": _proxied_preview_url(name),
                "high_res": s.get("highResPreview") or s.get("lowResPreview"),
                "author": s.get("author") or "",
                "times_used": s.get("timesUsed", 0),
            }
        )
    return {"skins": skins}


def _proxied_preview_url(skin_name: str) -> str:
    # settings.server_url es un HttpUrl de pydantic, no un str -> str() antes de rstrip.
    base = str(settings.server_url or "").rstrip("/")
    return f"{base}/api/v2/torii/replay-render/skin-preview?skin={quote(skin_name)}"


@router.get(
    "/torii/replay-render/skin-preview",
    tags=["Torii"],
    name="o!rdr skin preview image",
    description="Proxy PNG del preview de una skin de o!rdr (sin auth: lo carga la texture store del cliente).",
)
async def skin_preview(
    redis: Redis,
    skin: str = Query(...),
) -> Response:
    # sin auth a proposito: la texture store del cliente hace un GET pelado sin token.
    # el nombre se sanitiza (nada de path traversal) y solo pegamos al host fijo de o!rdr.
    name = re.sub(r"[^A-Za-z0-9._\- ]", "", skin or "")[:120].strip()
    if not name:
        raise HTTPException(status_code=404, detail="No skin")

    headers = {"Cache-Control": "public, max-age=86400"}
    cache_key = _SKIN_PREVIEW_CACHE.format(skin=name)

    try:
        cached = await redis.get(cache_key)
    except Exception:
        cached = None
    if cached:
        try:
            return Response(content=base64.b64decode(cached), media_type="image/png", headers=headers)
        except Exception:
            pass

    url = _ORDR_SKIN_PREVIEW.format(skin=quote(name))
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
        if resp.status_code != 200 or not resp.content:
            raise HTTPException(status_code=404, detail="Preview not available")
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
        # chico a proposito: el box del cliente es ~168px, no hace falta mas y asi
        # la subida a GPU es liviana (evita hitches al mostrar la preview).
        img.thumbnail((400, 225))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png = buf.getvalue()
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"[ReplayRender] skin preview failed for {name!r}: {e}")
        raise HTTPException(status_code=404, detail="Preview not available")

    try:
        await redis.set(cache_key, base64.b64encode(png), ex=86400)
    except Exception:
        pass

    return Response(content=png, media_type="image/png", headers=headers)


@router.get(
    "/torii/replay-render/mine",
    tags=["Torii"],
    name="My recent replay renders",
    description="Los ultimos renders del user (para verlos aunque no los haya compartido en discord).",
)
async def my_replay_renders(
    db: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    limit: int = Query(default=10, ge=1, le=30),
) -> dict[str, Any]:
    user_id = current_user.id
    rows = (
        await db.exec(
            select(ToriiReplayRender)
            .where(ToriiReplayRender.user_id == user_id)
            .order_by(ToriiReplayRender.created_at.desc())
            .limit(limit)
        )
    ).all()
    return {
        "renders": [
            {
                "render_id": r.ordr_render_id,
                "beatmap_title": r.beatmap_title,
                "status": r.status,
                "progress": r.progress,
                "video_url": r.video_url,
                "share": r.share,
                "resolution": r.resolution,
                "skin": r.skin,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.post(
    "/torii/replay-render/{score_id}",
    tags=["Torii"],
    name="Render a replay to video via o!rdr",
    description=(
        "Submit a stored score's replay to o!rdr for video rendering. Returns "
        "a renderID to poll. One render per 10 minutes per user. Resolutions "
        "above 720p and motion blur require supporter status."
    ),
)
async def submit_replay_render(
    score_id: int,
    db: Database,
    redis: Redis,
    storage: StorageService,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    resolution: str = Query(default="1280x720"),
    skin: str = Query(default=_DEFAULT_SKIN),
    motion_blur: bool = Query(default=False),
    share: bool = Query(default=True, description="Post the finished video in the Torii Discord"),
) -> dict[str, Any]:
    # snapshot de la identidad del user en locales ANTES de cualquier commit.
    # expire_on_commit (default True) expira los objetos ORM de la sesion en el
    # commit; acceder a current_user.id/username despues dispara un lazy-load
    # sincronico que revienta en async (MissingGreenlet). con locales lo evitamos.
    user_id = current_user.id
    username = current_user.username

    score = (await db.exec(select(Score).where(Score.id == score_id))).first()
    if score is None:
        raise HTTPException(status_code=404, detail="Score not found")
    if not score.replay_filename:
        raise HTTPException(status_code=404, detail="This score has no replay to render")

    if resolution not in _ALL_RESOLUTIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported resolution: {resolution}")

    skin = (skin or _DEFAULT_SKIN).strip()[:120]

    supporter = is_currently_supporting(current_user)
    if not supporter and (resolution in _SUPPORTER_RESOLUTIONS or motion_blur):
        raise HTTPException(
            status_code=403,
            detail="Higher resolutions and motion blur are a supporter perk.",
        )

    # ── cooldown por user (1 cada 10 min, requisito de o!rdr). SET NX EX es
    # atomico: si la key ya existe, el user esta en cooldown.
    cooldown_key = _COOLDOWN_KEY.format(user_id=user_id)
    acquired = await redis.set(cooldown_key, "1", ex=_COOLDOWN_SECONDS, nx=True)
    if not acquired:
        ttl = await redis.ttl(cooldown_key)
        remaining = max(1, ttl if isinstance(ttl, int) else _COOLDOWN_SECONDS)
        raise HTTPException(
            status_code=429,
            detail=f"You can render one video every 10 minutes — {remaining}s remaining.",
            headers={"Retry-After": str(remaining)},
        )

    async def _refund_cooldown() -> None:
        """El submit no llego a encolar un render real: devolvemos el intento."""
        try:
            await redis.delete(cooldown_key)
        except Exception:
            pass

    try:
        replay_bytes = await storage.read_file(score.replay_filename)
    except Exception as e:
        logger.warning(f"[ReplayRender] failed to read replay for score {score_id}: {e}")
        await _refund_cooldown()
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
        await _refund_cooldown()
        raise HTTPException(status_code=502, detail="Could not reach the render service")

    payload = _safe_json(resp)
    if resp.status_code not in (200, 201) or payload.get("errorCode", 0) not in (0, None):
        await _refund_cooldown()
        raise HTTPException(status_code=502, detail=_friendly_ordr_error(payload, resp.status_code))

    render_id = payload.get("renderID")

    # registro para el poller + el bot de discord. best-effort: si esto falla,
    # el render igual quedo encolado en o!rdr y el cliente puede pollear.
    meta = await _render_meta_for_score(db, score)
    player_user_id = score.user_id
    try:
        record = ToriiReplayRender(
            ordr_render_id=int(render_id),
            user_id=user_id,
            score_id=score_id,
            username=username,
            player_username=meta["player_username"],
            player_user_id=player_user_id,
            beatmap_title=meta["beatmap_title"],
            beatmap_online_id=meta["beatmap_online_id"],
            beatmapset_id=meta["beatmapset_id"],
            gamemode=meta["gamemode"],
            resolution=resolution,
            skin=skin,
            motion_blur=motion_blur,
            share=share,
            status="queued",
        )
        db.add(record)
        await db.commit()
    except Exception as e:
        logger.warning(f"[ReplayRender] failed to record render {render_id}: {e}")

    logger.info(
        f"[ReplayRender] user {user_id} queued render {render_id} "
        f"for score {score_id} ({resolution}, skin={skin!r}, share={share})"
    )
    return {
        "render_id": render_id,
        "message": payload.get("message", "Render queued"),
        "cooldown_seconds": _COOLDOWN_SECONDS,
    }


@router.get(
    "/torii/replay-render/{render_id}",
    tags=["Torii"],
    name="Poll an o!rdr replay render",
    description="Fetch the status of a previously-submitted render (progress + final video URL).",
)
async def get_replay_render(
    render_id: int,
    db: Database,
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
    result = {
        "render_id": r.get("renderID"),
        "progress": r.get("progress"),
        # nombre del host que renderiza (o!rdr lo expone en "renderer"), para la
        # notificacion tipo "Rendering 45% on <host>".
        "renderer": r.get("renderer"),
        "video_url": _clean_video_url(r.get("videoUrl")),
        "removed": bool(r.get("removed", False)),
        "error_code": r.get("errorCode", 0),
        "error_message": r.get("errorMessage"),
        "resolution": r.get("resolution"),
        "motion_blur": bool(r.get("motionBlur960fps", False)),
    }

    # aprovechamos el poll del cliente para actualizar nuestro registro (el
    # poller de fondo cubre el caso de cliente cerrado).
    try:
        await apply_ordr_state_to_record(db, r)
    except Exception as e:
        logger.warning(f"[ReplayRender] failed to update record for render {render_id}: {e}")

    return result


def _clean_video_url(video_url: Any) -> str | None:
    # o!rdr manda videoUrl="None" (string) hasta que el render termina.
    if not video_url or video_url == "None":
        return None
    return str(video_url)


async def apply_ordr_state_to_record(db, ordr_render: dict[str, Any]) -> None:
    """Aplica el estado que reporta o!rdr a nuestra fila de registro (si existe).

    Compartido entre el GET del cliente y el poller de fondo. done/failed son
    terminales; una vez ahi no se vuelve atras.
    """
    rid = ordr_render.get("renderID")
    if rid is None:
        return
    record = (
        await db.exec(select(ToriiReplayRender).where(ToriiReplayRender.ordr_render_id == int(rid)))
    ).first()
    if record is None or record.status in ("done", "failed"):
        return

    video_url = _clean_video_url(ordr_render.get("videoUrl"))
    error_code = ordr_render.get("errorCode", 0) or 0
    progress = str(ordr_render.get("progress") or "")[:155]
    renderer = str(ordr_render.get("renderer") or "")[:60] or None
    if renderer:
        record.renderer = renderer

    if video_url:
        record.status = "done"
        record.video_url = video_url
        record.progress = progress or record.progress
        record.finished_at = utcnow()
    elif error_code not in (0, None) or ordr_render.get("removed"):
        record.status = "failed"
        record.error_code = int(error_code) if error_code else None
        record.error_message = (str(ordr_render.get("errorMessage") or "") or None)
        record.finished_at = utcnow()
    else:
        record.status = "rendering" if progress and "queue" not in progress.lower() else "queued"
        record.progress = progress

    db.add(record)
    await db.commit()


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
