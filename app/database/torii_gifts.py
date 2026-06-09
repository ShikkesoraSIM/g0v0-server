"""Torii gifts — DB model.

A gift is a staff-sent reward (points and/or cosmetics) queued for a specific
player. The client fetches pending gifts and claims them after a play, which
awards the points and unlocks the cosmetics (and pops the reveal). One row per
gift; ``claimed_at`` set on claim so it can't be claimed twice.
"""

from __future__ import annotations

from datetime import datetime

from app.utils import utcnow

from sqlmodel import (
    BigInteger,
    Column,
    DateTime,
    Field,
    ForeignKey,
    Integer,
    SQLModel,
    String,
)


class ToriiGift(SQLModel, table=True):
    __tablename__: str = "torii_gifts"

    id: int | None = Field(default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True))
    recipient_id: int = Field(
        sa_column=Column(BigInteger, ForeignKey("lazer_users.id", ondelete="CASCADE"), index=True, nullable=False)
    )
    points: int = Field(default=0, sa_column=Column(Integer, nullable=False, default=0))
    # JSON array of catalog ids unlocked on claim. Null = points only.
    grant_cosmetics: str | None = Field(default=None, sa_column=Column(String(1024), nullable=True))
    message: str | None = Field(default=None, sa_column=Column(String(256), nullable=True))
    # Display name shown as the sender (defaults to "Torii Halo" on the client).
    sender: str | None = Field(default=None, sa_column=Column(String(64), nullable=True))
    created_by: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True),
    )
    claimed_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
