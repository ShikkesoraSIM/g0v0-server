"""Torii points economy — award / spend / balance.

Earn-only economy (no buying points, ever). Every change is written to the
torii_point_transactions ledger (the source of truth); lazer_users.points is a
cached running balance kept in sync here so reads are a single column.

Idempotency: awards that must fire at most once pass an ``idempotency_key``. We
pre-check the ledger for that key and skip if it's already there. The key column
is indexed but NOT unique on purpose — a hard DB constraint could turn a rare
race into an IntegrityError that poisons the surrounding transaction (score /
medal processing), which is far worse than the occasional duplicate it would
prevent. The one money-like flow that truly must not double (access-code
redemption) gets its hard guarantee from torii_access_code_redemptions instead.

All functions operate on the CALLER's session and do NOT commit — the caller's
existing commit boundary persists the points change atomically with whatever
triggered it. award() never raises on the points side; failures are logged and
swallowed so a points hiccup can't break score submission.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.database.torii_points import ToriiPointTransaction
from app.log import log
from app.models.torii_points import (
    POINTS_DAILY_PLAY,
    POINTS_DAILY_STREAK_MAX,
    POINTS_DAILY_STREAK_STEP,
    PointReason,
)
from app.utils import utcnow

from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

logger = log("Points")


def _start_of_utc_day() -> datetime:
    # Naive UTC midnight — matches how DateTime columns round-trip on MySQL.
    return utcnow().replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)


async def get_balance(session: AsyncSession, user_id: int) -> int:
    from app.database.user import User

    user = await session.get(User, user_id)
    return int(user.points) if user and user.points else 0


async def award(
    session: AsyncSession,
    user_id: int,
    amount: int,
    reason: str | PointReason,
    *,
    ref: str | None = None,
    idempotency_key: str | None = None,
) -> bool:
    """Credit ``amount`` points. Returns True if applied, False if skipped
    (non-positive amount, unknown user, or this idempotency_key already seen).
    Adds to the session only; the caller commits."""
    from app.database.user import User

    try:
        if amount <= 0:
            return False
        if idempotency_key is not None:
            seen = (
                await session.exec(
                    select(ToriiPointTransaction.id).where(
                        ToriiPointTransaction.idempotency_key == idempotency_key
                    )
                )
            ).first()
            if seen is not None:
                return False
        user = await session.get(User, user_id)
        if user is None:
            return False
        new_balance = (user.points or 0) + amount
        user.points = new_balance
        session.add(
            ToriiPointTransaction(
                user_id=user_id,
                amount=amount,
                reason=str(reason),
                ref=ref,
                idempotency_key=idempotency_key,
                balance_after=new_balance,
            )
        )
        logger.info(
            "Awarded {amount} points to user {user_id} | reason={reason} ref={ref}",
            amount=amount,
            user_id=user_id,
            reason=str(reason),
            ref=ref,
        )
        return True
    except Exception as e:  # never let a points hiccup break the caller
        logger.warning(
            "Points award failed (user={user_id} reason={reason}): {err}",
            user_id=user_id,
            reason=str(reason),
            err=str(e),
        )
        return False


async def spend(
    session: AsyncSession,
    user_id: int,
    amount: int,
    reason: str | PointReason,
    *,
    ref: str | None = None,
) -> bool:
    """Debit ``amount`` points if the balance covers it. Returns True on
    success, False if insufficient balance / unknown user / bad amount. Adds to
    the session only; the caller commits."""
    from app.database.user import User

    if amount <= 0:
        return False
    user = await session.get(User, user_id)
    if user is None or (user.points or 0) < amount:
        return False
    new_balance = (user.points or 0) - amount
    user.points = new_balance
    session.add(
        ToriiPointTransaction(
            user_id=user_id,
            amount=-amount,
            reason=str(reason),
            ref=ref,
            balance_after=new_balance,
        )
    )
    logger.info(
        "Spent {amount} points for user {user_id} | reason={reason} ref={ref}",
        amount=amount,
        user_id=user_id,
        reason=str(reason),
        ref=ref,
    )
    return True


async def count_today_awards(session: AsyncSession, user_id: int, reason: str | PointReason) -> int:
    """How many positive awards of ``reason`` the user already earned today
    (UTC). Used for per-day caps (e.g. top plays)."""
    start = _start_of_utc_day()
    return (
        await session.exec(
            select(func.count())
            .select_from(ToriiPointTransaction)
            .where(
                ToriiPointTransaction.user_id == user_id,
                ToriiPointTransaction.reason == str(reason),
                ToriiPointTransaction.amount > 0,
                ToriiPointTransaction.created_at >= start,
            )
        )
    ).one()


async def sum_today_awards(session: AsyncSession, user_id: int, reason: str | PointReason) -> int:
    """Total positive points of ``reason`` the user earned today (UTC). Used for
    per-day point ceilings (e.g. the top-play cap)."""
    start = _start_of_utc_day()
    total = (
        await session.exec(
            select(func.coalesce(func.sum(ToriiPointTransaction.amount), 0)).where(
                ToriiPointTransaction.user_id == user_id,
                ToriiPointTransaction.reason == str(reason),
                ToriiPointTransaction.amount > 0,
                ToriiPointTransaction.created_at >= start,
            )
        )
    ).one()
    return int(total or 0)


async def award_top_play(
    session: AsyncSession,
    user_id: int,
    existing_top_plays: int,
    pp_gained: int,
    score_id: int,
) -> bool:
    """Award a scaled new-top-play reward: base + veteran bonus + the pp this play
    added, tiered by your existing top-play count and capped per day. The
    breakdown is stored in the ledger ref (``score:ID|b:..|v:..|pp:..``) so the
    client can show the calc in its toast."""
    from app.models.torii_points import TOP_PLAY_DAILY_POINTS_CAP, top_play_breakdown

    if await sum_today_awards(session, user_id, PointReason.TOP_PLAY) >= TOP_PLAY_DAILY_POINTS_CAP:
        return False

    base, veteran, pp_bonus = top_play_breakdown(existing_top_plays, pp_gained)
    total = base + veteran + pp_bonus
    if total <= 0:
        return False

    return await award(
        session,
        user_id,
        total,
        PointReason.TOP_PLAY,
        ref=f"score:{score_id}|b:{base}|v:{veteran}|pp:{pp_bonus}",
        idempotency_key=f"top_play:{score_id}",
    )


async def award_daily_play(session: AsyncSession, user_id: int) -> bool:
    """First ranked play of the (UTC) day: base + consecutive-day streak bonus.

    The streak length lives in the previous day's ledger row ``ref`` as
    ``streak:N`` — we read yesterday's row to continue the count, so no separate
    streak table is needed."""
    today = utcnow().date()
    today_key = f"{PointReason.DAILY_PLAY}:{user_id}:{today.isoformat()}"

    seen = (
        await session.exec(
            select(ToriiPointTransaction.id).where(ToriiPointTransaction.idempotency_key == today_key)
        )
    ).first()
    if seen is not None:
        return False

    yest_key = f"{PointReason.DAILY_PLAY}:{user_id}:{(today - timedelta(days=1)).isoformat()}"
    yest_ref = (
        await session.exec(
            select(ToriiPointTransaction.ref).where(ToriiPointTransaction.idempotency_key == yest_key)
        )
    ).first()
    streak = 1
    if isinstance(yest_ref, str) and yest_ref.startswith("streak:"):
        try:
            streak = int(yest_ref.split(":", 1)[1]) + 1
        except ValueError:
            streak = 1

    bonus = min((streak - 1) * POINTS_DAILY_STREAK_STEP, POINTS_DAILY_STREAK_MAX)
    return await award(
        session,
        user_id,
        POINTS_DAILY_PLAY + bonus,
        PointReason.DAILY_PLAY,
        ref=f"streak:{streak}",
        idempotency_key=today_key,
    )
