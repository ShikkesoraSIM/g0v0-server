"""Per-user score summary helpers consumed by the detection service.

Provides per-user aggregates and recent-activity counts used downstream.
The actual interpretation of these numbers lives outside this module.

All errors degrade to a neutral empty struct rather than raising — the
caller must never block on this computation.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlmodel import col, func, select

from app.database.score import Score
from app.log import log

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

logger = log("BehavioralProfile")

DEFAULT_BASELINE_SAMPLE_SIZE = 50


@dataclass(slots=True)
class BehavioralProfile:
    sample_size: int = 0
    avg_accuracy: float = 0.0
    std_accuracy: float = 0.0
    avg_pp: float = 0.0
    std_pp: float = 0.0
    max_pp_ever: float = 0.0
    avg_max_combo: float = 0.0
    avg_total_score: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


async def compute_user_baseline(
    session: "AsyncSession",
    *,
    user_id: int,
    gamemode: int,
    exclude_score_id: int | None = None,
    sample_size: int = DEFAULT_BASELINE_SAMPLE_SIZE,
) -> BehavioralProfile:
    """Return the per-gamemode stats of this user's recent passing scores.

    `exclude_score_id` lets the caller skip the score that triggered the
    check, so the baseline represents prior history rather than the
    score currently under review.
    """
    profile = BehavioralProfile()
    try:
        stmt = (
            select(Score)
            .where(Score.user_id == user_id)
            .where(Score.gamemode == gamemode)
            .where(Score.passed == True)  # noqa: E712
            .order_by(col(Score.ended_at).desc())
            .limit(sample_size)
        )
        if exclude_score_id is not None:
            stmt = stmt.where(Score.id != exclude_score_id)
        rows = (await session.exec(stmt)).all()
        if not rows:
            return profile

        accuracies = [float(s.accuracy or 0.0) for s in rows]
        pps = [float(s.pp or 0.0) for s in rows]
        combos = [float(s.max_combo or 0) for s in rows]
        scores = [float(s.total_score or 0) for s in rows]

        profile.sample_size = len(rows)
        profile.avg_accuracy = sum(accuracies) / len(accuracies)
        profile.std_accuracy = statistics.pstdev(accuracies) if len(accuracies) > 1 else 0.0
        profile.avg_pp = sum(pps) / len(pps)
        profile.std_pp = statistics.pstdev(pps) if len(pps) > 1 else 0.0
        profile.max_pp_ever = max(pps) if pps else 0.0
        profile.avg_max_combo = sum(combos) / len(combos)
        profile.avg_total_score = sum(scores) / len(scores)
    except Exception as e:
        logger.debug(f"baseline compute failed for user {user_id} mode {gamemode}: {e}")
    return profile


@dataclass(slots=True)
class SubmissionVelocity:
    """Recent score submission counts across several time windows."""

    scores_last_60s: int = 0
    scores_last_5min: int = 0
    scores_last_1h: int = 0
    scores_last_24h: int = 0
    distinct_beatmaps_last_1h: int = 0
    same_beatmap_repeats_last_1h: int = 0
    same_beatmap_id: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


async def compute_submission_velocity(
    session: "AsyncSession",
    *,
    user_id: int,
    beatmap_id: int | None = None,
) -> SubmissionVelocity:
    """Look at the user's recent submissions and return rate metrics.

    All four windows hit the same (user_id, ended_at desc) prefix on
    the score index, so the cost is one indexed range scan + four
    cheap COUNT aggregations. Failures degrade to a zeroed-out struct
    so the anti-cheat path never blocks on this.
    """
    vel = SubmissionVelocity(same_beatmap_id=beatmap_id)
    try:
        now = datetime.now(timezone.utc)
        windows = {
            "scores_last_60s": now - timedelta(seconds=60),
            "scores_last_5min": now - timedelta(minutes=5),
            "scores_last_1h": now - timedelta(hours=1),
            "scores_last_24h": now - timedelta(hours=24),
        }
        for field_name, cutoff in windows.items():
            stmt = (
                select(func.count())
                .select_from(Score)
                .where(Score.user_id == user_id)
                .where(Score.ended_at >= cutoff)
            )
            setattr(vel, field_name, int((await session.exec(stmt)).first() or 0))

        # Distinct beatmaps + same-beatmap repeats inside the last hour
        one_hour_ago = now - timedelta(hours=1)
        distinct_stmt = (
            select(func.count(func.distinct(Score.beatmap_id)))
            .where(Score.user_id == user_id)
            .where(Score.ended_at >= one_hour_ago)
        )
        vel.distinct_beatmaps_last_1h = int((await session.exec(distinct_stmt)).first() or 0)
        if beatmap_id is not None:
            same_stmt = (
                select(func.count())
                .select_from(Score)
                .where(Score.user_id == user_id)
                .where(Score.beatmap_id == beatmap_id)
                .where(Score.ended_at >= one_hour_ago)
            )
            vel.same_beatmap_repeats_last_1h = int((await session.exec(same_stmt)).first() or 0)
    except Exception as e:
        logger.debug(f"velocity compute failed for user {user_id}: {e}")
    return vel
