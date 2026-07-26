"""POST /api/private/discord-redeem — canjear coins de Discord por puntos de Torii.

ToriiHalo tiene su propia moneda ("Torii Coins") que se gana tipeando /daily y
/work en el server de Discord. Esto la conecta con la economia del juego: se
canjea por puntos, y los puntos llegan como un REGALO in-game, o sea que se
cobran terminando un mapa.

Por que el precio vive aca y no en el bot
-----------------------------------------
Mismo motivo por el que los precios de cosmeticos son server-side: el bot es un
segundo cliente y no puede ser la fuente de verdad de cuanto sale algo. El bot
solo dice "quiero canjear el tier X"; el server decide el precio, valida y
escribe el regalo. Un bot comprometido puede pedir un canje, no inventarse uno
gratis ni saltearse el cooldown.

Que NO hace esto
----------------
No deja conseguir cosmeticos sin jugar. Los puntos caen en tu cuenta y despues
se gastan en la tienda del juego al precio real, y encima el regalo se cobra
jugando. El canje es un goteo lento arriba de jugar, no un atajo.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Annotated, Any

from app.config import settings
from app.database.score import Score
from app.database.torii_gifts import ToriiGift
from app.database.user import User
from app.dependencies.database import Database
from app.log import log
from app.utils import utcnow

from fastapi import Header, HTTPException
from pydantic import BaseModel, Field as PydanticField
from sqlmodel import col, func, select

from .router import router

logger = log("DiscordRedeem")


# El catalogo. Precio y puntos viven aca y en ningun otro lado.
#
# Numeros de arranque, elegidos mirando la economia real del bot en jul-2026:
# 14 billeteras, 8.794 coins en total, el mas rico con 4.505. Con 1.000 coins
# por 100 puntos, si TODO el server canjeara todo lo que tiene salen ~880
# puntos, que son como tres trails repartidos entre catorce personas. Cuando la
# economia crezca hay que volver a mirar esto.
REDEEM_TIERS: dict[str, dict[str, int]] = {
    "small": {"cost_coins": 1000, "points": 100},
}

# Un canje por cuenta de TORII (no de Discord) cada tantos dias. Va por cuenta
# de Torii a proposito: diez alts de Discord linkeados a la misma cuenta
# comparten el mismo cooldown, asi que armar alts no sirve de nada.
REDEEM_COOLDOWN_DAYS = 7

# Y hay que estar jugando. Esto es lo que hace que el canje sea un premio para
# el que ya juega y no una forma de saltearse el juego.
REQUIRE_PLAY_WITHIN_DAYS = 30

GIFT_SENDER = "ToriiHalo"
# Marcador para reconocer nuestros regalos despues (el cooldown se calcula
# buscando el ultimo canje, asi que el texto tiene que ser estable).
GIFT_MESSAGE = "Redeemed with Torii Coins from the Discord. You typed for this."


def _validate_token(token: str | None) -> None:
    expected = (settings.discord_redeem_token or "").strip()
    provided = (token or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="discord redeem token is not configured")
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid discord redeem token")


class RedeemRequest(BaseModel):
    torii_user_id: int
    tier: str = PydanticField(default="small")


@router.post(
    "/discord-redeem",
    tags=["Torii"],
    name="Redeem Discord coins for points",
    description=(
        "Canjea coins de ToriiHalo por puntos de Torii, entregados como regalo in-game. "
        "El precio y las reglas son server-side. Solo para el bot."
    ),
)
async def discord_redeem(
    body: RedeemRequest,
    db: Database,
    x_torii_redeem_token: Annotated[str | None, Header(alias="X-Torii-Redeem-Token")] = None,
) -> dict[str, Any]:
    _validate_token(x_torii_redeem_token)

    tier = REDEEM_TIERS.get(body.tier)
    if tier is None:
        raise HTTPException(status_code=400, detail=f"unknown tier '{body.tier}'")

    user = await db.get(User, body.torii_user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="no such Torii user")

    # Un restringido no cobra nada.
    if await user.is_restricted(db):
        raise HTTPException(status_code=403, detail="account is restricted")

    now = utcnow()

    # 1. Tenes que haber jugado hace poco.
    since = now - timedelta(days=REQUIRE_PLAY_WITHIN_DAYS)
    recent_plays = (
        await db.exec(
            select(func.count())
            .select_from(Score)
            .where(
                Score.user_id == user.id,
                col(Score.ended_at) >= since,
            )
        )
    ).one()
    if not recent_plays:
        raise HTTPException(
            status_code=409,
            detail=f"no plays in the last {REQUIRE_PLAY_WITHIN_DAYS} days",
        )

    # 2. Cooldown. Esto ademas nos sirve de idempotencia: si el bot reintenta el
    #    mismo canje, el segundo cae adentro de la ventana y se rechaza, asi que
    #    no hace falta una columna de idempotency key.
    last = (
        await db.exec(
            select(ToriiGift)
            .where(
                col(ToriiGift.recipient_id) == user.id,
                col(ToriiGift.message) == GIFT_MESSAGE,
            )
            .order_by(col(ToriiGift.created_at).desc())
            .limit(1)
        )
    ).first()
    if last is not None and last.created_at is not None:
        created = last.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=now.tzinfo)
        available_at = created + timedelta(days=REDEEM_COOLDOWN_DAYS)
        if available_at > now:
            raise HTTPException(
                status_code=429,
                detail=f"on cooldown until {available_at.isoformat()}",
            )

    # 3. Listo, se escribe el regalo. Lo cobra en el juego despues del proximo mapa.
    gift = ToriiGift(
        recipient_id=user.id,
        points=tier["points"],
        grant_cosmetics=None,
        message=GIFT_MESSAGE,
        sender=GIFT_SENDER,
        created_by=None,
    )
    db.add(gift)

    user_id = user.id
    await db.commit()
    await db.refresh(gift)

    logger.info(
        "Discord redeem: user {user_id} got {points} points (tier {tier}, cost {cost} coins)",
        user_id=user_id,
        points=tier["points"],
        tier=body.tier,
        cost=tier["cost_coins"],
    )

    return {
        "gift_id": gift.id,
        "points": tier["points"],
        "cost_coins": tier["cost_coins"],
        "next_redeem_at": (now + timedelta(days=REDEEM_COOLDOWN_DAYS)).isoformat(),
    }
