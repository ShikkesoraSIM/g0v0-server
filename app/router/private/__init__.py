from app.config import settings

from . import (  # noqa: F401
    admin,
    admin_donations,
    anticheat,
    audio_proxy,
    avatar,
    beatmapset,
    cover,
    discord_redeem,
    donations,
    mod_alerts,
    oauth,
    ordr_renders,
    password,
    relationship,
    score,
    team,
    user,
)
from .router import router as private_router

if settings.enable_totp_verification:
    from . import totp  # noqa: F401

__all__ = [
    "private_router",
]
