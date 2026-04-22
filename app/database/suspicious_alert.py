from __future__ import annotations

from datetime import datetime
from typing import Any

from app.utils import utcnow

from sqlalchemy import Column, DateTime, Text
from sqlmodel import JSON, BigInteger, Field, SQLModel, VARCHAR


class SuspiciousAlert(SQLModel, table=True):
    __tablename__: str = "suspicious_alerts"

    id: int | None = Field(default=None, primary_key=True)
    kind: str = Field(sa_column=Column(VARCHAR(64), nullable=False, index=True))
    severity: str = Field(sa_column=Column(VARCHAR(16), nullable=False, index=True))
    fingerprint: str = Field(sa_column=Column(VARCHAR(128), nullable=False, unique=True, index=True))

    user_id: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True, index=True))
    score_id: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True, index=True))
    beatmap_id: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True, index=True))

    title: str = Field(sa_column=Column(VARCHAR(200), nullable=False))
    body: str = Field(sa_column=Column(Text, nullable=False))
    payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("metadata", JSON, nullable=False),
    )

    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime, nullable=False, index=True))
    dispatched_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True, index=True))
    resolved_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True, index=True))
