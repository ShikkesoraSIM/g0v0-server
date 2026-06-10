"""Torii points economy — values, reasons, gates.

Design rules (non-negotiable, peppy-safe):
  - Points are EARNED ONLY by playing / engagement. There is NO way to buy
    points with money, and paying never grants or speeds up points.
  - The ledger (torii_point_transactions) is the source of truth; the
    `points` column on lazer_users is a cached running balance.

All the numbers below are starting values — tune freely, they're just
constants. Every earn is idempotency-keyed so nothing double-awards.
"""

from __future__ import annotations

from enum import StrEnum


class PointReason(StrEnum):
    """Goes into torii_point_transactions.reason. Stable strings."""

    TOP_PLAY = "top_play"
    DAILY_PLAY = "daily_play"
    DAILY_CHALLENGE = "daily_challenge"
    MEDAL = "medal"
    MILESTONE = "milestone"
    ACCESS_CODE = "access_code"
    GIFT = "gift"
    STORE_PURCHASE = "store_purchase"
    ADMIN_ADJUST = "admin_adjust"


# ── Earn values ──────────────────────────────────────────────────────────────

# A new top play (a new PP-best on a map) is rewarded by top_play_award() below:
# scaled by the play's RANK among your best plays + your tenure, plus the pp it
# added to your account total. Random low PBs and fresh accounts earn ~nothing.

# Small bonus for your first ranked play of the day (the "play daily" nudge).
# Kept low on purpose — the bulk of points come from top plays.
POINTS_DAILY_PLAY = 15
# +N per consecutive day, capped, on top of POINTS_DAILY_PLAY.
POINTS_DAILY_STREAK_STEP = 5
POINTS_DAILY_STREAK_MAX = 30

# Completing the daily challenge (once per day).
POINTS_DAILY_CHALLENGE = 30

# Each medal / achievement unlocked.
POINTS_MEDAL = 10


# ── Top-play scaling (rank + tenure, anti-farm) ──────────────────────────────

# Soft ceiling on top-play points per UTC day. Below it, top plays pay the full
# rank+pp reward. Once you cross it, further top plays still pay the pp they added
# to your account (so a genuine top play is never a flat zero) but drop the
# rank/base bonus — see award_top_play(). The client is told (ledger ref carries
# "capped:1") so it can show "you hit today's top-play limit".
TOP_PLAY_DAILY_POINTS_CAP = 500

# Need a real top-play history before top plays pay out at all (a fresh account
# on a private server gets free PBs early; this stops farming them).
TOP_PLAY_MIN_TOPS = 50


def top_play_award(rank: int, existing_top_plays: int, account_pp_delta: int) -> tuple[int, int]:
    """Points for a new top play as (base, pp_bonus); total = base + pp_bonus.

    Two axes:
      - RANK: where this play lands among the user's best plays by pp (rank 1 = a
        new #1 best). A random PB far down the list pays little or nothing, so you
        can't farm points by just setting easy first-time PBs on low maps.
      - TENURE: how many top plays you already have. A #1 with 80 plays is easy to
        get, so it pays less than a #1 with 500+ behind it.

    The pp_bonus is the pp this play added to your ACCOUNT total
    (``account_pp_delta``), capped by tenure — NOT the raw pp of the play. A 1200pp
    play that adds only +3 to your weighted total adds +3, not +1200. This rewards
    plays that actually move your profile, which naturally favours lower-pp players
    (whose plays move their total more): distribution over raw skill.
    """
    account_pp_delta = max(0, account_pp_delta)

    # Rookie: crumbs only, no pp bonus.
    if existing_top_plays < TOP_PLAY_MIN_TOPS:
        if rank <= 5:
            return 8, 0
        if rank <= 25:
            return 5, 0
        if rank <= 50:
            return 3, 0
        return 0, 0

    developing = existing_top_plays < 500

    if rank == 1:
        base = 120 if developing else 250
    elif rank <= 5:
        base = 60 if developing else 120
    elif rank <= 25:
        base = 25 if developing else 45
    elif rank <= 50:
        base = 12 if developing else 20
    elif rank <= 100:
        base = 5 if developing else 8
    else:
        base = 0

    if base <= 0:
        return 0, 0

    pp_cap = 60 if developing else 150
    return base, min(account_pp_delta, pp_cap)


# ── One-time milestones ──────────────────────────────────────────────────────
# Crossed once, ever. {threshold: points}. Checked on score submission.
PLAYCOUNT_MILESTONES: dict[int, int] = {1_000: 200, 5_000: 500, 10_000: 1_000, 25_000: 2_000}
PP_MILESTONES: dict[int, int] = {5_000: 250, 7_500: 400, 10_000: 600, 15_000: 1_000, 20_000: 1_500}
