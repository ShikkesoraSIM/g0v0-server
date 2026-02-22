from datetime import date as dt_date, datetime
from typing import Any

from sqlmodel import Field, SQLModel


class DailyChallengeBase(SQLModel):
    beatmap_id: int
    ruleset_id: int
    required_mods: str
    allowed_mods: str
    room_id: int | None = None
    max_attempts: int | None = None
    time_limit: int | None = None


class DailyChallenge(DailyChallengeBase, table=True):
    __tablename__ = "daily_challenge"

    date: dt_date = Field(primary_key=True, nullable=False, index=True)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DailyChallengeCreate(DailyChallengeBase):
    date: str


class DailyChallengeUpdate(SQLModel):
    beatmap_id: int | None = None
    ruleset_id: int | None = None
    required_mods: str | None = None
    allowed_mods: str | None = None
    room_id: int | None = None
    max_attempts: int | None = None
    time_limit: int | None = None


class DailyChallengeResponse(DailyChallengeBase):
    date: dt_date
    created_at: datetime
    updated_at: datetime
    beatmap: dict[str, Any] | None = None
