"""Registro de renders de replay via o!rdr (torii_replay_renders).

Una fila por render pedido desde el cliente. Sirve para tres cosas:
- rate limiting / historial por user (el cooldown vive en Redis, esto es el registro)
- que el poller de fondo pueda seguir renders aunque el cliente se haya cerrado
- que ToriiHalo (bot de discord) anuncie los renders terminados con share=True
  (mismo patron poll+dispatch que suspicious_alerts / mod-alerts)

Estados: queued -> rendering -> done | failed. dispatched_at marca que el bot
ya lo posteo (dedup server-side, el bot es stateless).
"""

from __future__ import annotations

from datetime import datetime

from app.utils import utcnow

from sqlalchemy import Column, DateTime
from sqlmodel import BigInteger, Boolean, Field, SQLModel, VARCHAR


class ToriiReplayRender(SQLModel, table=True):
    __tablename__: str = "torii_replay_renders"

    id: int | None = Field(default=None, primary_key=True)

    # el renderID que devuelve o!rdr (POST /renders). unico por render.
    ordr_render_id: int = Field(sa_column=Column(BigInteger, nullable=False, unique=True, index=True))

    user_id: int = Field(sa_column=Column(BigInteger, nullable=False, index=True))
    score_id: int = Field(sa_column=Column(BigInteger, nullable=False, index=True))

    # denormalizado al momento del submit para que el bot arme el embed sin joins
    username: str = Field(sa_column=Column(VARCHAR(32), nullable=False))
    beatmap_title: str = Field(default="", sa_column=Column(VARCHAR(255), nullable=False))

    resolution: str = Field(sa_column=Column(VARCHAR(16), nullable=False))
    skin: str = Field(sa_column=Column(VARCHAR(128), nullable=False))
    motion_blur: bool = Field(default=False, sa_column=Column(Boolean, nullable=False))

    # opt-in del user para que el bot lo postee en discord
    share: bool = Field(default=True, sa_column=Column(Boolean, nullable=False, index=True))

    # queued / rendering / done / failed
    status: str = Field(default="queued", sa_column=Column(VARCHAR(16), nullable=False, index=True))
    progress: str = Field(default="", sa_column=Column(VARCHAR(160), nullable=False))
    video_url: str | None = Field(default=None, sa_column=Column(VARCHAR(255), nullable=True))
    error_code: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    error_message: str | None = Field(default=None, sa_column=Column(VARCHAR(255), nullable=True))

    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime, nullable=False, index=True))
    finished_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    dispatched_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True, index=True))
