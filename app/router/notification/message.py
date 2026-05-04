from typing import Annotated

from app.database import ChatChannelModel
from app.database.chat import (
    ChannelType,
    ChatChannel,
    ChatMessage,
    ChatMessageModel,
    MessageType,
    SilenceUser,
    UserSilenceResp,
)
from app.database.user import User, UserModel
from app.dependencies.database import Database, Redis, redis_message_client
from app.dependencies.param import BodyOrForm
from app.dependencies.user import get_current_user
from app.log import log
from app.models.notification import ChannelMessage, ChannelMessageTeam
from app.router.v2 import api_v2_router as router
from app.service.chat_url_normalizer import normalize_chat_urls
from app.service.redis_message_system import redis_message_system
from app.utils import api_doc

from .banchobot import bot
from .server import server

from fastapi import Depends, HTTPException, Path, Query, Security
from pydantic import BaseModel, Field
from sqlmodel import col, select


class KeepAliveResp(BaseModel):
    silences: list[UserSilenceResp] = Field(default_factory=list)


logger = log("Chat")


def _canonical_pm_channel_name(user_a_id: int, user_b_id: int) -> str:
    low, high = sorted((int(user_a_id), int(user_b_id)))
    return f"pm_{low}_{high}"


def _resolve_pm_receiver_ids(channel_name: str | None, channel_id: int, sender_id: int) -> list[int]:
    receiver_ids: list[int] = []

    pm_user_ids = ChatChannelModel._pm_user_ids_from_channel_name(channel_name)
    if pm_user_ids is not None:
        receiver_ids = [uid for uid in pm_user_ids if uid != sender_id]

    if not receiver_ids:
        receiver_ids = [uid for uid in server.channels.get(channel_id, []) if uid != sender_id]

    return list(dict.fromkeys(receiver_ids))


@router.post(
    "/chat/ack",
    name="Chat ack",
    response_model=KeepAliveResp,
    description="Keep chat connection alive and return recent silences.",
    tags=["Chat"],
)
async def keep_alive(
    session: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["chat.read"])],
    history_since: Annotated[int | None, Query(description="Fetch silences after this silence ID")] = None,
    since: Annotated[int | None, Query(description="Fetch silences after this message ID")] = None,
):
    resp = KeepAliveResp()
    if history_since:
        silences = (await session.exec(select(SilenceUser).where(col(SilenceUser.id) > history_since))).all()
        resp.silences.extend([UserSilenceResp.from_db(silence) for silence in silences])
    elif since:
        msg = await session.get(ChatMessage, since)
        if msg:
            silences = (await session.exec(select(SilenceUser).where(col(SilenceUser.banned_at) > msg.timestamp))).all()
            resp.silences.extend([UserSilenceResp.from_db(silence) for silence in silences])

    return resp


class MessageReq(BaseModel):
    message: str
    is_action: bool = False
    uuid: str | None = None


@router.post(
    "/chat/channels/{channel}/messages",
    responses={200: api_doc("Sent message", ChatMessageModel, ["sender", "is_action"])},
    name="Send message",
    description="Send message to a channel.",
    tags=["Chat"],
)
async def send_message(
    session: Database,
    channel: Annotated[str, Path(..., description="Channel ID or name")],
    req: Annotated[MessageReq, Depends(BodyOrForm(MessageReq))],
    current_user: Annotated[User, Security(get_current_user, scopes=["chat.write"])],
):
    if await current_user.is_restricted(session):
        raise HTTPException(status_code=403, detail="You are restricted from sending messages")

    if channel.isdigit():
        db_channel = (await session.exec(select(ChatChannel).where(ChatChannel.channel_id == int(channel)))).first()
    else:
        db_channel = (await session.exec(select(ChatChannel).where(ChatChannel.channel_name == channel))).first()

    if db_channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    # Rewrite known-broken host references in the message body before
    # it reaches Redis / broadcast. Specifically: the Torii client's
    # /np action used to (and may still, on older releases) post
    # links pointing to the API subdomain (lazer-api.shikkesora.com)
    # which serves no HTML — clicking such a link externally hits
    # 404, and the in-client chat parser doesn't recognise it as a
    # beatmap link either (see app/service/chat_url_normalizer.py
    # for the full backstory). Doing this server-side catches old
    # clients + any third-party chat client too, so a single
    # release of the server fixes /np everywhere immediately.
    req.message = normalize_chat_urls(req.message) or ""

    channel_id = db_channel.channel_id
    channel_type = db_channel.type
    channel_name = db_channel.channel_name
    user_id = current_user.id

    if channel_type == ChannelType.MULTIPLAYER:
        try:
            redis = redis_message_client
            key = f"channel:{channel_id}:messages"
            key_type = await redis.type(key)
            if key_type not in ["none", "zset"]:
                logger.warning("Fixing Redis key %s with wrong type: %s", key, key_type)
                await redis.delete(key)
        except Exception as exc:
            logger.warning("Failed to check/fix Redis key for channel %s: %s", channel_id, exc)

    resp = await redis_message_system.send_message(
        channel_id=channel_id,
        user=current_user,
        content=req.message,
        is_action=req.is_action,
        user_uuid=req.uuid,
    )

    is_bot_command = req.message.startswith("!")
    await server.send_message_to_channel(resp, is_bot_command and channel_type == ChannelType.PUBLIC)

    if is_bot_command:
        await bot.try_handle(current_user, db_channel, req.message, session)

    await session.refresh(current_user)

    if channel_type in [ChannelType.PM, ChannelType.TEAM]:
        temp_msg = ChatMessage(
            message_id=resp["message_id"],
            channel_id=channel_id,
            content=req.message,
            sender_id=user_id,
            type=MessageType.ACTION if req.is_action else MessageType.PLAIN,
            uuid=req.uuid,
        )

        if channel_type == ChannelType.PM:
            receiver_ids = _resolve_pm_receiver_ids(channel_name, channel_id, user_id)
            if receiver_ids:
                await server.new_private_notification(
                    ChannelMessage.init(temp_msg, current_user, receiver_ids, channel_type)
                )
            else:
                logger.warning(
                    "Skipping PM notification: unresolved receiver (channel_id=%s channel_name=%r sender=%s)",
                    channel_id,
                    channel_name,
                    user_id,
                )
        elif channel_type == ChannelType.TEAM:
            await server.new_private_notification(ChannelMessageTeam.init(temp_msg, current_user))

    return resp


@router.get(
    "/chat/channels/{channel}/messages",
    responses={200: api_doc("Channel messages", list[ChatMessageModel], ["sender"])},
    name="Get messages",
    description="Fetch messages from channel in chronological order.",
    tags=["Chat"],
)
async def get_message(
    session: Database,
    channel: str,
    current_user: Annotated[User, Security(get_current_user, scopes=["chat.read"])],
    limit: Annotated[int, Query(ge=1, le=50, description="Message count")] = 50,
    since: Annotated[int, Query(ge=0, description="Fetch messages after this message ID")] = 0,
    until: Annotated[int | None, Query(description="Fetch messages before this message ID")] = None,
):
    show_nsfw_media = await UserModel.viewer_allows_nsfw_media(current_user)

    if channel.isdigit():
        db_channel = (await session.exec(select(ChatChannel).where(ChatChannel.channel_id == int(channel)))).first()
    else:
        db_channel = (await session.exec(select(ChatChannel).where(ChatChannel.channel_name == channel))).first()

    if db_channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    channel_id = db_channel.channel_id

    try:
        messages = await redis_message_system.get_messages(channel_id, limit, since)
        if len(messages) >= 2 and messages[0]["message_id"] > messages[-1]["message_id"]:
            messages.reverse()
        return messages
    except Exception as exc:
        logger.warning("Failed to get messages from Redis system: %s", exc)

    base_query = select(ChatMessage).where(ChatMessage.channel_id == channel_id)

    if since > 0 and until is None:
        query = base_query.where(col(ChatMessage.message_id) > since).order_by(col(ChatMessage.message_id).asc()).limit(limit)
        rows = (await session.exec(query)).all()
        return await ChatMessageModel.transform_many(rows, includes=["sender"], show_nsfw_media=show_nsfw_media)

    if until is not None:
        query = (
            base_query.where(col(ChatMessage.message_id) < until).order_by(col(ChatMessage.message_id).desc()).limit(limit)
        )
        rows = list((await session.exec(query)).all())
        rows.reverse()
        return await ChatMessageModel.transform_many(rows, includes=["sender"], show_nsfw_media=show_nsfw_media)

    query = base_query.order_by(col(ChatMessage.message_id).desc()).limit(limit)
    rows = list((await session.exec(query)).all())
    rows.reverse()
    return await ChatMessageModel.transform_many(rows, includes=["sender"], show_nsfw_media=show_nsfw_media)


@router.put(
    "/chat/channels/{channel}/mark-as-read/{message}",
    status_code=204,
    name="Mark as read",
    description="Mark channel message as read.",
    tags=["Chat"],
)
async def mark_as_read(
    session: Database,
    channel: Annotated[str, Path(..., description="Channel ID or name")],
    message: Annotated[int, Path(..., description="Message ID")],
    current_user: Annotated[User, Security(get_current_user, scopes=["chat.read"])],
):
    if channel.isdigit():
        db_channel = (await session.exec(select(ChatChannel).where(ChatChannel.channel_id == int(channel)))).first()
    else:
        db_channel = (await session.exec(select(ChatChannel).where(ChatChannel.channel_name == channel))).first()

    if db_channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    await server.mark_as_read(db_channel.channel_id, current_user.id, message)


class PMReq(BaseModel):
    target_id: int
    message: str
    is_action: bool = False
    uuid: str | None = None


@router.post(
    "/chat/new",
    name="Create PM",
    description="Create a private channel and send initial message.",
    tags=["Chat"],
    responses={
        200: api_doc(
            "Create PM response",
            {
                "channel": ChatChannelModel,
                "message": ChatMessageModel,
                "new_channel_id": int,
            },
            ["recent_messages.sender", "sender"],
            name="NewPMResponse",
        )
    },
)
async def create_new_pm(
    session: Database,
    req: Annotated[PMReq, Depends(BodyOrForm(PMReq))],
    current_user: Annotated[User, Security(get_current_user, scopes=["chat.write"])],
    redis: Redis,
):
    show_nsfw_media = await UserModel.viewer_allows_nsfw_media(current_user)
    if await current_user.is_restricted(session):
        raise HTTPException(status_code=403, detail="You are restricted from sending messages")

    user_id = current_user.id
    target = await session.get(User, req.target_id)
    if target is None or await target.is_restricted(session):
        raise HTTPException(status_code=404, detail="Target user not found")

    is_can_pm, block = await target.is_user_can_pm(current_user, session)
    if not is_can_pm:
        raise HTTPException(status_code=403, detail=block)

    canonical_name = _canonical_pm_channel_name(user_id, req.target_id)
    channel = await ChatChannel.get_pm_channel(user_id, req.target_id, session)
    if channel is None:
        channel = ChatChannel(
            channel_name=canonical_name,
            description="Private message channel",
            type=ChannelType.PM,
        )
        session.add(channel)
        await session.commit()
        await session.refresh(channel)
        await session.refresh(target)
        await session.refresh(current_user)
    elif channel.channel_name != canonical_name:
        channel.channel_name = canonical_name
        session.add(channel)
        await session.commit()
        await session.refresh(channel)

    await server.batch_join_channel([target, current_user], channel)
    channel_resp = await ChatChannelModel.transform(
        channel,
        user=current_user,
        server=server,
        includes=["recent_messages.sender"],
        show_nsfw_media=show_nsfw_media,
    )

    message_resp = await redis_message_system.send_message(
        channel_id=channel.channel_id,
        user=current_user,
        content=req.message,
        is_action=req.is_action,
        user_uuid=req.uuid,
    )
    await server.send_message_to_channel(message_resp)

    temp_msg = ChatMessage(
        message_id=message_resp["message_id"],
        channel_id=channel.channel_id,
        content=req.message,
        sender_id=user_id,
        type=MessageType.ACTION if req.is_action else MessageType.PLAIN,
        uuid=req.uuid,
    )

    receiver_ids = _resolve_pm_receiver_ids(channel.channel_name, channel.channel_id, user_id)
    if receiver_ids:
        await server.new_private_notification(
            ChannelMessage.init(temp_msg, current_user, receiver_ids, ChannelType.PM)
        )
    else:
        logger.warning(
            "Skipping PM notification in /chat/new: unresolved receiver (channel_id=%s channel_name=%r sender=%s)",
            channel.channel_id,
            channel.channel_name,
            user_id,
        )

    return {
        "channel": channel_resp,
        "message": message_resp,
        "new_channel_id": channel_resp["channel_id"],
    }
