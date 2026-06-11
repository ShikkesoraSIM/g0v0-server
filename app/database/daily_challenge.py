from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from app.models.model import UTCBaseModel
from app.utils import are_adjacent_weeks, are_same_weeks, utcnow

from sqlmodel import (
    BigInteger,
    Column,
    DateTime,
    Field,
    ForeignKey,
    Relationship,
    SQLModel,
    select,
)
from sqlmodel.ext.asyncio.session import AsyncSession

if TYPE_CHECKING:
    from .user import User


class DailyChallengeStatsBase(SQLModel, UTCBaseModel):
    daily_streak_best: int = Field(default=0)
    daily_streak_current: int = Field(default=0)
    last_update: datetime | None = Field(default=None, sa_column=Column(DateTime))
    last_day_streak: datetime | None = Field(default=None, sa_column=Column(DateTime), exclude=True)
    last_weekly_streak: datetime | None = Field(default=None, sa_column=Column(DateTime))
    playcount: int = Field(default=0)
    top_10p_placements: int = Field(default=0)
    top_50p_placements: int = Field(default=0)
    weekly_streak_best: int = Field(default=0)
    weekly_streak_current: int = Field(default=0)


class DailyChallengeStats(DailyChallengeStatsBase, table=True):
    __tablename__: str = "daily_challenge_stats"

    user_id: int | None = Field(
        default=None,
        sa_column=Column(
            BigInteger,
            ForeignKey("lazer_users.id"),
            unique=True,
            index=True,
            primary_key=True,
        ),
    )
    user: "User" = Relationship(back_populates="daily_challenge_stats")


class DailyChallengeStatsResp(DailyChallengeStatsBase):
    user_id: int

    @classmethod
    def from_db(
        cls,
        obj: DailyChallengeStats,
    ) -> "DailyChallengeStatsResp":
        stats = cls.model_validate(obj)
        stats.last_update = obj.last_day_streak
        return stats


async def process_daily_challenge_score(session: AsyncSession, user_id: int, room_id: int):
    from .playlist_best_score import PlaylistBestScore

    score = (
        await session.exec(
            select(PlaylistBestScore).where(
                PlaylistBestScore.user_id == user_id,
                PlaylistBestScore.room_id == room_id,
                PlaylistBestScore.playlist_id == 0,
            )
        )
    ).first()
    if not score or not score.score.passed:
        return
    stats = await session.get(DailyChallengeStats, user_id)
    if not stats:
        stats = DailyChallengeStats(user_id=user_id)
        session.add(stats)

    stats.playcount += 1
    now = utcnow()
    # Use last_day_streak (the real last-play timestamp, written only here) for
    # the streak/playcount logic, NOT last_update: the daily placement task
    # (process_daily_challenge_top) overwrites last_update with its own run time
    # for every user, which made this think "already played today" and undo the
    # playcount + freeze the daily streak.
    last_played = stats.last_day_streak
    if last_played is None:
        stats.daily_streak_best = 1
        stats.daily_streak_current = 1
    elif last_played.replace(tzinfo=UTC).date() == now.date() - timedelta(days=1):
        stats.daily_streak_current += 1
        if stats.daily_streak_current > stats.daily_streak_best:
            stats.daily_streak_best = stats.daily_streak_current
    elif last_played.replace(tzinfo=UTC).date() == now.date():
        # Already counted a play today; don't double-count.
        stats.playcount -= 1
    else:
        stats.daily_streak_current = 1
    if stats.last_weekly_streak is None:
        stats.weekly_streak_current = 1
        stats.weekly_streak_best = 1
    elif are_same_weeks(stats.last_weekly_streak.replace(tzinfo=UTC), now):
        # Already credited this week; extra plays in the same week don't bump it.
        pass
    elif are_adjacent_weeks(stats.last_weekly_streak, now):
        stats.weekly_streak_current += 1
        if stats.weekly_streak_current > stats.weekly_streak_best:
            stats.weekly_streak_best = stats.weekly_streak_current
    else:
        stats.weekly_streak_current = 1
    stats.last_update = now
    stats.last_day_streak = now
    stats.last_weekly_streak = now

    # Torii points: completing the daily challenge pays once per (UTC) day. The
    # date-stamped idempotency key makes re-submissions on the same day a no-op.
    from app.models.torii_points import POINTS_DAILY_CHALLENGE, PointReason
    from app.service.points_service import award

    await award(
        session,
        user_id,
        POINTS_DAILY_CHALLENGE,
        PointReason.DAILY_CHALLENGE,
        ref=str(room_id),
        idempotency_key=f"daily_challenge:{user_id}:{now.date().isoformat()}",
    )
