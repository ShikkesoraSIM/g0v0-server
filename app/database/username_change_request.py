from __future__ import annotations

from datetime import datetime

from app.utils import utcnow

from sqlalchemy import Column, DateTime, Text
from sqlmodel import VARCHAR, BigInteger, Field, SQLModel

# Lifecycle of a user-requested username change. Requests start PENDING and are
# resolved by an admin from the web panel. We keep terminal rows around as an
# audit trail instead of deleting them.
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


class UsernameChangeRequest(SQLModel, table=True):
    __tablename__: str = "username_change_requests"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(sa_column=Column(BigInteger, nullable=False, index=True))
    # Snapshot of the username at request time so the panel can show "old -> new"
    # even if the account is renamed by some other path before review.
    current_username: str = Field(sa_column=Column(VARCHAR(32), nullable=False))
    requested_username: str = Field(sa_column=Column(VARCHAR(32), nullable=False, index=True))
    status: str = Field(default=STATUS_PENDING, sa_column=Column(VARCHAR(16), nullable=False, index=True))
    reject_reason: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime, nullable=False, index=True))
    reviewed_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    reviewed_by_id: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
