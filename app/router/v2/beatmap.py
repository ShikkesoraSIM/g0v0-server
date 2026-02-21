import asyncio
import hashlib
import json
from typing import Annotated

from app.calculator import get_calculator
from app.calculators.performance import ConvertError
from app.database import Beatmap, BeatmapModel, User
from app.database.beatmap import calculate_beatmap_attributes
from app.dependencies.database import Database, Redis
from app.dependencies.fetcher import Fetcher
from app.dependencies.user import get_current_user, get_optional_user
from app.helpers.asset_proxy_helper import asset_proxy_response
from app.models.mods import APIMod, int_to_mods
from app.models.performance import DifficultyAttributes, DifficultyAttributesUnion
from app.models.score import GameMode
from app.utils import api_doc

from .router import router

from fastapi import HTTPException, Path, Query, Security
from httpx import HTTPError, HTTPStatusError
from sqlmodel import col, select


def _beatmap_includes_for_user(user: User | None) -> list[str]:
    if user is not None:
        return BeatmapModel.TRANSFORMER_INCLUDES
    return [
        include
        for include in BeatmapModel.TRANSFORMER_INCLUDES
        if not include.startswith("current_user_")
    ]


@router.get(
    "/beatmaps/lookup",
    tags=["beatmap"],
    name="lookup beatmap",
    responses={200: api_doc("beatmap detail", BeatmapModel, BeatmapModel.TRANSFORMER_INCLUDES)},
    description="Lookup a beatmap by id/checksum/filename.",
)
@asset_proxy_response
async def lookup_beatmap(
    db: Database,
    fetcher: Fetcher,
    current_user: User | None = Security(get_optional_user, scopes=["public"]),
    id: Annotated[int | None, Query(alias="id", description="beatmap id")] = None,
    md5: Annotated[str | None, Query(alias="checksum", description="beatmap md5")] = None,
    filename: Annotated[str | None, Query(alias="filename", description="beatmap filename")] = None,
):
    if id is None and md5 is None and filename is None:
        raise HTTPException(
            status_code=400,
            detail="At least one of 'id', 'checksum', or 'filename' must be provided.",
        )
    try:
        beatmap = await Beatmap.get_or_fetch(db, fetcher, bid=id, md5=md5)
    except HTTPError:
        raise HTTPException(status_code=404, detail="Beatmap not found")

    if beatmap is None:
        raise HTTPException(status_code=404, detail="Beatmap not found")
    if current_user is not None:
        await db.refresh(current_user)

    return await BeatmapModel.transform(
        beatmap,
        user=current_user,
        includes=_beatmap_includes_for_user(current_user),
    )


@router.get(
    "/beatmaps/{beatmap_id}",
    tags=["beatmap"],
    name="get beatmap",
    responses={200: api_doc("beatmap detail", BeatmapModel, BeatmapModel.TRANSFORMER_INCLUDES)},
    description="Get beatmap detail.",
)
@asset_proxy_response
async def get_beatmap(
    db: Database,
    beatmap_id: Annotated[int, Path(..., description="beatmap id")],
    fetcher: Fetcher,
    current_user: User | None = Security(get_optional_user, scopes=["public"]),
):
    try:
        beatmap = await Beatmap.get_or_fetch(db, fetcher, beatmap_id)
        if current_user is not None:
            await db.refresh(current_user)
        return await BeatmapModel.transform(
            beatmap,
            user=current_user,
            includes=_beatmap_includes_for_user(current_user),
        )
    except HTTPError:
        raise HTTPException(status_code=404, detail="Beatmap not found")


@router.get(
    "/beatmaps/",
    tags=["beatmap"],
    name="batch get beatmaps",
    responses={
        200: api_doc(
            "beatmap list", {"beatmaps": list[BeatmapModel]}, BeatmapModel.TRANSFORMER_INCLUDES, name="BatchBeatmapResponse"
        )
    },
    description="Batch beatmap fetch (max 50).",
)
@asset_proxy_response
async def batch_get_beatmaps(
    db: Database,
    fetcher: Fetcher,
    beatmap_ids: Annotated[
        list[int],
        Query(alias="ids[]", default_factory=list, description="beatmap ids (max 50)"),
    ],
    current_user: User | None = Security(get_optional_user, scopes=["public"]),
):
    if not beatmap_ids:
        beatmaps = (await db.exec(select(Beatmap).order_by(col(Beatmap.last_updated).desc()).limit(50))).all()
    else:
        beatmaps = list((await db.exec(select(Beatmap).where(col(Beatmap.id).in_(beatmap_ids)).limit(50))).all())
        not_found_beatmaps = [bid for bid in beatmap_ids if bid not in [bm.id for bm in beatmaps]]
        beatmaps.extend(
            beatmap
            for beatmap in await asyncio.gather(
                *[Beatmap.get_or_fetch(db, fetcher, bid=bid) for bid in not_found_beatmaps],
                return_exceptions=True,
            )
            if isinstance(beatmap, Beatmap)
        )
        for beatmap in beatmaps:
            await db.refresh(beatmap)
    if current_user is not None:
        await db.refresh(current_user)
    return {
        "beatmaps": [
            await BeatmapModel.transform(
                bm,
                user=current_user,
                includes=_beatmap_includes_for_user(current_user),
            )
            for bm in beatmaps
        ]
    }


@router.post(
    "/beatmaps/{beatmap_id}/attributes",
    tags=["beatmap"],
    name="calculate beatmap attributes",
    response_model=DifficultyAttributesUnion,
    description="Calculate difficulty/performance attributes with mods/ruleset.",
)
async def get_beatmap_attributes(
    db: Database,
    beatmap_id: Annotated[int, Path(..., description="beatmap id")],
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    mods: Annotated[
        list[str],
        Query(
            default_factory=list,
            description="mods list: int bitmask or json/acronyms",
        ),
    ],
    redis: Redis,
    fetcher: Fetcher,
    ruleset: Annotated[GameMode | None, Query(description="ruleset; default beatmap mode")] = None,
    ruleset_id: Annotated[int | None, Query(description="ruleset as numeric id")] = None,
):
    mods_ = []
    if mods and mods[0].isdigit():
        mods_ = int_to_mods(int(mods[0]))
    else:
        for i in mods:
            try:
                mods_.append(json.loads(i))
            except json.JSONDecodeError:
                mods_.append(APIMod(acronym=i, settings={}))
    mods_.sort(key=lambda x: x["acronym"])
    if ruleset_id is not None and ruleset is None:
        ruleset = GameMode.from_int(ruleset_id)
    if ruleset is None:
        beatmap_db = await Beatmap.get_or_fetch(db, fetcher, beatmap_id)
        ruleset = beatmap_db.mode
    key = (
        f"beatmap:{beatmap_id}:{ruleset}:"
        f"{hashlib.md5(str(mods_).encode(), usedforsecurity=False).hexdigest()}:attributes"
    )
    if await redis.exists(key):
        return DifficultyAttributes.model_validate_json(await redis.get(key))  # pyright: ignore[reportArgumentType]

    if await get_calculator().can_calculate_difficulty(ruleset) is False:
        raise HTTPException(status_code=422, detail="Cannot calculate difficulty for the specified ruleset")

    try:
        return await calculate_beatmap_attributes(beatmap_id, ruleset, mods_, redis, fetcher)
    except HTTPStatusError:
        raise HTTPException(status_code=404, detail="Beatmap not found")
    except ConvertError as e:
        raise HTTPException(status_code=400, detail=str(e))
