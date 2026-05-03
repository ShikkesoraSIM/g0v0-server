from datetime import datetime, timedelta
import copy
import sys
from typing import Annotated, Literal

from app.calculator import calculate_pp_weight
from app.config import settings
from app.const import BANCHOBOT_ID
from app.database import (
    Beatmap,
    BeatmapModel,
    BeatmapPlaycounts,
    Beatmapset,
    BeatmapsetModel,
    FavouriteBeatmapset,
    User,
)
from app.database.beatmap_playcounts import BeatmapPlaycountsModel
from app.database.best_scores import BestScore
from app.database.events import Event
from app.database.score import Score, get_user_first_scores
from app.database.user import UserModel
from app.dependencies.api_version import APIVersion
from app.dependencies.cache import UserCacheService
from app.dependencies.database import Database, get_redis
from app.dependencies.fetcher import get_fetcher
from app.dependencies.user import get_current_user, get_optional_user
from app.helpers.asset_proxy_helper import asset_proxy_response
from app.log import log
from app.models.mods import API_MODS
from app.models.beatmap import BeatmapRankStatus
from app.models.score import GameMode
from app.models.user import BeatmapsetType
from app.service.pp_variant_service import (
    apply_pp_variant_to_score_responses,
    apply_pp_variant_to_user_response,
    get_score_pp_variant,
    get_score_pp_variant_batch,
    normalize_pp_variant,
)
from app.service.user_cache_service import get_user_cache_service, prewarm_pp_dev_profile_background
from app.utils import api_doc, utcnow

from .router import router

from fastapi import BackgroundTasks, HTTPException, Path, Query, Request, Security
from sqlalchemy.orm import joinedload
from sqlmodel import exists, func, select, tuple_
from sqlmodel.sql.expression import col


def _get_difficulty_reduction_mods() -> set[str]:
    mods: set[str] = set()
    for ruleset_mods in API_MODS.values():
        for mod_acronym, mod_meta in ruleset_mods.items():
            if mod_meta.get("Type") == "DifficultyReduction":
                mods.add(mod_acronym)
    return mods


def _normalize_user_mode(mode: GameMode | None) -> GameMode | None:
    if mode == GameMode.FRUITSRX:
        return GameMode.FRUITS
    return mode


async def visible_to_current_user(user: User, current_user: User | None, session: Database) -> bool:
    if user.id == BANCHOBOT_ID:
        return False
    if current_user and current_user.id == user.id:
        return True
    return not await user.is_restricted(session)


async def viewer_allows_nsfw_media(current_user: User | None) -> bool:
    if current_user is None:
        return False
    await current_user.awaitable_attrs.user_preference
    return bool(current_user.user_preference and current_user.user_preference.profile_media_show_nsfw)


@router.get(
    "/users/",
    responses={
        200: api_doc("æ‰¹é‡èŽ·å–ç”¨æˆ·ä¿¡æ¯", {"users": list[UserModel]}, User.CARD_INCLUDES, name="UsersLookupResponse")
    },
    name="æ‰¹é‡èŽ·å–ç”¨æˆ·ä¿¡æ¯",
    description="é€šè¿‡ç”¨æˆ· ID åˆ—è¡¨æ‰¹é‡èŽ·å–ç”¨æˆ·ä¿¡æ¯ã€‚",
    tags=["ç”¨æˆ·"],
)
@router.get("/users/lookup", include_in_schema=False)
@router.get("/users/lookup/", include_in_schema=False)
@asset_proxy_response
async def get_users(
    session: Database,
    request: Request,
    background_task: BackgroundTasks,
    user_ids: Annotated[list[int], Query(default_factory=list, alias="ids[]", description="è¦æŸ¥è¯¢çš„ç”¨æˆ· ID åˆ—è¡¨")],
    current_user: User | None = Security(get_optional_user, scopes=["public"]),
    include_variant_statistics: Annotated[
        bool,
        Query(description="æ˜¯å¦åŒ…å«å„æ¨¡å¼çš„ç»Ÿè®¡ä¿¡æ¯"),
    ] = False,  # TODO: future use
):
    redis = get_redis()
    cache_service = get_user_cache_service(redis)
    show_nsfw_media = await viewer_allows_nsfw_media(current_user)

    if user_ids:
        # å…ˆå°è¯•ä»Žç¼“å­˜èŽ·å–
        cached_users = []
        uncached_user_ids = []

        for user_id in user_ids[:50]:  # é™åˆ¶50ä¸ª
            # When viewer allows NSFW, bypass cache to avoid stale sanitized payloads.
            if show_nsfw_media:
                uncached_user_ids.append(user_id)
                continue
            cached_user = await cache_service.get_user_from_cache(user_id)
            if cached_user:
                cached_users.append(UserModel.apply_nsfw_media_policy(copy.deepcopy(cached_user), show_nsfw_media))
            else:
                uncached_user_ids.append(user_id)

        # æŸ¥è¯¢æœªç¼“å­˜çš„ç”¨æˆ·
        if uncached_user_ids:
            searched_users = (
                await session.exec(
                    select(User).where(col(User.id).in_(uncached_user_ids), ~User.is_restricted_query(col(User.id)))
                )
            ).all()

            # å°†æŸ¥è¯¢åˆ°çš„ç”¨æˆ·æ·»åŠ åˆ°ç¼“å­˜å¹¶è¿”å›ž
            for searched_user in searched_users:
                if searched_user.id != BANCHOBOT_ID:
                    canonical_user_resp = await UserModel.transform(
                        searched_user,
                        includes=User.CARD_INCLUDES,
                        show_nsfw_media=True,
                    )
                    user_resp = UserModel.apply_nsfw_media_policy(copy.deepcopy(canonical_user_resp), show_nsfw_media)
                    cached_users.append(user_resp)

        response = {"users": cached_users}
        return response
    else:
        searched_users = (
            await session.exec(select(User).limit(50).where(~User.is_restricted_query(col(User.id))))
        ).all()
        users = []
        for searched_user in searched_users:
            if searched_user.id == BANCHOBOT_ID:
                continue
            canonical_user_resp = await UserModel.transform(
                searched_user,
                includes=User.CARD_INCLUDES,
                show_nsfw_media=True,
            )
            user_resp = UserModel.apply_nsfw_media_policy(copy.deepcopy(canonical_user_resp), show_nsfw_media)
            users.append(user_resp)

        response = {"users": users}
        return response


@router.get(
    "/users/{user_id}/recent_activity",
    tags=["ç”¨æˆ·"],
    response_model=list[Event],
    name="èŽ·å–ç”¨æˆ·æœ€è¿‘æ´»åŠ¨",
    description="èŽ·å–ç”¨æˆ·åœ¨æœ€è¿‘ 30 å¤©å†…çš„æ´»åŠ¨æ—¥å¿—ã€‚",
)
async def get_user_events(
    session: Database,
    user_id: Annotated[int, Path(description="ç”¨æˆ· ID")],
    limit: Annotated[int, Query(description="é™åˆ¶è¿”å›žçš„æ´»åŠ¨æ•°é‡")] = 50,
    offset: Annotated[int | None, Query(description="æ´»åŠ¨æ—¥å¿—çš„åç§»é‡")] = None,
    current_user: User | None = Security(get_optional_user, scopes=["public"]),
):
    db_user = await session.get(User, user_id)
    if db_user is None or not await visible_to_current_user(db_user, current_user, session):
        raise HTTPException(404, "User Not found")
    if offset is None:
        offset = 0
    if limit > 100:
        limit = 100

    if offset == 0:
        cursor = sys.maxsize
    else:
        cursor = (
            await session.exec(
                select(Event.id)
                .where(Event.user_id == db_user.id, Event.created_at >= utcnow() - timedelta(days=30))
                .order_by(col(Event.id).desc())
                .limit(1)
                .offset(offset - 1)
            )
        ).first()
        if cursor is None:
            return []

    events = (
        await session.exec(
            select(Event)
            .where(Event.user_id == db_user.id, Event.created_at >= utcnow() - timedelta(days=30), Event.id < cursor)
            .order_by(col(Event.id).desc())
            .limit(limit)
        )
    ).all()
    return events


@router.get(
    "/users/{user_id}/kudosu",
    response_model=list,
    name="èŽ·å–ç”¨æˆ· kudosu è®°å½•",
    description="èŽ·å–æŒ‡å®šç”¨æˆ·çš„ kudosu è®°å½•ã€‚TODO: å¯èƒ½ä¼šå®žçŽ°",
    tags=["ç”¨æˆ·"],
)
async def get_user_kudosu(
    session: Database,
    user_id: Annotated[int, Path(description="ç”¨æˆ· ID")],
    offset: Annotated[int, Query(description="åç§»é‡")] = 0,
    limit: Annotated[int, Query(description="è¿”å›žè®°å½•æ•°é‡é™åˆ¶")] = 6,
    current_user: User | None = Security(get_optional_user, scopes=["public"]),
):
    """
    èŽ·å–ç”¨æˆ·çš„ kudosu è®°å½•

    TODO: å¯èƒ½ä¼šå®žçŽ°
    ç›®å‰è¿”å›žç©ºæ•°ç»„ä½œä¸ºå ä½ç¬¦
    """
    # éªŒè¯ç”¨æˆ·æ˜¯å¦å­˜åœ¨
    db_user = await session.get(User, user_id)
    if db_user is None or not await visible_to_current_user(db_user, current_user, session):
        raise HTTPException(404, "User not found")

    # TODO: å®žçŽ° kudosu è®°å½•èŽ·å–é€»è¾‘
    return []


@router.get(
    "/users/{user_id}/beatmaps-passed",
    name="èŽ·å–ç”¨æˆ·å·²é€šè¿‡è°±é¢",
    description="èŽ·å–æŒ‡å®šç”¨æˆ·åœ¨ç»™å®šè°±é¢é›†ä¸­çš„å·²é€šè¿‡è°±é¢åˆ—è¡¨ã€‚",
    tags=["ç”¨æˆ·"],
    responses={
        200: api_doc("ç”¨æˆ·å·²é€šè¿‡è°±é¢åˆ—è¡¨", {"beatmaps_passed": list[BeatmapModel]}, name="BeatmapsPassedResponse")
    },
)
@asset_proxy_response
async def get_user_beatmaps_passed(
    session: Database,
    user_id: Annotated[int, Path(description="ç”¨æˆ· ID")],
    current_user: User | None = Security(get_optional_user, scopes=["public"]),
    beatmapset_ids: Annotated[
        list[int],
        Query(
            alias="beatmapset_ids[]",
            description="è¦æŸ¥è¯¢çš„è°±é¢é›† ID åˆ—è¡¨ (æœ€å¤š 50 ä¸ª)",
        ),
    ] = [],
    ruleset_id: Annotated[
        int | None,
        Query(description="æŒ‡å®š ruleset ID"),
    ] = None,
    exclude_converts: Annotated[bool, Query(description="æ˜¯å¦æŽ’é™¤è½¬è°±æˆç»©")] = False,
    is_legacy: Annotated[bool | None, Query(description="æ˜¯å¦ä»…è¿”å›ž Stable æˆç»©")] = None,
    no_diff_reduction: Annotated[bool, Query(description="æ˜¯å¦æŽ’é™¤å‡éš¾ MOD æˆç»©")] = True,
):
    if not beatmapset_ids:
        return {"beatmaps_passed": []}
    if len(beatmapset_ids) > 50:
        raise HTTPException(status_code=413, detail="beatmapset_ids cannot exceed 50 items")

    user = await session.get(User, user_id)
    if user is None or not await visible_to_current_user(user, current_user, session):
        raise HTTPException(404, detail="User not found")

    allowed_mode: GameMode | None = None
    if ruleset_id is not None:
        try:
            allowed_mode = GameMode.from_int_extra(ruleset_id)
        except KeyError as exc:
            raise HTTPException(status_code=422, detail="Invalid ruleset_id") from exc

    score_query = (
        select(Score.beatmap_id, Score.mods, Score.gamemode, Beatmap.mode)
        .where(
            Score.user_id == user.id,
            col(Score.beatmap_id).in_(select(Beatmap.id).where(col(Beatmap.beatmapset_id).in_(beatmapset_ids))),
            col(Score.passed).is_(True),
        )
        .join(Beatmap, col(Beatmap.id) == Score.beatmap_id)
    )
    if allowed_mode:
        score_query = score_query.where(Score.gamemode == allowed_mode)

    scores = (await session.exec(score_query)).all()
    if not scores:
        return {"beatmaps_passed": []}

    difficulty_reduction_mods = _get_difficulty_reduction_mods() if no_diff_reduction else set()
    passed_beatmap_ids: set[int] = set()
    for beatmap_id, mods, _mode, _beatmap_mode in scores:
        gamemode = GameMode(_mode)
        beatmap_mode = GameMode(_beatmap_mode)

        if exclude_converts and gamemode.to_base_ruleset() != beatmap_mode:
            continue
        if difficulty_reduction_mods and any(mod["acronym"] in difficulty_reduction_mods for mod in mods):
            continue
        passed_beatmap_ids.add(beatmap_id)
    if not passed_beatmap_ids:
        return {"beatmaps_passed": []}

    beatmaps = (
        await session.exec(
            select(Beatmap)
            .where(col(Beatmap.id).in_(passed_beatmap_ids))
            .order_by(col(Beatmap.difficulty_rating).desc())
        )
    ).all()

    return {
        "beatmaps_passed": [
            await BeatmapModel.transform(
                beatmap,
            )
            for beatmap in beatmaps
        ]
    }


@router.get(
    "/users/{user_id}/{ruleset}",
    name="èŽ·å–ç”¨æˆ·ä¿¡æ¯(æŒ‡å®šruleset)",
    description="é€šè¿‡ç”¨æˆ· ID æˆ–ç”¨æˆ·åèŽ·å–å•ä¸ªç”¨æˆ·çš„è¯¦ç»†ä¿¡æ¯ï¼Œå¹¶æŒ‡å®šç‰¹å®š rulesetã€‚",
    tags=["ç”¨æˆ·"],
    responses={
        200: api_doc("ç”¨æˆ·ä¿¡æ¯", UserModel, User.USER_INCLUDES),
    },
)
@asset_proxy_response
async def get_user_info_ruleset(
    session: Database,
    background_task: BackgroundTasks,
    user_id: Annotated[str, Path(description="ç”¨æˆ· ID æˆ–ç”¨æˆ·å")],
    ruleset: Annotated[GameMode | None, Path(description="æŒ‡å®š ruleset")],
    pp_variant: Annotated[str | None, Query(description="pp variant: stable / pp_dev")] = None,
    current_user: User | None = Security(get_optional_user, scopes=["public"]),
):
    ruleset = _normalize_user_mode(ruleset)
    resolved_pp_variant = normalize_pp_variant(pp_variant)
    redis = get_redis()
    cache_service = get_user_cache_service(redis)
    show_nsfw_media = await viewer_allows_nsfw_media(current_user)

    # å¦‚æžœæ˜¯æ•°å­—IDï¼Œå…ˆå°è¯•ä»Žç¼“å­˜èŽ·å–ï¼ˆcache stores canonical payloadï¼‰
    if user_id.isdigit():
        user_id_int = int(user_id)
        cached_user = await cache_service.get_user_from_cache(user_id_int, ruleset, resolved_pp_variant)
        if cached_user and "statistics" in cached_user:
            return UserModel.apply_nsfw_media_policy(copy.deepcopy(cached_user), show_nsfw_media)

    searched_user = (
        await session.exec(
            select(User).where(
                User.id == int(user_id) if user_id.isdigit() else User.username == user_id.removeprefix("@")
            )
        )
    ).first()
    if not searched_user or searched_user.id == BANCHOBOT_ID:
        raise HTTPException(404, detail="User not found")
    searched_is_self = current_user is not None and current_user.id == searched_user.id
    should_not_show = not searched_is_self and await searched_user.is_restricted(session)
    if should_not_show:
        raise HTTPException(404, detail="User not found")

    canonical_user_resp = await UserModel.transform(
        searched_user,
        includes=User.USER_INCLUDES,
        ruleset=ruleset,
        show_nsfw_media=True,
    )

    if resolved_pp_variant == "pp_dev":
        fetcher = await get_fetcher()
        await apply_pp_variant_to_user_response(
            session=session,
            user_resp=canonical_user_resp,
            user_id=searched_user.id,
            mode=ruleset or searched_user.playmode,
            pp_variant=resolved_pp_variant,
            redis=redis,
            fetcher=fetcher,
            country_code=searched_user.country_code,
        )

    user_resp = UserModel.apply_nsfw_media_policy(copy.deepcopy(canonical_user_resp), show_nsfw_media)

    # å¼‚æ­¥ç¼”å­˜ canonical result
    background_task.add_task(cache_service.cache_user, canonical_user_resp, ruleset, None, resolved_pp_variant)
    # Pre-warm the pp_dev variant so toggling is instant.
    if resolved_pp_variant == "stable":
        background_task.add_task(prewarm_pp_dev_profile_background, redis, searched_user.id, ruleset)
    return user_resp


@router.get("/users/{user_id}/", include_in_schema=False)
@router.get(
    "/users/{user_id}",
    name="èŽ·å–ç”¨æˆ·ä¿¡æ¯",
    description="é€šè¿‡ç”¨æˆ· ID æˆ–ç”¨æˆ·åèŽ·å–å•ä¸ªç”¨æˆ·çš„è¯¦ç»†ä¿¡æ¯ã€‚",
    tags=["ç”¨æˆ·"],
    responses={
        200: api_doc("ç”¨æˆ·ä¿¡æ¯", UserModel, User.USER_INCLUDES),
    },
)
@asset_proxy_response
async def get_user_info(
    background_task: BackgroundTasks,
    session: Database,
    request: Request,
    user_id: Annotated[str, Path(description="ç”¨æˆ· ID æˆ–ç”¨æˆ·å")],
    pp_variant: Annotated[str | None, Query(description="pp variant: stable / pp_dev")] = None,
    current_user: User | None = Security(get_optional_user, scopes=["public"]),
):
    resolved_pp_variant = normalize_pp_variant(pp_variant)
    redis = get_redis()
    cache_service = get_user_cache_service(redis)
    show_nsfw_media = await viewer_allows_nsfw_media(current_user)

    # å¦‚æžœæ˜¯æ•°å­—IDï¼Œå…ˆå°è¯•ä»Žç¼“å­˜èŽ·å–ï¼ˆcache stores canonical payloadï¼‰
    if user_id.isdigit():
        user_id_int = int(user_id)
        cached_user = await cache_service.get_user_from_cache(user_id_int, None, resolved_pp_variant)
        if cached_user and "statistics" in cached_user:
            return UserModel.apply_nsfw_media_policy(copy.deepcopy(cached_user), show_nsfw_media)

    searched_user = (
        await session.exec(
            select(User).where(
                User.id == int(user_id) if user_id.isdigit() else User.username == user_id.removeprefix("@")
            )
        )
    ).first()
    if not searched_user or searched_user.id == BANCHOBOT_ID:
        raise HTTPException(404, detail="User not found")
    searched_is_self = current_user is not None and current_user.id == searched_user.id
    should_not_show = not searched_is_self and await searched_user.is_restricted(session)
    if should_not_show:
        raise HTTPException(404, detail="User not found")

    canonical_user_resp = await UserModel.transform(
        searched_user,
        includes=User.USER_INCLUDES,
        show_nsfw_media=True,
    )

    if resolved_pp_variant == "pp_dev":
        fetcher = await get_fetcher()
        await apply_pp_variant_to_user_response(
            session=session,
            user_resp=canonical_user_resp,
            user_id=searched_user.id,
            mode=searched_user.playmode,
            pp_variant=resolved_pp_variant,
            redis=redis,
            fetcher=fetcher,
            country_code=searched_user.country_code,
        )

    user_resp = UserModel.apply_nsfw_media_policy(copy.deepcopy(canonical_user_resp), show_nsfw_media)

    # å¼‚æ­¥ç¼“å­˜ canonical result
    background_task.add_task(cache_service.cache_user, canonical_user_resp, None, None, resolved_pp_variant)
    # Pre-warm the pp_dev variant so toggling is instant.
    if resolved_pp_variant == "stable":
        background_task.add_task(prewarm_pp_dev_profile_background, redis, searched_user.id, None)

    # Admin-only attachment: suspicious-status summary.
    #
    # Computed AFTER the cache write so the cached canonical payload
    # stays viewer-agnostic (one cached blob serves admins and non-
    # admins alike). Adding it here only mutates the per-request
    # user_resp copy, which is exactly the privacy posture we want:
    # the "is suspicious" flag and reasons are visible only to admins
    # browsing the profile, never to the public or to the user
    # themselves.
    if current_user is not None and getattr(current_user, "is_admin", False):
        from app.service.suspicious_summary import summarize_user
        summary = await summarize_user(session, searched_user.id)
        # Always attach -- even when not suspicious -- so the admin
        # client can render a "trust 100" green pill consistently
        # rather than treating absence of the field as ambiguous.
        user_resp["is_suspicious"] = summary.is_suspicious
        user_resp["trust_score"] = summary.trust_score
        user_resp["suspicious_reasons"] = summary.reasons
        user_resp["open_suspicious_alert_count"] = summary.open_alert_count

    return user_resp


beatmapset_includes = [*BeatmapsetModel.BEATMAPSET_TRANSFORMER_INCLUDES, "beatmaps"]


def _ensure_beatmapset_status(item: dict, beatmapset_type: BeatmapsetType) -> dict:
    if item.get("status"):
        return item

    beatmap_status = item.get("beatmap_status")
    if isinstance(beatmap_status, str) and beatmap_status:
        item["status"] = beatmap_status.lower()
        return item
    if isinstance(beatmap_status, int):
        try:
            item["status"] = BeatmapRankStatus(beatmap_status).name.lower()
            return item
        except ValueError:
            pass

    fallback = {
        BeatmapsetType.RANKED: "ranked",
        BeatmapsetType.PENDING: "pending",
        BeatmapsetType.LOVED: "loved",
        BeatmapsetType.GRAVEYARD: "graveyard",
    }
    item["status"] = fallback.get(beatmapset_type, "pending")
    return item


@router.get(
    "/users/{user_id}/beatmapsets/{type}",
    name="èŽ·å–ç”¨æˆ·è°±é¢é›†åˆ—è¡¨",
    description="èŽ·å–æŒ‡å®šç”¨æˆ·ç‰¹å®šç±»åž‹çš„è°±é¢é›†åˆ—è¡¨ï¼Œå¦‚æœ€å¸¸æ¸¸çŽ©ã€æ”¶è—ç­‰ã€‚",
    tags=["ç”¨æˆ·"],
    responses={
        200: api_doc(
            "å½“ç±»åž‹ä¸º `most_played` æ—¶è¿”å›ž `list[BeatmapPlaycountsModel]`ï¼Œå…¶ä»–ä¸º `list[BeatmapsetModel]`",
            list[BeatmapsetModel] | list[BeatmapPlaycountsModel],
            beatmapset_includes,
        )
    },
)
@asset_proxy_response
async def get_user_beatmapsets(
    session: Database,
    background_task: BackgroundTasks,
    cache_service: UserCacheService,
    user_id: Annotated[int, Path(description="ç”¨æˆ· ID")],
    type: Annotated[BeatmapsetType, Path(description="è°±é¢é›†ç±»åž‹")],
    current_user: User | None = Security(get_optional_user, scopes=["public"]),
    limit: Annotated[int, Query(ge=1, le=1000, description="è¿”å›žæ¡æ•° (1-1000)")] = 100,
    offset: Annotated[int, Query(ge=0, description="åç§»é‡")] = 0,
):
    # å…ˆå°è¯•ä»Žç¼“å­˜èŽ·å–
    cached_result = await cache_service.get_user_beatmapsets_from_cache(user_id, type.value, limit, offset)
    if cached_result is not None:
        return cached_result

    user = await session.get(User, user_id)
    if not user or user.id == BANCHOBOT_ID or not await visible_to_current_user(user, current_user, session):
        raise HTTPException(404, detail="User not found")

    if type in {
        BeatmapsetType.GRAVEYARD,
        BeatmapsetType.GUEST,
        BeatmapsetType.LOVED,
        BeatmapsetType.NOMINATED,
        BeatmapsetType.PENDING,
        BeatmapsetType.RANKED,
    }:
        status_filters: list[BeatmapRankStatus] = []
        if type == BeatmapsetType.GRAVEYARD:
            status_filters = [BeatmapRankStatus.GRAVEYARD]
        elif type == BeatmapsetType.LOVED:
            status_filters = [BeatmapRankStatus.LOVED]
        elif type == BeatmapsetType.PENDING:
            # Local submissions are usually WIP first, so include both.
            status_filters = [BeatmapRankStatus.WIP, BeatmapRankStatus.PENDING]
        elif type == BeatmapsetType.RANKED:
            if settings.enable_all_beatmap_leaderboard or settings.enable_all_beatmap_pp:
                status_filters = list(BeatmapRankStatus)
            else:
                status_filters = [
                    BeatmapRankStatus.RANKED,
                    BeatmapRankStatus.APPROVED,
                    BeatmapRankStatus.QUALIFIED,
                ]
        elif type in {BeatmapsetType.GUEST, BeatmapsetType.NOMINATED}:
            status_filters = []

        if status_filters:
            owner_filter = (
                (col(Beatmapset.is_local).is_(True) & (Beatmapset.user_id == user.id))
                | (
                    col(Beatmapset.is_local).is_not(True)
                    & (func.lower(col(Beatmapset.creator)) == user.username.lower())
                )
            )
            stmt = (
                select(Beatmapset)
                .where(
                    owner_filter,
                    col(Beatmapset.beatmap_status).in_(status_filters),
                )
                .order_by(col(Beatmapset.last_updated).desc(), col(Beatmapset.id).desc())
                .offset(offset)
                .limit(limit)
            )
            beatmapsets = (await session.exec(stmt)).all()
            resp = []
            for beatmapset in beatmapsets:
                transformed = await BeatmapsetModel.transform(
                    beatmapset,
                    session=session,
                    user=current_user,
                    includes=beatmapset_includes,
                )
                resp.append(_ensure_beatmapset_status(transformed, type))
        else:
            resp = []

    elif type == BeatmapsetType.FAVOURITE:
        if offset == 0:
            cursor = sys.maxsize
        else:
            cursor = (
                await session.exec(
                    select(FavouriteBeatmapset.id)
                    .where(FavouriteBeatmapset.user_id == user_id)
                    .order_by(col(FavouriteBeatmapset.id).desc())
                    .limit(1)
                    .offset(offset - 1)
                )
            ).first()
        if cursor is None:
            return []
        favourites = (
            await session.exec(
                select(FavouriteBeatmapset)
                .where(FavouriteBeatmapset.user_id == user_id, FavouriteBeatmapset.id < cursor)
                .order_by(col(FavouriteBeatmapset.id).desc())
                .limit(limit)
            )
        ).all()
        resp = [
            _ensure_beatmapset_status(
                await BeatmapsetModel.transform(
                    favourite.beatmapset, session=session, user=user, includes=beatmapset_includes
                ),
                type,
            )
            for favourite in favourites
        ]

    elif type == BeatmapsetType.MOST_PLAYED:
        if offset == 0:
            cursor = sys.maxsize, sys.maxsize
        else:
            cursor = (
                await session.exec(
                    select(BeatmapPlaycounts.playcount, BeatmapPlaycounts.id)
                    .where(BeatmapPlaycounts.user_id == user_id)
                    .order_by(col(BeatmapPlaycounts.playcount).desc(), col(BeatmapPlaycounts.id).desc())
                    .limit(1)
                    .offset(offset - 1)
                )
            ).first()
        if cursor is None:
            return []
        cursor_pc, cursor_id = cursor
        most_played = await session.exec(
            select(BeatmapPlaycounts)
            .where(
                BeatmapPlaycounts.user_id == user_id,
                tuple_(BeatmapPlaycounts.playcount, BeatmapPlaycounts.id) < tuple_(cursor_pc, cursor_id),
            )
            .order_by(col(BeatmapPlaycounts.playcount).desc(), col(BeatmapPlaycounts.id).desc())
            .limit(limit)
        )
        resp = [
            await BeatmapPlaycountsModel.transform(most_played_beatmap, user=user, includes=beatmapset_includes)
            for most_played_beatmap in most_played
        ]
    else:
        raise HTTPException(400, detail="Invalid beatmapset type")

    # å¼‚æ­¥ç¼“å­˜ç»“æžœ
    async def cache_beatmapsets():
        try:
            await cache_service.cache_user_beatmapsets(user_id, type.value, resp, limit, offset)
        except Exception as e:
            log("Beatmapset").error(f"Error caching user beatmapsets for user {user_id}, type {type.value}: {e}")

    background_task.add_task(cache_beatmapsets)

    return resp


async def _warm_pp_dev_scores_background(
    scores: list,
    pp_variant: str,
    user_id: int,
    gamemode: object,
) -> None:
    """Background task: recalculate and cache pp_dev values for scores not yet in Redis."""
    if not scores:
        return
    try:
        from app.dependencies.database import with_db
        from app.dependencies.fetcher import get_fetcher
        from app.service.pp_variant_service import get_score_pp_variant_batch

        redis = get_redis()
        fetcher = await get_fetcher()
        async with with_db() as session:
            # Only warm the top 15 scores to avoid flooding beatmap-raw mirrors with
            # simultaneous requests (which causes HTTP 429 rate-limiting).
            top_scores = scores[:15]
            await get_score_pp_variant_batch(
                session=session,
                scores=top_scores,
                pp_variant=pp_variant,
                redis=redis,
                fetcher=fetcher,
                recalc_top_n=len(top_scores),
            )
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug(
            f"Background pp_dev warm-up failed for user {user_id} mode {gamemode}: {e}"
        )


@router.get(
    "/users/{user_id}/scores/{type}",
    name="èŽ·å–ç”¨æˆ·æˆç»©åˆ—è¡¨",
    description=(
        "èŽ·å–ç”¨æˆ·ç‰¹å®šç±»åž‹çš„æˆç»©åˆ—è¡¨ï¼Œå¦‚æœ€å¥½æˆç»©ã€æœ€è¿‘æˆç»©ç­‰ã€‚\n\n"
        "å¦‚æžœ `x-api-version >= 20220705`ï¼Œè¿”å›žå€¼ä¸º `ScoreResp`åˆ—è¡¨ï¼Œ"
        "å¦åˆ™ä¸º `LegacyScoreResp`åˆ—è¡¨ã€‚"
    ),
    tags=["ç”¨æˆ·"],
)
@asset_proxy_response
async def get_user_scores(
    session: Database,
    api_version: APIVersion,
    background_task: BackgroundTasks,
    user_id: Annotated[int, Path(description="ç”¨æˆ· ID")],
    type: Annotated[
        Literal["best", "recent", "firsts", "pinned"],
        Path(description=("æˆç»©ç±»åž‹: best æœ€å¥½æˆç»© / recent æœ€è¿‘ 24h æ¸¸çŽ©æˆç»© / firsts ç¬¬ä¸€åæˆç»© / pinned ç½®é¡¶æˆç»©")),
    ],
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    legacy_only: Annotated[bool, Query(description="æ˜¯å¦åªæŸ¥è¯¢ Stable æˆç»©")] = False,
    include_fails: Annotated[bool, Query(description="æ˜¯å¦åŒ…å«å¤±è´¥çš„æˆç»©")] = False,
    mode: Annotated[GameMode | None, Query(description="æŒ‡å®š ruleset (å¯é€‰ï¼Œé»˜è®¤ä¸ºç”¨æˆ·ä¸»æ¨¡å¼)")] = None,
    limit: Annotated[int, Query(ge=1, le=1000, description="è¿”å›žæ¡æ•° (1-1000)")] = 100,
    offset: Annotated[int, Query(ge=0, description="åç§»é‡")] = 0,
    pp_variant: Annotated[str | None, Query(description="pp variant: stable / pp_dev")] = None,
):
    resolved_pp_variant = normalize_pp_variant(pp_variant)
    use_pp_dev_variant = resolved_pp_variant == "pp_dev"

    is_legacy_api = api_version < 20220705
    show_nsfw_media = await viewer_allows_nsfw_media(current_user)
    add_weight = type == "best" and not is_legacy_api
    redis = get_redis()
    cache_service = get_user_cache_service(redis)

    # Keep "recent" fresh, but let best/pinned/firsts lean harder on Redis.
    # Those views are explicitly invalidated on score submit / pin changes, so a
    # longer TTL lowers DB pressure without changing the eventual result.
    cache_expire = 20 if type == "recent" else max(settings.user_scores_cache_expire_seconds, 300)
    cached_scores = await cache_service.get_user_scores_from_cache(
        user_id,
        type,
        include_fails,
        mode,
        limit,
        offset,
        is_legacy_api,
        resolved_pp_variant,
    )
    if cached_scores is not None:
        return cached_scores

    db_user = await session.get(User, user_id)
    if db_user is None or not await visible_to_current_user(db_user, current_user, session):
        raise HTTPException(404, detail="User not found")

    gamemode = _normalize_user_mode(mode) or db_user.playmode
    where_clause = (col(Score.user_id) == db_user.id) & (col(Score.gamemode) == gamemode)
    includes = Score.USER_PROFILE_INCLUDES.copy()
    eager_score_relations = (
        joinedload(Score.user),
        joinedload(Score.beatmap).joinedload(Beatmap.beatmapset),
    )
    if not include_fails:
        where_clause &= col(Score.passed).is_(True)

    scores: list[Score] = []
    pp_dev_rank_by_score_id: dict[int, int] | None = None
    pp_dev_by_score_id: dict[int, float] = {}

    if type == "pinned":
        where_clause &= Score.pinned_order > 0
        if offset == 0:
            cursor = 0, sys.maxsize
        else:
            cursor = (
                await session.exec(
                    select(Score.pinned_order, Score.id)
                    .where(where_clause)
                    .order_by(col(Score.pinned_order).asc(), col(Score.id).desc())
                    .limit(1)
                    .offset(offset - 1)
                )
            ).first()
        if cursor:
            cursor_pinned, cursor_id = cursor
            where_clause &= (col(Score.pinned_order) > cursor_pinned) | (
                (col(Score.pinned_order) == cursor_pinned) & (col(Score.id) < cursor_id)
            )
            scores = (
                await session.exec(
                    select(Score)
                    .options(*eager_score_relations)
                    .where(where_clause)
                    .order_by(col(Score.pinned_order).asc(), col(Score.id).desc())
                    .limit(limit)
                )
            ).all()

    elif type == "best":
        if use_pp_dev_variant:
            fetcher = await get_fetcher()
            # Serve from cache only — no inline recalculation (avoids 30s+ client timeouts).
            # Background task warms any uncached scores so the next request is fast.
            pp_dev_recalc_limit = 0
            # Mirror pp-dev ranking from canonical best-score rows to keep variant queries responsive.
            candidate_scores = (
                await session.exec(
                    select(Score)
                    .options(*eager_score_relations)
                    .where(
                        col(Score.user_id) == db_user.id,
                        col(Score.gamemode) == gamemode,
                        col(Score.passed).is_(True),
                        col(Score.ranked).is_(True),
                        exists().where(col(BestScore.score_id) == Score.id),
                    )
                )
            ).all()

            best_by_beatmap: dict[int, tuple[Score, float]] = {}
            ordered_candidates = sorted(candidate_scores, key=lambda s: (float(s.pp or 0.0), s.id), reverse=True)
            pp_by_candidate_id = await get_score_pp_variant_batch(
                session=session,
                scores=ordered_candidates,
                pp_variant=resolved_pp_variant,
                redis=redis,
                fetcher=fetcher,
                recalc_top_n=pp_dev_recalc_limit,
            )

            # Warm up all best scores in the background so the next request serves from cache.
            # The warm function is idempotent — already-cached scores are skipped quickly.
            background_task.add_task(
                _warm_pp_dev_scores_background,
                list(ordered_candidates),
                resolved_pp_variant,
                db_user.id,
                gamemode,
            )

            for candidate in ordered_candidates:
                pp_value = float(pp_by_candidate_id.get(candidate.id, float(candidate.pp or 0.0)))
                if pp_value <= 0:
                    continue
                previous = best_by_beatmap.get(candidate.beatmap_id)
                if previous is None or pp_value > previous[1] or (pp_value == previous[1] and candidate.id > previous[0].id):
                    best_by_beatmap[candidate.beatmap_id] = (candidate, pp_value)

            ranked_best = sorted(best_by_beatmap.values(), key=lambda item: (item[1], item[0].id), reverse=True)
            pp_dev_rank_by_score_id = {score.id: index + 1 for index, (score, _pp) in enumerate(ranked_best)}
            for score, pp_value in ranked_best:
                pp_dev_by_score_id[score.id] = float(pp_value)

            paged_best = ranked_best[offset : offset + limit]
            scores = [score for score, _pp in paged_best]
        else:
            where_clause &= exists().where(col(BestScore.score_id) == Score.id)

            if offset == 0:
                cursor = sys.maxsize, sys.maxsize
            else:
                cursor = (
                    await session.exec(
                        select(Score.pp, Score.id)
                        .where(where_clause)
                        .order_by(col(Score.pp).desc(), col(Score.id).desc())
                        .limit(1)
                        .offset(offset - 1)
                    )
                ).first()
            if cursor:
                cursor_pp, cursor_id = cursor
                where_clause &= tuple_(col(Score.pp), col(Score.id)) < tuple_(cursor_pp, cursor_id)
                scores = (
                    await session.exec(
                        select(Score)
                        .options(*eager_score_relations)
                        .where(where_clause)
                        .order_by(col(Score.pp).desc(), col(Score.id).desc())
                        .limit(limit)
                    )
                ).all()

    elif type == "recent":
        where_clause &= Score.ended_at > utcnow() - timedelta(hours=24)
        if offset == 0:
            cursor = datetime.max, sys.maxsize
        else:
            cursor = (
                await session.exec(
                    select(Score.ended_at, Score.id)
                    .where(where_clause)
                    .order_by(col(Score.ended_at).desc(), col(Score.id).desc())
                    .limit(1)
                    .offset(offset - 1)
                )
            ).first()
        if cursor:
            cursor_date, cursor_id = cursor
            where_clause &= tuple_(col(Score.ended_at), col(Score.id)) < tuple_(cursor_date, cursor_id)
            scores = (
                await session.exec(
                    select(Score)
                    .options(*eager_score_relations)
                    .where(where_clause)
                    .order_by(col(Score.ended_at).desc(), col(Score.id).desc())
                    .limit(limit)
                )
            ).all()

    elif type == "firsts":
        best_scores = await get_user_first_scores(session, db_user.id, gamemode, limit, offset)
        scores = [best_score.score for best_score in best_scores]

    score_responses = [
        await score.to_resp(
            session,
            api_version,
            includes=includes,
            show_nsfw_media=show_nsfw_media,
        )
        for score in scores
    ]

    if use_pp_dev_variant and scores:
        fetcher = await get_fetcher()
        if not pp_dev_by_score_id:
            for score in scores:
                pp_dev_by_score_id[score.id] = await get_score_pp_variant(
                    session=session,
                    score=score,
                    pp_variant=resolved_pp_variant,
                    redis=redis,
                    fetcher=fetcher,
                )

        apply_pp_variant_to_score_responses(
            scores=scores,
            score_responses=score_responses,
            pp_by_score_id=pp_dev_by_score_id,
            add_weight=add_weight,
            rank_by_score_id=pp_dev_rank_by_score_id,
        )
    elif add_weight and scores:
        # Avoid N per-score window-function queries in Score.weight while keeping
        # identical ranking semantics (including pp ties) as get_best_id().
        score_ids = [score.id for score in scores]
        rownum = (
            func.row_number()
            .over(partition_by=(col(BestScore.user_id), col(BestScore.gamemode)), order_by=col(BestScore.pp).desc())
            .label("rn")
        )
        ranked_subq = (
            select(BestScore.score_id.label("score_id"), rownum)
            .where(BestScore.user_id == db_user.id, BestScore.gamemode == gamemode)
            .subquery()
        )
        ranked_best_scores = (
            await session.exec(
                select(ranked_subq.c.score_id, ranked_subq.c.rn).where(ranked_subq.c.score_id.in_(score_ids))
            )
        ).all()
        rank_by_score_id = {score_id: rank for score_id, rank in ranked_best_scores}

        for score, score_resp in zip(scores, score_responses):
            if not isinstance(score_resp, dict):
                continue
            rank = rank_by_score_id.get(score.id)
            if rank is not None:
                score_resp["weight"] = calculate_pp_weight(rank - 1)

    # å¼‚æ­¥ç¼“å­˜ç»“æžœ
    background_task.add_task(
        cache_service.cache_user_scores,
        user_id,
        type,
        score_responses,  # pyright: ignore[reportArgumentType]
        include_fails,
        mode,
        limit,
        offset,
        cache_expire,
        is_legacy_api,
        resolved_pp_variant,
    )

    return score_responses



