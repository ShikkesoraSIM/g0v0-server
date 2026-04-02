"""
ç”¨æˆ·ç¼“å­˜æœåŠ¡
ç”¨äºŽç¼“å­˜ç”¨æˆ·ä¿¡æ¯ï¼Œæä¾›çƒ­ç¼“å­˜å’Œå®žæ—¶åˆ·æ–°åŠŸèƒ½
"""

import json
from typing import TYPE_CHECKING, Any

from app.config import settings
from app.const import BANCHOBOT_ID
from app.database import User
from app.database.score import LegacyScoreResp
from app.database.user import UserDict, UserModel
from app.dependencies.database import with_db
from app.helpers.asset_proxy_helper import replace_asset_urls
from app.log import logger
from app.models.beatmap import BeatmapRankStatus
from app.models.score import GameMode
from app.service.pp_variant_service import (
    apply_pp_variant_to_user_response,
    get_user_pp_variant_statistics,
    invalidate_pp_variant_caches_for_user,
    USER_PP_DEV_PROFILE_CACHE_TTL_SECONDS,
)
from app.utils import safe_json_dumps

from redis.asyncio import Redis
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

if TYPE_CHECKING:
    from app.fetcher import Fetcher


class UserCacheService:
    """ç”¨æˆ·ç¼“å­˜æœåŠ¡"""

    def __init__(self, redis: Redis):
        self.redis = redis
        self._refreshing = False
        self._background_tasks: set = set()

    def _get_v1_user_cache_key(self, user_id: int, ruleset: GameMode | None = None) -> str:
        """ç”Ÿæˆ V1 ç”¨æˆ·ç¼“å­˜é”®"""
        if ruleset is not None:
            return f"v1_user:{user_id}:ruleset:{ruleset}"
        return f"v1_user:{user_id}"

    async def get_v1_user_from_cache(self, user_id: int, ruleset: GameMode | None = None) -> dict | None:
        """ä»Žç¼“å­˜èŽ·å– V1 ç”¨æˆ·ä¿¡æ¯"""
        try:
            cache_key = self._get_v1_user_cache_key(user_id, ruleset)
            cached_data = await self.redis.get(cache_key)
            if cached_data:
                logger.debug(f"V1 User cache hit for user {user_id}")
                return json.loads(cached_data)
            return None
        except Exception as e:
            logger.error(f"Error getting V1 user from cache: {e}")
            return None

    async def cache_v1_user(
        self,
        user_data: dict,
        user_id: int,
        ruleset: GameMode | None = None,
        expire_seconds: int | None = None,
    ):
        """ç¼“å­˜ V1 ç”¨æˆ·ä¿¡æ¯"""
        try:
            if expire_seconds is None:
                expire_seconds = settings.user_cache_expire_seconds
            cache_key = self._get_v1_user_cache_key(user_id, ruleset)
            cached_data = safe_json_dumps(user_data)
            await self.redis.setex(cache_key, expire_seconds, cached_data)
            logger.debug(f"Cached V1 user {user_id} for {expire_seconds}s")
        except Exception as e:
            logger.error(f"Error caching V1 user: {e}")

    async def invalidate_v1_user_cache(self, user_id: int):
        """ä½¿ V1 ç”¨æˆ·ç¼“å­˜å¤±æ•ˆ"""
        try:
            # åˆ é™¤ V1 ç”¨æˆ·ä¿¡æ¯ç¼“å­˜
            pattern = f"v1_user:{user_id}*"
            keys = await self.redis.keys(pattern)
            if keys:
                await self.redis.delete(*keys)
                logger.info(f"Invalidated {len(keys)} V1 cache entries for user {user_id}")
        except Exception as e:
            logger.error(f"Error invalidating V1 user cache: {e}")

    def _get_user_cache_key(
        self,
        user_id: int,
        ruleset: GameMode | None = None,
        pp_variant: str = "stable",
    ) -> str:
        """ç”Ÿæˆç”¨æˆ·ç¼“å­˜é”®"""
        pp_variant_part = "" if pp_variant == "stable" else f":pp_variant:{pp_variant}"
        if ruleset is not None:
            return f"user:{user_id}:ruleset:{ruleset}{pp_variant_part}"
        return f"user:{user_id}{pp_variant_part}"

    def _get_user_scores_cache_key(
        self,
        user_id: int,
        score_type: str,
        include_fail: bool,
        mode: GameMode | None = None,
        limit: int = 100,
        offset: int = 0,
        is_legacy: bool = False,
        pp_variant: str = "stable",
    ) -> str:
        """ç”Ÿæˆç”¨æˆ·æˆç»©ç¼“å­˜é”®"""
        mode_part = f":{mode}" if mode is not None else ""
        pp_variant_part = "" if pp_variant == "stable" else f":pp_variant:{pp_variant}"
        return (
            f"user:{user_id}:scores:{score_type}{mode_part}:limit:{limit}:offset:"
            f"{offset}:include_fail:{include_fail}:is_legacy:{is_legacy}{pp_variant_part}"
        )

    def _get_user_beatmapsets_cache_key(
        self, user_id: int, beatmapset_type: str, limit: int = 100, offset: int = 0
    ) -> str:
        """ç”Ÿæˆç”¨æˆ·è°±é¢é›†ç¼“å­˜é”®"""
        return f"user:{user_id}:beatmapsets:{beatmapset_type}:limit:{limit}:offset:{offset}"

    @staticmethod
    def _normalize_cached_beatmapset_status(beatmapsets: list[Any], beatmapset_type: str) -> list[Any]:
        fallback_by_type = {
            "ranked": "ranked",
            "pending": "pending",
            "loved": "loved",
            "graveyard": "graveyard",
        }
        fallback_status = fallback_by_type.get(beatmapset_type)

        for row in beatmapsets:
            if not isinstance(row, dict):
                continue
            if row.get("status"):
                continue

            beatmap_status = row.get("beatmap_status")
            normalized_status = None
            if isinstance(beatmap_status, str) and beatmap_status:
                normalized_status = beatmap_status.lower()
            elif isinstance(beatmap_status, int):
                try:
                    normalized_status = BeatmapRankStatus(beatmap_status).name.lower()
                except ValueError:
                    normalized_status = None

            row["status"] = normalized_status or fallback_status or "pending"

        return beatmapsets

    async def get_user_from_cache(
        self,
        user_id: int,
        ruleset: GameMode | None = None,
        pp_variant: str = "stable",
    ) -> UserDict | None:
        """ä»Žç¼“å­˜èŽ·å–ç”¨æˆ·ä¿¡æ¯"""
        try:
            cache_key = self._get_user_cache_key(user_id, ruleset, pp_variant)
            cached_data = await self.redis.get(cache_key)
            if cached_data:
                logger.debug(f"User cache hit for user {user_id}")
                data = json.loads(cached_data)
                return data
            return None
        except Exception as e:
            logger.error(f"Error getting user from cache: {e}")
            return None

    async def cache_user(
        self,
        user_resp: UserDict,
        ruleset: GameMode | None = None,
        expire_seconds: int | None = None,
        pp_variant: str = "stable",
    ):
        """ç¼“å­˜ç”¨æˆ·ä¿¡æ¯"""
        try:
            if expire_seconds is None:
                # pp_dev profiles are more expensive to rebuild -- keep them hot longer.
                expire_seconds = (
                    USER_PP_DEV_PROFILE_CACHE_TTL_SECONDS
                    if pp_variant != "stable"
                    else settings.user_cache_expire_seconds
                )
            cache_key = self._get_user_cache_key(user_resp["id"], ruleset, pp_variant)
            cached_data = safe_json_dumps(user_resp)
            await self.redis.setex(cache_key, expire_seconds, cached_data)
            logger.debug(f"Cached user {user_resp['id']} for {expire_seconds}s")
        except Exception as e:
            logger.error(f"Error caching user: {e}")

    async def get_user_scores_from_cache(
        self,
        user_id: int,
        score_type: str,
        include_fail: bool,
        mode: GameMode | None = None,
        limit: int = 100,
        offset: int = 0,
        is_legacy: bool = False,
        pp_variant: str = "stable",
    ) -> list[UserDict] | list[LegacyScoreResp] | None:
        """ä»Žç¼“å­˜èŽ·å–ç”¨æˆ·æˆç»©"""
        try:
            cache_key = self._get_user_scores_cache_key(
                user_id, score_type, include_fail, mode, limit, offset, is_legacy, pp_variant
            )
            cached_data = await self.redis.get(cache_key)
            if cached_data:
                logger.debug(f"User scores cache hit for user {user_id}, type {score_type}")
                data = json.loads(cached_data)
                return [LegacyScoreResp(**score_data) for score_data in data] if is_legacy else data
            return None
        except Exception as e:
            logger.error(f"Error getting user scores from cache: {e}")
            return None

    async def cache_user_scores(
        self,
        user_id: int,
        score_type: str,
        scores: list[UserDict] | list[LegacyScoreResp],
        include_fail: bool,
        mode: GameMode | None = None,
        limit: int = 100,
        offset: int = 0,
        expire_seconds: int | None = None,
        is_legacy: bool = False,
        pp_variant: str = "stable",
    ):
        """ç¼“å­˜ç”¨æˆ·æˆç»©"""
        try:
            if expire_seconds is None:
                expire_seconds = settings.user_scores_cache_expire_seconds
            cache_key = self._get_user_scores_cache_key(
                user_id, score_type, include_fail, mode, limit, offset, is_legacy, pp_variant
            )
            if len(scores) == 0:
                return
            if isinstance(scores[0], dict):
                scores_json_list = [safe_json_dumps(score) for score in scores]
            else:
                scores_json_list = [score.model_dump_json() for score in scores]  # pyright: ignore[reportAttributeAccessIssue]
            cached_data = f"[{','.join(scores_json_list)}]"
            await self.redis.setex(cache_key, expire_seconds, cached_data)
            logger.debug(f"Cached user {user_id} scores ({score_type}) for {expire_seconds}s")
        except Exception as e:
            logger.error(f"Error caching user scores: {e}")

    async def get_user_beatmapsets_from_cache(
        self, user_id: int, beatmapset_type: str, limit: int = 100, offset: int = 0
    ) -> list[Any] | None:
        """ä»Žç¼“å­˜èŽ·å–ç”¨æˆ·è°±é¢é›†"""
        try:
            cache_key = self._get_user_beatmapsets_cache_key(user_id, beatmapset_type, limit, offset)
            cached_data = await self.redis.get(cache_key)
            if cached_data:
                logger.debug(f"User beatmapsets cache hit for user {user_id}, type {beatmapset_type}")
                payload = json.loads(cached_data)
                if isinstance(payload, list):
                    return self._normalize_cached_beatmapset_status(payload, beatmapset_type)
                return payload
            return None
        except Exception as e:
            logger.error(f"Error getting user beatmapsets from cache: {e}")
            return None

    async def cache_user_beatmapsets(
        self,
        user_id: int,
        beatmapset_type: str,
        beatmapsets: list[Any],
        limit: int = 100,
        offset: int = 0,
        expire_seconds: int | None = None,
    ):
        """ç¼“å­˜ç”¨æˆ·è°±é¢é›†"""
        try:
            if expire_seconds is None:
                expire_seconds = settings.user_beatmapsets_cache_expire_seconds
            cache_key = self._get_user_beatmapsets_cache_key(user_id, beatmapset_type, limit, offset)
            if isinstance(beatmapsets, list):
                beatmapsets = self._normalize_cached_beatmapset_status(beatmapsets, beatmapset_type)
            # ä½¿ç”¨ model_dump_json() å¤„ç†æœ‰ model_dump_json æ–¹æ³•çš„å¯¹è±¡ï¼Œå¦åˆ™ä½¿ç”¨ safe_json_dumps
            serialized_beatmapsets = []
            for bms in beatmapsets:
                if hasattr(bms, "model_dump_json"):
                    serialized_beatmapsets.append(bms.model_dump_json())
                else:
                    serialized_beatmapsets.append(safe_json_dumps(bms))
            cached_data = f"[{','.join(serialized_beatmapsets)}]"
            await self.redis.setex(cache_key, expire_seconds, cached_data)
            logger.debug(f"Cached user {user_id} beatmapsets ({beatmapset_type}) for {expire_seconds}s")
        except Exception as e:
            logger.error(f"Error caching user beatmapsets: {e}")

    async def invalidate_user_cache(self, user_id: int):
        """Invalidate cached user profile payloads."""
        try:
            # Delete both scoped and unscoped profile caches.
            keys = []
            keys.extend(await self.redis.keys(f"user:{user_id}"))
            keys.extend(await self.redis.keys(f"user:{user_id}:pp_variant:*"))
            keys.extend(await self.redis.keys(f"user:{user_id}:ruleset:*"))
            if keys:
                await self.redis.delete(*keys)
                logger.info(f"Invalidated {len(keys)} cache entries for user {user_id}")
        except Exception as e:
            logger.error(f"Error invalidating user cache: {e}")

    async def invalidate_user_all_cache(self, user_id: int):
        """ä½¿ç”¨æˆ·æ‰€æœ‰ç¼“å­˜å¤±æ•ˆ"""
        try:
            # åˆ é™¤ç”¨æˆ·ä¿¡æ¯ç¼“å­˜
            pattern = f"user:{user_id}*"
            keys = await self.redis.keys(pattern)
            if keys:
                await self.redis.delete(*keys)
                logger.info(f"Invalidated {len(keys)} all cache entries for user {user_id}")
        except Exception as e:
            logger.error(f"Error invalidating user all cache: {e}")

    async def invalidate_user_scores_cache(self, user_id: int, mode: GameMode | None = None):
        """ä½¿ç”¨æˆ·æˆç»©ç¼“å­˜å¤±æ•ˆ"""
        try:
            # åˆ é™¤ç”¨æˆ·æˆç»©ç›¸å…³ç¼“å­˜
            mode_pattern = f":{mode}" if mode is not None else "*"
            pattern = f"user:{user_id}:scores:*{mode_pattern}*"
            keys = await self.redis.keys(pattern)
            if keys:
                await self.redis.delete(*keys)
                logger.info(f"Invalidated {len(keys)} score cache entries for user {user_id}")
        except Exception as e:
            logger.error(f"Error invalidating user scores cache: {e}")

    async def invalidate_user_beatmapsets_cache(self, user_id: int):
        """ä½¿ç”¨æˆ·è°±é¢é›†ç¼“å­˜å¤±æ•ˆ"""
        try:
            # åˆ é™¤ç”¨æˆ·è°±é¢é›†ç›¸å…³ç¼“å­˜
            pattern = f"user:{user_id}:beatmapsets:*"
            keys = await self.redis.keys(pattern)
            if keys:
                await self.redis.delete(*keys)
                logger.info(f"Invalidated {len(keys)} beatmapset cache entries for user {user_id}")
        except Exception as e:
            logger.error(f"Error invalidating user beatmapsets cache: {e}")

    async def preload_user_cache(self, session: AsyncSession, user_ids: list[int]):
        """é¢„åŠ è½½ç”¨æˆ·ç¼“å­˜"""
        if self._refreshing:
            return

        self._refreshing = True
        try:
            logger.info(f"Preloading cache for {len(user_ids)} users")

            # æ‰¹é‡èŽ·å–ç”¨æˆ·
            users = (await session.exec(select(User).where(col(User.id).in_(user_ids)))).all()

            # ä¸²è¡Œç¼“å­˜ç”¨æˆ·ä¿¡æ¯ï¼Œé¿å…å¹¶å‘æ•°æ®åº“è®¿é—®é—®é¢˜
            cached_count = 0
            for user in users:
                if user.id != BANCHOBOT_ID:
                    try:
                        await self._cache_single_user(user)
                        cached_count += 1
                    except Exception as e:
                        logger.error(f"Failed to cache user {user.id}: {e}")

            logger.info(f"Preloaded cache for {cached_count} users")

        except Exception as e:
            logger.error(f"Error preloading user cache: {e}")
        finally:
            self._refreshing = False

    async def _cache_single_user(self, user: User):
        """ç¼“å­˜å•ä¸ªç”¨æˆ·"""
        try:
            user_resp = await UserModel.transform(user, includes=User.USER_INCLUDES)

            # åº”ç”¨èµ„æºä»£ç†å¤„ç†
            if settings.enable_asset_proxy:
                try:
                    user_resp = await replace_asset_urls(user_resp)
                except Exception as e:
                    logger.warning(f"Asset proxy processing failed for user cache {user.id}: {e}")

            await self.cache_user(user_resp)
        except Exception as e:
            logger.error(f"Error caching single user {user.id}: {e}")

    async def refresh_user_cache_on_score_submit(
        self,
        session: AsyncSession,
        user_id: int,
        mode: GameMode,
        fetcher: "Fetcher | None" = None,
    ):
        """æˆç»©æäº¤åŽåˆ·æ–°ç”¨æˆ·ç¼“å­˜"""
        try:
            # ä½¿ç›¸å…³ç¼“å­˜å¤±æ•ˆï¼ˆåŒ…æ‹¬ v1 å’Œ v2ï¼‰
            await self.invalidate_user_cache(user_id)
            await self.invalidate_v1_user_cache(user_id)
            await self.invalidate_user_scores_cache(user_id, mode)
            await invalidate_pp_variant_caches_for_user(redis=self.redis, user_id=user_id, mode=mode)

            # Warm pp-dev mirrors so profile/rank views switch immediately after score submit.
            if fetcher is not None:
                try:
                    await get_user_pp_variant_statistics(
                        session=session,
                        user_id=user_id,
                        mode=mode,
                        pp_variant="pp_dev",
                        redis=self.redis,
                        fetcher=fetcher,
                    )
                except Exception as pp_dev_error:
                    logger.warning(f"Failed to warm pp-dev caches after score submit for user {user_id}: {pp_dev_error}")

            # ç«‹å³é‡æ–°åŠ è½½ç”¨æˆ·ä¿¡æ¯
            user = await session.get(User, user_id)
            if user and user.id != BANCHOBOT_ID:
                await self._cache_single_user(user)
                logger.info(f"Refreshed cache for user {user_id} after score submit")
        except Exception as e:
            logger.error(f"Error refreshing user cache on score submit: {e}")

    async def get_cache_stats(self) -> dict:
        """èŽ·å–ç¼“å­˜ç»Ÿè®¡ä¿¡æ¯"""
        try:
            user_keys = await self.redis.keys("user:*")
            v1_user_keys = await self.redis.keys("v1_user:*")
            all_keys = user_keys + v1_user_keys
            total_size = 0

            for key in all_keys[:100]:  # é™åˆ¶æ£€æŸ¥æ•°é‡
                try:
                    size = await self.redis.memory_usage(key)
                    if size:
                        total_size += size
                except Exception:
                    logger.warning(f"Failed to get memory usage for key {key}")
                    continue

            return {
                "cached_users": len([k for k in user_keys if ":scores:" not in k and ":beatmapsets:" not in k]),
                "cached_v1_users": len([k for k in v1_user_keys if ":scores:" not in k]),
                "cached_user_scores": len([k for k in user_keys if ":scores:" in k]),
                "cached_user_beatmapsets": len([k for k in user_keys if ":beatmapsets:" in k]),
                "total_cached_entries": len(all_keys),
                "estimated_total_size_mb": (round(total_size / 1024 / 1024, 2) if total_size > 0 else 0),
                "refreshing": self._refreshing,
            }
        except Exception as e:
            logger.error(f"Error getting user cache stats: {e}")
            return {"error": str(e)}


# å…¨å±€ç¼“å­˜æœåŠ¡å®žä¾‹
_user_cache_service: UserCacheService | None = None


def get_user_cache_service(redis: Redis) -> UserCacheService:
    """èŽ·å–ç”¨æˆ·ç¼“å­˜æœåŠ¡å®žä¾‹"""
    global _user_cache_service
    if _user_cache_service is None:
        _user_cache_service = UserCacheService(redis)
    return _user_cache_service


async def refresh_user_cache_background(redis: Redis, user_id: int, mode: GameMode):
    """åŽå°ä»»åŠ¡ï¼šåˆ·æ–°ç”¨æˆ·ç¼“å­˜"""
    try:
        user_cache_service = get_user_cache_service(redis)
        # åˆ›å»ºç‹¬ç«‹çš„æ•°æ®åº“ä¼šè¯
        async with with_db() as session:
            from app.dependencies.fetcher import get_fetcher

            fetcher = await get_fetcher()
            await user_cache_service.refresh_user_cache_on_score_submit(session, user_id, mode, fetcher)
    except Exception as e:
        logger.error(f"Failed to refresh user cache after score submit: {e}")


async def prewarm_pp_dev_profile_background(
    redis: Redis,
    user_id: int,
    ruleset: "GameMode | None",
):
    """Background task: build and cache the pp_dev user profile if not already cached.

    Called whenever the stable variant is served so that the first pp_dev toggle
    hits the cache instead of blocking on an inline recalculation.
    """
    cache_service = get_user_cache_service(redis)
    cache_key = cache_service._get_user_cache_key(user_id, ruleset, "pp_dev")
    try:
        if await redis.exists(cache_key):
            return  # already warm
    except Exception:
        return

    # Prevent concurrent prewarms for the same user/mode - only one task at a time.
    # Without this, every profile request for an uncached user fires a new background
    # task, and they all contend on the same DB rows causing lock wait timeouts.
    ruleset_key = ruleset.value if ruleset is not None else "none"
    lock_key = f"prewarm_lock:{user_id}:{ruleset_key}"
    acquired = await redis.set(lock_key, 1, nx=True, ex=60)
    if not acquired:
        return  # another prewarm task is already in progress for this user

    try:
        async with with_db() as session:
            from app.dependencies.fetcher import get_fetcher

            user = await session.get(User, user_id)
            if user is None or user.id == BANCHOBOT_ID:
                return

            fetcher = await get_fetcher()
            effective_mode: GameMode = ruleset or user.playmode

            canonical_user_resp = await UserModel.transform(
                user,
                includes=User.USER_INCLUDES,
                ruleset=ruleset,
                show_nsfw_media=True,
            )
            # Use no_autoflush to prevent SQLAlchemy from flushing Score objects
            # whose .pp attribute gets modified in-memory during pp_dev computation.
            # Without this, COUNT queries in apply_pp_variant_to_user_response
            # trigger autoflush -> UPDATE scores SET pp=... -> lock wait timeout (1205).
            with session.no_autoflush:
                await apply_pp_variant_to_user_response(
                    session=session,
                    user_resp=canonical_user_resp,
                    user_id=user.id,
                    mode=effective_mode,
                    pp_variant="pp_dev",
                    redis=redis,
                    fetcher=fetcher,
                    country_code=user.country_code,
                )
            await cache_service.cache_user(canonical_user_resp, ruleset, pp_variant="pp_dev")
            logger.debug(f"Pre-warmed pp_dev profile cache for user {user_id} mode={effective_mode}")
    except Exception as e:
        logger.warning(f"Failed to pre-warm pp_dev profile for user {user_id}: {e}")
    finally:
        try:
            await redis.delete(lock_key)
        except Exception:
            pass
