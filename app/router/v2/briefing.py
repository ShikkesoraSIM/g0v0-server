from typing import Annotated

from app.database.user import User
from app.dependencies.database import Database
from app.dependencies.user import get_client_user
from app.models.score import GameMode
from app.service.briefing_radar_service import BriefingRadarResponse, get_briefing_radar
from app.service.pp_variant_service import normalize_pp_variant

from .router import router

from fastapi import Depends, Query


@router.get(
    "/torii/briefing/radar",
    tags=["Torii"],
    response_model=BriefingRadarResponse,
    name="Get Torii briefing dojo radar",
    description="Returns per-user leaderboard movement since the user's last Torii briefing snapshot.",
)
async def router_get_torii_briefing_radar(
    session: Database,
    current_user: Annotated[User, Depends(get_client_user)],
    mode: Annotated[GameMode, Query()] = GameMode.OSU,
    pp_variant: Annotated[str | None, Query()] = None,
    track_top: Annotated[int, Query(ge=1, le=50)] = 5,
    max_events: Annotated[int, Query(ge=1, le=20)] = 8,
    candidate_limit: Annotated[int, Query(ge=25, le=500)] = 200,
):
    return await get_briefing_radar(
        session=session,
        user=current_user,
        mode=mode,
        variant=normalize_pp_variant(pp_variant),
        track_top=track_top,
        max_events=max_events,
        candidate_limit=candidate_limit,
    )
