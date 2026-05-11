"""Batch backfill: classify existing TD-tagged osu! replays.

Background. The TD mod ("Touch Device") gets auto-applied whenever the
client detects a touchscreen input device, and carries a flat pp
penalty. The penalty is justified for drag-and-side-tap "cheese" play
but unjustified for discrete-tap (stylus / finger) play.

The classifier in osu-performance-server replays each .osr against its
beatmap and emits one of {tap, drag, mixed, unknown}. When it says
``tap``, the pp pipeline strips TD from the calculator input on every
subsequent pp computation for that score — the "FairTouchScreen"
outcome — without the user having had to play on a specific client.

This script iterates every osu! score with TD mod + replay file +
``td_play_style = 0`` (still Unknown), calls the classifier, persists
the verdict and confidence on the score row, and then triggers a one-
shot pp recalc for that score iff the verdict actually shifts the
penalty (Unknown→Tap, or Tap→anything-else).

Usage (typically once after deploy of the migration + perf-server build):

    python tools/classify_touchscreen.py
    python tools/classify_touchscreen.py --dry-run
    python tools/classify_touchscreen.py --limit 50 --concurrency 4

The script is idempotent: re-running picks up only rows still at
``td_play_style = 0`` (use ``--force`` to reclassify everything).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
from collections.abc import Iterable
from pathlib import Path

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.calculator import calculate_pp, get_calculator, init_calculator
from app.calculators.performance.performance_server import (
    PerformanceServerPerformanceCalculator,
    TD_PLAY_STYLE_TAP,
    td_play_style_from_wire,
)
from app.config import settings
from app.database.score import Score
from app.dependencies.database import engine, get_redis
from app.dependencies.fetcher import get_fetcher
from app.dependencies.storage import get_storage_service
from app.log import log
from app.models.score import GameMode

from sqlalchemy import text
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession


logger = log("classify-touchscreen")


async def _select_candidate_score_ids(
    session: AsyncSession,
    force: bool,
    limit: int | None,
) -> list[int]:
    """Find osu! score IDs eligible for classification.

    Eligible = osu! ruleset, has_replay=1, ranked=1, mods JSON contains a
    TD entry (acronym match), and — unless ``force`` is set — currently
    sits at td_play_style=0 (no verdict yet).
    """
    # JSON_SEARCH on the MySQL side avoids loading every score into Python
    # just to filter for TD. The acronym match is exact-cased; APIMod
    # always serialises acronyms uppercase so this is the right shape.
    where_clauses = [
        "gamemode = 'OSU'",
        "has_replay = 1",
        "ranked = 1",
        "JSON_SEARCH(mods, 'one', 'TD') IS NOT NULL",
    ]
    if not force:
        where_clauses.append("(td_play_style IS NULL OR td_play_style = 0)")

    sql = f"SELECT id FROM scores WHERE {' AND '.join(where_clauses)} ORDER BY id"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    result = await session.exec(text(sql))
    return [row[0] for row in result.all()]


async def _classify_one(
    score_id: int,
    calculator: PerformanceServerPerformanceCalculator,
    dry_run: bool,
) -> tuple[int, str | None]:
    """Run one classification end-to-end. Returns (score_id, error_msg)
    where ``error_msg`` is None on success."""
    # get_redis() is sync (returns the module-level Redis client);
    # get_fetcher() is async (warms the OAuth token on first call);
    # get_storage_service() is sync. Don't await the wrong ones — the
    # type errors will surface as AttributeErrors deep inside the call
    # stack when each is used.
    storage = get_storage_service()
    fetcher = await get_fetcher()
    redis = get_redis()

    # expire_on_commit=False prevents the post-commit attribute expiration
    # that would otherwise force a re-fetch of every column the moment we
    # touch it for logging. The async re-fetch path can land outside the
    # greenlet that the original session was running in (the recalc step
    # detours through HTTP and back), and a re-fetch there raises
    # MissingGreenlet. None of this script needs to see fresh column values
    # after commit anyway.
    async with AsyncSession(engine, expire_on_commit=False) as session:
        score = await session.get(Score, score_id)
        if score is None:
            return score_id, "score row vanished"
        if not score.has_replay:
            return score_id, "has_replay=0 (post-filter); skipping"

        replay_path = score.replay_filename
        try:
            replay_bytes = await storage.read_file(replay_path)
        except FileNotFoundError:
            return score_id, f"replay file missing at {replay_path}"
        except Exception as exc:  # noqa: BLE001 — storage layer can raise anything
            return score_id, f"storage read failed: {exc}"

        beatmap_raw = await fetcher.get_or_fetch_beatmap_raw(redis, score.beatmap_id)
        if not beatmap_raw:
            return score_id, f"no beatmap_raw for bm {score.beatmap_id}"

        try:
            result = await calculator.classify_touchscreen(
                replay_bytes=replay_bytes,
                beatmap_raw=beatmap_raw,
                beatmap_id=score.beatmap_id,
                score_id=score.id,
            )
        except Exception as exc:  # noqa: BLE001 — perf server failure modes vary
            return score_id, f"classifier call failed: {exc}"

        new_style = td_play_style_from_wire(result["style"])
        prev_style = int(score.td_play_style or 0)

        logger.info(
            "score={sid} bm={bm} -> {style} (conf {conf:.2f}, prev={prev}, "
            "stationary={stat:.2f} moving={mov:.2f} inflation={infl:.2f} intervals={int_})",
            sid=score.id,
            bm=score.beatmap_id,
            style=result["style"],
            conf=result["confidence"],
            prev=prev_style,
            stat=result["metrics"].get("stationary_ratio", 0.0),
            mov=result["metrics"].get("moving_ratio", 0.0),
            infl=result["metrics"].get("path_inflation", 0.0),
            int_=int(result["metrics"].get("intervals_analysed", 0)),
        )

        if dry_run:
            return score_id, None

        score.td_play_style = new_style
        score.td_classification_confidence = result["confidence"]
        await session.commit()
        await session.refresh(score)

        # Re-run pp iff the verdict actually toggles the TD bypass.
        # Unknown↔Drag↔Mixed transitions don't affect pp (TD penalty
        # remains applied for all of those), so skip the recalc — saves
        # one calculate_pp call per non-Tap classification, which is the
        # common case (~70% of replays based on early sampling).
        if new_style != prev_style and (new_style == TD_PLAY_STYLE_TAP or prev_style == TD_PLAY_STYLE_TAP):
            try:
                new_pp = await calculate_pp(score, beatmap_raw, session)
                score.pp = new_pp
                await session.commit()
                logger.info(
                    "score={sid} pp recomputed: {pp:.2f} (verdict flipped {prev}→{new})",
                    sid=score.id, pp=new_pp, prev=prev_style, new=new_style,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "score={sid} pp recalc failed after verdict flip: {exc}",
                    sid=score.id, exc=exc,
                )

        return score_id, None


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Cap the number of scores processed in this run (default: no cap).",
    )
    parser.add_argument(
        "--concurrency", type=int, default=4,
        help="Number of replays to classify in parallel. The perf server "
             "handles concurrent classify calls fine; the limit is mostly "
             "to avoid hammering the .osu cache backend.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Reclassify scores that already have a verdict. Use after a "
             "threshold change in TouchScreenClassifierConfig.cs.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run the classifier but don't persist results or trigger pp "
             "recalc. Useful for measuring per-replay timing and surveying "
             "the verdict distribution before committing.",
    )
    args = parser.parse_args()

    await init_calculator()

    # get_calculator returns the module-level singleton populated by
    # init_calculator above. settings.performance_calculator isn't a
    # thing — settings.calculator is the string module name, not the
    # instance.
    calculator = get_calculator()
    if not isinstance(calculator, PerformanceServerPerformanceCalculator):
        logger.error(
            "Active performance calculator is {kind}; need "
            "PerformanceServerPerformanceCalculator. Refusing to run.",
            kind=type(calculator).__name__,
        )
        sys.exit(2)

    async with AsyncSession(engine) as session:
        score_ids = await _select_candidate_score_ids(session, args.force, args.limit)

    logger.info(
        "Found {n} candidate TD osu! score(s) to classify (force={force}, dry_run={dry}).",
        n=len(score_ids), force=args.force, dry=args.dry_run,
    )
    if not score_ids:
        return

    sem = asyncio.Semaphore(max(1, args.concurrency))
    ok = 0
    failed: list[tuple[int, str]] = []

    async def _worker(sid: int) -> None:
        nonlocal ok
        async with sem:
            _, err = await _classify_one(sid, calculator, args.dry_run)
            if err is None:
                ok += 1
            else:
                failed.append((sid, err))

    await asyncio.gather(*(_worker(sid) for sid in score_ids))

    logger.info("Done. {ok}/{total} succeeded.", ok=ok, total=len(score_ids))
    if failed:
        logger.warning("Failures:")
        for sid, err in failed:
            logger.warning("  score={sid}: {err}", sid=sid, err=err)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
