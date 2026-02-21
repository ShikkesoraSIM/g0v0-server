from typing import Annotated

from app.database.auth import OAuthToken
from app.database.chat import ChannelType, ChatChannel, ChatMessage, ChatMessageModel, MessageType
from app.database.user import User
from app.database.verification import LoginSession, LoginSessionResp, TrustedDevice, TrustedDeviceResp
from app.const import BANCHOBOT_ID
from app.dependencies.database import Database
from app.dependencies.geoip import GeoIPService
from app.dependencies.user import UserAndToken, get_client_user_and_token
from app.models.notification import ChannelMessage, GlobalAnnouncement
from app.router.notification.server import server

from .router import router

from fastapi import HTTPException, Security
from pydantic import BaseModel
from sqlmodel import col, select


class SessionsResp(BaseModel):
    total: int
    current: int = 0
    sessions: list[LoginSessionResp]


@router.get(
    "/admin/sessions",
    name="获取当前用户的登录会话列表",
    tags=["用户会话", "g0v0 API", "管理"],
    response_model=SessionsResp,
)
async def get_sessions(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    geoip: GeoIPService,
):
    current_user, token = user_and_token
    current = 0

    sessions = (
        await session.exec(
            select(
                LoginSession,
            )
            .where(LoginSession.user_id == current_user.id, col(LoginSession.is_verified).is_(True))
            .order_by(col(LoginSession.created_at).desc())
        )
    ).all()
    resp = []
    for s in sessions:
        resp.append(LoginSessionResp.from_db(s, geoip))
        if s.token_id == token.id:
            current = s.id

    return SessionsResp(
        total=len(sessions),
        current=current,
        sessions=resp,
    )


@router.delete(
    "/admin/sessions/{session_id}",
    name="注销指定的登录会话",
    tags=["用户会话", "g0v0 API", "管理"],
    status_code=204,
)
async def delete_session(
    session: Database,
    session_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    current_user, token = user_and_token
    if session_id == token.id:
        raise HTTPException(status_code=400, detail="Cannot delete the current session")

    db_session = await session.get(LoginSession, session_id)
    if not db_session or db_session.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Session not found")

    await session.delete(db_session)

    token = await session.get(OAuthToken, db_session.token_id or 0)
    if token:
        await session.delete(token)

    await session.commit()
    return


class TrustedDevicesResp(BaseModel):
    total: int
    current: int = 0
    devices: list[TrustedDeviceResp]


@router.get(
    "/admin/trusted-devices",
    name="获取当前用户的受信任设备列表",
    tags=["用户会话", "g0v0 API", "管理"],
    response_model=TrustedDevicesResp,
)
async def get_trusted_devices(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    geoip: GeoIPService,
):
    current_user, token = user_and_token
    devices = (
        await session.exec(
            select(TrustedDevice)
            .where(TrustedDevice.user_id == current_user.id)
            .order_by(col(TrustedDevice.last_used_at).desc())
        )
    ).all()

    current_device_id = (
        await session.exec(
            select(TrustedDevice.id)
            .join(LoginSession, col(LoginSession.device_id) == TrustedDevice.id)
            .where(
                LoginSession.token_id == token.id,
                TrustedDevice.user_id == current_user.id,
            )
            .limit(1)
        )
    ).first()

    return TrustedDevicesResp(
        total=len(devices),
        current=current_device_id or 0,
        devices=[TrustedDeviceResp.from_db(device, geoip) for device in devices],
    )


@router.delete(
    "/admin/trusted-devices/{device_id}",
    name="移除受信任设备",
    tags=["用户会话", "g0v0 API", "管理"],
    status_code=204,
)
async def delete_trusted_device(
    session: Database,
    device_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    current_user, token = user_and_token
    device = await session.get(TrustedDevice, device_id)
    current_device_id = (
        await session.exec(
            select(TrustedDevice.id)
            .join(LoginSession, col(LoginSession.device_id) == TrustedDevice.id)
            .where(
                LoginSession.token_id == token.id,
                TrustedDevice.user_id == current_user.id,
            )
            .limit(1)
        )
    ).first()
    if device_id == current_device_id:
        raise HTTPException(status_code=400, detail="Cannot delete the current trusted device")

    if not device or device.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Trusted device not found")

    await session.delete(device)
    await session.commit()
    return


class GlobalAnnouncementReq(BaseModel):
    message: str
    title: str = "Server Announcement"
    severity: str = "warning"
    also_send_pm: bool = True
    online_only: bool = True


class GlobalAnnouncementResp(BaseModel):
    sent_to: int
    severity: str
    title: str
    online_only: bool


@router.post(
    "/admin/global-announcement",
    name="Send global announcement notification",
    tags=["管理", "通知"],
    response_model=GlobalAnnouncementResp,
)
async def send_global_announcement(
    session: Database,
    req: GlobalAnnouncementReq,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    current_user, _token = user_and_token
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")

    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="message cannot be empty")

    severity = req.severity.lower()
    if severity not in {"info", "warning", "error"}:
        raise HTTPException(status_code=422, detail="severity must be one of: info, warning, error")

    if req.online_only:
        # Prefer websocket presence; DB is_online can lag behind.
        connected_user_ids = [uid for uid, sockets in server.connect_client.items() if sockets]
        if not connected_user_ids:
            receivers: list[int] = []
        else:
            receivers = (
                await session.exec(
                    select(User.id).where(
                        col(User.id).in_(connected_user_ids),
                        User.id != BANCHOBOT_ID,
                        ~User.is_restricted_query(col(User.id)),
                    )
                )
            ).all()
    else:
        receivers = (
            await session.exec(
                select(User.id).where(
                    User.id != BANCHOBOT_ID,
                    ~User.is_restricted_query(col(User.id)),
                )
            )
        ).all()

    detail = GlobalAnnouncement.init(
        source_user_id=current_user.id,
        title=req.title.strip() or "Server Announcement",
        message=message,
        severity=severity,  # pyright: ignore[reportArgumentType]
        receiver_ids=receivers,
    )
    await server.new_private_notification(detail)

    if req.also_send_pm:
        bot_user = await session.get(User, BANCHOBOT_ID)
        if bot_user is None:
            raise HTTPException(status_code=500, detail="BanchoBot user not found")

        announce_channel = (
            await session.exec(
                select(ChatChannel).where(
                    ChatChannel.type == ChannelType.PUBLIC,
                    ChatChannel.channel_name == "announce",
                )
            )
        ).first()
        if announce_channel is None:
            announce_channel = (
                await session.exec(
                    select(ChatChannel).where(
                        ChatChannel.type == ChannelType.PUBLIC,
                        ChatChannel.channel_name == "osu!",
                    )
                )
            ).first()
        if announce_channel is not None:
            notif_message = ChatMessage(
                channel_id=announce_channel.channel_id,
                sender_id=BANCHOBOT_ID,
                type=MessageType.PLAIN,
                content=f"[{detail.title}] {message}",
            )
            detail_as_channel_msg = ChannelMessage.init(
                message=notif_message,
                user=bot_user,
                receiver=receivers,
                channel_type=announce_channel.type,
            )
            await server.new_private_notification(detail_as_channel_msg)

        targets = (
            await session.exec(
                select(User).where(
                    col(User.id).in_(receivers),
                )
            )
        ).all()

        for target in targets:
            channel = await ChatChannel.get_pm_channel(target.id, BANCHOBOT_ID, session)
            if channel is None:
                user_min = min(target.id, BANCHOBOT_ID)
                user_max = max(target.id, BANCHOBOT_ID)
                channel = ChatChannel(
                    channel_name=f"pm_{user_min}_{user_max}",
                    description="Private message channel",
                    type=ChannelType.PM,
                )
                session.add(channel)
                await session.flush()
                await session.refresh(channel)

            await server.batch_join_channel([target, bot_user], channel)

            chat_msg = ChatMessage(
                channel_id=channel.channel_id,
                sender_id=BANCHOBOT_ID,
                type=MessageType.PLAIN,
                content=f"[{detail.title}] {message}",
            )
            session.add(chat_msg)
            await session.flush()
            await session.refresh(chat_msg)
            chat_resp = await ChatMessageModel.transform(chat_msg, includes=["sender"])
            await server.send_message_to_channel(chat_resp)
            pm_detail = ChannelMessage.init(
                message=chat_msg,
                user=bot_user,
                receiver=[target.id],
                channel_type=ChannelType.PM,
            )
            await server.new_private_notification(pm_detail)

        await session.commit()

    return GlobalAnnouncementResp(
        sent_to=len(receivers),
        severity=severity,
        title=detail.title,
        online_only=req.online_only,
    )
