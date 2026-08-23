"""Aviso al #feed cuando alguien genera un mapa con Mapperatorinator.

El cliente solo llama a esto cuando el mapa salio con identidad propia (titulo,
artista e imagen custom): un mapa generado por probar, sin tocarle nada, no
spamea el feed. Cooldown por usuario en redis para que tampoco spamee el que
genera veinte seguidos con titulo.
"""

import json
from datetime import datetime
from typing import Annotated

from fastapi import HTTPException, Security
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlmodel import col, select

from app.database import ToriiMapperatorinatorPreset, User
from app.dependencies.database import Database, Redis
from app.dependencies.user import get_current_user
from app.service.discord_feed import notify_mapperatorinator_map
from app.utils import utcnow

from .router import router

_COOLDOWN_SECONDS = 600


class MapperatorinatorFeedRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    artist: str = Field(min_length=1, max_length=120)
    difficulty_name: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=40)


class MapperatorinatorFeedResponse(BaseModel):
    posted: bool


@router.post(
    "/torii/mapperatorinator/feed",
    tags=["Torii"],
    name="Announce a Mapperatorinator generation",
    description="Posts an AI-generated-map event to the community feed (rate-limited per user).",
    response_model=MapperatorinatorFeedResponse,
)
async def announce_mapperatorinator_map(
    payload: MapperatorinatorFeedRequest,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    redis: Redis,
) -> MapperatorinatorFeedResponse:
    # snapshot antes de cualquier await largo (expire_on_commit)
    user_id = current_user.id
    username = current_user.username

    # nx=True: si la key ya existe, alguien anuncio hace menos del cooldown.
    if not await redis.set(f"feed:mapperatorinator:{user_id}", "1", ex=_COOLDOWN_SECONDS, nx=True):
        return MapperatorinatorFeedResponse(posted=False)

    notify_mapperatorinator_map(
        username=username,
        user_id=user_id,
        title=payload.title.strip(),
        artist=payload.artist.strip(),
        difficulty_name=(payload.difficulty_name or "").strip() or None,
        model=(payload.model or "").strip() or None,
    )
    return MapperatorinatorFeedResponse(posted=True)


# ---------------------------------------------------------------------------
# Presets de generacion
#
# "esta combinacion de opciones me gusto, guardala": el cliente manda el JSON
# con lo que uso y un nombre. Viven en el server y no en la maquina para que
# sobrevivan a formatear la PC, que es todo el punto de tenerlos.
# ---------------------------------------------------------------------------

_MAX_PRESETS_PER_USER = 60
_MAX_SETTINGS_CHARS = 8000


class MapperatorinatorPresetPayload(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    settings: str = Field(min_length=2, max_length=_MAX_SETTINGS_CHARS)
    # de que preset salio, si salio de alguno: el cliente lo sabe porque el mapa del que
    # copiaste las opciones se lleva adentro con que preset se genero.
    origin_preset_id: int | None = None


class MapperatorinatorPreset(BaseModel):
    id: int
    name: str
    settings: str
    updated_at: datetime
    # de donde salio este preset...
    origin_username: str | None = None
    # ...y quienes se llevaron el tuyo.
    forks: int = 0
    forked_by: list[str] = Field(default_factory=list)


class MapperatorinatorPresetList(BaseModel):
    presets: list[MapperatorinatorPreset]


def _as_preset(
    row: ToriiMapperatorinatorPreset,
    forks: int = 0,
    forked_by: list[str] | None = None,
) -> MapperatorinatorPreset:
    return MapperatorinatorPreset(
        id=row.id or 0,
        name=row.name,
        settings=row.settings,
        updated_at=row.updated_at,
        origin_username=row.origin_username,
        forks=forks,
        forked_by=forked_by or [],
    )


# cuantos nombres se muestran al pasar el mouse por el contador de forks.
_MAX_FORK_NAMES = 20


@router.get(
    "/torii/mapperatorinator/presets",
    tags=["Torii"],
    name="List your Mapperatorinator presets",
    description="Generation presets saved by the current user, newest first.",
    response_model=MapperatorinatorPresetList,
)
async def list_mapperatorinator_presets(
    db: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
) -> MapperatorinatorPresetList:
    user_id = current_user.id

    rows = (
        await db.exec(
            select(ToriiMapperatorinatorPreset)
            .where(ToriiMapperatorinatorPreset.user_id == user_id)
            .order_by(col(ToriiMapperatorinatorPreset.updated_at).desc())
        )
    ).all()

    ids = [row.id for row in rows if row.id is not None]
    forks: dict[int, list[str]] = {}

    if ids:
        # quien se llevo cada uno. Se traen los nombres directo: son pocos y sirven para
        # el "te lo forkearon estos" al pasar el mouse.
        taken = (
            await db.exec(
                select(
                    ToriiMapperatorinatorPreset.origin_preset_id,
                    ToriiMapperatorinatorPreset.user_id,
                )
                .where(
                    col(ToriiMapperatorinatorPreset.origin_preset_id).in_(ids),
                    ToriiMapperatorinatorPreset.user_id != user_id,
                )
            )
        ).all()

        user_ids = {row[1] for row in taken}
        names: dict[int, str] = {}

        if user_ids:
            names = {
                row[0]: row[1]
                for row in (
                    await db.exec(select(User.id, User.username).where(col(User.id).in_(user_ids)))
                ).all()
            }

        for origin_id, forker_id in taken:
            if origin_id is None:
                continue

            name = names.get(forker_id, f"user {forker_id}")
            people = forks.setdefault(origin_id, [])

            if name not in people:
                people.append(name)

    return MapperatorinatorPresetList(
        presets=[
            _as_preset(row, len(forks.get(row.id or 0, [])), forks.get(row.id or 0, [])[:_MAX_FORK_NAMES])
            for row in rows
        ]
    )


@router.put(
    "/torii/mapperatorinator/presets",
    tags=["Torii"],
    name="Save a Mapperatorinator preset",
    description="Creates a preset, or overwrites the one with the same name (like a collection).",
    response_model=MapperatorinatorPreset,
)
async def save_mapperatorinator_preset(
    payload: MapperatorinatorPresetPayload,
    db: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
) -> MapperatorinatorPreset:
    user_id = current_user.id
    name = payload.name.strip()

    if not name:
        raise HTTPException(status_code=422, detail="The preset needs a name.")

    try:
        json.loads(payload.settings)
    except ValueError as e:
        raise HTTPException(status_code=422, detail="The settings aren't valid JSON.") from e

    existing = (
        await db.exec(
            select(ToriiMapperatorinatorPreset).where(
                ToriiMapperatorinatorPreset.user_id == user_id,
                ToriiMapperatorinatorPreset.name == name,
            )
        )
    ).first()

    origin_user_id: int | None = None
    origin_username: str | None = None

    if payload.origin_preset_id is not None:
        source = await db.get(ToriiMapperatorinatorPreset, payload.origin_preset_id)

        # forkear el tuyo propio no cuenta: es guardarlo de nuevo.
        if source is not None and source.user_id != user_id:
            origin_user_id = source.user_id
            owner = await db.get(User, source.user_id)
            origin_username = owner.username if owner is not None else None

    if existing is not None:
        existing.settings = payload.settings
        existing.updated_at = utcnow()

        if origin_user_id is not None:
            existing.origin_preset_id = payload.origin_preset_id
            existing.origin_user_id = origin_user_id
            existing.origin_username = origin_username

        db.add(existing)
        await db.commit()
        await db.refresh(existing)
        return _as_preset(existing)

    total = (
        await db.exec(
            select(func.count())
            .select_from(ToriiMapperatorinatorPreset)
            .where(ToriiMapperatorinatorPreset.user_id == user_id)
        )
    ).one()

    if (total or 0) >= _MAX_PRESETS_PER_USER:
        raise HTTPException(
            status_code=422,
            detail=f"You already have {_MAX_PRESETS_PER_USER} presets. Delete one first.",
        )

    row = ToriiMapperatorinatorPreset(
        user_id=user_id,
        name=name,
        settings=payload.settings,
        origin_preset_id=payload.origin_preset_id if origin_user_id is not None else None,
        origin_user_id=origin_user_id,
        origin_username=origin_username,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _as_preset(row)


@router.delete(
    "/torii/mapperatorinator/presets/{preset_id}",
    tags=["Torii"],
    name="Delete a Mapperatorinator preset",
    description="Deletes one of your own presets.",
    status_code=204,
)
async def delete_mapperatorinator_preset(
    preset_id: int,
    db: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
) -> None:
    user_id = current_user.id
    row = await db.get(ToriiMapperatorinatorPreset, preset_id)

    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="That preset isn't yours or doesn't exist.")

    await db.delete(row)
    await db.commit()
