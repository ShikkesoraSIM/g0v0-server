"""Notas de score: /api/v2/torii/score-notes.

El dueño de un score le agrega una nota corta (texto + imagen opcional). La
imagen se procesa server-side con Pillow (thumbnail max 400x400, JPEG liviano)
y se guarda en storage bajo ``score-notes/{score_id}.jpg``; se sirve SIN auth
desde este mismo dominio para que la texture store del cliente pueda cargarla
(TrustedDomainOnlineStore solo permite *.shikkesora.com / *.ppy.sh).

Surfaces (orden importa: estaticas antes de /{score_id}):
``GET    /torii/score-notes/batch?score_ids=1,2``  notas de varios scores (leaderboard)
``GET    /torii/score-notes/{score_id}``           nota de un score
``GET    /torii/score-notes/{score_id}/image``     imagen procesada (sin auth)
``PUT    /torii/score-notes/{score_id}``           crear/editar la nota (multipart)
``DELETE /torii/score-notes/{score_id}``           borrar la propia nota
"""

from __future__ import annotations

import io
from typing import Annotated, Any

from fastapi import File, Form, HTTPException, Query, Response, Security, UploadFile
from PIL import Image
from sqlmodel import col, select

from app.database import Score, ToriiScoreNote, User
from app.dependencies.database import Database
from app.dependencies.storage import StorageService
from app.dependencies.user import get_current_user
from app.log import logger
from app.utils import utcnow

from .router import router

_MAX_TEXT = 280
_MAX_UPLOAD_BYTES = 6 * 1024 * 1024  # 6MB de upload crudo; sale ~30KB procesado
_IMAGE_PATH = "score-notes/{score_id}.jpg"


def _image_storage_path(score_id: int) -> str:
    return _IMAGE_PATH.format(score_id=score_id)


def _serialize(note: ToriiScoreNote) -> dict[str, Any]:
    return {
        "score_id": note.score_id,
        "user_id": note.user_id,
        "username": note.username,
        "text": note.text,
        "has_image": note.has_image,
        "updated_at": note.updated_at.isoformat() if note.updated_at else None,
    }


def _process_note_image(raw: bytes) -> bytes:
    """Thumbnail liviano: max 400x400, JPEG q82. Tira ValueError si no es imagen."""
    try:
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
    except Exception:
        raise ValueError("not a valid image")
    img.thumbnail((400, 400))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return buf.getvalue()


# ── rutas estaticas primero (sino /{score_id} int se come "batch") ──


@router.get(
    "/torii/score-notes/batch",
    tags=["Torii"],
    name="Notas de varios scores",
    description="Notas para una lista de score ids (para marcar el iconito en la leaderboard).",
)
async def get_score_notes_batch(
    db: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],  # noqa: ARG001
    score_ids: str = Query(..., description="ids separados por coma, max 100"),
) -> dict[str, Any]:
    ids: list[int] = []
    for part in score_ids.split(",")[:100]:
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    if not ids:
        return {"notes": []}

    notes = (
        await db.exec(select(ToriiScoreNote).where(col(ToriiScoreNote.score_id).in_(ids)))
    ).all()
    return {"notes": [_serialize(n) for n in notes]}


@router.get(
    "/torii/score-notes/{score_id}/image",
    tags=["Torii"],
    name="Imagen de la nota de un score",
    description="Thumbnail JPEG de la nota (sin auth: lo carga la texture store del cliente).",
)
async def get_score_note_image(
    score_id: int,
    db: Database,
    storage: StorageService,
) -> Response:
    note = (
        await db.exec(select(ToriiScoreNote).where(ToriiScoreNote.score_id == score_id))
    ).first()
    if note is None or not note.has_image:
        raise HTTPException(status_code=404, detail="No image for this note")
    try:
        data = await storage.read_file(_image_storage_path(score_id))
    except Exception:
        raise HTTPException(status_code=404, detail="Image not found")
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"})


@router.get(
    "/torii/score-notes/{score_id}",
    tags=["Torii"],
    name="Nota de un score",
)
async def get_score_note(
    score_id: int,
    db: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],  # noqa: ARG001
) -> dict[str, Any]:
    note = (
        await db.exec(select(ToriiScoreNote).where(ToriiScoreNote.score_id == score_id))
    ).first()
    if note is None:
        raise HTTPException(status_code=404, detail="No note for this score")
    return _serialize(note)


@router.put(
    "/torii/score-notes/{score_id}",
    tags=["Torii"],
    name="Crear/editar la nota de un score propio",
    description="Multipart: text (obligatorio, max 280) + image (opcional, max 6MB, se procesa a 400x400).",
)
async def upsert_score_note(
    score_id: int,
    db: Database,
    storage: StorageService,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    text: Annotated[str, Form(max_length=2000)],
    image: Annotated[UploadFile | None, File()] = None,
    remove_image: Annotated[bool, Form()] = False,
) -> dict[str, Any]:
    # snapshot pre-commit (expire_on_commit expira current_user; leccion aprendida).
    user_id = current_user.id
    username = current_user.username

    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Note text can't be empty")
    if len(text) > _MAX_TEXT:
        text = text[:_MAX_TEXT]

    score = (await db.exec(select(Score).where(Score.id == score_id))).first()
    if score is None:
        raise HTTPException(status_code=404, detail="Score not found")
    if score.user_id != user_id:
        raise HTTPException(status_code=403, detail="You can only add notes to your own plays")

    # procesar la imagen ANTES de tocar la DB (si viene rota no dejamos nada a medias).
    processed: bytes | None = None
    if image is not None:
        raw = await image.read()
        if len(raw) > _MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Image too large (max 6MB)")
        try:
            processed = _process_note_image(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="That file doesn't look like an image")

    note = (
        await db.exec(select(ToriiScoreNote).where(ToriiScoreNote.score_id == score_id))
    ).first()
    if note is None:
        note = ToriiScoreNote(score_id=score_id, user_id=user_id, username=username, text=text)
    note.text = text
    note.username = username
    note.updated_at = utcnow()

    if processed is not None:
        await storage.write_file(_image_storage_path(score_id), processed, content_type="image/jpeg")
        note.has_image = True
    elif remove_image and note.has_image:
        try:
            await storage.delete_file(_image_storage_path(score_id))
        except Exception:
            pass
        note.has_image = False

    # snapshot ANTES del commit: expire_on_commit expira el objeto y tocar
    # note.has_image despues dispara un lazy-load sincronico (MissingGreenlet).
    final_has_image = note.has_image

    db.add(note)
    await db.commit()

    logger.info(f"[ScoreNote] user {user_id} noted score {score_id} (image={processed is not None})")
    return {
        "score_id": score_id,
        "user_id": user_id,
        "username": username,
        "text": text,
        "has_image": final_has_image,
    }


@router.delete(
    "/torii/score-notes/{score_id}",
    tags=["Torii"],
    name="Borrar la nota de un score propio",
)
async def delete_score_note(
    score_id: int,
    db: Database,
    storage: StorageService,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
) -> dict[str, Any]:
    user_id = current_user.id

    note = (
        await db.exec(select(ToriiScoreNote).where(ToriiScoreNote.score_id == score_id))
    ).first()
    if note is None:
        raise HTTPException(status_code=404, detail="No note for this score")
    if note.user_id != user_id:
        raise HTTPException(status_code=403, detail="Not your note")

    if note.has_image:
        try:
            await storage.delete_file(_image_storage_path(score_id))
        except Exception:
            pass
    await db.delete(note)
    await db.commit()
    return {"ok": True}
