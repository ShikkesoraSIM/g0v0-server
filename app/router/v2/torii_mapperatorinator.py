"""Aviso al #feed cuando alguien genera un mapa con Mapperatorinator.

El cliente solo llama a esto cuando el mapa salio con identidad propia (titulo,
artista e imagen custom): un mapa generado por probar, sin tocarle nada, no
spamea el feed. Cooldown por usuario en redis para que tampoco spamee el que
genera veinte seguidos con titulo.
"""

from typing import Annotated

from fastapi import Security
from pydantic import BaseModel, Field

from app.database import User
from app.dependencies.database import Redis
from app.dependencies.user import get_current_user
from app.service.discord_feed import notify_mapperatorinator_map

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
