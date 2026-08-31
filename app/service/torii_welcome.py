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
import json

from app.const import BANCHOBOT_ID
from app.database.user import User
from app.log import logger

from sqlmodel import col, select
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

# Lo que se ve como remitente en el regalo, en vez del "Torii Halo" por defecto.
FOUNDER_GIFT_SENDER = "Shikkesora"

FOUNDER_GIFT_MESSAGE = (
    "Thank you for being here! Run away with this small pouch and don't tell anyone! "
    "-Shikkesora, Torii Founder"
)

# El aura que viene con el regalo.
#
# APAGADO hasta que el .toriicosmetic este SHIPPEADO en un cliente publicado.
# Mientras el archivo no viaje en el cliente, otorgarla deja a la persona con el
# aura marcada como suya, un mensaje diciendole que la equipe, y nada que
# equipar: el cliente no la puede dibujar porque no la tiene.
#
# Poner el id aca de nuevo SOLO despues de que salga la release que la incluye.
FOUNDER_GIFT_AURA_ID: str | None = None

FOUNDER_GIFT_AURA_MESSAGE = (
    "Oh, and the grasshoppers seem to like you. They're yours now: "
    "equip them from Settings, under your aura."
)


async def _send_pm_from(sender_id: int, session: AsyncSession, user_id: int, text: str) -> bool:
    """Un PM de `sender_id` al usuario `user_id`. Devuelve si salio.

    Reusa el Bot de banchobot porque ya resuelve lo dificil (crear el canal PM si
    no existe, meter a los dos, y avisarle al server de chat para que le llegue en
    vivo al que esta conectado). Lo unico que cambia es de quien sale.

    Toma el ID y NO el objeto User a proposito, y lo recarga con populate_existing
    en cada llamada. Mandar el PM commitea, y con expire_on_commit en True (el
    default) ese commit deja expirado al User que nos pasaron: la llamada siguiente
    toca un atributo, eso dispara IO lazy fuera del greenlet y explota con
    MissingGreenlet. Pasaba justo con los dos mensajes seguidos del regalo del
    fundador: salia el primero y el segundo se perdia.
    """
    try:
        from app.router.notification.banchobot import Bot

        # populate_existing fuerza releer: sin eso, si el objeto ya esta en el
        # identity map expirado, get() lo devuelve tal cual y el problema sigue.
        user = await session.get(User, user_id, populate_existing=True)
        if user is None:
            return False

        emisor = Bot(bot_user_id=sender_id)
        channel = await emisor._ensure_pm_channel(user, session)
        if channel is None:
            return False
        await emisor._send_message(channel, text, session)
        return True
    except Exception as e:
        logger.warning(f"torii_welcome: no pude mandar el PM de {sender_id} a {user_id}: {e}")
        return False


# Respiro DESPUES de que el chat del jugador ya esta escuchando. No se cuenta desde
# el login: a los 12 segundos de loguearse el cliente todavia esta armando la
# conexion, y meterlo a un canal nuevo en ese momento le tiraba dos errores en la
# cara ("Failed to join channel" y una excepcion de SignalR). Un mensaje de
# bienvenida que aterriza junto a dos carteles rojos asusta mas de lo que saluda.
WELCOME_DELAY_SECONDS = 8.0

# Hasta cuando esperamos que el chat conecte antes de rendirnos. Si no llega, el flag
# queda pendiente y se reintenta en el proximo login.
WELCOME_CONNECT_TIMEOUT_SECONDS = 120.0

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


async def _wait_for_chat(user_id: int) -> bool:
    """Espera a que el chat del jugador este escuchando de verdad.

    `connect_client` tiene los websockets de chat abiertos por usuario. Mientras no
    haya ninguno, mandarle un PM significa crearle un canal y empujarle un mensaje a
    alguien que todavia no puede recibirlos: el cliente responde con "Failed to join
    channel" y le queda el cartel de error puesto.
    """
    from app.router.notification.server import server

    esperado = 0.0
    while esperado < WELCOME_CONNECT_TIMEOUT_SECONDS:
        if server.connect_client.get(user_id):
            return True
        await asyncio.sleep(1.0)
        esperado += 1.0
    return False


async def _deliver_pending_welcome(user_id: int) -> None:
    """Espera, y recien ahi manda el saludo si sigue pendiente.

    El flag se apaga DESPUES de mandarlo, no antes: si el mensaje falla (o el proceso
    se reinicia en medio de la espera) queda pendiente y lo entrega el proximo login
    desde el cliente. Preferimos que llegue tarde a que se pierda.
    """
    try:
        if not await _wait_for_chat(user_id):
            logger.info(f"torii_welcome: {user_id} nunca conecto el chat, queda pendiente")
            return

        await asyncio.sleep(WELCOME_DELAY_SECONDS)

        from app.dependencies.database import with_db

        async with with_db() as session:
            user = await session.get(User, user_id)
            if user is None or not user.torii_welcome_pending:
                return

            if not await _send_pm_from(BANCHOBOT_ID, session, user_id, WELCOME_MESSAGE):
                return

            # Mandar el PM commiteo, asi que el User de arriba quedo expirado y
            # tocarlo ahora reventaria. Lo traemos de nuevo antes de marcarlo.
            user = await session.get(User, user_id, populate_existing=True)
            if user is None:
                return

            user.torii_welcome_pending = False
            session.add(user)
            await session.commit()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"torii_welcome: no pude entregar el saludo a {user_id}: {e}")


class SkipAuraGift(Exception):
    """El aura del regalo esta apagada. No es un error: se saltea y listo."""


async def send_bot_pm(session: AsyncSession, user_id: int, text: str) -> bool:
    """Un PM de ToriiHalo a `user_id`. Devuelve si salio.

    Envoltorio publico de `_send_pm_from` para el resto del codigo. A diferencia de
    una notificacion in-game, esto queda guardado en el canal: si la persona no
    estaba conectada, lo lee cuando entra.
    """
    return await _send_pm_from(BANCHOBOT_ID, session, user_id, text)


async def handle_founder_friend(session: AsyncSession, user_id: int) -> bool:
    """Alguien agrego al fundador: le contesta y le regala puntos. Una sola vez.

    El regalo manda: si `award` dice que no lo aplico (ya lo habia cobrado antes),
    tampoco se mandan los mensajes. Asi los dos quedan atados a la misma decision y
    no puede pasar que reciba el texto del regalo sin el regalo, ni que le llegue
    de nuevo por agregar y sacar al fundador en loop.
    """
    # Recibe el ID y no el objeto User a proposito. El router commitea la amistad justo
    # antes de llamar aca, y las sesiones se arman con expire_on_commit en True (el
    # default, ver app/dependencies/database.py): despues de ese commit CUALQUIER atributo
    # del User queda expirado, y leerlo dispara un refresh lazy que en async revienta.
    # Con el objeto, esto se caia en el propio `user.id` y nadie cobraba el regalo.
    founder_id = _founder_user_id()
    if user_id is None or user_id == founder_id:
        return False

    from app.database.torii_gifts import ToriiGift

    # Un REGALO de verdad, no puntos sueltos mas un PM.
    #
    # Antes esto otorgaba los puntos por su cuenta y mandaba el texto del regalo
    # como mensaje de chat. Estaba mal por dos lados: el texto del regalo es parte
    # del regalo y va adentro de su presentacion (el cliente ya lo muestra
    # envuelto, con remitente y puntos), y ademas cada PM extra creaba su propia
    # entrada en la lista de chats.
    #
    # ToriiGift ya trae todo lo necesario: puntos, mensaje, remitente y hasta
    # cosmeticos para cuando el aura este aprobada y shippeada.
    # Se mira tambien el mensaje y no solo created_by: un regalo que el fundador
    # mande A MANO desde el admin tambien lleva su id, y sin esta condicion
    # bloquearia el automatico para esa persona para siempre.
    ya_tiene = (
        await session.exec(
            select(ToriiGift.id).where(
                col(ToriiGift.recipient_id) == user_id,
                col(ToriiGift.created_by) == founder_id,
                col(ToriiGift.message) == FOUNDER_GIFT_MESSAGE,
            )
        )
    ).first()

    # Idempotente por su cuenta: agregar y sacar al fundador en loop no paga otra vez.
    if ya_tiene is not None:
        return False

    session.add(
        ToriiGift(
            recipient_id=user_id,
            points=FOUNDER_GIFT_POINTS,
            grant_cosmetics=(
                json.dumps([FOUNDER_GIFT_AURA_ID]) if FOUNDER_GIFT_AURA_ID else None
            ),
            message=FOUNDER_GIFT_MESSAGE,
            sender=FOUNDER_GIFT_SENDER,
            created_by=founder_id,
        )
    )
    await session.commit()

    # Por chat va SOLO el saludo del fundador. Lo del regalo lo cuenta el regalo.
    await _send_pm_from(founder_id, session, user_id, FOUNDER_FRIEND_MESSAGE)
    return True
