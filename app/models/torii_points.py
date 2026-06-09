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

# A new top play (a new PP-best on a map) is rewarded by a scaled formula, see
# top_play_breakdown() below: base + veteran bonus + the pp this play added,
# tiered by how many top plays you already have so fresh accounts can't farm it.

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


# ── Top-play scaling (anti-farm) ─────────────────────────────────────────────

# Hard ceiling on top-play points per UTC day. Bounds even a big day of genuine
# new bests and makes grinding a fresh account pointless.
TOP_PLAY_DAILY_POINTS_CAP = 400


def top_play_breakdown(existing_top_plays: int, pp_gained: int) -> tuple[int, int, int]:
    """Points for a new top play as (base, veteran_bonus, pp_bonus); total = sum.

    Scales with how established you are (your existing top-play count) plus the pp
    this play added to your best on the map. New accounts earn only a small flat
    base with no pp bonus, so spinning up a fresh account on a private server
    (where early plays are free PBs) can't farm points. The satisfying pp reward
    unlocks once you have a real history.
    """
    pp_gained = max(0, pp_gained)
    if existing_top_plays < 50:
        base, veteran, pp_cap = 8, 0, 0
    elif existing_top_plays < 500:
        base, veteran, pp_cap = 40, 0, 60
    elif existing_top_plays < 2000:
        base, veteran, pp_cap = 100, 50, 250
    else:
        base, veteran, pp_cap = 100, 75, 250
    return base, veteran, min(pp_gained, pp_cap)


# ── One-time milestones ──────────────────────────────────────────────────────
# Crossed once, ever. {threshold: points}. Checked on score submission.
PLAYCOUNT_MILESTONES: dict[int, int] = {1_000: 200, 5_000: 500, 10_000: 1_000, 25_000: 2_000}
PP_MILESTONES: dict[int, int] = {5_000: 250, 7_500: 400, 10_000: 600, 15_000: 1_000, 20_000: 1_500}
