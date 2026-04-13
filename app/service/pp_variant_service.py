from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from app.calculator import (
    calculate_pp_weight,
    calculate_weighted_acc,
    calculate_weighted_pp,
    get_pp_dev_calculator,
    pre_fetch_and_calculate_pp,
)
from app.database import User, UserStatistics
from app.database.best_scores import BestScore
from app.database.score import LegacyScoreResp, Score, _RELAX_AP_MODES, _OSU_STANDARD_MODES, _get_effective_od_cs
from app.log import log
from app.models.mods import mods_can_get_pp
from app.models.score import GameMode
from app.utils import safe_json_dumps

from redis.asyncio import Redis
from sqlmodel import col, exists, func, select
from sqlmodel.ext.asyncio.session import AsyncSession

if False:  # TYPE_CHECKING
    from app.fetcher import Fetcher  # pragma: no cover

PpVariant = Literal["stable", "pp_dev"]

logger = log("PpVariant")

SCORE_PP_DEV_CACHE_TTL_SECONDS = 60 * 60 * 24
SCORE_PP_DEV_FALLBACK_CACHE_TTL_SECONDS = 60
USER_PP_DEV_CACHE_TTL_SECONDS = 60 * 30       # user stats: 30 min (was 10)
USER_PP_DEV_PROFILE_CACHE_TTL_SECONDS = 60 * 30  # full profile: 30 min
RANKING_PP_DEV_CACHE_TTL_SECONDS = 60 * 10    # ranking snapshot: 10 min (was 5)
SCORE_PP_DEV_CALC_TIMEOUT_SECONDS = 2.5
PP_DEV_RECALC_TOP_SCORE_LIMIT = 100
PP_DEV_RANKING_RECALC_USER_LIMIT = 50
PP_DEV_CALC_BATCH_CONCURRENCY = 3
PP_DEV_PROFILE_SNAPSHOT_TIMEOUT_SECONDS = 6.0


def normalize_pp_variant(value: str | None) -> PpVariant:
    if value is None:
        return "stable"
    normalized = value.strip().lower()
    if normalized in {"pp_dev", "ppdev", "dev", "alpha_ppdev"}:
        return "pp_dev"
    return "stable"


def is_pp_dev_variant(pp_variant: PpVariant) -> bool:
    return pp_variant == "pp_dev"


def _score_pp_dev_cache_key(score_id: int) -> str:
    return f"score:{score_id}:pp_variant:pp_dev"


def _user_pp_dev_stats_cache_key(user_id: int, mode: GameMode) -> str:
    return f"user:{user_id}:pp_variant:pp_dev:stats:{mode.value}"


def _ranking_pp_dev_snapshot_cache_key(mode: GameMode) -> str:
    return f"ranking:pp_variant:pp_dev:snapshot:{mode.value}"


async def invalidate_pp_variant_caches_for_user(
    *,
    redis: Redis,
    user_id: int,
    mode: GameMode,
) -> None:
    keys = [
        _user_pp_dev_stats_cache_key(user_id, mode),
        _ranking_pp_dev_snapshot_cache_key(mode),
    ]
    await redis.delete(*keys)


async def get_score_pp_variant(
    *,
    session: AsyncSession,
    score: Score,
    pp_variant: PpVariant,
    redis: Redis,
    fetcher: "Fetcher",
    recalculate_if_missing: bool = True,
) -> float:
    if not is_pp_dev_variant(pp_variant):
        return float(score.pp or 0.0)

    cache_key = _score_pp_dev_cache_key(score.id)
    cached = await redis.get(cache_key)
    if cached is not None:
        try:
            return float(cached)
        except (TypeError, ValueError):
            pass

    if not recalculate_if_missing:
        return float(score.pp or 0.0)

    if not (score.passed and score.ranked):
        await redis.set(cache_key, "0", ex=SCORE_PP_DEV_CACHE_TTL_SECONDS)
        return 0.0

    # Legacy score rows can miss mod settings that ranked_mods validation expects.
    # If the score already has stable PP, still include it in pp-dev recalculation.
    if not mods_can_get_pp(int(score.gamemode), score.mods) and float(score.pp or 0.0) <= 0:
        await redis.set(cache_key, "0", ex=SCORE_PP_DEV_CACHE_TTL_SECONDS)
        return 0.0

    # Accuracy floor for relax / autopilot modes: < 75% acc → 0 pp.
    if score.gamemode in _RELAX_AP_MODES and score.accuracy < 0.75:
        await redis.set(cache_key, "0", ex=SCORE_PP_DEV_CACHE_TTL_SECONDS)
        return 0.0

    # OD + CS difficulty floor for osu! standard-based modes.
    if score.gamemode in _OSU_STANDARD_MODES:
        od_cs = await _get_effective_od_cs(score, session)
        if od_cs is not None:
            eff_od, eff_cs = od_cs
            if (eff_od == 0.0 and eff_cs == 0.0) or (eff_od + eff_cs) / 2.0 <= 4.0:
                await redis.set(cache_key, "0", ex=SCORE_PP_DEV_CACHE_TTL_SECONDS)
                return 0.0

    pp_dev_calculator = await get_pp_dev_calculator()
    if pp_dev_calculator is None:
        fallback = float(score.pp or 0.0)
        await redis.set(cache_key, str(fallback), ex=SCORE_PP_DEV_FALLBACK_CACHE_TTL_SECONDS)
        return fallback

    pp_value = float(score.pp or 0.0)
    calc_succeeded = False
    try:
        # Each pp-dev calc gets its OWN session (avoids "concurrent operations"
        # when batch tasks share the caller's session).  We use wait_for so
        # timed-out tasks are cancelled — the `async with` ensures the session
        # is closed even on CancelledError, preventing connection-pool exhaustion.
        from app.dependencies.database import engine as _engine
        from sqlmodel.ext.asyncio.session import AsyncSession as _AsyncSession

        async def _calc_with_own_session():
            async with _AsyncSession(_engine) as own_session:
                return await pre_fetch_and_calculate_pp(
                    score,
                    own_session,
                    redis,
                    fetcher,
                    calculator_override=pp_dev_calculator,
                )

        calculated, success = await asyncio.wait_for(
            _calc_with_own_session(),
            timeout=SCORE_PP_DEV_CALC_TIMEOUT_SECONDS,
        )
        if success:
            pp_value = float(calculated)
            calc_succeeded = True
    except TimeoutError:
        logger.warning(
            f"pp-dev calculation timeout for score {score.id} "
            f"after {SCORE_PP_DEV_CALC_TIMEOUT_SECONDS}s; using stable pp fallback"
        )
    except Exception as e:
        logger.warning(f"Failed to calculate pp-dev for score {score.id}, using stable pp: {e}")

    # Avoid "sticky stable" results: cache fallback briefly so we retry pp-dev soon.
    ttl = SCORE_PP_DEV_CACHE_TTL_SECONDS if calc_succeeded else SCORE_PP_DEV_FALLBACK_CACHE_TTL_SECONDS
    await redis.set(cache_key, str(pp_value), ex=ttl)
    return pp_value


async def get_score_pp_variant_batch(
    *,
    session: AsyncSession,
    scores: list[Score],
    pp_variant: PpVariant,
    redis: Redis,
    fetcher: "Fetcher",
    recalc_top_n: int = 0,
) -> dict[int, float]:
    if not scores:
        return {}

    if not is_pp_dev_variant(pp_variant):
        return {score.id: float(score.pp or 0.0) for score in scores}

    semaphore = asyncio.Semaphore(PP_DEV_CALC_BATCH_CONCURRENCY)
    result: dict[int, float] = {}

    async def compute(index: int, score: Score) -> tuple[int, float]:
        async with semaphore:
            try:
                value = await get_score_pp_variant(
                    session=session,
                    score=score,
                    pp_variant=pp_variant,
                    redis=redis,
                    fetcher=fetcher,
                    recalculate_if_missing=index < recalc_top_n,
                )
            except Exception as e:
                logger.warning(f"pp-dev batch calculation failed for score {score.id}, using stable pp: {e}")
                value = float(score.pp or 0.0)
            return score.id, float(value)

    tasks = [asyncio.create_task(compute(index, score)) for index, score in enumerate(scores)]
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    for score, outcome in zip(scores, outcomes, strict=False):
        if isinstance(outcome, BaseException):
            logger.warning(f"pp-dev batch task failed for score {score.id}, using stable pp: {outcome}")
            result[score.id] = float(score.pp or 0.0)
            continue

        score_id, value = outcome
        result[score_id] = value

    return result


async def get_user_pp_variant_statistics(
    *,
    session: AsyncSession,
    user_id: int,
    mode: GameMode,
    pp_variant: PpVariant,
    redis: Redis,
    fetcher: "Fetcher",
    recalculate_top_scores: bool = True,
) -> tuple[float, float]:
    if not is_pp_dev_variant(pp_variant):
        stat = (
            await session.exec(
                select(UserStatistics).where(
                    UserStatistics.user_id == user_id,
                    UserStatistics.mode == mode,
                )
            )
        ).first()
        if stat is None:
            return 0.0, 0.0
        return float(stat.pp or 0.0), float(stat.hit_accuracy or 0.0)

    cache_key = _user_pp_dev_stats_cache_key(user_id, mode)
    cached = await redis.get(cache_key)
    if cached:
        try:
            payload = json.loads(cached)
            return float(payload.get("pp", 0.0)), float(payload.get("hit_accuracy", 0.0))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

    # Use canonical best-score rows (already one per beatmap in stable) as the pp-dev mirror base.
    # This keeps the variant responsive while still recalculating PP values via pp-dev formula.
    scores = (
        await session.exec(
            select(Score).where(
                Score.user_id == user_id,
                Score.gamemode == mode,
                col(Score.passed).is_(True),
                col(Score.ranked).is_(True),
                exists().where(col(BestScore.score_id) == Score.id),
            )
        )
    ).all()

    ordered_scores = sorted(scores, key=lambda s: (float(s.pp or 0.0), s.id), reverse=True)
    pp_by_score_id = await get_score_pp_variant_batch(
        session=session,
        scores=ordered_scores,
        pp_variant=pp_variant,
        redis=redis,
        fetcher=fetcher,
        recalc_top_n=PP_DEV_RECALC_TOP_SCORE_LIMIT if recalculate_top_scores else 0,
    )

    best_by_beatmap: dict[int, tuple[float, float, int]] = {}
    for score in ordered_scores:
        # Same compatibility rule as get_score_pp_variant():
        # keep historically valid ranked scores that already have stable PP.
        if not mods_can_get_pp(int(score.gamemode), score.mods) and float(score.pp or 0.0) <= 0:
            continue

        pp_value = float(pp_by_score_id.get(score.id, float(score.pp or 0.0)))
        if pp_value <= 0:
            continue

        previous = best_by_beatmap.get(score.beatmap_id)
        if previous is None or pp_value > previous[0] or (pp_value == previous[0] and score.id > previous[2]):
            best_by_beatmap[score.beatmap_id] = (pp_value, float(score.accuracy), score.id)

    best_scores = sorted(best_by_beatmap.values(), key=lambda item: (item[0], item[2]), reverse=True)

    pp_sum = 0.0
    acc_sum = 0.0
    for index, (pp_value, acc_value, _score_id) in enumerate(best_scores):
        pp_sum += calculate_weighted_pp(pp_value, index)
        acc_sum += calculate_weighted_acc(acc_value, index)

    if best_scores:
        acc_sum *= 100 / (20 * (1 - pow(0.95, len(best_scores))))
        acc_sum = max(0.0, min(100.0, acc_sum))

    payload = {
        "pp": pp_sum,
        "hit_accuracy": acc_sum,
    }
    await redis.set(cache_key, safe_json_dumps(payload), ex=USER_PP_DEV_CACHE_TTL_SECONDS)
    return pp_sum, acc_sum


async def get_pp_dev_ranking_snapshot(
    *,
    session: AsyncSession,
    ruleset: GameMode,
    redis: Redis,
    fetcher: "Fetcher",
) -> list[dict[str, Any]]:
    cache_key = _ranking_pp_dev_snapshot_cache_key(ruleset)
    cached = await redis.get(cache_key)
    if cached:
        try:
            payload = json.loads(cached)
            if isinstance(payload, list):
                return payload
        except json.JSONDecodeError:
            pass

    rows = (
        await session.exec(
            select(UserStatistics.user_id, UserStatistics.ranked_score, UserStatistics.pp, User.country_code)
            .join(User, col(User.id) == col(UserStatistics.user_id))
            .where(
                col(UserStatistics.mode) == ruleset,
                col(UserStatistics.is_ranked).is_(True),
                ~User.is_restricted_query(col(UserStatistics.user_id)),
            )
        )
    ).all()

    rows = sorted(rows, key=lambda row: (float(row[2] or 0.0), int(row[1] or 0), int(row[0])), reverse=True)

    snapshot: list[dict[str, Any]] = []
    for index, (user_id, ranked_score, _stable_pp, country_code) in enumerate(rows):
        pp_value, hit_accuracy = await get_user_pp_variant_statistics(
            session=session,
            user_id=int(user_id),
            mode=ruleset,
            pp_variant="pp_dev",
            redis=redis,
            fetcher=fetcher,
            recalculate_top_scores=index < PP_DEV_RANKING_RECALC_USER_LIMIT,
        )
        if pp_value <= 0:
            continue
        snapshot.append(
            {
                "user_id": int(user_id),
                "pp": float(pp_value),
                "hit_accuracy": float(hit_accuracy),
                "ranked_score": int(ranked_score or 0),
                "country_code": str(country_code or "").upper(),
            }
        )

    snapshot.sort(key=lambda row: (row["pp"], row["ranked_score"], row["user_id"]), reverse=True)
    await redis.set(cache_key, safe_json_dumps(snapshot), ex=RANKING_PP_DEV_CACHE_TTL_SECONDS)
    return snapshot


def apply_pp_variant_to_score_responses(
    *,
    scores: list[Score],
    score_responses: list[dict[str, Any] | LegacyScoreResp],
    pp_by_score_id: dict[int, float],
    add_weight: bool,
    rank_by_score_id: dict[int, int] | None = None,
) -> None:
    if not score_responses:
        return

    rank_map = rank_by_score_id
    if add_weight and rank_map is None:
        ranked = sorted(scores, key=lambda s: (pp_by_score_id.get(s.id, float(s.pp or 0.0)), s.id), reverse=True)
        rank_map = {score.id: index + 1 for index, score in enumerate(ranked)}

    for score, score_resp in zip(scores, score_responses):
        score_pp = float(pp_by_score_id.get(score.id, float(score.pp or 0.0)))

        if isinstance(score_resp, dict):
            score_resp["pp"] = score_pp
        else:
            score_resp.pp = score_pp

        if add_weight:
            rank = rank_map.get(score.id) if rank_map is not None else None
            if isinstance(score_resp, dict):
                score_resp["weight"] = calculate_pp_weight(rank - 1) if rank is not None else 0.0


def get_user_ranks_from_snapshot(
    *,
    snapshot: list[dict[str, Any]],
    user_id: int,
    country_code: str | None = None,
) -> tuple[int | None, int | None]:
    global_rank = None
    country_rank = None
    normalized_country = (country_code or "").upper()

    for idx, row in enumerate(snapshot, start=1):
        if int(row.get("user_id") or 0) == user_id:
            global_rank = idx
            break

    if normalized_country:
        rank_in_country = 0
        for row in snapshot:
            if str(row.get("country_code") or "").upper() != normalized_country:
                continue
            rank_in_country += 1
            if int(row.get("user_id") or 0) == user_id:
                country_rank = rank_in_country
                break

    return global_rank, country_rank


async def apply_pp_variant_to_user_response(
    *,
    session: AsyncSession,
    user_resp: dict[str, Any],
    user_id: int,
    mode: GameMode,
    pp_variant: PpVariant,
    redis: Redis,
    fetcher: "Fetcher",
    country_code: str | None,
) -> None:
    if not is_pp_dev_variant(pp_variant):
        return

    pp_value, acc_value = await get_user_pp_variant_statistics(
        session=session,
        user_id=user_id,
        mode=mode,
        pp_variant=pp_variant,
        redis=redis,
        fetcher=fetcher,
    )
    statistics = user_resp.get("statistics")
    fallback_global_rank = statistics.get("global_rank") if isinstance(statistics, dict) else None
    fallback_country_rank = statistics.get("country_rank") if isinstance(statistics, dict) else None
    global_rank = fallback_global_rank
    country_rank = fallback_country_rank

    # Prefer exact pp-dev ranking from snapshot cache (or on-demand build).
    # This provides real variant leaderboard parity for profile rank display.
    # Uses its own DB session so cancellation on timeout properly releases the connection.
    snapshot: list[dict[str, Any]] | None = None
    try:
        from app.dependencies.database import engine as _engine
        from sqlmodel.ext.asyncio.session import AsyncSession as _AsyncSession

        async def _snapshot_with_own_session():
            async with _AsyncSession(_engine) as own_session:
                return await get_pp_dev_ranking_snapshot(
                    session=own_session,
                    ruleset=mode,
                    redis=redis,
                    fetcher=fetcher,
                )

        snapshot = await asyncio.wait_for(
            _snapshot_with_own_session(),
            timeout=PP_DEV_PROFILE_SNAPSHOT_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning(
            "pp-dev ranking snapshot timed out for profile response "
            f"(user_id={user_id}, mode={mode.value}, timeout={PP_DEV_PROFILE_SNAPSHOT_TIMEOUT_SECONDS}s)"
        )
    except Exception as e:
        logger.warning(f"pp-dev ranking snapshot failed for profile response (user_id={user_id}): {e}")

    if snapshot:
        global_rank, country_rank = get_user_ranks_from_snapshot(
            snapshot=snapshot,
            user_id=user_id,
            country_code=country_code,
        )

    # Fallback: fast approximation when snapshot is unavailable.
    normalized_country = (country_code or "").upper()
    needs_country_rank = bool(normalized_country) and country_rank is None
    if pp_value > 0 and (global_rank is None or needs_country_rank):
        global_higher = (
            await session.exec(
                select(func.count())
                .select_from(UserStatistics)
                .where(
                    col(UserStatistics.mode) == mode,
                    col(UserStatistics.is_ranked).is_(True),
                    col(UserStatistics.pp) > pp_value,
                    ~User.is_restricted_query(col(UserStatistics.user_id)),
                )
            )
        ).one()
        global_rank = int(global_higher or 0) + 1

        if normalized_country:
            country_higher = (
                await session.exec(
                    select(func.count())
                    .select_from(UserStatistics)
                    .join(User, col(User.id) == col(UserStatistics.user_id))
                    .where(
                        col(UserStatistics.mode) == mode,
                        col(UserStatistics.is_ranked).is_(True),
                        col(UserStatistics.pp) > pp_value,
                        col(User.country_code) == normalized_country,
                        ~User.is_restricted_query(col(UserStatistics.user_id)),
                    )
                )
            ).one()
            country_rank = int(country_higher or 0) + 1

    statistics = user_resp.get("statistics")
    if isinstance(statistics, dict):
        statistics["pp"] = pp_value
        statistics["hit_accuracy"] = acc_value
        statistics["global_rank"] = global_rank
        statistics["country_rank"] = country_rank
        user_resp["statistics"] = statistics

    statistics_rulesets = user_resp.get("statistics_rulesets")
    if isinstance(statistics_rulesets, dict):
        mode_key = mode.value
        mode_stats = statistics_rulesets.get(mode_key)
        if isinstance(mode_stats, dict):
            mode_stats["pp"] = pp_value
            mode_stats["hit_accuracy"] = acc_value
            mode_stats["global_rank"] = global_rank
            mode_stats["country_rank"] = country_rank
            statistics_rulesets[mode_key] = mode_stats
            user_resp["statistics_rulesets"] = statistics_rulesets
