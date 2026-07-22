"""Comfort star-rating pick (torii_comfort_picks).

Una vez por season, el jugador elige la dificultad (star rating) en la que se
siente comodo para ranked play. Ese pick decide QUE DIFICULTAD DE MAPAS le tira
el pool selector en sus matches.

IMPORTANTE: elegir star rating != elegir ELO. El pick NO siembra el MMR directo; el MMR
se GANA jugando (W/L). Si pickeas alto y no das la talla, jugas mapas dificiles y
perdes puntos merecidamente. ``seed_rating`` guarda la DIFICULTAD objetivo de los
mapas equivalente al SR elegido (para el selector), NO el rating del jugador.

Anti-sandbag: el pick tiene un PISO derivado de las top plays del jugador
(``floor = SR_de_su_top_play_con_mods - comfort_floor_offset``, clampeado a
``comfort_floor_min``), asi un jugador de 300pp que FCea 6 estrellas no puede
declararse "comfy" en 1 estrella para stompear gente debil. Se guarda una fila
por (user_id, ruleset_id, season_id) -> el unique constraint hace el gate de
"una vez por season". ``floor_at_pick`` y ``seed_rating`` van denormalizados
para auditoria.
"""

from __future__ import annotations

from datetime import datetime

from app.utils import utcnow

from sqlalchemy import Column, DateTime, Float, Integer, SmallInteger, UniqueConstraint
from sqlmodel import BigInteger, Field, SQLModel, VARCHAR


class ToriiComfortPick(SQLModel, table=True):
    __tablename__: str = "torii_comfort_picks"

    id: int | None = Field(default=None, primary_key=True)

    user_id: int = Field(sa_column=Column(BigInteger, nullable=False, index=True))
    # ruleset BASE (0 osu / 1 taiko / 2 catch / 3 mania), el pick es por ruleset base.
    ruleset_id: int = Field(sa_column=Column(SmallInteger, nullable=False))
    # id de season (config-driven, ej "2026-S1"). unico junto a (user, ruleset).
    season_id: str = Field(sa_column=Column(VARCHAR(32), nullable=False, index=True))

    picked_star_rating: float = Field(sa_column=Column(Float, nullable=False))
    # snapshot del piso calculado al momento del pick (auditoria).
    floor_at_pick: float = Field(sa_column=Column(Float, nullable=False))
    # el mu con el que sembramos matchmaking_user_stats (auditoria).
    seed_rating: int = Field(sa_column=Column(Integer, nullable=False))

    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime, nullable=False))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime, nullable=False))

    __table_args__ = (
        UniqueConstraint("user_id", "ruleset_id", "season_id", name="uq_comfort_user_mode_season"),
    )
