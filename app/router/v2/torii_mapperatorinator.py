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
from sqlalchemy.exc import IntegrityError
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
    # y de quien era ese preset, tambien segun el mapa. Va junto con el id a proposito:
    # el server no traduce id -> dueño, ver _resolve_origin.
    origin_username: str | None = Field(default=None, max_length=32)
    # "si ya tenes uno con este nombre, pisalo". Por default NO: los tres lugares desde
    # donde se guarda abren un cuadro con el nombre ya escrito y sin la lista a la vista,
    # asi que la persona no puede ver el choque antes de apretar, y se le iba un preset
    # sin decirle nada. Con esto el cliente pregunta primero.
    overwrite: bool = False


class MapperatorinatorPresetPatch(BaseModel):
    """Cambiar el nombre y/o los settings de un preset propio, sin mover el id.

    Campo ausente o null es "no lo toques": renombrar no puede pisar los settings ni
    al reves, asi que nunca se escribe null encima de lo que ya habia.
    """

    name: str | None = Field(default=None, min_length=1, max_length=60)
    settings: str | None = Field(default=None, min_length=2, max_length=_MAX_SETTINGS_CHARS)


class MapperatorinatorPreset(BaseModel):
    id: int
    name: str
    settings: str
    updated_at: datetime
    # de donde salio este preset...
    origin_username: str | None = None
    # ...con que id, para que el cliente pueda pasarle la atribucion a las copias.
    origin_preset_id: int | None = None
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
        origin_preset_id=row.origin_preset_id,
        forks=forks,
        forked_by=forked_by or [],
    )


# cuantos nombres se muestran al pasar el mouse por el contador de forks.
_MAX_FORK_NAMES = 20


async def _forkers(db, preset_id: int, owner_id: int) -> list[str]:
    """Quienes se llevaron este preset. Lo mismo que hace la lista, pero de a un id."""
    taken = (
        await db.exec(
            select(ToriiMapperatorinatorPreset.user_id).where(
                col(ToriiMapperatorinatorPreset.origin_preset_id) == preset_id,
                ToriiMapperatorinatorPreset.user_id != owner_id,
            )
        )
    ).all()

    # uno puede tener dos presets colgados del mismo origen y sigue siendo una persona.
    user_ids: list[int] = []

    for forker_id in taken:
        if forker_id not in user_ids:
            user_ids.append(forker_id)

    if not user_ids:
        return []

    names = {
        row[0]: row[1]
        for row in (await db.exec(select(User.id, User.username).where(col(User.id).in_(user_ids)))).all()
    }

    return [names.get(forker_id, f"user {forker_id}") for forker_id in user_ids]


async def _resolve_origin(
    db,
    user_id: int,
    payload: MapperatorinatorPresetPayload,
) -> tuple[int | None, int | None, str | None]:
    """De que preset ajeno salio este, si es que salio de alguno.

    El id solo no prueba nada: son correlativos, asi que traducirlo a un dueño desde el
    server era un buscador de "de quien es el preset N" para cualquiera, y sumarle un fork
    a una fila ajena salia gratis. Entonces se acepta en dos casos nada mas: el preset es
    tuyo (y ahi la atribucion se hereda, duplicar no lava el "taken from X"), o el que
    guarda ya sabia de quien era, porque el mapa generado se lleva el nombre adentro.
    """
    if payload.origin_preset_id is None:
        return None, None, None

    # lockeada: entre leerla y guardar la copia, el dueño puede borrarla, y ahi la copia
    # nace apuntando a un id que ya no existe (el delete junto a sus hijos antes de que
    # esta existiera).
    source = (
        await db.exec(
            select(ToriiMapperatorinatorPreset)
            .where(col(ToriiMapperatorinatorPreset.id) == payload.origin_preset_id)
            .with_for_update()
        )
    ).first()

    if source is None:
        return None, None, None

    # forkear el tuyo propio no cuenta: es guardarlo de nuevo.
    if source.user_id == user_id:
        return source.origin_preset_id, source.origin_user_id, source.origin_username

    owner = await db.get(User, source.user_id)
    claimed = (payload.origin_username or "").strip()

    # sin nombre, o con el nombre equivocado, no se atribuye nada y no se contesta nada:
    # el que va probando ids no se entera de quien es la fila ni le infla el contador.
    if owner is None or not claimed:
        return None, None, None

    if claimed.casefold() != owner.username.casefold() and not await _had_username(db, owner, claimed):
        return None, None, None

    return source.id, source.user_id, owner.username


async def _had_username(db, owner: User, claimed: str) -> bool:
    """Si el dueño se llamo asi alguna vez.

    El nombre lo manda el cliente sacado de datos viejos (el mapa se lo lleva adentro
    cuando se genera), y en Torii se puede cambiar de nombre: sin esto, el dueño se
    renombra y todas las copias que se hagan de ahi en mas dejan de acreditarlo en
    silencio. Mismo criterio que usa el submit manual para encontrar a alguien por un
    replay viejo.
    """
    found = (
        await db.exec(
            select(User.id).where(
                User.id == owner.id,
                func.json_contains(User.previous_usernames, func.json_quote(claimed)) == 1,
            )
        )
    ).first()

    return found is not None


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

    # se lockea la fila del usuario para que los saves de esta cuenta entren en fila india:
    # el limite de abajo es contar y despues insertar, y dos guardadas al mismo tiempo
    # contaban las dos 59 y quedaban 61. De paso serializa el "existe uno con ese nombre".
    locked = (await db.exec(select(User).where(User.id == user_id).with_for_update())).first()

    if locked is None:
        raise HTTPException(status_code=404, detail="Unknown user")

    existing = (
        await db.exec(
            select(ToriiMapperatorinatorPreset).where(
                ToriiMapperatorinatorPreset.user_id == user_id,
                ToriiMapperatorinatorPreset.name == name,
            )
        )
    ).first()

    if existing is not None and not payload.overwrite:
        raise HTTPException(status_code=409, detail=f'You already have a preset called "{name}".')

    origin_preset_id, origin_user_id, origin_username = await _resolve_origin(db, user_id, payload)

    if existing is not None:
        existing.settings = payload.settings
        existing.updated_at = utcnow()

        # la atribucion sigue a los settings: si lo que se guarda encima no viene de
        # ningun lado, el "taken from X" de antes ya no describe nada de lo que hay
        # adentro, y a X le seguia contando un fork para siempre.
        existing.origin_preset_id = origin_preset_id
        existing.origin_user_id = origin_user_id
        existing.origin_username = origin_username

        db.add(existing)
        await db.commit()
        await db.refresh(existing)

        forkers = await _forkers(db, existing.id or 0, user_id)
        return _as_preset(existing, len(forkers), forkers[:_MAX_FORK_NAMES])

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
        origin_preset_id=origin_preset_id if origin_user_id is not None else None,
        origin_user_id=origin_user_id,
        origin_username=origin_username,
    )
    db.add(row)

    try:
        await db.commit()
    except IntegrityError as e:
        # el lock de arriba ya cubre la carrera; esto es el cinturon por si alguna vez se
        # guarda por fuera de ese camino, para que no salga un 500 pelado.
        await db.rollback()
        raise HTTPException(status_code=409, detail=f'You already have a preset called "{name}".') from e

    await db.refresh(row)
    return _as_preset(row)


@router.patch(
    "/torii/mapperatorinator/presets/{preset_id}",
    tags=["Torii"],
    name="Rename or edit a Mapperatorinator preset",
    description="Changes the name and/or the settings of one of your presets, keeping its id.",
    response_model=MapperatorinatorPreset,
)
async def update_mapperatorinator_preset(
    preset_id: int,
    payload: MapperatorinatorPresetPatch,
    db: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
) -> MapperatorinatorPreset:
    user_id = current_user.id

    if payload.name is None and payload.settings is None:
        raise HTTPException(status_code=422, detail="Nothing to update.")

    # el mismo lock que toma el PUT: sin esto los dos caminos escriben la misma fila sin
    # verse, y un guardado que entra en el medio de un renombrado se pierde entero.
    locked = (await db.exec(select(User).where(User.id == user_id).with_for_update())).first()

    if locked is None:
        raise HTTPException(status_code=404, detail="Unknown user")

    row = await db.get(ToriiMapperatorinatorPreset, preset_id)

    # el mismo 404 para "no existe" y para "no es tuyo": los ids son correlativos, asi que
    # separarlos seria decirle a cualquiera cuales existen.
    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="That preset isn't yours or doesn't exist.")

    name = None

    if payload.name is not None:
        name = payload.name.strip()

        if not name:
            raise HTTPException(status_code=422, detail="The preset needs a name.")

    if payload.settings is not None:
        try:
            json.loads(payload.settings)
        except ValueError as e:
            raise HTTPException(status_code=422, detail="The settings aren't valid JSON.") from e

    # comparacion en python, no en mysql: la collation es case-insensitive y "Stream" ->
    # "stream" le parece lo mismo, pero para el que renombra es un cambio.
    changed_name = name is not None and name != row.name
    changed_settings = payload.settings is not None and payload.settings != row.settings

    if changed_name:
        # el id != preset_id es todo el asunto: sin eso la fila choca contra si misma por
        # collation y no te deja arreglarle las mayusculas a tu propio preset.
        taken = (
            await db.exec(
                select(ToriiMapperatorinatorPreset.id)
                .where(
                    ToriiMapperatorinatorPreset.user_id == user_id,
                    ToriiMapperatorinatorPreset.name == name,
                    col(ToriiMapperatorinatorPreset.id) != preset_id,
                )
                .limit(1)
            )
        ).first()

        if taken is not None:
            raise HTTPException(status_code=409, detail=f'You already have a preset called "{name}".')

    if changed_name or changed_settings:
        if changed_name and name is not None:
            row.name = name

        if changed_settings and payload.settings is not None:
            row.settings = payload.settings
            # renombrar no es volver a guardar: la lista ordena por esto y la fila dice
            # "last saved", asi que solo lo mueve un cambio de settings.
            row.updated_at = utcnow()

        db.add(row)

        try:
            await db.commit()
        except IntegrityError as e:
            # dos clientes renombrando a la vez: el unique (user_id, name) lo agarra igual.
            await db.rollback()
            raise HTTPException(
                status_code=409, detail=f'You already have a preset called "{name}".'
            ) from e

        await db.refresh(row)

    forked_by = await _forkers(db, preset_id, user_id)
    return _as_preset(row, len(forked_by), forked_by[:_MAX_FORK_NAMES])


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

    # origin_preset_id es un int suelto, sin foreign key: si se borra la fila los hijos
    # quedan apuntando a un id que no existe y, peor, un id reciclado los haria forks de
    # cualquier otro. Se les corta ESE puntero y nada mas.
    #
    # De quien lo sacaron (origin_user_id / origin_username) queda como estaba: es cierto
    # igual, paso de verdad, y no depende de que la fila siga existiendo. Colgarlos del
    # abuelo estaria peor: el del medio pudo haber cambiado el preset entero, asi que lo
    # que el hijo se llevo no es el estilo del abuelo, y le inflaria el contador con un
    # fork que nunca le hicieron.
    children = (
        await db.exec(
            select(ToriiMapperatorinatorPreset).where(
                col(ToriiMapperatorinatorPreset.origin_preset_id) == preset_id
            )
        )
    ).all()

    for child in children:
        child.origin_preset_id = None
        db.add(child)

    await db.delete(row)
    await db.commit()
