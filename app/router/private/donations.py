"""Webhook endpoints for inbound donation events.

Currently supports Ko-fi (one-time tips, memberships, shop orders).
Stripe / PayPal direct adapters can be slotted in alongside this one
without touching the core service layer (`app/database/donation.py`).

Ko-fi-specific notes:
  - Content-Type is application/x-www-form-urlencoded.
  - The actual payload is a single field named `data` containing JSON.
  - `verification_token` inside that JSON authenticates the request.
  - `kofi_transaction_id` is the idempotency key (Ko-fi RETRIES on
    non-200 with the SAME id, so we MUST tolerate duplicates).
  - `is_public` controls whether the donor's name + message can be
    rendered on a public supporters page. We always store it; the
    consumer must check this flag at render time.
"""

from __future__ import annotations

import json
import secrets
from typing import Annotated, Any

import httpx

from app.config import settings
from app.database.donation import (
    Donation,
    apply_supporter_grant,
    is_duplicate,
    match_user_for_kofi,
    months_for_amount,
)
from app.dependencies.database import Database
from app.log import log

from fastapi import HTTPException, Request, status
from pydantic import BaseModel

from .router import router

logger = log("DonationsWebhook")


# ---------------------------------------------------------------------------
# Ko-fi payload — only the fields we actually consume. We tolerate any
# extras Ko-fi adds in future without failing.
# ---------------------------------------------------------------------------


class _KofiPayload(BaseModel):
    """Subset of Ko-fi's webhook JSON we care about. Extras ignored."""
    verification_token: str
    message_id: str
    timestamp: str
    type: str  # "Donation" | "Subscription" | "Commission" | "Shop Order"
    is_public: bool = False
    from_name: str | None = None
    message: str | None = None
    amount: str  # Ko-fi sends amount as a string like "5.00"
    currency: str
    email: str | None = None
    is_subscription_payment: bool = False
    is_first_subscription_payment: bool = False
    kofi_transaction_id: str
    tier_name: str | None = None
    shop_items: Any = None  # list of {direct_link_code: str} or None


def _parse_amount_to_cents(amount_str: str) -> int:
    """Ko-fi formats amounts like '5.00' or '12.50'. Convert to integer
    cents to keep arithmetic exact downstream."""
    try:
        as_float = float(amount_str)
    except ValueError:
        return 0
    return int(round(as_float * 100))


# ---------------------------------------------------------------------------
# Discord forwarding — best-effort, fire-and-forget. A Discord outage
# must NEVER make us drop a donation, so we catch everything.
# ---------------------------------------------------------------------------


def _money_str(cents: int, currency: str) -> str:
    return f"{cents / 100:.2f} {currency}"


async def _post_to_discord(payload: dict) -> None:
    url = (settings.discord_donations_webhook_url or "").strip()
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json=payload)
    except Exception as exc:  # pragma: no cover — Discord must never block ingest
        logger.warning("Failed to forward donation to Discord: {}", exc)


def _build_discord_embed(
    *,
    payload: _KofiPayload,
    matched_username: str | None,
    matched_user_id: int | None,
    months_granted: int,
    total_supporter_months_after: int | None,
    new_tier_label: str | None,
    is_duplicate_event: bool,
) -> dict:
    """Build the Discord embed for a donation event.

    is_duplicate_event=True: Ko-fi retried a payload we already had.
    matched_username=None  : webhook arrived but we couldn't match a user.
    """
    money = _money_str(_parse_amount_to_cents(payload.amount), payload.currency)
    donor = payload.from_name or "Anonymous"

    if is_duplicate_event:
        return {
            "embeds": [{
                "title": "🔁 Duplicate Ko-fi webhook (no-op)",
                "description": f"`{payload.kofi_transaction_id}` re-delivered. Ignored.",
                "color": 0x6B7280,
            }],
        }

    if matched_username is None:
        return {
            "embeds": [{
                "title": "💜 New donation (unmatched — needs admin review)",
                "description": (
                    f"**{money}** from **{donor}**\n"
                    f"_Type: {payload.type}_"
                    + (f"\n> {payload.message}" if payload.message else "")
                    + "\n\n*Donor's @username wasn't found. "
                    "Use the admin panel to link this to a Torii user.*"
                ),
                "color": 0xF59E0B,
                "footer": {"text": f"Transaction {payload.kofi_transaction_id}"},
            }],
        }

    # Matched — full celebratory embed.
    fields = [
        {"name": "Linked to", "value": f"**{matched_username}** (id `{matched_user_id}`)", "inline": True},
        {"name": "Months granted", "value": str(months_granted), "inline": True},
    ]
    if total_supporter_months_after is not None:
        fields.append({
            "name": "Lifetime supporter months",
            "value": f"**{total_supporter_months_after}**" + (
                f" → **{new_tier_label}**" if new_tier_label else ""
            ),
            "inline": True,
        })
    if payload.message and payload.is_public:
        fields.append({"name": "Message", "value": payload.message, "inline": False})
    return {
        "embeds": [{
            "title": "💜 New donation",
            "description": f"**{money}** from **{donor}**" + (
                "  *(recurring)*" if payload.is_subscription_payment else ""
            ),
            "color": 0xEC4899,
            "fields": fields,
            "footer": {"text": f"Type: {payload.type} · {payload.kofi_transaction_id}"},
        }],
    }


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/donations/webhook/kofi",
    name="Ko-fi donation webhook",
    description=(
        "Receives Ko-fi donation events. Ko-fi POSTs form-encoded with a single "
        "`data` field containing JSON. Authentication is by `verification_token` "
        "inside the payload (Ko-fi's standard, paired with HTTPS). Idempotent on "
        "`kofi_transaction_id`."
    ),
    include_in_schema=False,  # internal infrastructure, not for clients
)
async def kofi_webhook(request: Request, session: Database):
    expected_token = (settings.kofi_verification_token or "").strip()
    if not expected_token:
        # Misconfigured server. Return 503 (Ko-fi will retry with backoff)
        # rather than 200-and-ignore — that way an admin notices the outage.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Donations webhook not configured.",
        )

    # Ko-fi sends as application/x-www-form-urlencoded with a single
    # `data` field. We can't use FastAPI's `Form()` cleanly because the
    # field VALUE is JSON we need to parse, so just grab the raw form.
    form = await request.form()
    raw_data = form.get("data")
    if not raw_data or not isinstance(raw_data, str):
        raise HTTPException(status_code=400, detail="Missing 'data' field in form body.")

    try:
        json_payload = json.loads(raw_data)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="'data' field is not valid JSON.")

    try:
        payload = _KofiPayload.model_validate(json_payload)
    except Exception as exc:
        # Don't echo back the parser detail — could leak schema if hostile.
        logger.warning("Ko-fi payload schema mismatch: {}", exc)
        raise HTTPException(status_code=400, detail="Payload schema mismatch.")

    # constant-time compare to make timing-attacks against the token
    # nontrivial even though it's plain-text in the JSON.
    if not secrets.compare_digest(payload.verification_token, expected_token):
        logger.warning(
            "Ko-fi webhook with bad verification_token (txn={}, from={})",
            payload.kofi_transaction_id, payload.from_name,
        )
        raise HTTPException(status_code=401, detail="Invalid verification token.")

    provider = "kofi"

    # Idempotency — Ko-fi retries on non-200, so we MUST handle dupes.
    if await is_duplicate(
        session, provider=provider, provider_transaction_id=payload.kofi_transaction_id
    ):
        logger.info(
            "Duplicate Ko-fi webhook ignored (txn={})", payload.kofi_transaction_id
        )
        await _post_to_discord(_build_discord_embed(
            payload=payload,
            matched_username=None,
            matched_user_id=None,
            months_granted=0,
            total_supporter_months_after=None,
            new_tier_label=None,
            is_duplicate_event=True,
        ))
        return {"status": "ok", "duplicate": True}

    amount_cents = _parse_amount_to_cents(payload.amount)

    # Shop orders / commissions don't grant supporter status (we can wire
    # specific shop items to perks later if we add a shop). Donations &
    # subscriptions all roll into supporter time.
    grants_supporter = payload.type in ("Donation", "Subscription")
    months_granted = months_for_amount(amount_cents, payload.currency) if grants_supporter else 0

    matched_user = await match_user_for_kofi(
        session,
        message=payload.message,
        from_name=payload.from_name,
    )

    donation = Donation(
        user_id=matched_user.id if matched_user else None,
        provider=provider,
        provider_transaction_id=payload.kofi_transaction_id,
        provider_message_id=payload.message_id,
        amount_cents=amount_cents,
        currency=payload.currency,
        is_recurring=payload.is_subscription_payment,
        is_first_recurring=payload.is_first_subscription_payment,
        tier_name=payload.tier_name,
        donor_display_name=payload.from_name,
        donor_message=payload.message,
        donor_message_is_public=payload.is_public,
        donor_email=payload.email,
        months_granted=months_granted,
    )
    session.add(donation)

    # If we matched a user, bump their counters in the same transaction
    # so the donation row + supporter grant land atomically.
    new_tier_label: str | None = None
    if matched_user is not None and months_granted > 0:
        await apply_supporter_grant(session, user=matched_user, months_granted=months_granted)
        # Build a friendly tier label for the Discord embed.
        from app.models.torii_groups import (
            supporter_tier_key_for_months,
            TORII_GROUPS,
        )
        tier_key = supporter_tier_key_for_months(matched_user.total_supporter_months)
        if tier_key and tier_key in TORII_GROUPS:
            new_tier_label = TORII_GROUPS[tier_key]["name"]

    await session.commit()

    # Best-effort Discord forwarding AFTER the DB write — we want the
    # state durable before announcing.
    await _post_to_discord(_build_discord_embed(
        payload=payload,
        matched_username=matched_user.username if matched_user else None,
        matched_user_id=matched_user.id if matched_user else None,
        months_granted=months_granted,
        total_supporter_months_after=(
            matched_user.total_supporter_months if matched_user else None
        ),
        new_tier_label=new_tier_label,
        is_duplicate_event=False,
    ))

    return {
        "status": "ok",
        "matched": matched_user is not None,
        "months_granted": months_granted,
    }
