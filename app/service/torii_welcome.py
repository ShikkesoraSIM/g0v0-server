"""Los mensajes que recibe alguien recien llegado a Torii.

Dos momentos distintos:

1. Al registrarse: ToriiHalo le manda el link del Discord. Va en el registro y no
   en cada login porque es un mensaje de BIENVENIDA: mandarselo a los mil que ya
   juegan hace meses seria un mensaje masivo disfrazado.
2. Cuando agrega al fundador como amigo: le contesta el fundador en persona y le
   cae un regalo de puntos.

El regalo es idempotente POR USUARIO y para siempre (idempotency_key), no "una vez
por amistad nueva": si no, alcanzaba con borrar el amigo y volver a agregarlo para
cobrar 300 puntos cada vez.
"""

from __future__ import annotations

import asyncio

from app.const import BANCHOBOT_ID
from app.database.user import User
from app.log import logger
from app.models.torii_points import PointReason
from app.service import points_service

from sqlmodel.ext.asyncio.session import AsyncSession

def _founder_user_id() -> int:
    """El id del dueño, del mismo lugar que lo saca el auto-follow.

    Importado adentro y no arriba para no atar este modulo al de relaciones en el
    import: los dos se cargan desde routers distintos.
    """
    from app.service.owner_friend_service import owner_user_id

    return owner_user_id()

FOUNDER_GIFT_POINTS = 300

WELCOME_MESSAGE = (
    "Welcome to Torii! Please join the Discord server for important announcements "
    "and update changelogs! https://discord.gg/fZXsZFT5Xv"
)

FOUNDER_FRIEND_MESSAGE = (
    "Welcome to Torii! I hope you enjoy your time here. If you have any great ideas "
    "to make this server better with new and cool features (as crazy as they might "
    "sound), please request them. If you have any troubles, please use the Discord "
    "Server to report them or get help through tickets! Thanks young grasshopper, "
    "a thank-you-gift is on its way to you!"
)

FOUNDER_GIFT_MESSAGE = (
    "Thank you for being here! Run away with this small pouch and don't tell anyone! "
    "-Shikkesora, Torii Founder"
)


async def _send_pm_from(sender_id: int, session: AsyncSession, user: User, text: str) -> bool:
    """Un PM de `sender_id` a `user`. Devuelve si salio.

    Reusa el Bot de banchobot porque ya resuelve lo dificil (crear el canal PM si
    no existe, meter a los dos, y avisarle al server de chat para que le llegue en
    vivo al que esta conectado). Lo unico que cambia es de quien sale.
    """
    try:
        from app.router.notification.banchobot import Bot

        emisor = Bot(bot_user_id=sender_id)
        channel = await emisor._ensure_pm_channel(user, session)
        if channel is None:
            return False
        await emisor._send_message(channel, text, session)
        return True
    except Exception as e:
        logger.warning(f"torii_welcome: no pude mandar el PM de {sender_id} a {user.id}: {e}")
        return False


# Cuanto espera el saludo desde que entras. Cae con la sesion ya arrancada en vez de
# encima de la pantalla de login, que es cuando nadie mira el chat.
WELCOME_DELAY_SECONDS = 12.0

# Los ids que ya tienen un saludo en camino. asyncio no guarda referencia fuerte a las
# tareas sueltas (se las puede llevar el recolector a mitad de la espera), y de paso
# esto evita que dos logins seguidos manden el saludo dos veces.
_en_camino: dict[int, asyncio.Task] = {}


def schedule_pending_welcome(user_id: int) -> None:
    """Deja el saludo en camino. No se espera: el login no se frena por esto."""
    if user_id in _en_camino:
        return
    tarea = asyncio.create_task(_deliver_pending_welcome(user_id))
    _en_camino[user_id] = tarea
    tarea.add_done_callback(lambda _: _en_camino.pop(user_id, None))


async def _deliver_pending_welcome(user_id: int) -> None:
    """Espera, y recien ahi manda el saludo si sigue pendiente.

    El flag se apaga DESPUES de mandarlo, no antes: si el mensaje falla (o el proceso
    se reinicia en medio de la espera) queda pendiente y lo entrega el proximo login
    desde el cliente. Preferimos que llegue tarde a que se pierda.
    """
    try:
        await asyncio.sleep(WELCOME_DELAY_SECONDS)

        from app.dependencies.database import with_db

        async with with_db() as session:
            user = await session.get(User, user_id)
            if user is None or not user.torii_welcome_pending:
                return

            if not await _send_pm_from(BANCHOBOT_ID, session, user, WELCOME_MESSAGE):
                return

            user.torii_welcome_pending = False
            session.add(user)
            await session.commit()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"torii_welcome: no pude entregar el saludo a {user_id}: {e}")


async def handle_founder_friend(session: AsyncSession, user: User) -> bool:
    """Alguien agrego al fundador: le contesta y le regala puntos. Una sola vez.

    El regalo manda: si `award` dice que no lo aplico (ya lo habia cobrado antes),
    tampoco se mandan los mensajes. Asi los dos quedan atados a la misma decision y
    no puede pasar que reciba el texto del regalo sin el regalo, ni que le llegue
    de nuevo por agregar y sacar al fundador en loop.
    """
    user_id = user.id
    founder_id = _founder_user_id()
    if user_id is None or user_id == founder_id:
        return False

    otorgado = await points_service.award(
        session,
        user_id,
        FOUNDER_GIFT_POINTS,
        PointReason.GIFT,
        ref="founder_friend_gift",
        idempotency_key=f"founder_friend_gift:{user_id}",
    )
    if not otorgado:
        return False

    await _send_pm_from(founder_id, session, user, FOUNDER_FRIEND_MESSAGE)
    await _send_pm_from(founder_id, session, user, FOUNDER_GIFT_MESSAGE)
    return True
