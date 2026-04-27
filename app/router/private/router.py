from app.dependencies.rate_limit import LIMITERS

from fastapi import APIRouter

router = APIRouter(prefix="/api/private", dependencies=LIMITERS)

# 导入并包含子路由
from .audio_proxy import router as audio_proxy_router
from . import client_version_webhook  # noqa: F401 — registers routes on import

router.include_router(audio_proxy_router)
