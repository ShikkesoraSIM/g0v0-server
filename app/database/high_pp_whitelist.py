from datetime import datetime

from app.utils import utcnow

from sqlalchemy import BigInteger, Column, DateTime, Text
from sqlmodel import Field, SQLModel


class HighPpWhitelist(SQLModel, table=True):
    """Jugadores que no vuelven a disparar la alerta de pp alto."""

    __tablename__: str = "high_pp_whitelist"

    user_id: int = Field(sa_column=Column(BigInteger, primary_key=True))
    added_by_id: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    reason: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime, nullable=False))
