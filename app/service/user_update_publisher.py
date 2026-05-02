"""Publish "user payload changed" notifications to the spectator's Redis channel.

The spectator (osu-server-spectator-m1pp) subscribes to ``torii:user_updated``
and rebroadcasts each integer user id to every connected SignalR client.
Connected lazer clients consume the broadcast through ``IMetadataClient.UserUpdated``
and refresh their cached snapshot of that user (badges, equipped aura, group
membership, custom title, profile hue, anything else that ships in the public
``APIUser`` payload).

Cross-process so that g0v0 (Python, FastAPI) can hand off to spectator
(C#, SignalR) without either side having to poll the database. The payload
is intentionally just the user id — the receiver decides what to do with
that signal (typically: refetch the user via ``GetUserRequest``).

This module is fire-and-forget on purpose: a publish failure should NEVER
block the underlying mutation (the picker still needs to confirm, the
admin save still needs to succeed). All errors are logged and swallowed.
"""

from __future__ import annotations

from app.dependencies.database import get_redis
from app.log import logger

# Channel name shared with the spectator's MetadataBroadcaster. Keep both
# sides aligned — if you rename here, also update
# osu-server-spectator-m1pp/.../MetadataBroadcaster.cs::user_updated_channel.
USER_UPDATED_CHANNEL = "torii:user_updated"


async def publish_user_updated(user_id: int) -> None:
    """Best-effort fanout that ``user_id``'s public payload has changed.

    Connected lazer clients will receive a ``UserUpdated(user_id)`` SignalR
    event from the spectator and refresh their local snapshot.

    Safe to call from any request handler that mutates a field shipped in
    the user payload — the most common ones today are equipped_aura
    (cosmetic picker) and torii_titles (admin user-edit modal). Adding a
    new mutation? Call this right after the SQL commit so the broadcast
    carries the post-commit truth.
    """
    if user_id <= 0:
        return

    try:
        redis = get_redis()
        # publish() is a coroutine on the async redis client; the spectator's
        # subscriber decodes the payload as ASCII text and parses to int.
        await redis.publish(USER_UPDATED_CHANNEL, str(user_id))
    except Exception:
        # Swallow + log: notifications are a "nice to have", not a hard
        # requirement. The mutation already succeeded in the DB; a missed
        # broadcast just means clients pick up the change on next refetch
        # instead of instantly. Worth logging though, because a sustained
        # publish failure means the cross-client refresh feature is broken.
        logger.exception("Failed to publish UserUpdated for user_id=%s", user_id)
