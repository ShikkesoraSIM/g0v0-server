"""SQLModel + service layer for the `donations` table.

A donation is one inbound event from any external provider (Ko-fi, future
Stripe / PayPal direct, etc) — stored verbatim for audit and idempotently
deduplicated on (provider, provider_transaction_id).

The service helpers in this module are the single entry point used by
webhook routes:

  - `record_donation(...)`     — upsert by (provider, provider_transaction_id),
                                  match donor to a Torii user when possible,
                                  bump the user's loyalty counters.
  - `match_user_for_kofi(...)` — encapsulates the @username + kofi_display_name
                                  matching heuristics so future providers can
                                  reuse the same logic.

The webhook router is kept thin (just I/O + parse + verify) so the actual
state mutation logic lives here and can be unit-tested without HTTP.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from app.models.torii_groups import supporter_tier_key_for_months
from app.utils import utcnow

from sqlalchemy import Column, DateTime, ForeignKey
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import BigInteger, Field, Relationship, SQLModel, UniqueConstraint, select

if TYPE_CHECKING:
    from .user import User


# Each US dollar buys one fifth of a month of supporter time (so $5 = 1 month,
# $20 = 4 months, etc). Linear all the way up — no caps, no tier-pricing
# weirdness, no "buy big get bigger" bonus curve. Keeps the framing as
# "donations cover costs proportionally" rather than "we sell tiers."
SUPPORTER_DOLLARS_PER_MONTH: int = 5


class Donation(SQLModel, table=True):
    __tablename__: str = "donations"
    __table_args__ = (
        # Composite uniqueness on (provider, provider_transaction_id) so
        # duplicate webhook deliveries from any provider are no-ops.
        UniqueConstraint("provider", "provider_transaction_id", name="uq_donations_provider_txn"),
    )

    id: int | None = Field(default=None, primary_key=True)

    # Nullable — anonymous donations or ones whose donor display name
    # didn't match any Torii user live here unlinked until an admin
    # links them via the queue.
    user_id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, ForeignKey("lazer_users.id", ondelete="SET NULL"), index=True, nullable=True),
    )
    user: "User | None" = Relationship()

    provider: str = Field(max_length=32, index=True)
    provider_transaction_id: str = Field(max_length=128)
    provider_message_id: str | None = Field(default=None, max_length=128)

    amount_cents: int
    currency: str = Field(max_length=3)

    is_recurring: bool = Field(default=False)
    is_first_recurring: bool = Field(default=False)
    tier_name: str | None = Field(default=None, max_length=64)

    donor_display_name: str | None = Field(default=None, max_length=128)
    donor_message: str | None = Field(default=None)
    donor_message_is_public: bool = Field(default=False)
    donor_email: str | None = Field(default=None, max_length=254)

    months_granted: int = Field(default=0)

    received_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime, index=True),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Match anything starting with "@" then one or more username-safe chars.
# osu! usernames allow spaces and a-z/0-9/_/-/[]; we deliberately stop on
# whitespace so a `@nahuel thanks for the server` works.
_USERNAME_RE = re.compile(r"@([A-Za-z0-9_\-\[\]]+)")


def parse_username_from_message(message: str | None) -> str | None:
    """Find the first @username token in a free-form donor message.
    Returns None if the message is empty or doesn't contain a token.
    Used by the webhook matcher; intentionally simple — donors who want
    to be matched should follow the @username convention.
    """
    if not message:
        return None
    match = _USERNAME_RE.search(message)
    return match.group(1) if match else None


def months_for_amount(amount_cents: int, currency: str) -> int:
    """How many months of supporter time a donation buys.

    USD baseline: $5 = 1 month, $10 = 2, $20 = 4 (etc). For non-USD
    currencies we accept the raw amount as-if-USD for now — Ko-fi /
    Stripe handle the FX so the cents arrive in whatever currency the
    donor paid in. A future improvement is to convert via stored FX
    rates, but for the MVP "be slightly generous" is fine and simple.
    """
    if amount_cents <= 0:
        return 0
    # 100 cents = $1, $5 = 1 month → 500 cents per month.
    cents_per_month = SUPPORTER_DOLLARS_PER_MONTH * 100
    return amount_cents // cents_per_month


async def match_user_for_kofi(
    session: AsyncSession,
    *,
    message: str | None,
    from_name: str | None,
) -> "User | None":
    """Try to identify the Torii user behind a Ko-fi donation.

    Strategy (most-explicit-first):
      1. `@username` parsed out of the donor's message.
      2. Case-insensitive match on `User.kofi_display_name` (donor set this
         in their Torii settings if their Ko-fi name differs).
      3. Case-insensitive match on `User.username` against `from_name` —
         falls through if the names happen to be identical.

    Returns None if nothing matches; admin queue handles those manually.
    """
    from .user import User as DbUser

    handle = parse_username_from_message(message)
    if handle:
        u = (
            await session.exec(
                select(DbUser).where(DbUser.username.collate("utf8mb4_general_ci") == handle)  # type: ignore[attr-defined]
            )
        ).first()
        if u is not None:
            return u

    if from_name:
        # Try kofi_display_name explicit override first — this is the
        # field users set when their Ko-fi handle differs from Torii.
        u = (
            await session.exec(
                select(DbUser).where(DbUser.kofi_display_name.collate("utf8mb4_general_ci") == from_name)  # type: ignore[attr-defined]
            )
        ).first()
        if u is not None:
            return u

        # Last-chance: same string as username. Cheap fallback for users
        # whose Ko-fi handle and Torii username happen to be identical.
        u = (
            await session.exec(
                select(DbUser).where(DbUser.username.collate("utf8mb4_general_ci") == from_name)  # type: ignore[attr-defined]
            )
        ).first()
        if u is not None:
            return u

    return None


async def is_duplicate(
    session: AsyncSession, *, provider: str, provider_transaction_id: str
) -> bool:
    """Idempotency check — has this exact donation already been ingested?
    Webhook providers retry on non-200 responses so we MUST tolerate
    the same payload arriving multiple times."""
    existing = (
        await session.exec(
            select(Donation.id).where(
                Donation.provider == provider,
                Donation.provider_transaction_id == provider_transaction_id,
            )
        )
    ).first()
    return existing is not None


async def apply_supporter_grant(
    session: AsyncSession, *, user: "User", months_granted: int
) -> None:
    """Mutate the user's supporter counters in response to a donation
    being linked to them. Doesn't commit — the caller composes this
    with the donation insert in a single transaction.

    Behaviour:
      - is_supporter / has_supported flip TRUE permanently on first
        donation. (Once you've donated, you've donated.)
      - total_supporter_months grows cumulatively — drives the loyalty
        tier resolved at /me time by build_groups.
      - donor_end_at extends from "max(now, current donor_end_at)" by
        the months granted, so recurring subscriptions stack cleanly
        without retroactively shortening anyone's window.
      - support_level snaps to the tier index (1..4) so the existing
        osu!-supporter-style hexagon icon renders the right count.
    """
    if months_granted <= 0:
        return

    user.is_supporter = True
    user.has_supported = True
    user.total_supporter_months = (user.total_supporter_months or 0) + months_granted

    base = max(user.donor_end_at or utcnow(), utcnow())
    user.donor_end_at = base + timedelta(days=30 * months_granted)

    # Map cumulative months to support_level so any client reading the
    # native osu! field (hex-icon count) shows something sensible.
    tier_key = supporter_tier_key_for_months(user.total_supporter_months)
    user.support_level = {
        "supporter-gold":   4,
        "supporter-silver": 3,
        "supporter-bronze": 2,
        "supporter":        1,
    }.get(tier_key or "", 0)

    session.add(user)
