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

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Query, Security
from fastapi_limiter.depends import RateLimiter
from pydantic import BaseModel, Field as PydanticField
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, func, select

from app.database import User
from app.database.torii_points import (
    ToriiAccessCode,
    ToriiAccessCodeRedemption,
    ToriiPointTransaction,
)
from app.database.torii_store import record_owned_cosmetics
from app.dependencies.database import Database
from app.dependencies.user import get_current_user
from app.log import log
from app.models.torii_cosmetic_prices import clean_cosmetic_ids
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


def _parse_cosmetics(raw: str | None) -> list[str]:
    """Decode a code's grant_cosmetics JSON list into a clean list of ids."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return [str(x) for x in data if str(x).strip()] if isinstance(data, list) else []


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


def _as_utc(value: datetime) -> datetime:
    """Marca como UTC un datetime naive.

    MySQL guarda DATETIME sin zona, asi que lo que sale de la base es naive
    aunque el valor SEA UTC (se escribe con utcnow()). Serializado sin offset,
    el cliente lo parsea como hora LOCAL: en el juego, DateTimeOffset lo asume
    local y le suma el huso de la persona.

    Eso rompia el cartel de puntos para todo el hemisferio este. El filtro
    "recien ganado" del cliente acepta menos de 10 minutos, y a alguien en UTC+8
    un evento de hace 30 segundos le daba 8 horas y media de antiguedad, asi que
    no se mostraba nunca. Al oeste de UTC daba negativo y pasaba de casualidad.
    """
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


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
                created_at=_as_utc(r.created_at),
            )
            for r in rows
        ],
    }


class PointEventResp(BaseModel):
    id: int
    amount: int
    reason: str
    ref: str | None
    balance_after: int
    created_at: datetime


@router.get(
    "/torii/points/feed",
    tags=["Torii"],
    name="Get my recent points earnings",
    description=(
        "Earned (positive) point events with id greater than since_id, oldest first. "
        "The client uses this to pop a '+N points' toast and explain why it was earned. "
        "Clients persist last_id as their cursor so each earn is celebrated once."
    ),
)
async def get_my_points_feed(
    db: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    since_id: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    rows = (
        await db.exec(
            select(ToriiPointTransaction)
            .where(
                ToriiPointTransaction.user_id == current_user.id,
                ToriiPointTransaction.id > since_id,
                ToriiPointTransaction.amount > 0,
            )
            .order_by(ToriiPointTransaction.id.asc())
            .limit(limit)
        )
    ).all()
    return {
        "balance": current_user.points or 0,
        "last_id": rows[-1].id if rows else since_id,
        "events": [
            PointEventResp(
                id=r.id,
                amount=r.amount,
                reason=r.reason,
                ref=r.ref,
                balance_after=r.balance_after,
                created_at=_as_utc(r.created_at),
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

    code = (await db.exec(select(ToriiAccessCode).where(ToriiAccessCode.code == code_str).with_for_update())).first()
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
    granted = _parse_cosmetics(code.grant_cosmetics)
    await record_owned_cosmetics(db, current_user.id, granted, "code")

    # Snapshot before commit: commit/rollback expires the ORM rows (current_user, code),
    # and reading an expired attribute in async SQLAlchemy does implicit IO with no
    # greenlet -> MissingGreenlet (500). Keep only plain values past this point.
    uid = current_user.id
    code_amount = code.amount

    try:
        await db.commit()
    except IntegrityError:
        # Lost the race on the (code_id, user_id) unique constraint.
        await db.rollback()
        raise HTTPException(status_code=400, detail="You already redeemed this code")

    logger.info(
        "User {user_id} redeemed code {code} for {amount} points + {n} cosmetic(s)",
        user_id=uid,
        code=code_str,
        amount=code_amount,
        n=len(granted),
    )
    return {
        "awarded": code_amount,
        "balance": await get_balance(db, uid),
        "granted_cosmetics": granted,
    }


class CreateCodeRequest(BaseModel):
    # 0 is allowed so a code can grant ONLY a cosmetic (no points).
    amount: int = PydanticField(default=0, ge=0, le=1_000_000)
    grant_cosmetics: list[str] | None = None
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

    grant = clean_cosmetic_ids(body.grant_cosmetics)
    if body.amount <= 0 and not grant:
        raise HTTPException(status_code=400, detail="A code must grant points, a cosmetic, or both")

    code = ToriiAccessCode(
        code=code_str,
        amount=body.amount,
        grant_cosmetics=json.dumps(grant) if grant else None,
        note=body.note,
        max_uses=body.max_uses,
        expires_at=body.expires_at,
        created_by=current_user.id,
    )
    db.add(code)

    # Snapshot before commit: commit expires current_user (it shares this session),
    # and reading current_user.id afterwards does implicit IO with no greenlet ->
    # MissingGreenlet (500). code is re-populated by the refresh below, so it is fine.
    admin_id = current_user.id

    await db.commit()
    await db.refresh(code)

    logger.info(
        "Admin {admin_id} minted code {code} worth {amount} ({max_uses} uses)",
        admin_id=admin_id,
        code=code.code,
        amount=code.amount,
        max_uses=code.max_uses,
    )
    return {
        "code": code.code,
        "amount": code.amount,
        "grant_cosmetics": _parse_cosmetics(code.grant_cosmetics),
        "max_uses": code.max_uses,
        "note": code.note,
        "expires_at": code.expires_at,
    }


@router.get(
    "/torii/points/admin/activity",
    tags=["Torii"],
    name="Points activity (admin)",
    description="Earning activity for spotting abuse: top earners in the window plus recent large awards. Admin only.",
)
async def points_activity(
    db: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    days: int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=25, ge=1, le=100),
) -> dict[str, Any]:
    _require_admin(current_user)

    cutoff = utcnow().replace(tzinfo=None) - timedelta(days=days)
    earned = func.sum(ToriiPointTransaction.amount)

    top_rows = (
        await db.exec(
            select(User.id, User.username, earned, User.points)
            .join(ToriiPointTransaction, col(ToriiPointTransaction.user_id) == col(User.id))
            .where(ToriiPointTransaction.amount > 0, ToriiPointTransaction.created_at >= cutoff)
            .group_by(col(User.id), col(User.username), col(User.points))
            .order_by(earned.desc())
            .limit(limit)
        )
    ).all()

    large_rows = (
        await db.exec(
            select(
                ToriiPointTransaction.user_id,
                User.username,
                ToriiPointTransaction.amount,
                ToriiPointTransaction.reason,
                ToriiPointTransaction.ref,
                ToriiPointTransaction.created_at,
            )
            .join(User, col(User.id) == col(ToriiPointTransaction.user_id))
            .where(ToriiPointTransaction.amount >= 200, ToriiPointTransaction.created_at >= cutoff)
            .order_by(col(ToriiPointTransaction.created_at).desc())
            .limit(limit)
        )
    ).all()

    return {
        "days": days,
        "top_earners": [
            {"user_id": r[0], "username": r[1], "earned": int(r[2] or 0), "balance": int(r[3] or 0)}
            for r in top_rows
        ],
        "recent_large": [
            {"user_id": r[0], "username": r[1], "amount": r[2], "reason": r[3], "ref": r[4], "created_at": r[5]}
            for r in large_rows
        ],
    }
