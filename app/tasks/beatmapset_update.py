from datetime import datetime, timedelta

from app.config import settings
from app.dependencies.scheduler import get_scheduler
from app.service.beatmapset_update_service import get_beatmapset_update_service
from app.utils import bg_tasks

if settings.enable_auto_beatmap_sync:

    @get_scheduler().scheduled_job(
        "interval",
        id="update_beatmaps",
        minutes=settings.beatmap_sync_interval_minutes,
        # Delay first run by 30 minutes after startup — the cache warmup
        # already fires on startup and consumes most of the rate budget.
        next_run_time=datetime.now() + timedelta(minutes=30),
    )
    async def beatmapset_update_job():
        service = get_beatmapset_update_service()
        bg_tasks.add_task(service.add_missing_beatmapsets)
        await service._update_beatmaps()
