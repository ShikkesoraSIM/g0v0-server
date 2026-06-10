from datetime import timedelta
import math
from typing import TYPE_CHECKING, ClassVar, NotRequired, TypedDict
from weakref import WeakKeyDictionary

from app.models.score import GameMode
from app.utils import utcnow

from ._base import DatabaseModel, included, ondemand
from .rank_history import RankHistory

from pydantic import field_validator
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlmodel import (
    BigInteger,
    Column,
    Field,
    ForeignKey,
    Relationship,
    col,
    func,
    select,
)
from sqlmodel.ext.asyncio.session import AsyncSession

if TYPE_CHECKING:
    from .user import User, UserDict


class UserStatisticsDict(TypedDict):
    mode: GameMode
    count_100: int
    count_300: int
    count_50: int
    count_miss: int
    pp: float
    ranked_score: int
    hit_accuracy: float
    total_score: int
    total_hits: int
    maximum_combo: int
    play_count: int
    play_time: int
    replays_watched_by_others: int
    is_ranked: bool
    level: NotRequired[dict[str, int]]
    global_rank: NotRequired[int | None]
    global_rank_percent: NotRequired[float | None]
    grade_counts: NotRequired[dict[str, int]]
    rank_change_since_30_days: NotRequired[int]
    country_rank: NotRequired[int | None]
    user: NotRequired["UserDict"]


class UserStatisticsModel(DatabaseModel[UserStatisticsDict]):
    RANKING_INCLUDES: ClassVar[list[str]] = [
        "user.country",
        "user.cover",
        "user.team",
    ]

    mode: GameMode = Field(index=True)
    count_100: int = Field(default=0, sa_column=Column(BigInteger))
    count_300: int = Field(default=0, sa_column=Column(BigInteger))
    count_50: int = Field(default=0, sa_column=Column(BigInteger))
    count_miss: int = Field(default=0, sa_column=Column(BigInteger))

    pp: float = Field(default=0.0, index=True)
    ranked_score: int = Field(default=0, sa_column=Column(BigInteger))
    hit_accuracy: float = Field(default=0.00)
    total_score: int = Field(default=0, sa_column=Column(BigInteger))
    total_hits: int = Field(default=0, sa_column=Column(BigInteger))
    maximum_combo: int = Field(default=0)

    play_count: int = Field(default=0)
    play_time: int = Field(default=0, sa_column=Column(BigInteger))
    replays_watched_by_others: int = Field(default=0)
    is_ranked: bool = Field(default=True)

    @field_validator("mode", mode="before")
    @classmethod
    def validate_mode(cls, v):
        """将字符串转换为 GameMode 枚举"""
        if isinstance(v, str):
            try:
                return GameMode(v)
            except ValueError:
                # 如果转换失败，返回默认值
                return GameMode.OSU
        return v

    @included
    @staticmethod
    async def level(_session: AsyncSession, statistics: "UserStatistics") -> dict[str, int]:
        return {
            "current": int(statistics.level_current),
            "progress": int(math.fmod(statistics.level_current, 1) * 100),
        }

    @included
    @staticmethod
    async def global_rank(session: AsyncSession, statistics: "UserStatistics") -> int | None:
        return await get_rank(session, statistics)

    # Minimum virtual population for global_rank_percent so that
    # rank-tier colours spread out on small servers instead of
    # everyone landing in the same tier.
    _VIRTUAL_MIN_POPULATION: ClassVar[int] = 10_000

    @included
    @staticmethod
    async def global_rank_percent(session: AsyncSession, statistics: "UserStatistics") -> float | None:
        from .user import User

        rank = await get_rank(session, statistics)
        if rank is None:
            return None
        total = (
            await session.exec(
                select(func.count()).where(
                    UserStatistics.mode == statistics.mode,
                    UserStatistics.pp > 0,
                    col(UserStatistics.is_ranked).is_(True),
                    col(UserStatistics.user).has(is_active=True),
                    ~User.is_restricted_query(col(UserStatistics.user_id)),
                )
            )
        ).one()
        if total == 0:
            return None
        return rank / max(total, UserStatisticsModel._VIRTUAL_MIN_POPULATION)

    @included
    @staticmethod
    async def grade_counts(_session: AsyncSession, statistics: "UserStatistics") -> dict[str, int]:
        return {
            "ss": statistics.grade_ss,
            "ssh": statistics.grade_ssh,
            "s": statistics.grade_s,
            "sh": statistics.grade_sh,
            "a": statistics.grade_a,
        }

    @ondemand
    @staticmethod
    async def rank_change_since_30_days(session: AsyncSession, statistics: "UserStatistics") -> int:
        global_rank = await get_rank(session, statistics)
        rank_best = (
            await session.exec(
                select(func.max(RankHistory.rank)).where(
                    RankHistory.date > utcnow() - timedelta(days=30),
                    RankHistory.user_id == statistics.user_id,
                )
            )
        ).first()
        if rank_best is None or global_rank is None:
            return 0
        return rank_best - global_rank

    @ondemand
    @staticmethod
    async def country_rank(
        session: AsyncSession, statistics: "UserStatistics", user_country: str | None = None
    ) -> int | None:
        return await get_rank(session, statistics, user_country)

    @ondemand
    @staticmethod
    async def user(
        _session: AsyncSession,
        statistics: "UserStatistics",
        show_nsfw_media: bool = False,
    ) -> "UserDict":
        from .user import UserModel

        user_instance = await statistics.awaitable_attrs.user
        return await UserModel.transform(user_instance, show_nsfw_media=show_nsfw_media)


class UserStatistics(AsyncAttrs, UserStatisticsModel, table=True):
    __tablename__: str = "lazer_user_statistics"
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(
        default=None,
        sa_column=Column(
            BigInteger,
            ForeignKey("lazer_users.id"),
            index=True,
        ),
    )
    grade_ss: int = Field(default=0)
    grade_ssh: int = Field(default=0)
    grade_s: int = Field(default=0)
    grade_sh: int = Field(default=0)
    grade_a: int = Field(default=0)

    level_current: float = Field(default=1)

    user: "User" = Relationship(back_populates="statistics")


# Per-request memo for get_rank(). A single profile transform resolves the same
# global rank from several fields (global_rank, global_rank_percent,
# rank_change_since_30_days) and each one used to re-run the full ROW_NUMBER scan.
# Keyed weakly by the request's AsyncSession (so it clears when the session is
# dropped), then by (user_id, mode, country).
_rank_memo: "WeakKeyDictionary[AsyncSession, dict]" = WeakKeyDictionary()


async def get_rank(session: AsyncSession, statistics: UserStatistics, country: str | None = None) -> int | None:
    from .user import User

    cache_key = (statistics.user_id, statistics.mode, country)
    try:
        memo = _rank_memo.get(session)
        if memo is None:
            memo = _rank_memo[session] = {}
    except TypeError:
        # Session not weakly referenceable for some reason; just skip the memo.
        memo = None
    if memo is not None and cache_key in memo:
        return memo[cache_key]

    query = select(
        UserStatistics.user_id,
        func.row_number().over(order_by=col(UserStatistics.pp).desc()).label("rank"),
    ).where(
        UserStatistics.mode == statistics.mode,
        UserStatistics.pp > 0,
        col(UserStatistics.is_ranked).is_(True),
        col(UserStatistics.user).has(is_active=True),
        ~User.is_restricted_query(col(UserStatistics.user_id)),
    )

    if country is not None:
        query = query.join(User).where(User.country_code == country)

    subq = query.subquery()
    result = await session.exec(select(subq.c.rank).where(subq.c.user_id == statistics.user_id))

    rank = result.first()
    if rank is None:
        if memo is not None:
            memo[cache_key] = None
        return None

    if country is None:
        today = utcnow().date()
        # Daily snapshot for the profile rank graph. Insert it once per day; do
        # NOT update it on every call. get_rank() runs on hot read paths (profile
        # views, cache pre-warm) as well as score submits, so updating the shared
        # "today" row from all of them serialised concurrent callers on one hot
        # row and held its lock until each caller transaction committed, which is
        # the source of the rank_history 1205 lock-wait-timeouts on the busiest
        # users. The midnight batch (calculate_user_rank) finalises today's value;
        # the live rank shown elsewhere is computed fresh above.
        already = (
            await session.exec(
                select(RankHistory.id).where(
                    RankHistory.user_id == statistics.user_id,
                    RankHistory.mode == statistics.mode,
                    RankHistory.date == today,
                )
            )
        ).first()
        if already is None:
            session.add(
                RankHistory(
                    user_id=statistics.user_id,
                    mode=statistics.mode,
                    date=today,
                    rank=rank,
                )
            )
    if memo is not None:
        memo[cache_key] = rank
    return rank
