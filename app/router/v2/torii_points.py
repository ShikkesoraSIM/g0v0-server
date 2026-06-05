"""/api/v2/torii/points — the earned-only points economy API.

Surfaces
--------
``GET  /torii/points/me``            current balance
``GET  /torii/points/me/history``    paginated ledger (newest first)
``POST /torii/points/redeem``        redeem an access code for points
``POST /torii/points/admin/codes``   (admin) mint an access code

Points are NEVER sold. They're earned by playing (see app.service.points_service
hooks) or by redeeming staff-issued access codes (bug-report rewards, event
payouts). Spending happens in the cosmetics store (separate, later).
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Query, Security
from fastapi_limiter.depends import RateLimiter
from pydantic import BaseModel, Field as PydanticField
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.database import User
from app.database.torii_points import (
    ToriiAccessCode,
    ToriiAccessCodeRedemption,
    ToriiPointTransaction,
)
from app.dependencies.database import Database
from app.dependencies.user import get_current_user
from app.log import log
from app.models.torii_points import PointReason
from app.service.points_service import award, get_balance
from app.utils import utcnow

from .router import router

logger = log("Points")

# Readable, unambiguous code alphabet (no 0/O/1/I) for minted codes.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _require_admin(user: User) -> None:
    if not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin only")


def _generate_code() -> str:
    return "TORII-" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))


def _naive(dt: datetime | None) -> datetime | None:
    # DateTime columns round-trip naive on MySQL; utcnow() is tz-aware. Strip tz
    # on both sides before comparing so we never mix naive/aware.
    return dt.replace(tzinfo=None) if dt is not None else None


@router.get(
    "/torii/points/me",
    tags=["Torii"],
    name="Get my points balance",
    description="Current Torii points balance for the authenticated user.",
)
async def get_my_points(
    db: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
) -> dict[str, int]:
    return {"balance": await get_balance(db, current_user.id)}


class PointTransactionResp(BaseModel):
    amount: int
    reason: str
    ref: str | None
    balance_after: int
    created_at: datetime


@router.get(
    "/torii/points/me/history",
    tags=["Torii"],
    name="Get my points history",
    description="Paginated points ledger for the authenticated user, newest first.",
)
async def get_my_points_history(
    db: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    rows = (
        await db.exec(
            select(ToriiPointTransaction)
            .where(ToriiPointTransaction.user_id == current_user.id)
            .order_by(ToriiPointTransaction.created_at.desc(), ToriiPointTransaction.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return {
        "balance": current_user.points or 0,
        "transactions": [
            PointTransactionResp(
                amount=r.amount,
                reason=r.reason,
                ref=r.ref,
                balance_after=r.balance_after,
                created_at=r.created_at,
            )
            for r in rows
        ],
    }


class RedeemRequest(BaseModel):
    code: str


@router.post(
    "/torii/points/redeem",
    tags=["Torii"],
    name="Redeem an access code",
    description="Redeem a staff-issued access code for points. One redemption per user per code.",
    dependencies=[Depends(RateLimiter(times=10, minutes=10))],
)
async def redeem_code(
    body: RedeemRequest,
    db: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
) -> dict[str, Any]:
    code_str = body.code.strip().upper()
    if not code_str:
        raise HTTPException(status_code=400, detail="Enter a code")

    code = (await db.exec(select(ToriiAccessCode).where(ToriiAccessCode.code == code_str))).first()
    if code is None:
        raise HTTPException(status_code=404, detail="Invalid code")
    if code.expires_at is not None and _naive(code.expires_at) < _naive(utcnow()):
        raise HTTPException(status_code=400, detail="This code has expired")
    if code.uses >= code.max_uses:
        raise HTTPException(status_code=400, detail="This code has already been fully redeemed")

    already = (
        await db.exec(
            select(ToriiAccessCodeRedemption.id).where(
                ToriiAccessCodeRedemption.code_id == code.id,
                ToriiAccessCodeRedemption.user_id == current_user.id,
            )
        )
    ).first()
    if already is not None:
        raise HTTPException(status_code=400, detail="You already redeemed this code")

    db.add(ToriiAccessCodeRedemption(code_id=code.id, user_id=current_user.id))
    code.uses += 1
    await award(
        db,
        current_user.id,
        code.amount,
        PointReason.ACCESS_CODE,
        ref=f"code:{code.id}",
        idempotency_key=f"access_code:{code.id}:{current_user.id}",
    )
    try:
        await db.commit()
    except IntegrityError:
        # Lost the race on the (code_id, user_id) unique constraint.
        await db.rollback()
        raise HTTPException(status_code=400, detail="You already redeemed this code")

    logger.info(
        "User {user_id} redeemed code {code} for {amount} points",
        user_id=current_user.id,
        code=code_str,
        amount=code.amount,
    )
    return {"awarded": code.amount, "balance": await get_balance(db, current_user.id)}


class CreateCodeRequest(BaseModel):
    amount: int = PydanticField(ge=1, le=1_000_000)
    note: str | None = None
    code: str | None = None
    max_uses: int = PydanticField(default=1, ge=1, le=1_000_000)
    expires_at: datetime | None = None


@router.post(
    "/torii/points/admin/codes",
    tags=["Torii"],
    name="Create an access code (admin)",
    description="Mint a redeemable points code. Admin only. Used for bug-report rewards, event payouts, giveaways.",
)
async def create_access_code(
    body: CreateCodeRequest,
    db: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
) -> dict[str, Any]:
    _require_admin(current_user)

    code_str = (body.code or _generate_code()).strip().upper()
    if not code_str:
        raise HTTPException(status_code=400, detail="Invalid code")

    existing = (await db.exec(select(ToriiAccessCode.id).where(ToriiAccessCode.code == code_str))).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="That code already exists")

    code = ToriiAccessCode(
        code=code_str,
        amount=body.amount,
        note=body.note,
        max_uses=body.max_uses,
        expires_at=body.expires_at,
        created_by=current_user.id,
    )
    db.add(code)
    await db.commit()
    await db.refresh(code)

    logger.info(
        "Admin {admin_id} minted code {code} worth {amount} ({max_uses} uses)",
        admin_id=current_user.id,
        code=code.code,
        amount=code.amount,
        max_uses=code.max_uses,
    )
    return {
        "code": code.code,
        "amount": code.amount,
        "max_uses": code.max_uses,
        "note": code.note,
        "expires_at": code.expires_at,
    }
