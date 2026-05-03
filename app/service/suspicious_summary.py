"""Per-user suspicious-status summary, used by the profile endpoint to
attach a small "trust" badge for admin viewers.

Strictly an aggregation over the existing ``suspicious_alerts`` table —
no extra columns on User, no extra tables. Each unresolved alert dings
the trust score; the size of the ding is graded by severity.

Why a separate module:
  - The user serializer in app/database/user.py is already huge and we
    don't want a fresh service-shaped concept living in the model file.
  - Keeps the "is-this-a-flagged-user" rule in one place — easy to
    re-tune the per-severity weights later without grepping callers.

Privacy posture
  - The summary is intended for **admin viewers only**. The router
    decides when to attach the result to the user payload; this module
    just computes it. Never call the result a "public field".
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database.suspicious_alert import SuspiciousAlert


# Per-severity score deduction. Tuned so that a single `critical` alone
# already takes the user well into the orange band; multiple `warning`s
# stack but never push as hard as one `critical`.
_SEVERITY_PENALTY: dict[str, int] = {
    "critical": 35,
    "warning": 12,
}

# Trust-score band thresholds. Mirrored on the frontend for the banner
# colour gradient (>=80 amber, 50-79 orange, <50 red). Keep these in
# sync with SuspiciousBanner.tsx.
TRUST_FULL = 100
TRUST_AMBER_FLOOR = 80
TRUST_ORANGE_FLOOR = 50

# Cap on how many alert titles we expose as `suspicious_reasons`. The
# UI shows up to 3 with a "+N more" suffix; capping here means the API
# response stays small and avoids leaking unbounded metadata to the
# admin browser.
MAX_REASONS_EXPOSED = 6


@dataclass(slots=True, frozen=True)
class SuspiciousSummary:
    """Result of summarising a user's open suspicious alerts."""

    is_suspicious: bool
    trust_score: int
    reasons: list[str]
    open_alert_count: int


_EMPTY_SUMMARY = SuspiciousSummary(
    is_suspicious=False,
    trust_score=TRUST_FULL,
    reasons=[],
    open_alert_count=0,
)


async def summarize_user(session: AsyncSession, user_id: int) -> SuspiciousSummary:
    """Compute a SuspiciousSummary for ``user_id`` from currently-unresolved
    rows in ``suspicious_alerts``.

    Resolved alerts (``resolved_at IS NOT NULL``) are excluded — they
    represent issues an admin already dealt with, so the user shouldn't
    keep being marked. Returns the EMPTY summary for users with no open
    alerts (shape-stable so callers don't need a None check).
    """
    if user_id <= 0:
        return _EMPTY_SUMMARY

    result = await session.exec(
        select(SuspiciousAlert.severity, SuspiciousAlert.title).where(
            col(SuspiciousAlert.user_id) == user_id,
            col(SuspiciousAlert.resolved_at).is_(None),
        )
    )
    rows = result.all()
    if not rows:
        return _EMPTY_SUMMARY

    score = TRUST_FULL
    reasons: list[str] = []
    for severity, title in rows:
        score -= _SEVERITY_PENALTY.get(severity, _SEVERITY_PENALTY["warning"])
        if title and len(reasons) < MAX_REASONS_EXPOSED:
            reasons.append(title)

    score = max(0, min(TRUST_FULL, score))
    return SuspiciousSummary(
        is_suspicious=True,
        trust_score=score,
        reasons=reasons,
        open_alert_count=len(rows),
    )


async def count_open_alerts(session: AsyncSession, user_id: int) -> int:
    """Lightweight count-only variant for places that just need the
    flag without the per-row details (e.g. an admin user-list badge)."""
    result = await session.exec(
        select(func.count(SuspiciousAlert.id)).where(
            col(SuspiciousAlert.user_id) == user_id,
            col(SuspiciousAlert.resolved_at).is_(None),
        )
    )
    return int(result.one() or 0)
