"""Notas de score (torii_score_notes).

Una nota por score: el dueño de la play le agrega un comentario corto ("mice
un miss en el slider mas facil del mapa...") y opcionalmente una imagen (se
procesa a un thumbnail chico server-side y se guarda en storage bajo
``score-notes/{score_id}.jpg``). Las leaderboards del cliente muestran un
iconito en los scores con nota y un tooltip con texto + imagen.

Solo el dueño del score puede crear/editar/borrar su nota (se valida contra
``scores.user_id``). username va denormalizado para que el tooltip no joinee.
"""

from __future__ import annotations

from datetime import datetime

from app.utils import utcnow

from sqlalchemy import Column, DateTime
from sqlmodel import BigInteger, Boolean, Field, SQLModel, VARCHAR


class ToriiScoreNote(SQLModel, table=True):
    __tablename__: str = "torii_score_notes"

    id: int | None = Field(default=None, primary_key=True)

    # el score anotado. unico: una nota por score.
    score_id: int = Field(sa_column=Column(BigInteger, nullable=False, unique=True, index=True))
    # dueño de la nota (== dueño del score, validado en el endpoint).
    user_id: int = Field(sa_column=Column(BigInteger, nullable=False, index=True))
    username: str = Field(sa_column=Column(VARCHAR(32), nullable=False))

    text: str = Field(sa_column=Column(VARCHAR(280), nullable=False))
    has_image: bool = Field(default=False, sa_column=Column(Boolean, nullable=False))

    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime, nullable=False))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime, nullable=False))
