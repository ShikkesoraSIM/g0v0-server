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
    try:
        if amount <= 0:
            return False
        # Lock the user row first so concurrent award / spend for the same user
        # serialise; the idempotency pre-check below then reliably sees a prior
        # award's committed row instead of racing it (the key index is non-unique).
        user = await _lock_user(session, user_id)
        if user is None:
            return False
        if idempotency_key is not None:
            # LOCKING read (FOR UPDATE), not a plain select: under MySQL REPEATABLE
            # READ a plain read uses the transaction's stale snapshot, so two
            # concurrent same-key awards (serialised by the user-row lock above)
            # could BOTH miss each other's committed row and double-pay. A locking
            # read always sees the latest committed row, so the second one is
            # correctly skipped.
            seen = (
                await session.exec(
                    select(ToriiPointTransaction.id)
                    .where(ToriiPointTransaction.idempotency_key == idempotency_key)
                    .with_for_update()
                )
            ).first()
            if seen is not None:
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
    if amount <= 0:
        return False
    user = await _lock_user(session, user_id)
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


async def _lock_user(session: AsyncSession, user_id: int):
    """Row-lock the user (SELECT ... FOR UPDATE) so concurrent award / spend for
    the same user serialise within their transactions. Returns the user or None."""
    from app.database.user import User

    return (await session.exec(select(User).where(User.id == user_id).with_for_update())).first()


async def award_top_play(
    session: AsyncSession,
    user_id: int,
    rank: int,
    existing_top_plays: int,
    account_pp_delta: int,
    score_id: int,
    gamemode=None,
) -> bool:
    """Award a new-top-play reward scaled by the play's RANK among the user's best
    plays + their tenure, plus the pp it added to the account total, reduced for
    relax/autopilot modes. The breakdown is stored in the ledger ref
    (``score:ID|rank:R|b:..|pp:..``) so the client can show the calc + the rank."""
    from app.models.torii_points import (
        TOP_PLAY_DAILY_HARD_CAP,
        TOP_PLAY_DAILY_POINTS_CAP,
        earn_multiplier,
        top_play_award,
    )

    # Hold the user row lock across the cap check + award so concurrent top plays
    # serialise and read a consistent daily total.
    if await _lock_user(session, user_id) is None:
        return False

    today = await sum_today_awards(session, user_id, PointReason.TOP_PLAY)

    # Absolute daily ceiling: nothing more today (closes the soft-cap pp tail).
    if today >= TOP_PLAY_DAILY_HARD_CAP:
        return False

    base, pp_bonus = top_play_award(rank, existing_top_plays, account_pp_delta)

    # Relax/autopilot inflate pp cheaply, so scale the components down (kept on the
    # components, not just the total, so the ref/toast breakdown stays honest).
    mult = earn_multiplier(gamemode)
    base = int(round(base * mult))
    pp_bonus = int(round(pp_bonus * mult))

    # Soft cap: once past it, keep paying the pp this play added (never a flat zero)
    # but drop the rank/base bonus. Mark it (capped:1) so the client can show
    # "you hit today's limit".
    capped = today >= TOP_PLAY_DAILY_POINTS_CAP
    if capped:
        base = 0
        total = pp_bonus
    else:
        total = base + pp_bonus

    # Never overshoot the hard ceiling.
    total = min(total, TOP_PLAY_DAILY_HARD_CAP - today)
    if total <= 0:
        return False

    ref = f"score:{score_id}|rank:{rank}|b:{base}|pp:{pp_bonus}"
    if capped:
        ref += "|capped:1"

    return await award(
        session,
        user_id,
        total,
        PointReason.TOP_PLAY,
        ref=ref,
        idempotency_key=f"top_play:{score_id}",
    )


async def award_pp_milestones(session: AsyncSession, user_id: int, mode, total_pp: float) -> None:
    """One-time pp milestones. Each threshold pays once ever (idempotency-keyed
    without mode, so it fires on your first time reaching it in any mode), gated
    by >=500 best plays in the reaching mode so fresh accounts can't trip them."""
    from app.models.torii_points import PP_MILESTONES, earn_multiplier

    reached = [thr for thr in PP_MILESTONES if total_pp >= thr]
    if not reached:
        return

    from app.database.score import BestScore

    best_count = (
        await session.exec(
            select(func.count()).select_from(BestScore).where(
                BestScore.user_id == user_id,
                BestScore.gamemode == mode,
            )
        )
    ).one()
    if best_count < 500:
        return

    mult = earn_multiplier(mode)
    for thr in reached:
        await award(
            session,
            user_id,
            max(1, int(round(PP_MILESTONES[thr] * mult))),
            PointReason.MILESTONE,
            ref=f"pp:{thr}",
            idempotency_key=f"pp_milestone:{user_id}:{thr}",
        )


async def award_daily_play(session: AsyncSession, user_id: int, gamemode=None) -> bool:
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

    # Relax/autopilot earn less.
    from app.models.torii_points import earn_multiplier

    amount = max(1, int(round((POINTS_DAILY_PLAY + bonus) * earn_multiplier(gamemode))))
    return await award(
        session,
        user_id,
        amount,
        PointReason.DAILY_PLAY,
        ref=f"streak:{streak}",
        idempotency_key=today_key,
    )
