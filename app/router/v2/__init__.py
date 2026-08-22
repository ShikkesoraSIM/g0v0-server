from . import (  # noqa: F401
    beatmap,
    beatmapset,
    briefing,
    changelog,
    matchmaking,
    me,
    misc,
    ranking,
    relationship,
    room,
    score,
    session_verify,
    tags,
    torii_comfort_pick,
    torii_gifts,
    torii_hiccup_reports,
    torii_mapperatorinator,
    torii_points,
    torii_replay_render,
    torii_score_note,
    torii_restriction,
    torii_server_pulse,
    torii_store,
    user,
)
from .router import router as api_v2_router

__all__ = [
    "api_v2_router",
]
