"""Torii points economy — DB models.

Three tables:
  - torii_point_transactions: append-only ledger (every earn/spend). Source of
    truth for balances + history. `idempotency_key` (unique) guarantees no
    award fires twice even under retries / races.
  - torii_access_codes: redeemable codes that grant points (bug-report rewards,
    giveaways, event payouts). Created by staff.
  - torii_access_code_redemptions: who redeemed which code (one per user/code).

The running balance lives on lazer_users.points (cached); see points_service.
"""

from __future__ import annotations

from datetime import datetime

from app.utils import utcnow

from sqlalchemy import Index
from sqlmodel import (
    BigInteger,
    Column,
    DateTime,
    Field,
    ForeignKey,
    Integer,
    SQLModel,
    String,
    UniqueConstraint,
)


class ToriiPointTransaction(SQLModel, table=True):
    """One ledger line. amount > 0 = earned, amount < 0 = spent."""

    __tablename__: str = "torii_point_transactions"
    __table_args__ = (
        Index("ix_torii_point_tx_user", "user_id", "created_at"),
    )

    id: int | None = Field(default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True))
    user_id: int = Field(
        sa_column=Column(BigInteger, ForeignKey("lazer_users.id", ondelete="CASCADE"), index=True, nullable=False)
    )
    amount: int = Field(sa_column=Column(Integer, nullable=False))
    reason: str = Field(sa_column=Column(String(32), nullable=False))
    # Free-form reference for traceability: score_id, achievement_id, code, etc.
    ref: str | None = Field(default=None, sa_column=Column(String(128), nullable=True))
    # Set for awards that must fire at most once (daily bonus per day, a medal,
    # a milestone, a specific score's top-play). Indexed (NOT unique on purpose):
    # idempotency is enforced by a pre-check in points_service. A hard unique
    # constraint could turn a rare race into an IntegrityError that rolls back
    # the surrounding score/medal transaction — far worse than the occasional
    # duplicate it would stop. Flows that truly must not double (access-code
    # redemption) get their hard guarantee on torii_access_code_redemptions.
    idempotency_key: str | None = Field(default=None, sa_column=Column(String(160), index=True, nullable=True))
    # Running balance after this line, for cheap history rendering.
    balance_after: int = Field(default=0, sa_column=Column(Integer, nullable=False, default=0))
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True),
    )


class ToriiAccessCode(SQLModel, table=True):
    """A redeemable code that grants points. v1 grants points only; the
    reward shape can grow later (auras, supporter days) without a schema change
    by reusing `note` / adding columns then."""

    __tablename__: str = "torii_access_codes"

    id: int | None = Field(default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True))
    code: str = Field(sa_column=Column(String(64), unique=True, index=True, nullable=False))
    amount: int = Field(default=0, sa_column=Column(Integer, nullable=False, default=0))
    # Optional cosmetic reward: a JSON array of catalog ids (trail / name-colour
    # / aura ids) the code unlocks on redeem, on top of any points. Null/empty =
    # points only. The reward shape can grow further (supporter days, etc.).
    grant_cosmetics: str | None = Field(default=None, sa_column=Column(String(1024), nullable=True))
    # Why this code exists, shown nowhere public — e.g. "bug report: thumbnails".
    note: str | None = Field(default=None, sa_column=Column(String(256), nullable=True))
    max_uses: int = Field(default=1, sa_column=Column(Integer, nullable=False, default=1))
    uses: int = Field(default=0, sa_column=Column(Integer, nullable=False, default=0))
    expires_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    created_by: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))


class ToriiAccessCodeRedemption(SQLModel, table=True):
    """One row per (code, user). The unique constraint stops a user redeeming
    the same code twice."""

    __tablename__: str = "torii_access_code_redemptions"
    __table_args__ = (UniqueConstraint("code_id", "user_id", name="uq_torii_access_code_user"),)

    id: int | None = Field(default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True))
    code_id: int = Field(
        sa_column=Column(BigInteger, ForeignKey("torii_access_codes.id", ondelete="CASCADE"), index=True, nullable=False)
    )
    user_id: int = Field(
        sa_column=Column(BigInteger, ForeignKey("lazer_users.id", ondelete="CASCADE"), index=True, nullable=False)
    )
    redeemed_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
