from __future__ import annotations

from datetime import datetime

from sqlmodel import BigInteger, Column, DateTime, Field, SQLModel


class UserBadgeBase(SQLModel):
    user_id: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True, index=True))
    description: str
    image_url: str
    image_2x_url: str | None = None
    url: str | None = None
    awarded_at: datetime = Field(default_factory=datetime.utcnow, sa_column=Column(DateTime, nullable=False))


class UserBadge(UserBadgeBase, table=True):
    __tablename__ = "user_badges"

    id: int | None = Field(default=None, primary_key=True)


class UserBadgeCreate(UserBadgeBase):
    pass


class UserBadgeUpdate(SQLModel):
    user_id: int | None = None
    description: str | None = None
    image_url: str | None = None
    image_2x_url: str | None = None
    url: str | None = None
    awarded_at: datetime | None = None


class UserBadgeResponse(UserBadgeBase):
    id: int
    username: str | None = None

