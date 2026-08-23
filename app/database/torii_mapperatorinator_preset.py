"""Presets de Mapperatorinator (torii_mapperatorinator_presets).

Un preset es "esta combinacion de opciones me gusto": modelo, dificultad, año,
mapper a imitar, CS/AR/OD/HP y los descriptores que se pidieron y se evitaron.
Se guardan en el server y no en la maquina para que sobrevivan a formatear la
PC o a cambiar de computadora, que es todo el punto.

El contenido va como JSON crudo tal como lo arma el cliente: el server no
necesita entender que significa cada campo, solo devolverlo igual. Un nombre por
usuario (se pisa el anterior si repetis nombre, como una coleccion).
"""

from __future__ import annotations

from datetime import datetime

from app.utils import utcnow

from sqlalchemy import Column, DateTime, Text, UniqueConstraint
from sqlmodel import BigInteger, Field, Integer, SQLModel, VARCHAR


class ToriiMapperatorinatorPreset(SQLModel, table=True):
    __tablename__: str = "torii_mapperatorinator_presets"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_torii_mappera_preset_user_name"),)

    id: int | None = Field(default=None, primary_key=True)

    user_id: int = Field(sa_column=Column(BigInteger, nullable=False, index=True))
    name: str = Field(sa_column=Column(VARCHAR(60), nullable=False))

    # json del cliente, opaco para el server.
    settings: str = Field(sa_column=Column(Text, nullable=False))

    # de donde salio, cuando salio de algun lado: un preset guardado desde el mapa de
    # otra persona, o copiado de otro preset. El nombre va denormalizado para que la
    # lista no tenga que joinear usuarios solo para decir "de quien lo sacaste".
    origin_preset_id: int | None = Field(default=None, sa_column=Column(Integer, nullable=True, index=True))
    origin_user_id: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    origin_username: str | None = Field(default=None, sa_column=Column(VARCHAR(32), nullable=True))

    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime, nullable=False))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime, nullable=False))
