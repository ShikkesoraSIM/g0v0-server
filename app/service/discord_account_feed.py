"""Discord webhook poster for new-account events.

Companion to app/service/discord_title_feed.py — both publish to the
same Discord "Torii feed" channel and share its webhook URL setting.
Kept as a separate module so the embed builder for "user registered"
doesn't get tangled with the diff-builder for title grants; they're
unrelated event shapes that just happen to share a destination.

Same fail-open contract as the title feed: a Discord outage or a
missing env var must never break account registration. The only
side-effect of any failure is a warning log line.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from app.config import settings
from app.log import log

if TYPE_CHECKING:
    # Type-only import: pulling app.database.user at module load time
    # would drag in the full SQLModel registry, slowing the cold start
    # for callers that just want the notifier.
    from app.database.user import User

logger = log("DiscordAccountFeed")


# Soft green that matches the "Title granted" embed colour from
# discord_title_feed.py. Both events are "something nice happened" so
# they share the celebratory palette — keeps the channel readable as
# one cohesive feed rather than a colour soup.
_COLOUR_NEW_ACCOUNT = 0x10B981  # emerald-500


def _country_flag_emoji(country_code: str | None) -> str:
    """Convert a 2-letter ISO country code into the matching flag emoji
    (regional indicator letter pair). Returns "" when the code is
    missing or malformed — Discord renders an empty string fine, no
    need to insert a placeholder. Examples: AR → 🇦🇷, JP → 🇯🇵."""
    if not country_code:
        return ""
    code = country_code.strip().upper()
    if len(code) != 2 or not code.isalpha():
        return ""
    # Regional indicator A = U+1F1E6, so ord('A') maps onto it directly.
    return chr(0x1F1E6 + (ord(code[0]) - ord("A"))) + chr(0x1F1E6 + (ord(code[1]) - ord("A")))


def _user_profile_url(user_id: int) -> str | None:
    """Frontend profile URL when the server has one configured. Returns
    None otherwise so the embed can fall back to plain text instead of
    pointing at a broken link."""
    base = settings.web_url
    if not base or base == "/":
        return None
    return f"{base.rstrip('/')}/users/{user_id}"


def _build_embed(*, user: "User", source_label: str | None) -> dict:
    """Build the Discord webhook payload for a successful registration.

    `source_label` is a short human string like "osu! client" or "web"
    that we surface in a field — useful at a glance to know which
    surface the user came in through.
    """
    profile_url = _user_profile_url(user.id)
    description = (
        f"**[{user.username}]({profile_url})** (id `{user.id}`)"
        if profile_url
        else f"**{user.username}** (id `{user.id}`)"
    )

    fields: list[dict] = []
    flag = _country_flag_emoji(user.country_code)
    country_value = (
        f"{flag} `{user.country_code}`" if flag else f"`{user.country_code or 'unknown'}`"
    )
    fields.append({"name": "Country", "value": country_value, "inline": True})

    if source_label:
        fields.append({"name": "Source", "value": source_label, "inline": True})

    embed: dict = {
        "title": "🌱 New Torii account",
        "description": description,
        "color": _COLOUR_NEW_ACCOUNT,
        "fields": fields,
    }

    # Avatar thumbnail. Brand new accounts always have the default
    # avatar so this will look identical for every embed at first;
    # still worth posting because admins can swap their avatar later
    # and the historical embed will then reflect the original default.
    avatar = (user.avatar_url or "").strip()
    if avatar.startswith(("http://", "https://")):
        embed["thumbnail"] = {"url": avatar}

    return {"embeds": [embed]}


async def notify_account_created(
    *,
    user: "User",
    source_label: str | None = None,
) -> None:
    """Fire-and-forget Discord notification for a successful registration.

    No-op when the webhook URL is unset (feature disabled). Any HTTP
    failure is logged at WARNING and swallowed — registration must
    never depend on Discord being reachable.

    The webhook URL is intentionally shared with the title-grant feed
    (settings.discord_title_feed_webhook_url): both event types post to
    the same "Torii feed" channel. If we ever want them in separate
    channels we'd add a dedicated env var here and fall back to the
    title-feed one when it's unset.
    """
    url = (settings.discord_title_feed_webhook_url or "").strip()
    if not url:
        return

    try:
        payload = _build_embed(user=user, source_label=source_label)
    except Exception as build_err:  # pragma: no cover — defensive only
        logger.warning(
            "Failed to build account-created embed for user_id={}: {}",
            getattr(user, "id", "?"), build_err,
        )
        return

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json=payload)
    except Exception as exc:  # pragma: no cover — Discord must never block registration
        logger.warning(
            "Failed to forward account creation for user_id={} to Discord: {}",
            user.id, exc,
        )
