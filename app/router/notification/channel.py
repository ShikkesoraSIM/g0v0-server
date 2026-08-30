from typing import Annotated, Literal, Self

from app.database.chat import (
    ChannelType,
    ChatChannel,
    ChatChannelModel,
    ChatMessage,
    SilenceUser,
    UserSilenceResp,
)
from app.database.user import User, UserModel
from app.dependencies.database import Database, Redis
from app.dependencies.param import BodyOrForm
from app.dependencies.user import get_current_user
from app.log import log
from app.router.v2 import api_v2_router as router
from app.utils import api_doc

from .server import server

from fastapi import Depends, HTTPException, Path, Query, Security
from pydantic import BaseModel, model_validator
from sqlmodel import col, select

logger = log("ChatChannel")


def _canonical_pm_channel_name(user_a_id: int, user_b_id: int) -> str:
    low, high = sorted((int(user_a_id), int(user_b_id)))
    return f"pm_{low}_{high}"


async def _resolve_pm_target_user_id(
    session: Database,
    db_channel: ChatChannel,
    current_user_id: int,
) -> int | None:
    target_user_id = ChatChannelModel._pm_target_user_id_from_channel_name(
        db_channel.channel_name,
        current_user_id,
    )
    if target_user_id is not None:
        return target_user_id

    channel_users = server.channels.get(db_channel.channel_id, [])
    target_user_id = next((user_id for user_id in channel_users if user_id != current_user_id), None)
    if target_user_id is not None:
        return target_user_id

    recent_sender_ids = (
        await session.exec(
            select(ChatMessage.sender_id)
            .where(ChatMessage.channel_id == db_channel.channel_id)
            .order_by(col(ChatMessage.message_id).desc())
            .limit(100)
        )
    ).all()

    for sender_id in recent_sender_ids:
        if sender_id != current_user_id:
            return int(sender_id)

    return None


@router.get(
    "/chat/updates",
    name="Get updates",
    description="Get current channel presence and silence updates.",
    tags=["Chat"],
    responses={
        200: api_doc(
            "Update response.",
            {"presence": list[ChatChannelModel], "silences": list[UserSilenceResp]},
            ChatChannel.LISTING_INCLUDES,
            name="UpdateResponse",
        )
    },
)
async def get_update(
    session: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["chat.read"])],
    redis: Redis,
    history_since: Annotated[int | None, Query(description="Fetch silences after this silence ID")] = None,
    since: Annotated[int | None, Query(description="Fetch silences after this message ID")] = None,
    includes: Annotated[
        list[str],
        Query(alias="includes[]", description="Requested update payload sections"),
    ] = ["presence", "silences"],
):
    show_nsfw_media = await UserModel.viewer_allows_nsfw_media(current_user)
    resp = {"presence": [], "silences": []}

    if "presence" in includes:
        channel_ids = server.get_user_joined_channel(current_user.id)
        for channel_id in channel_ids:
            db_channel = (
                await session.exec(select(ChatChannel).where(ChatChannel.channel_id == channel_id))
            ).first()
            if db_channel:
                resp["presence"].append(
                    await ChatChannelModel.transform(
                        db_channel,
                        user=current_user,
                        server=server,
                        includes=ChatChannel.LISTING_INCLUDES,
                        show_nsfw_media=show_nsfw_media,
                    )
                )

    if "silences" in includes:
        if history_since:
            silences = (await session.exec(select(SilenceUser).where(col(SilenceUser.id) > history_since))).all()
            resp["silences"].extend([UserSilenceResp.from_db(silence) for silence in silences])
        elif since:
            msg = await session.get(ChatMessage, since)
            if msg:
                silences = (
                    await session.exec(select(SilenceUser).where(col(SilenceUser.banned_at) > msg.timestamp))
                ).all()
                resp["silences"].extend([UserSilenceResp.from_db(silence) for silence in silences])

    return resp


@router.put(
    "/chat/channels/{channel}/users/{user}",
    name="Join channel",
    description="Join a channel.",
    tags=["Chat"],
    responses={200: api_doc("Joined channel", ChatChannelModel, ChatChannel.LISTING_INCLUDES)},
)
async def join_channel(
    session: Database,
    channel: Annotated[str, Path(..., description="Channel ID or name")],
    user: Annotated[str, Path(..., description="User ID")],
    current_user: Annotated[User, Security(get_current_user, scopes=["chat.write_manage"])],
):
    if await current_user.is_restricted(session):
        raise HTTPException(status_code=403, detail="You are restricted from sending messages")

    if channel.isdigit():
        db_channel = (await session.exec(select(ChatChannel).where(ChatChannel.channel_id == int(channel)))).first()
    else:
        db_channel = (await session.exec(select(ChatChannel).where(ChatChannel.channel_name == channel))).first()

    if db_channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    return await server.join_channel(current_user, db_channel)


@router.delete(
    "/chat/channels/{channel}/users/{user}",
    status_code=204,
    name="Leave channel",
    description="Leave a channel.",
    tags=["Chat"],
)
async def leave_channel(
    session: Database,
    channel: Annotated[str, Path(..., description="Channel ID or name")],
    user: Annotated[str, Path(..., description="User ID")],
    current_user: Annotated[User, Security(get_current_user, scopes=["chat.write_manage"])],
):
    if await current_user.is_restricted(session):
        raise HTTPException(status_code=403, detail="You are restricted from sending messages")

    if channel.isdigit():
        db_channel = (await session.exec(select(ChatChannel).where(ChatChannel.channel_id == int(channel)))).first()
    else:
        db_channel = (await session.exec(select(ChatChannel).where(ChatChannel.channel_name == channel))).first()

    if db_channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    await server.leave_channel(current_user, db_channel)
    return


@router.get(
    "/chat/channels",
    responses={200: api_doc("Channel listing", list[ChatChannelModel])},
    name="List channels",
    description="List public channels.",
    tags=["Chat"],
)
async def get_channel_list(
    session: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["chat.read"])],
):
    show_nsfw_media = await UserModel.viewer_allows_nsfw_media(current_user)
    channels = (await session.exec(select(ChatChannel).where(ChatChannel.type == ChannelType.PUBLIC))).all()
    return await ChatChannelModel.transform_many(
        channels,
        user=current_user,
        server=server,
        show_nsfw_media=show_nsfw_media,
    )


@router.get(
    "/chat/channels/{channel}",
    responses={
        200: api_doc(
            "Channel detail response",
            {"channel": ChatChannelModel, "users": list[UserModel]},
            ChatChannel.LISTING_INCLUDES + User.CARD_INCLUDES,
            name="GetChannelResponse",
        )
    },
    name="Get channel",
    description="Get a channel by ID or name.",
    tags=["Chat"],
)
async def get_channel(
    session: Database,
    channel: Annotated[str, Path(..., description="Channel ID or name")],
    current_user: Annotated[User, Security(get_current_user, scopes=["chat.read"])],
    redis: Redis,
):
    show_nsfw_media = await UserModel.viewer_allows_nsfw_media(current_user)

    if channel.isdigit():
        db_channel = (await session.exec(select(ChatChannel).where(ChatChannel.channel_id == int(channel)))).first()
    else:
        db_channel = (await session.exec(select(ChatChannel).where(ChatChannel.channel_name == channel))).first()

    if db_channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")

    users: list[User] = [current_user]

    if db_channel.type == ChannelType.PM:
        target_user_id = await _resolve_pm_target_user_id(session, db_channel, current_user.id)
        if target_user_id is None:
            logger.warning(
                "Unable to resolve PM target user (channel_id=%s, channel_name=%r, requester=%s)",
                db_channel.channel_id,
                db_channel.channel_name,
                current_user.id,
            )
        else:
            target_user = await session.get(User, target_user_id)
            if target_user is not None and not await target_user.is_restricted(session):
                users = [target_user, current_user]

                canonical_name = _canonical_pm_channel_name(current_user.id, target_user_id)
                if db_channel.channel_name != canonical_name:
                    db_channel.channel_name = canonical_name
                    session.add(db_channel)
                    try:
                        await session.commit()
                        await session.refresh(db_channel)
                        # El commit expira a los DOS usuarios de la lista, no solo a
                        # current_user, y el transform de abajo los serializa a ambos.
                        # Refrescando uno solo, el otro salia como {} y la respuesta
                        # entera moria con un ValidationError de UserModel: el cliente
                        # lo mostraba como "Failed to join channel". Se veia solo la
                        # primera vez, porque despues el nombre ya queda normalizado y
                        # no se vuelve a commitear aca.
                        await session.refresh(current_user)
                        await session.refresh(target_user)
                    except Exception as exc:
                        await session.rollback()
                        logger.warning(
                            "Failed to normalize PM channel name (channel_id=%s): %s",
                            db_channel.channel_id,
                            exc,
                        )

    return {
        "channel": await ChatChannelModel.transform(
            db_channel,
            user=current_user,
            server=server,
            includes=ChatChannel.LISTING_INCLUDES,
            show_nsfw_media=show_nsfw_media,
        ),
        "users": [
            UserModel.apply_nsfw_media_policy(user_resp, show_nsfw_media)
            for user_resp in await UserModel.transform_many(users, includes=User.CARD_INCLUDES, show_nsfw_media=True)
        ],
    }


class CreateChannelReq(BaseModel):
    class AnnounceChannel(BaseModel):
        name: str
        description: str

    message: str | None = None
    type: Literal["ANNOUNCE", "PM"] = "PM"
    target_id: int | None = None
    target_ids: list[int] | None = None
    channel: AnnounceChannel | None = None

    @model_validator(mode="after")
    def check(self) -> Self:
        if self.type == "PM":
            if self.target_id is None:
                raise ValueError("target_id must be set for PM channels")
        else:
            if self.target_ids is None or self.channel is None or self.message is None:
                raise ValueError("target_ids, channel, and message must be set for ANNOUNCE channels")
        return self


@router.post(
    "/chat/channels",
    responses={200: api_doc("Created channel", ChatChannelModel, ["recent_messages.sender"])},
    name="Create channel",
    description="Create PM or announce channel.",
    tags=["Chat"],
)
async def create_channel(
    session: Database,
    req: Annotated[CreateChannelReq, Depends(BodyOrForm(CreateChannelReq))],
    current_user: Annotated[User, Security(get_current_user, scopes=["chat.write_manage"])],
    redis: Redis,
):
    show_nsfw_media = await UserModel.viewer_allows_nsfw_media(current_user)
    if await current_user.is_restricted(session):
        raise HTTPException(status_code=403, detail="You are restricted from sending messages")

    if req.type == "PM":
        target = await session.get(User, req.target_id)
        if not target or await target.is_restricted(session):
            raise HTTPException(status_code=404, detail="Target user not found")

        is_can_pm, block = await target.is_user_can_pm(current_user, session)
        if not is_can_pm:
            raise HTTPException(status_code=403, detail=block)

        channel_name = _canonical_pm_channel_name(current_user.id, req.target_id)
        channel = await ChatChannel.get_pm_channel(
            current_user.id,
            req.target_id,  # pyright: ignore[reportArgumentType]
            session,
        )

        if channel is None:
            channel = ChatChannel(
                channel_name=channel_name,
                description="Private message channel",
                type=ChannelType.PM,
            )
            session.add(channel)
            await session.commit()
            await session.refresh(channel)
        elif channel.channel_name != channel_name:
            channel.channel_name = channel_name
            session.add(channel)
            await session.commit()
            await session.refresh(channel)

        await session.refresh(target)
        await session.refresh(current_user)
        await server.batch_join_channel([target, current_user], channel)
    else:
        channel_name = req.channel.name if req.channel else "Unnamed Channel"
        channel = (
            await session.exec(
                select(ChatChannel).where(
                    ChatChannel.channel_name == channel_name,
                    ChatChannel.type == ChannelType.ANNOUNCE,
                )
            )
        ).first()

        if channel is None:
            channel = ChatChannel(
                channel_name=channel_name,
                description=req.channel.description if req.channel else "Announcement channel",
                type=ChannelType.ANNOUNCE,
            )
            session.add(channel)
            await session.commit()
            await session.refresh(channel)

        target_users = await session.exec(select(User).where(col(User.id).in_(req.target_ids or [])))
        await server.batch_join_channel([*target_users, current_user], channel)

    await server.join_channel(current_user, channel)

    return await ChatChannelModel.transform(
        channel,
        user=current_user,
        server=server,
        includes=["recent_messages.sender"],
        show_nsfw_media=show_nsfw_media,
    )
