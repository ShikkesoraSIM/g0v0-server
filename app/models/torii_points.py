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

# A new top play (a score that becomes your new PP-best on a map). Only earns
# once you already have TOP_PLAY_MIN_EXISTING top plays, so brand-new accounts
# grinding their first scores don't farm it.
POINTS_TOP_PLAY = 100

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


# ── Gates / caps (anti-farm) ─────────────────────────────────────────────────

# Need at least this many top plays before new ones start earning.
TOP_PLAY_MIN_EXISTING = 50
# Max top-play awards counted per UTC day (a real player rarely sets more than
# a few genuine new top plays a day; this bounds any edge-case farming).
TOP_PLAY_DAILY_CAP = 5


# ── One-time milestones ──────────────────────────────────────────────────────
# Crossed once, ever. {threshold: points}. Checked on score submission.
PLAYCOUNT_MILESTONES: dict[int, int] = {1_000: 200, 5_000: 500, 10_000: 1_000, 25_000: 2_000}
PP_MILESTONES: dict[int, int] = {5_000: 500, 10_000: 1_000, 15_000: 2_000}
