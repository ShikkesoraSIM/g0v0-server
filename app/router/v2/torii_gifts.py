"""/api/v2/torii/gifts — staff-sent gifts (points and/or cosmetics).

Surfaces
--------
``POST /torii/gifts/admin/create``   (admin) queue a gift for a player
``GET  /torii/gifts/pending``         the caller's unclaimed gifts
``POST /torii/gifts/claim``           claim a gift (awards points, returns grants)

The client fetches pending gifts after a play and claims them, which awards the
points server-side and returns the cosmetic ids to unlock locally + reveal.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import HTTPException, Security
from pydantic import BaseModel, Field as PydanticField
from sqlmodel import select

from app.database import User
from app.database.torii_gifts import ToriiGift
from app.database.torii_store import record_owned_cosmetics
from app.dependencies.database import Database
from app.dependencies.user import get_current_user
from app.log import log
from app.models.torii_cosmetic_prices import clean_cosmetic_ids
from app.models.torii_points import PointReason
from app.service.points_service import award, get_balance
from app.utils import utcnow

from .router import router

logger = log("Gifts")


def _require_admin(user: User) -> None:
    if not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin only")


def _parse_cosmetics(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


async def _resolve_user(db, recipient: str) -> User | None:
    recipient = recipient.strip()
    if not recipient:
        return None
    if recipient.isdigit():
        return await db.get(User, int(recipient))
    return (await db.exec(select(User).where(User.username == recipient))).first()


class CreateGiftRequest(BaseModel):
    recipient: str
    points: int = PydanticField(default=0, ge=0, le=1_000_000)
    grant_cosmetics: list[str] | None = None
    message: str | None = None
    sender: str | None = None


@router.post(
    "/torii/gifts/admin/create",
    tags=["Torii"],
    name="Send a gift (admin)",
    description="Queue a gift (points and/or cosmetics) for a player, delivered on their next play. Admin only.",
)
async def create_gift(
    body: CreateGiftRequest,
    db: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
) -> dict[str, Any]:
    _require_admin(current_user)

    recipient = await _resolve_user(db, body.recipient)
    if recipient is None:
        raise HTTPException(status_code=404, detail="No such user")

    grant = clean_cosmetic_ids(body.grant_cosmetics)
    if body.points <= 0 and not grant:
        raise HTTPException(status_code=400, detail="A gift must include points, a cosmetic, or both")

    gift = ToriiGift(
        recipient_id=recipient.id,
        points=body.points,
        grant_cosmetics=json.dumps(grant) if grant else None,
        message=body.message,
        sender=body.sender,
        created_by=current_user.id,
    )
    db.add(gift)

    # Snapshot before commit: commit expires the ORM rows (current_user, recipient),
    # and reading an expired attribute in async SQLAlchemy does implicit IO with no
    # greenlet -> MissingGreenlet (500). Capture the ids we still need afterwards.
    admin_id = current_user.id
    recipient_id = recipient.id

    await db.commit()
    await db.refresh(gift)
    gift_id = gift.id

    logger.info(
        "Admin {admin_id} gifted user {recipient_id}: {points} pts + {n} cosmetic(s)",
        admin_id=admin_id,
        recipient_id=recipient_id,
        points=body.points,
        n=len(grant),
    )
    return {"id": gift_id, "recipient_id": recipient_id}


class GiftResp(BaseModel):
    id: int
    points: int
    granted_cosmetics: list[str]
    message: str | None
    sender: str | None


@router.get(
    "/torii/gifts/pending",
    tags=["Torii"],
    name="Get my pending gifts",
    description="Unclaimed gifts for the authenticated user, oldest first.",
)
async def get_pending_gifts(
    db: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
) -> dict[str, Any]:
    rows = (
        await db.exec(
            select(ToriiGift)
            .where(ToriiGift.recipient_id == current_user.id, ToriiGift.claimed_at.is_(None))
            .order_by(ToriiGift.created_at.asc(), ToriiGift.id.asc())
        )
    ).all()
    return {
        "gifts": [
            GiftResp(
                id=g.id,
                points=g.points,
                granted_cosmetics=_parse_cosmetics(g.grant_cosmetics),
                message=g.message,
                sender=g.sender,
            )
            for g in rows
        ]
    }


class ClaimGiftRequest(BaseModel):
    gift_id: int


@router.post(
    "/torii/gifts/claim",
    tags=["Torii"],
    name="Claim a gift",
    description="Claim one of your pending gifts: awards the points and returns the cosmetic ids to unlock.",
)
async def claim_gift(
    body: ClaimGiftRequest,
    db: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
) -> dict[str, Any]:
    gift = (
        await db.exec(
            select(ToriiGift)
            .where(
                ToriiGift.id == body.gift_id,
                ToriiGift.recipient_id == current_user.id,
            )
            .with_for_update()
        )
    ).first()
    if gift is None:
        raise HTTPException(status_code=404, detail="Gift not found")
    if gift.claimed_at is not None:
        raise HTTPException(status_code=400, detail="Already claimed")

    gift.claimed_at = utcnow()
    if gift.points > 0:
        await award(
            db,
            current_user.id,
            gift.points,
            PointReason.GIFT,
            ref=f"gift:{gift.id}",
            idempotency_key=f"gift:{gift.id}",
        )
    granted = _parse_cosmetics(gift.grant_cosmetics)
    await record_owned_cosmetics(db, current_user.id, granted, "gift")

    # Snapshot before commit: commit expires gift + current_user, and reading an
    # expired attribute in async SQLAlchemy does implicit IO with no greenlet ->
    # MissingGreenlet (500). get_balance runs after commit, but on a plain int.
    user_id = current_user.id
    gift_id = gift.id
    gift_points = gift.points
    gift_message = gift.message
    gift_sender = gift.sender

    await db.commit()

    logger.info(
        "User {user_id} claimed gift {gift_id}: {points} pts + {n} cosmetic(s)",
        user_id=user_id,
        gift_id=gift_id,
        points=gift_points,
        n=len(granted),
    )
    return {
        "points": gift_points,
        "granted_cosmetics": granted,
        "balance": await get_balance(db, user_id),
        "message": gift_message,
        "sender": gift_sender,
    }
