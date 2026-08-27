"""El dueño del server sigue a todo el mundo.

Es una decision de Torii, no de osu!: el dueño quiere tener en su lista a todos
los que juegan, sin esperar a que lo agreguen a el. Ojo que es en UN SOLO
sentido. Que el te siga no significa que vos lo sigas, y la lista de amigos del
otro no cambia. Lo unico que se ve del otro lado es un seguidor mas.

Ya existia la mitad reciproca de esto (si alguien agrega al dueño, el dueño lo
agrega de vuelta). Aca esta la mitad que faltaba: los que nunca lo agregaron.
"""

from __future__ import annotations

from app.config import settings
from app.database.relationship import Relationship, RelationshipType
from app.log import log

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

logger = log("OwnerFriend")

# Historicamente esto estaba escrito a mano en el router de relaciones. El
# default se mantiene para no cambiarle el comportamiento a nadie que actualice
# sin tocar el .env.
DEFAULT_OWNER_FRIEND_USER_ID = 3


def owner_user_id() -> int:
    configured = settings.torii_owner_user_id
    return configured if configured > 0 else DEFAULT_OWNER_FRIEND_USER_ID


async def owner_follows(session: AsyncSession, user_id: int) -> bool:
    """Hace que el dueño siga a este usuario. Devuelve si agrego algo.

    No commitea: lo hace quien llama, junto con el resto de lo que este haciendo.

    Es idempotente y NO pisa un bloqueo: si el dueño bloqueó a alguien, que se
    registre de nuevo (o que lo siga) no puede convertir ese bloqueo en amistad.
    Sin ese cuidado, banear a alguien y que vuelva a entrar le limpiaba el
    bloqueo solo.
    """
    owner_id = owner_user_id()
    if not settings.torii_owner_follows_everyone or user_id == owner_id:
        return False

    existing = (
        await session.exec(
            select(Relationship).where(
                Relationship.user_id == owner_id,
                Relationship.target_id == user_id,
            )
        )
    ).first()

    if existing is not None:
        return False

    session.add(
        Relationship(
            user_id=owner_id,
            target_id=user_id,
            type=RelationshipType.FOLLOW,
        )
    )
    logger.info(f"owner {owner_id} now follows {user_id}")
    return True
