"""/api/v2/torii/restriction - restriction status for the authenticated user.

Restricted users get a 403 on every normal authenticated endpoint (see
app.dependencies.user), and the osu!lazer client / web frontend treat that as a
generic auth failure, so a restricted player just sees "couldn't log in" with no
reason. This endpoint is the ONE authenticated surface a restricted user can
reach: it resolves their token WITHOUT the is_restricted 403 and reports why
they're restricted and when (if ever) it lifts, so the client can show the
ToriiBriefingGlass restriction panel and the website can show the red banner.

Read-only. Write-side restriction enforcement lives at the individual write
endpoints and is unaffected by this.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, HTTPException
from sqlalchemy import func, text
from sqlmodel import select

from app.auth import get_token_by_access_token
from app.database.user_account_history import UserAccountHistory, UserAccountHistoryType
from app.dependencies.database import Database
from app.dependencies.user import oauth2_code, oauth2_password

from .router import router


@router.get(
    "/torii/restriction",
    tags=["Torii"],
    name="Get my restriction status",
    description=(
        "Restriction status for the authenticated user. Unlike every other "
        "authenticated endpoint, this one does NOT 403 a restricted user - it "
        "is what the client and website query to explain why a restricted "
        "player can't play. Returns is_restricted=false for everyone else."
    ),
)
async def get_my_restriction(
    db: Database,
    token_pw: Annotated[str | None, Depends(oauth2_password)] = None,
    token_code: Annotated[str | None, Depends(oauth2_code)] = None,
) -> dict[str, Any]:
    # Password grant = osu!lazer client, code grant = website. Either is fine.
    token = token_pw or token_code
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    token_record = await get_token_by_access_token(db, token)
    if not token_record:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # Latest currently-active RESTRICTION row for this user, if any. Active =
    # permanent, or timestamp+length still in the future - mirrors
    # User.is_restricted_query. timestamp is naive-UTC on MySQL.
    row = (
        await db.exec(
            select(UserAccountHistory)
            .where(
                UserAccountHistory.user_id == token_record.user_id,
                UserAccountHistory.type == UserAccountHistoryType.RESTRICTION,
                (
                    (UserAccountHistory.permanent.is_(True))
                    | (
                        func.timestampadd(
                            text("SECOND"),
                            UserAccountHistory.length,
                            UserAccountHistory.timestamp,
                        )
                        > func.now()
                    )
                ),
            )
            .order_by(UserAccountHistory.timestamp.desc())
            .limit(1)
        )
    ).first()

    if row is None:
        return {"is_restricted": False}

    ends_at: datetime | None = None
    if not row.permanent and row.length:
        ends_at = row.timestamp + timedelta(seconds=row.length)

    return {
        "is_restricted": True,
        "permanent": bool(row.permanent),
        "reason": row.description,
        # Naive-UTC in the DB; tag with Z so the client parses it as UTC.
        "ends_at": ends_at.isoformat() + "Z" if ends_at is not None else None,
    }
