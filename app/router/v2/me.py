from typing import Annotated

from app.database import FavouriteBeatmapset, User
from app.database.user import UserModel
from app.dependencies.database import Database, get_redis
from app.dependencies.fetcher import get_fetcher
from app.dependencies.user import UserAndToken, get_current_user, get_current_user_and_token
from app.models.score import GameMode
from app.service.pp_variant_service import apply_pp_variant_to_user_response, normalize_pp_variant
from app.utils import api_doc

from .router import router

from fastapi import Path, Query, Security
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlmodel import select

ME_INCLUDES = [*User.USER_INCLUDES, "session_verified", "session_verification_method", "user_preferences"]


async def _viewer_allows_nsfw_media(user: User) -> bool:
    await user.awaitable_attrs.user_preference
    return bool(user.user_preference and user.user_preference.profile_media_show_nsfw)


class BeatmapsetIds(BaseModel):
    beatmapset_ids: list[int]


@router.get(
    "/me/beatmapset-favourites",
    response_model=BeatmapsetIds,
    name="èŽ·å–å½“å‰ç”¨æˆ·æ”¶è—çš„è°±é¢é›† ID åˆ—è¡¨",
    description="èŽ·å–å½“å‰ç™»å½•ç”¨æˆ·æ”¶è—çš„è°±é¢é›† ID åˆ—è¡¨ã€‚",
    tags=["ç”¨æˆ·", "è°±é¢é›†"],
)
async def get_user_beatmapset_favourites(
    session: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["identify"])],
):
    beatmapset_ids = await session.exec(
        select(FavouriteBeatmapset.beatmapset_id).where(FavouriteBeatmapset.user_id == current_user.id)
    )
    return BeatmapsetIds(beatmapset_ids=list(beatmapset_ids.all()))


@router.get(
    "/me/{ruleset}",
    responses={200: api_doc("å½“å‰ç”¨æˆ·ä¿¡æ¯ï¼ˆå«æŒ‡å®š ruleset ç»Ÿè®¡ï¼‰", UserModel, ME_INCLUDES)},
    name="èŽ·å–å½“å‰ç”¨æˆ·ä¿¡æ¯ (æŒ‡å®š ruleset)",
    description="èŽ·å–å½“å‰ç™»å½•ç”¨æˆ·ä¿¡æ¯ ï¼ˆå«æŒ‡å®š ruleset ç»Ÿè®¡ï¼‰ã€‚",
    tags=["ç”¨æˆ·"],
)
async def get_user_info_with_ruleset(
    session: Database,
    ruleset: Annotated[GameMode, Path(description="æŒ‡å®š ruleset")],
    user_and_token: Annotated[UserAndToken, Security(get_current_user_and_token, scopes=["identify"])],
    pp_variant: Annotated[str | None, Query(description="pp variant: stable / pp_dev")] = None,
):
    resolved_pp_variant = normalize_pp_variant(pp_variant)
    redis = get_redis()
    show_nsfw_media = await _viewer_allows_nsfw_media(user_and_token[0])
    user_resp = await UserModel.transform(
        user_and_token[0],
        ruleset=ruleset,
        token_id=user_and_token[1].id,
        includes=ME_INCLUDES,
        show_nsfw_media=True,
    )

    if resolved_pp_variant == "pp_dev":
        fetcher = await get_fetcher()
        await apply_pp_variant_to_user_response(
            session=session,
            user_resp=user_resp,
            user_id=user_and_token[0].id,
            mode=ruleset,
            pp_variant=resolved_pp_variant,
            redis=redis,
            fetcher=fetcher,
            country_code=user_and_token[0].country_code,
        )

    user_resp = UserModel.apply_nsfw_media_policy(user_resp, show_nsfw_media)
    return user_resp


@router.get(
    "/me/",
    responses={200: api_doc("å½“å‰ç”¨æˆ·ä¿¡æ¯", UserModel, ME_INCLUDES)},
    name="èŽ·å–å½“å‰ç”¨æˆ·ä¿¡æ¯",
    description="èŽ·å–å½“å‰ç™»å½•ç”¨æˆ·ä¿¡æ¯ã€‚",
    tags=["ç”¨æˆ·"],
)
async def get_user_info_default(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_current_user_and_token, scopes=["identify"])],
    pp_variant: Annotated[str | None, Query(description="pp variant: stable / pp_dev")] = None,
):
    resolved_pp_variant = normalize_pp_variant(pp_variant)
    redis = get_redis()
    show_nsfw_media = await _viewer_allows_nsfw_media(user_and_token[0])
    user_resp = await UserModel.transform(
        user_and_token[0],
        ruleset=None,
        token_id=user_and_token[1].id,
        includes=ME_INCLUDES,
        show_nsfw_media=True,
    )

    if resolved_pp_variant == "pp_dev":
        fetcher = await get_fetcher()
        await apply_pp_variant_to_user_response(
            session=session,
            user_resp=user_resp,
            user_id=user_and_token[0].id,
            mode=user_and_token[0].playmode,
            pp_variant=resolved_pp_variant,
            redis=redis,
            fetcher=fetcher,
            country_code=user_and_token[0].country_code,
        )

    user_resp = UserModel.apply_nsfw_media_policy(user_resp, show_nsfw_media)
    return user_resp


@router.put("/users/{user_id}/page", include_in_schema=False)
async def update_userpage():
    return RedirectResponse(url="/api/private/user/page", status_code=307)


@router.post("/me/validate-bbcode", include_in_schema=False)
async def validate_bbcode():
    return RedirectResponse(url="/api/private/user/validate-bbcode", status_code=307)
