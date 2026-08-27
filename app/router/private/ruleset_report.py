"""El cliente avisa que encontro rulesets ajenos y por eso no se conecta."""

from __future__ import annotations

from typing import Annotated

from app.database.user import User
from app.dependencies.database import Database
from app.log import log
from app.service.suspicious_alert_service import SuspiciousAlertService

from .router import router

from fastapi import Request
from pydantic import BaseModel, Field
from sqlmodel import col, select

logger = log("RulesetReport")


class CustomRulesetReport(BaseModel):
    username: str = Field(max_length=64)
    rulesets: list[str] = Field(default_factory=list, max_length=20)
    client_hash: str | None = Field(default=None, max_length=64)


@router.post("/client/custom-rulesets", name="reporte de rulesets ajenos", tags=["g0v0 API"])
async def report_custom_rulesets(
    session: Database,
    request: Request,
    body: CustomRulesetReport,
):
    """Recibe el aviso del cliente y se lo pasa al feed de moderacion.

    SIN AUTENTICAR a proposito, y tiene que ser asi: el cliente descubre los
    rulesets ANTES de loguearse y justamente por eso no se loguea, o sea que no
    tiene token para mandar. Lo unico que sabe es que nombre estaba por usar.

    Por lo mismo, nada de lo que llega aca se cree: el nombre puede no existir,
    los nombres de ruleset son texto libre y el hash puede ser cualquier cosa.
    Se guarda como DATO para que un mod lo mire, nunca se usa para banear ni
    para decidir nada solo. Si el usuario existe se ata el id para que la alerta
    linkee al perfil, y si no existe se manda igual sin id.

    Devuelve 204 siempre, incluso si algo falla: es un aviso, no puede convertir
    'no te podes conectar' en 'ademas te tira un error'.
    """
    try:
        nombre = body.username.strip()
        if not nombre:
            return

        user_id = (
            await session.exec(select(User.id).where(col(User.username) == nombre))
        ).first()

        ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else None)

        resultado = await SuspiciousAlertService.alert_custom_rulesets(
            session,
            username=nombre,
            rulesets=body.rulesets,
            client_hash=body.client_hash,
            ip_address=ip,
            user_id=user_id,
        )
        if resultado.created:
            await session.commit()
            logger.warning(f"custom rulesets reported by {nombre!r}: {body.rulesets}")
    except Exception:
        logger.warning("failed to record custom ruleset report", exc_info=True)
