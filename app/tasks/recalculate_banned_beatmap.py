import asyncio
import json

from app.calculator import calculate_pp, is_suspicious_beatmap
from app.config import settings
from app.database.beatmap import BannedBeatmaps, Beatmap
from app.database.best_scores import BestScore
from app.database.score import Score, calculate_user_pp
from app.database.statistics import UserStatistics
from app.dependencies.database import get_redis, with_db
from app.dependencies.fetcher import get_fetcher
from app.dependencies.scheduler import get_scheduler
from app.log import logger
from app.models.mods import mods_can_get_pp

from sqlmodel import col, delete, select


@get_scheduler().scheduled_job("interval", id="recalculate_banned_beatmap", hours=1)
async def recalculate_banned_beatmap():
    redis = get_redis()
    fetcher = await get_fetcher()

    async with with_db() as session:
        # Load the complete current set of banned beatmaps from the DB first.
        # This is our authoritative source of truth.
        current_banned_set: set[int] = set(
            (await session.exec(select(BannedBeatmaps.beatmap_id).distinct())).all()
        )

        # Load what we last processed from Redis.
        last_banned_beatmaps: set[int] = set()
        last_banned_raw = await redis.get("last_banned_beatmap")
        if last_banned_raw:
            last_banned_beatmaps = set(json.loads(last_banned_raw))
        else:
            # Redis cold-start (restart/eviction): seed from the DB so we don't
            # treat every existing ban as "new" and wipe BestScore entries.
            last_banned_beatmaps = set(current_banned_set)
            logger.warning(
                "last_banned_beatmap key missing from Redis — seeding from DB "
                "(%d entries). No BestScores will be deleted this run.",
                len(last_banned_beatmaps),
            )
            await redis.set(
                "last_banned_beatmap",
                json.dumps(list(last_banned_beatmaps)),
            )

        # Newly banned: in DB but not in last seen set.
        new_banned_beatmaps = [b for b in current_banned_set if b not in last_banned_beatmaps]
        # Newly unbanned: in last seen set but no longer in DB.
        unbanned_beatmaps = [b for b in last_banned_beatmaps if b not in current_banned_set]

        affected_users: set[tuple[int, str]] = set()

        # ── Handle newly banned beatmaps ───────────────────────────────────────
        for beatmap_id in new_banned_beatmaps:
            last_banned_beatmaps.add(beatmap_id)
            await session.execute(
                delete(BestScore).where(col(BestScore.beatmap_id) == beatmap_id)
            )
            scores = (
                await session.exec(
                    select(Score).where(Score.beatmap_id == beatmap_id, Score.pp > 0)
                )
            ).all()
            for score in scores:
                score.pp = 0
                affected_users.add((score.user_id, score.gamemode))

        # ── Handle re-enabled (unbanned) beatmaps ─────────────────────────────
        if unbanned_beatmaps:
            for beatmap_id in unbanned_beatmaps:
                last_banned_beatmaps.discard(beatmap_id)
                try:
                    scores = (
                        await session.exec(
                            select(Score).where(
                                Score.beatmap_id == beatmap_id,
                                col(Score.passed).is_(True),
                            )
                        )
                    ).all()
                except Exception:
                    logger.exception(
                        "Failed to query scores for unbanned beatmap %d", beatmap_id
                    )
                    continue

                prev: dict[tuple[int, int], BestScore] = {}
                for score in scores:
                    attempts = 3
                    db_beatmap_raw = None
                    while attempts > 0:
                        try:
                            db_beatmap_raw = await fetcher.get_or_fetch_beatmap_raw(
                                redis, beatmap_id
                            )
                            break
                        except Exception:
                            attempts -= 1
                            await asyncio.sleep(1)
                    if db_beatmap_raw is None:
                        logger.warning(
                            "Could not fetch beatmap raw for %d, skipping pp calc",
                            beatmap_id,
                        )
                        continue

                    try:
                        beatmap_obj = await Beatmap.get_or_fetch(
                            session, fetcher, bid=beatmap_id
                        )
                    except Exception:
                        beatmap_obj = None

                    ranked = (
                        beatmap_obj.beatmap_status.has_pp() if beatmap_obj else False
                    ) | settings.enable_all_beatmap_pp

                    if not ranked or not mods_can_get_pp(int(score.gamemode), score.mods):
                        continue

                    try:
                        pp = await calculate_pp(score, db_beatmap_raw, session)
                        if not pp or pp == 0:
                            continue
                        key = (score.beatmap_id, score.user_id)
                        if key not in prev or prev[key].pp < pp:
                            best_score = BestScore(
                                user_id=score.user_id,
                                beatmap_id=beatmap_id,
                                acc=score.accuracy,
                                score_id=score.id,
                                pp=pp,
                                gamemode=score.gamemode,
                            )
                            prev[key] = best_score
                            affected_users.add((score.user_id, score.gamemode))
                            score.pp = pp
                    except Exception:
                        logger.exception(
                            "Error calculating pp for score %d on unbanned beatmap %d",
                            score.id,
                            beatmap_id,
                        )
                        continue

                for best in prev.values():
                    session.add(best)

        # ── Recalculate user pp totals for all affected users ─────────────────
        for user_id, gamemode in affected_users:
            statistics = (
                await session.exec(
                    select(UserStatistics)
                    .where(UserStatistics.user_id == user_id)
                    .where(col(UserStatistics.mode) == gamemode)
                )
            ).first()
            if not statistics:
                continue
            statistics.pp, statistics.hit_accuracy = await calculate_user_pp(
                session, user_id, gamemode
            )

        await session.commit()

    logger.info(
        "Recalculated banned beatmaps — newly banned: %d, unbanned: %d, affected users: %d",
        len(new_banned_beatmaps),
        len(unbanned_beatmaps),
        len(affected_users),
    )
    await redis.set(
        "last_banned_beatmap", json.dumps(list(last_banned_beatmaps))
    )


async def reverify_banned_beatmaps() -> dict:
    """
    Re-verify automatic beatmap-policy bans using the now-correct .osu fetcher.
    Manual bans are intentionally ignored so admin curation is never deleted by
    a fetcher/checksum recovery pass.
    Returns a dict with counts of removed / kept entries.
    The next recalculate_banned_beatmap run will then restore BestScore entries.
    """
    redis = get_redis()
    fetcher = await get_fetcher()
    removed: list[int] = []
    kept: list[int] = []
    skipped_manual: list[int] = []

    async with with_db() as session:
        banned_items = (
            await session.exec(select(BannedBeatmaps))
        ).all()

        for banned_item in banned_items:
            beatmap_id = banned_item.beatmap_id
            if banned_item.source != "auto_policy":
                skipped_manual.append(beatmap_id)
                continue

            try:
                beatmap_raw = await fetcher.get_or_fetch_beatmap_raw(redis, beatmap_id)
            except Exception:
                logger.warning(
                    "reverify: could not fetch .osu for beatmap %d — keeping ban",
                    beatmap_id,
                )
                kept.append(beatmap_id)
                continue

            if beatmap_raw is None:
                logger.warning(
                    "reverify: no .osu available for beatmap %d — keeping ban",
                    beatmap_id,
                )
                kept.append(beatmap_id)
                continue

            try:
                still_suspicious = is_suspicious_beatmap(beatmap_raw)
            except Exception:
                logger.exception(
                    "reverify: suspicious check failed for beatmap %d — keeping ban",
                    beatmap_id,
                )
                kept.append(beatmap_id)
                continue

            if still_suspicious:
                kept.append(beatmap_id)
                logger.debug(
                    "reverify: beatmap %d still suspicious, keeping ban", beatmap_id
                )
            else:
                # Remove from BannedBeatmaps so the next hourly task restores PP.
                await session.execute(
                    delete(BannedBeatmaps).where(
                        BannedBeatmaps.beatmap_id == beatmap_id,
                        BannedBeatmaps.source == "auto_policy",
                    )
                )
                removed.append(beatmap_id)
                logger.info(
                    "reverify: beatmap %d was a false positive — removed from ban list",
                    beatmap_id,
                )

        await session.commit()

    # Clear the Redis cache so the next hourly run sees the removals as "unbanned".
    if removed:
        remaining = set(kept)
        await redis.set(
            "last_banned_beatmap", json.dumps(list(remaining))
        )
        logger.info(
            "reverify complete — removed %d false positives, kept %d legitimate bans",
            len(removed),
            len(kept),
        )

    return {"removed": removed, "kept": kept, "skipped_manual": skipped_manual}
