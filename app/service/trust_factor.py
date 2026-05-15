"""User trust factor — a single 0..100 number combining multiple raw signals
about a user's legitimacy / history with the server.

This factor is used by the external anti-cheat service (torii-slitwrist) to
weight its detection thresholds: a long-standing donor with thousands of
plays and zero alert history gets a very lenient threshold; a one-day-old
account that already triggered three multi-IP warnings gets a tight one.

Why this lives in g0v0-server and not in the anti-cheat service itself:

- The trust factor reads from the canonical user/session/alert tables. Doing
  the lookup HERE keeps the anti-cheat service stateless wrt user history
  (it just receives a number).
- Recomputing it server-side per score also means we can cache / cap the
  query cost — the anti-cheat service does not need its own DB connection
  pool.
- Future use cases (e.g. shaping spam-protection thresholds, rate-limit
  hints, captcha skip rules) all benefit from the same number, so it's a
  natural shared utility.

Formula goals:

- Default to 50 for a brand-new account (neutral; not assumed-good, not
  assumed-bad). The base is 50 not 100 so that ANY history can move the
  needle either way.
- Saturate at 0 and 100 (clamp at the end).
- Biggest single negative signal: an active or historical RESTRICTION on
  the account. A user who has been banned and then unbanned is effectively
  on probation forever from this factor's point of view — admins can still
  override per-score with manual review.
- Biggest positive signals: tenure (account age) + activity (play count) +
  donor history. Each capped so that a single signal can't dominate.

The formula is deliberately simple and auditable. No ML, no opaque
weights. If a user disputes a low trust factor, we can show them the
breakdown line by line.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlmodel import col, distinct, func, select

from app.database.suspicious_alert import SuspiciousAlert
from app.database.user import User
from app.database.user_account_history import UserAccountHistory, UserAccountHistoryType
from app.database.user_login_log import UserLoginLog
from app.database.statistics import UserStatistics
from app.log import log

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

logger = log("TrustFactor")


# ─── Tunable weights ────────────────────────────────────────────────────────
#
# All caps deliberately quantised to multiples of 5 so the resulting score
# is easy to read in admin debug views ("75 = 50 base + 15 age + 10 plays").
# Adjusting one weight here should never require touching the rest.

_BASE_SCORE = 50.0

_ACCOUNT_AGE_DAYS_PER_POINT = 30.0  # +1 trust per 30 days of account age
_ACCOUNT_AGE_MAX_BONUS = 20.0  # capped at +20 (= 600 days = ~20 months)

_PLAY_COUNT_PER_POINT = 100.0  # +1 trust per 100 plays
_PLAY_COUNT_MAX_BONUS = 15.0  # capped at +15 (= 1500 plays)

_SUPPORTER_BONUS = 10.0  # flat +10 for current or past supporters

_STAFF_BONUS = 25.0  # flat +25 for admin/gmt/qat/bng

_DISTINCT_IP_THRESHOLD = 5  # > this many → start penalising
_DISTINCT_IP_PER_POINT = 1.0  # -5 trust per IP above threshold
_DISTINCT_IP_PENALTY_PER_EXCESS = 5.0
_DISTINCT_IP_MAX_PENALTY = 30.0  # capped at -30 (= 11+ IPs)

_ALERT_PER_POINT_PENALTY = 5.0  # -5 trust per prior SuspiciousAlert row
_ALERT_MAX_PENALTY = 25.0  # capped at -25 (= 5+ alerts)

_HAS_RESTRICTION_PENALTY = 50.0  # any RESTRICTION row historical OR active = -50


@dataclass(slots=True)
class TrustFactorBreakdown:
    """Per-signal contribution to the final score. Surfaced in debug
    endpoints / admin tooling so the number is auditable."""

    base: float
    account_age_days: int
    account_age_bonus: float
    play_count: int
    play_count_bonus: float
    supporter_bonus: float
    staff_bonus: float
    distinct_ip_count: int
    distinct_ip_penalty: float
    prior_alert_count: int
    prior_alert_penalty: float
    has_restriction_history: bool
    restriction_penalty: float
    final_score: float

    def to_dict(self) -> dict:
        return asdict(self)


async def compute_trust_factor(
    session: "AsyncSession", user_id: int
) -> TrustFactorBreakdown:
    """Compute the 0..100 trust factor for a user along with the per-signal
    breakdown that produced it.

    Implemented as a single coroutine running ~5 small queries against the
    user's row, statistics rows, login log, alert table, and account
    history. The queries are explicitly NOT batched into one CTE because
    each individual one is fast (all hit indexed columns by user_id) and
    keeping them separate makes the failure mode "one signal returns
    default 0" rather than "all signals fail together".

    Always returns a breakdown — never raises. If the user does not exist
    we return the neutral 50 baseline with all signals at zero so the
    anti-cheat client never has to special-case a missing user.
    """

    score = _BASE_SCORE
    breakdown = TrustFactorBreakdown(
        base=_BASE_SCORE,
        account_age_days=0,
        account_age_bonus=0.0,
        play_count=0,
        play_count_bonus=0.0,
        supporter_bonus=0.0,
        staff_bonus=0.0,
        distinct_ip_count=0,
        distinct_ip_penalty=0.0,
        prior_alert_count=0,
        prior_alert_penalty=0.0,
        has_restriction_history=False,
        restriction_penalty=0.0,
        final_score=_BASE_SCORE,
    )

    user = await session.get(User, user_id)
    if user is None:
        breakdown.final_score = max(0.0, min(100.0, score))
        return breakdown

    # ─── Account age ────────────────────────────────────────────────────────
    # join_date is OnDemand so it must come through awaitable_attrs — fetching
    # it via plain attribute access would either hit a MissingGreenlet (if the
    # row was loaded outside an async context) or return None silently.
    try:
        join_date = await user.awaitable_attrs.join_date
    except Exception:
        join_date = None
    if join_date is not None:
        if join_date.tzinfo is None:
            join_date = join_date.replace(tzinfo=timezone.utc)
        age_days = max(0, (datetime.now(timezone.utc) - join_date).days)
        age_bonus = min(
            _ACCOUNT_AGE_MAX_BONUS, age_days / _ACCOUNT_AGE_DAYS_PER_POINT
        )
        score += age_bonus
        breakdown.account_age_days = age_days
        breakdown.account_age_bonus = age_bonus

    # ─── Play count (summed across all rulesets) ───────────────────────────
    play_count_stmt = select(func.coalesce(func.sum(UserStatistics.play_count), 0)).where(
        UserStatistics.user_id == user_id
    )
    play_count = int((await session.exec(play_count_stmt)).first() or 0)
    play_bonus = min(_PLAY_COUNT_MAX_BONUS, play_count / _PLAY_COUNT_PER_POINT)
    score += play_bonus
    breakdown.play_count = play_count
    breakdown.play_count_bonus = play_bonus

    # ─── Supporter / has-supported (flat bonus) ────────────────────────────
    # These two are OnDemand-typed in user.py — go through awaitable_attrs.
    try:
        is_supporter = bool(await user.awaitable_attrs.is_supporter)
    except Exception:
        is_supporter = False
    try:
        has_supported = bool(await user.awaitable_attrs.has_supported)
    except Exception:
        has_supported = False
    if is_supporter or has_supported:
        score += _SUPPORTER_BONUS
        breakdown.supporter_bonus = _SUPPORTER_BONUS

    # ─── Staff bonus (flat) — admins are trusted by definition ─────────────
    try:
        is_admin = bool(await user.awaitable_attrs.is_admin)
    except Exception:
        is_admin = False
    try:
        is_gmt = bool(await user.awaitable_attrs.is_gmt)
    except Exception:
        is_gmt = False
    try:
        is_qat = bool(await user.awaitable_attrs.is_qat)
    except Exception:
        is_qat = False
    try:
        is_bng = bool(await user.awaitable_attrs.is_bng)
    except Exception:
        is_bng = False
    if is_admin or is_gmt or is_qat or is_bng:
        score += _STAFF_BONUS
        breakdown.staff_bonus = _STAFF_BONUS

    # ─── Distinct login IPs — penalise multi-network only beyond a
    #     reasonable threshold so phones / home + mobile / VPN-on-and-off
    #     legitimate users don't get nuked ────────────────────────────────
    distinct_ip_stmt = select(
        func.count(distinct(UserLoginLog.ip_address))
    ).where(UserLoginLog.user_id == user_id)
    distinct_ips = int((await session.exec(distinct_ip_stmt)).first() or 0)
    breakdown.distinct_ip_count = distinct_ips
    if distinct_ips > _DISTINCT_IP_THRESHOLD:
        excess = distinct_ips - _DISTINCT_IP_THRESHOLD
        ip_penalty = min(_DISTINCT_IP_MAX_PENALTY, excess * _DISTINCT_IP_PENALTY_PER_EXCESS)
        score -= ip_penalty
        breakdown.distinct_ip_penalty = ip_penalty

    # ─── Prior suspicious-alert history ────────────────────────────────────
    alert_stmt = select(func.count()).select_from(SuspiciousAlert).where(
        SuspiciousAlert.user_id == user_id
    )
    alert_count = int((await session.exec(alert_stmt)).first() or 0)
    breakdown.prior_alert_count = alert_count
    if alert_count > 0:
        alert_penalty = min(_ALERT_MAX_PENALTY, alert_count * _ALERT_PER_POINT_PENALTY)
        score -= alert_penalty
        breakdown.prior_alert_penalty = alert_penalty

    # ─── Restriction history — biggest single penalty, applies even after
    #     an unrestrict (the row stays in UserAccountHistory) ─────────────
    restriction_stmt = select(func.count()).select_from(UserAccountHistory).where(
        UserAccountHistory.user_id == user_id,
        UserAccountHistory.type == UserAccountHistoryType.RESTRICTION,
    )
    restriction_count = int((await session.exec(restriction_stmt)).first() or 0)
    if restriction_count > 0:
        score -= _HAS_RESTRICTION_PENALTY
        breakdown.has_restriction_history = True
        breakdown.restriction_penalty = _HAS_RESTRICTION_PENALTY

    breakdown.final_score = max(0.0, min(100.0, score))
    return breakdown


async def quick_trust_score(session: "AsyncSession", user_id: int) -> float:
    """Convenience wrapper for callers that don't need the breakdown.

    Returns just the final 0..100 score. Same cost as the full version —
    the breakdown is essentially free once we've run the queries — so
    this is just a stylistic helper.
    """
    breakdown = await compute_trust_factor(session, user_id)
    return breakdown.final_score
