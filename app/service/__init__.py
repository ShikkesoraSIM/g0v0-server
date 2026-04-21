from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.database.playlists import Playlist
    from app.models.room import APIUploadedRoom
    from sqlmodel.ext.asyncio.session import AsyncSession


async def create_playlist_room(session: "AsyncSession", playlist: "Playlist"):
    from .room import create_playlist_room as _create_playlist_room

    return await _create_playlist_room(session, playlist)


async def create_playlist_room_from_api(session: "AsyncSession", room: "APIUploadedRoom"):
    from .room import create_playlist_room_from_api as _create_playlist_room_from_api

    return await _create_playlist_room_from_api(session, room)

__all__ = [
    "create_playlist_room",
    "create_playlist_room_from_api",
]
