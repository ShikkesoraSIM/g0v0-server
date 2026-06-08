"""Cosmetic store curation — DB model.

A tiny key-value table holding store-wide config that admins control. Right
now there is one key, ``disabled``, whose value is a JSON array of catalog ids
(trail / name-colour / aura ids) an admin has pulled OUT of the store pool, so
they aren't offered for sale to anyone. Empty / missing = everything sellable.

A KV row (rather than a column per setting or a row per id) keeps room for
future store config — a featured/rotation queue, per-item price overrides —
without another schema change: just add a new key.
"""

from __future__ import annotations

from datetime import datetime

from app.utils import utcnow

from sqlalchemy import Text
from sqlmodel import (
    BigInteger,
    Column,
    DateTime,
    Field,
    SQLModel,
    String,
)


class ToriiStoreConfig(SQLModel, table=True):
    """One config key for the cosmetic store (e.g. ``disabled`` -> JSON list)."""

    __tablename__: str = "torii_store_config"

    config_key: str = Field(sa_column=Column(String(64), primary_key=True))
    value: str = Field(default="", sa_column=Column(Text, nullable=False))
    # Last admin who wrote this key, for a light audit trail.
    updated_by: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
