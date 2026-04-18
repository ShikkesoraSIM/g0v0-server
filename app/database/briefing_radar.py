from datetime import datetime

from app.utils import utcnow

from sqlalchemy import Column, DateTime, Index, JSON, String
from sqlmodel import BigInteger, Field, ForeignKey, SQLModel


class ToriiBriefingRadarSnapshot(SQLModel, table=True):
    __tablename__: str = "torii_briefing_radar_snapshots"
    __table_args__ = (
        Index("ix_torii_briefing_radar_updated_at", "updated_at"),
    )

    user_id: int = Field(sa_column=Column(BigInteger, ForeignKey("lazer_users.id"), primary_key=True))
    gamemode: str = Field(sa_column=Column(String(32), primary_key=True))
    variant: str = Field(sa_column=Column(String(32), primary_key=True))
    snapshot_data: list[dict] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime, nullable=False))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime, nullable=False))
