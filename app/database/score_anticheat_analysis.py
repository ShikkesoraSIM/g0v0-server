"""Cached anti-cheat analysis result, one row per score.

Every call to the external detection service produces a verdict. The
SuspiciousAlert table only stores the rows where the verdict crossed
the alert threshold; this table stores the result of EVERY analysis
regardless of verdict so the admin panel can browse all reviewed
replays without re-running the analyser.

Re-analysing the same score after a detector update is supported by
the upsert pattern in `app/database/score.py`: the upsert overwrites
this row with the newer result.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.utils import utcnow

from sqlalchemy import Column, DateTime, ForeignKey, Index, Text
from sqlmodel import JSON, BigInteger, Boolean, Field, Float, SQLModel, VARCHAR


class ScoreAnticheatAnalysis(SQLModel, table=True):
    __tablename__: str = "score_anticheat_analysis"
    __table_args__ = (
        Index("idx_sa_analysis_verdict", "verdict", "analyzed_at"),
        Index("idx_sa_analysis_user", "user_id", "analyzed_at"),
        Index("idx_sa_analysis_time", "analyzed_at"),
    )

    score_id: int = Field(
        sa_column=Column(
            BigInteger,
            ForeignKey("scores.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    user_id: int = Field(sa_column=Column(BigInteger, nullable=False, index=True))

    verdict: str = Field(sa_column=Column(VARCHAR(32), nullable=False, index=True))
    confidence: float = Field(default=0.0, sa_column=Column(Float, nullable=False))
    trust_factor_applied: float = Field(default=50.0, sa_column=Column(Float, nullable=False))

    detectors_fired: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    reasons: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    metrics: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))

    replay_was_available: bool = Field(default=False, sa_column=Column(Boolean, nullable=False))
    analyzer_version: str = Field(default="1", sa_column=Column(VARCHAR(64), nullable=False))
    error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    analyzed_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime, nullable=False, index=True),
    )
