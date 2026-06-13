"""One-off: recompute total_score for the mod-multiplier rebalance.

For every modded score, ``total_score = round(total_score_without_mods * mult)``
where ``mult`` comes from the post-rebalance per-ruleset calculator. Then the
per-beatmap leaderboard rows (total_score_best_scores) are rescaled to match,
and each affected user's ranked_score / total_score / level are refreshed.

``total_score_without_mods`` is the raw, multiplier-free score, so this is a
lossless one-shot (do NOT run it repeatedly -- rounding compounds). Scoring mode
is standardised, so display score == total_score and ranked_score is just the
sum of a user's ranked+passed best scores.

Usage (inside the app container):
    uv run python scripts/recalc_mod_multipliers.py            # dry run, reports only
    uv run python scripts/recalc_mod_multipliers.py --apply    # writes changes
    uv run python scripts/recalc_mod_multipliers.py --queue-pp # also queue low CS/OD
                                                               # osu scores for pp recalc
"""

import argparse
import asyncio
from collections import defaultdict

from sqlalchemy import text, update as sa_update
from sqlmodel import col, func, select

from app.calculator import calculate_score_to_level
from app.calculators.low_stat_pp import _effective_cs_od  # noqa: PLC2701
from app.calculators.score_multiplier import score_multiplier
from app.database.beatmap import Beatmap
from app.database.score import Score
from app.database.statistics import UserStatistics
from app.database.total_score_best_scores import TotalScoreBestScore
from app.dependencies.database import with_db
from app.log import logger

CHUNK = 1000


def _chunks(items, n=CHUNK):
    for i in range(0, len(items), n):
        yield items[i : i + n]


async def run(apply: bool, queue_pp: bool) -> None:
    async with with_db() as session:
        logger.info("loading beatmap difficulties...")
        rows = (await session.exec(select(Beatmap.id, Beatmap.cs, Beatmap.accuracy))).all()
        diff = {r[0]: (float(r[1] or 5.0), float(r[2] or 5.0)) for r in rows}
        logger.info(f"loaded {len(diff)} beatmaps")

        logger.info("scanning modded scores...")
        scores = (
            await session.exec(
                select(
                    Score.id,
                    Score.user_id,
                    Score.gamemode,
                    Score.mods,
                    Score.total_score,
                    Score.total_score_without_mods,
                    Score.beatmap_id,
                    Score.ranked,
                    Score.passed,
                ).where(text("JSON_LENGTH(mods) > 0"))
            )
        ).all()
        logger.info(f"scanned {len(scores)} modded scores")

        changed: list[tuple[int, int]] = []        # (score_id, new_total)
        total_delta = defaultdict(int)             # (user_id, gamemode) -> sum(new - old)
        affected_users: set[tuple[int, object]] = set()
        pp_queue: list[int] = []
        n_changed = 0
        abs_delta = 0

        for sid, uid, gamemode, mods, old_total, tswm, bid, ranked, passed in scores:
            if tswm is None or tswm == 0:
                continue
            base = int(gamemode.to_base_ruleset())
            cs, od = diff.get(bid, (5.0, 5.0))
            new_total = int(round(tswm * score_multiplier(base, mods, cs, od)))
            if new_total != old_total:
                changed.append((sid, new_total))
                total_delta[(uid, gamemode)] += new_total - old_total
                affected_users.add((uid, gamemode))
                n_changed += 1
                abs_delta += abs(new_total - old_total)

            if queue_pp and base == 0 and passed and pp_can_apply_penalty(mods, *diff.get(bid, (5.0, 5.0))):
                pp_queue.append(sid)

        print("==== total_score recalc ====")
        print(f"modded scores scanned : {len(scores)}")
        print(f"scores changing       : {n_changed}")
        print(f"sum |delta|           : {abs_delta:,}")
        print(f"affected (user,mode)  : {len(affected_users)}")
        if queue_pp:
            print(f"osu scores to pp-recalc: {len(pp_queue)}")

        # a few sample movers
        movers = sorted(
            ((sid, nt) for sid, nt in changed),
            key=lambda x: x[1],
            reverse=True,
        )[:5]
        for sid, nt in movers:
            print(f"  sample score {sid} -> total_score {nt:,}")

        if not apply:
            print("\n(dry run -- nothing written. re-run with --apply)")
            return

        logger.info(f"applying {n_changed} score updates...")
        for batch in _chunks(changed):
            await session.execute(sa_update(Score), [{"id": s, "total_score": t} for s, t in batch])
        await session.commit()

        # rescale the leaderboard rows that point at a changed score
        changed_ids = [s for s, _ in changed]
        new_by_id = dict(changed)
        tsbs_ids: list[int] = []
        for batch in _chunks(changed_ids):
            tsbs_ids.extend(
                (await session.exec(select(TotalScoreBestScore.score_id).where(col(TotalScoreBestScore.score_id).in_(batch)))).all()
            )
        logger.info(f"rescaling {len(tsbs_ids)} leaderboard rows...")
        for batch in _chunks(tsbs_ids):
            await session.execute(
                sa_update(TotalScoreBestScore),
                [{"score_id": s, "total_score": new_by_id[s]} for s in batch],
            )
        await session.commit()

        logger.info(f"refreshing statistics for {len(affected_users)} (user,mode)...")
        for uid, gamemode in affected_users:
            stat = (
                await session.exec(
                    select(UserStatistics).where(UserStatistics.user_id == uid, UserStatistics.mode == gamemode)
                )
            ).first()
            if stat is None:
                continue
            ranked_total = (
                await session.exec(
                    select(func.coalesce(func.sum(Score.total_score), 0))
                    .select_from(TotalScoreBestScore)
                    .join(Score, col(Score.id) == col(TotalScoreBestScore.score_id))
                    .where(
                        TotalScoreBestScore.user_id == uid,
                        TotalScoreBestScore.gamemode == gamemode,
                        col(Score.ranked).is_(True),
                        col(Score.passed).is_(True),
                    )
                )
            ).first()
            stat.ranked_score = int(ranked_total or 0)
            stat.total_score = max(0, stat.total_score + total_delta[(uid, gamemode)])
            stat.level_current = calculate_score_to_level(stat.total_score)
        await session.commit()
        logger.info("statistics refreshed")

        if queue_pp and pp_queue:
            await queue_pp_recalc(pp_queue)
            print(f"queued {len(pp_queue)} osu scores for pp recalc")

        print("\nDONE.")


def pp_can_apply_penalty(mods, base_cs, base_od) -> bool:
    """True if the low CS/OD pp penalty would bite (so the score's pp must be redone)."""
    if any(m.get("acronym") == "EZ" for m in mods):
        return False
    cs, od = _effective_cs_od(mods, base_cs, base_od)
    return cs < 2.5 or od < 4.0


async def queue_pp_recalc(score_ids: list[int]) -> None:
    """Push score ids onto the existing pp-recalc queue (drained by the background task)."""
    from app.dependencies.database import get_redis

    redis = get_redis()
    if redis is None:
        logger.warning("no redis; cannot queue pp recalc")
        return
    for batch in _chunks(score_ids):
        await redis.rpush("score:need_recalculate", *[str(s) for s in batch])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--queue-pp", action="store_true", help="queue low CS/OD osu scores for pp recalc")
    args = ap.parse_args()
    asyncio.run(run(apply=args.apply, queue_pp=args.queue_pp))


if __name__ == "__main__":
    main()
