from typing import Annotated

from app.database import FavouriteBeatmapset, User
from app.database.user import UserModel
from app.dependencies.database import Database, get_redis
from app.dependencies.fetcher import get_fetcher
from app.dependencies.user import UserAndToken, get_current_user, get_current_user_and_token
from app.models.score import GameMode
from app.models.torii_auras import (
    AURA_SENTINEL_DEFAULT,
    AURA_SENTINEL_NONE,
    aura_to_api_dict,
    available_auras_for_user,
    is_aura_id_known,
    is_aura_allowed_for_user,
    resolve_effective_aura_id,
)
from app.service.pp_variant_service import apply_pp_variant_to_user_response, normalize_pp_variant
from app.service.user_update_publisher import publish_user_updated
from app.utils import api_doc

from .router import router

from fastapi import HTTPException, Path, Query, Security
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


# ---------------------------------------------------------------------------
# Aura cosmetics — pick which particle effect renders behind the user's name
# everywhere it appears (chat, profile, leaderboards, ...).
#
# `aura-catalog`  : list the auras the current user is entitled to + their
#                   currently-stored pick (the raw value, including sentinels)
#                   plus the resolved aura id everyone else sees.
# `equipped-aura` : update the stored pick. Server validates ownership
#                   before persisting; the client never has to be the source
#                   of truth on which auras a user can equip.
#
# IMPORTANT: these MUST be declared before `/me/{ruleset}` below — FastAPI
# matches in registration order and the wildcard would otherwise swallow
# `/me/aura-catalog` and try to parse "aura-catalog" as a GameMode.
# ---------------------------------------------------------------------------


class AuraCatalogEntry(BaseModel):
    id: str
    display_name: str
    description: str
    owning_groups: list[str]


class AuraCatalogResponse(BaseModel):
    # Stable sentinel constants surfaced so clients don't have to hardcode
    # the strings — they can read these and pass them straight back to the
    # PATCH endpoint when the user picks "Default" or "None".
    sentinel_default: str = AURA_SENTINEL_DEFAULT
    sentinel_none: str = AURA_SENTINEL_NONE

    # All auras this user has the right to equip, ordered for display.
    available: list[AuraCatalogEntry]

    # The raw stored value (incl. sentinels). null when never picked.
    current_setting: str | None

    # Resolved aura id — what other users see on this user's name right
    # now. Mirrors what the standard APIUser.equipped_aura field carries.
    effective_aura_id: str | None


class UpdateEquippedAuraBody(BaseModel):
    # Accepts: null / "default" / "none" / any concrete aura id from the
    # catalog. Anything else, or an id the user doesn't own, gets a 4xx.
    aura_id: str | None = None


@router.get(
    "/me/aura-catalog",
    response_model=AuraCatalogResponse,
    name="List equipable auras for the current user",
    description=(
        "Returns the set of aura cosmetics the current user is entitled to equip "
        "(based on their groups), plus their current pick and the resolved id "
        "that other clients see. Used by the settings picker in the lazer client "
        "and the web frontend."
    ),
    tags=["user", "auras"],
)
async def get_aura_catalog(
    current_user: Annotated[User, Security(get_current_user, scopes=["identify"])],
):
    available = available_auras_for_user(current_user)
    return AuraCatalogResponse(
        available=[AuraCatalogEntry(**aura_to_api_dict(a)) for a in available],
        current_setting=current_user.equipped_aura,
        effective_aura_id=resolve_effective_aura_id(current_user, current_user.equipped_aura),
    )


@router.patch(
    "/me/equipped-aura",
    response_model=AuraCatalogResponse,
    name="Update equipped aura",
    description=(
        "Set the current user's equipped aura. Body: `{aura_id: <string|null>}`. "
        "Accepts a sentinel ('default' / 'none'), null (treated as 'default'), "
        "or any aura id from the catalog the user is entitled to. Returns the "
        "updated catalog so the client can update its UI in one round-trip."
    ),
    tags=["user", "auras"],
)
async def update_equipped_aura(
    session: Database,
    body: UpdateEquippedAuraBody,
    current_user: Annotated[User, Security(get_current_user, scopes=["identify"])],
):
    new_value = body.aura_id

    # Reject unknown values up front so a typo never ends up persisted.
    if not is_aura_id_known(new_value):
        raise HTTPException(
            status_code=400,
            detail=f"Unknown aura id: {new_value!r}. Use one of the catalog ids, "
            f"'{AURA_SENTINEL_DEFAULT}', '{AURA_SENTINEL_NONE}', or null.",
        )

    # Real aura ids must belong to a group the user holds.
    if not is_aura_allowed_for_user(current_user, new_value):
        raise HTTPException(
            status_code=403,
            detail=f"You don't have access to aura {new_value!r}.",
        )

    # Sentinels stored verbatim so a future read can distinguish "explicit
    # opt-out" from "never picked" if behaviour ever needs to differ.
    current_user.equipped_aura = new_value
    session.add(current_user)
    await session.commit()
    await session.refresh(current_user)

    # Fan out a UserUpdated notification so every connected lazer client
    # currently rendering this user (chat lines, dashboard online list,
    # leaderboard rows, the user's own profile, ...) refetches the public
    # payload and re-resolves the new aura without anyone having to close
    # / reopen anything. Best-effort: a publish failure does not roll back
    # the DB write.
    await publish_user_updated(current_user.id)

    available = available_auras_for_user(current_user)
    return AuraCatalogResponse(
        available=[AuraCatalogEntry(**aura_to_api_dict(a)) for a in available],
        current_setting=current_user.equipped_aura,
        effective_aura_id=resolve_effective_aura_id(current_user, current_user.equipped_aura),
    )


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
