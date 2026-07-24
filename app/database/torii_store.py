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
    ForeignKey,
    SQLModel,
    String,
    UniqueConstraint,
    col,
    select,
)
from sqlmodel.ext.asyncio.session import AsyncSession


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


class ToriiOwnedCosmetic(SQLModel, table=True):
    """One cosmetic a user owns (bought or granted). The server-authoritative
    ownership record; the client mirrors it into its local owned set so it
    persists across devices and reinstalls."""

    __tablename__: str = "torii_owned_cosmetics"
    __table_args__ = (UniqueConstraint("user_id", "cosmetic_id", name="uq_torii_owned_user_cosmetic"),)

    id: int | None = Field(default=None, sa_column=Column(BigInteger, primary_key=True))
    user_id: int = Field(
        sa_column=Column(
            BigInteger,
            ForeignKey("lazer_users.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        )
    )
    cosmetic_id: str = Field(sa_column=Column(String(128), nullable=False))
    source: str | None = Field(default=None, sa_column=Column(String(32), nullable=True))
    acquired_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


async def record_owned_cosmetics(session: AsyncSession, user_id: int, cosmetic_ids, source: str) -> None:
    """Idempotently record cosmetic ownership for a user, skipping any ids they
    already own. The caller commits."""
    ids = [str(c).strip() for c in (cosmetic_ids or []) if str(c).strip()]
    if not ids:
        return

    existing = set(
        (
            await session.exec(
                select(ToriiOwnedCosmetic.cosmetic_id).where(
                    ToriiOwnedCosmetic.user_id == user_id,
                    col(ToriiOwnedCosmetic.cosmetic_id).in_(ids),
                )
            )
        ).all()
    )

    for cid in ids:
        if cid not in existing:
            session.add(ToriiOwnedCosmetic(user_id=user_id, cosmetic_id=cid, source=source))


async def owned_aura_ids(session: AsyncSession, user_id: int) -> set[str]:
    """Los ids de AURA que un usuario posee como cosmetico desbloqueado (regalo/compra),
    con independencia de sus grupos.

    El entitlement de auras es ``owned OR group-granted``: un aura es un cosmetico que se
    desbloquea y listo (regalo/compra), y ADEMAS los roles la otorgan como bonus (ej el grupo
    ``admin`` habilita el ``admin-embers``). Esta funcion resuelve la parte ``owned``. Filtra por
    el catalogo de auras para no traer trails / name-colours que viven en la misma tabla."""
    from app.models.torii_auras import TORII_AURAS

    aura_ids = list(TORII_AURAS.keys())
    if not aura_ids:
        return set()

    rows = (
        await session.exec(
            select(ToriiOwnedCosmetic.cosmetic_id).where(
                ToriiOwnedCosmetic.user_id == user_id,
                col(ToriiOwnedCosmetic.cosmetic_id).in_(aura_ids),
            )
        )
    ).all()
    return set(rows)
