"""Discord webhook poster for admin title-grant events.

Mirrors the donation webhook's "fail open, never block the request" pattern
in app/router/private/donations.py — Discord being down or the URL being
unset is never allowed to break an admin's edit. The notifier:

  - returns immediately if no diff (admin saved the modal without
    actually changing the titles list)
  - returns immediately if the webhook URL is unset
  - swallows any HTTP / network exception with a warning log

Why a separate module instead of inlining in admin.py:
  - The admin endpoint already passes a thousand lines and is doing
    enough orchestration; embed building belongs near the webhook
    transport, not next to user-update logic.
  - Lets us hook the same notifier into other places later (e.g. a
    self-service team-pooler script) without copy-pasting embed code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

import httpx

from app.config import settings
from app.log import log
from app.models.torii_groups import TORII_GROUPS

if TYPE_CHECKING:
    # Imported only for type hints — pulling app.database.user at module
    # load time would drag in the full SQLModel registry, slowing down
    # the cold start for callers that just want the notifier.
    from app.database.user import User

logger = log("DiscordTitleFeed")


# Discord embed colour palette. We pick the most "important" colour in the
# diff to tint the entire embed: a grant (added) wins over a revoke
# (removed) because grants are the celebratory event the channel exists
# for. The colours roughly track the Tailwind palette used in the admin
# panel for the same purpose.
_COLOUR_ADDED = 0x10B981   # emerald-500
_COLOUR_REMOVED = 0xEF4444  # red-500
_COLOUR_MIXED = 0x6366F1   # indigo-500 (both added AND removed in one save)


def _label_for(key: str) -> str:
    """Pretty title display: "Developer (DEV)" — falls back to the raw key
    if a previously-known title was deleted from the catalog mid-flight."""
    g = TORII_GROUPS.get(key)
    if g is None:
        return f"`{key}`"
    name = g.get("name") or key
    short = g.get("short_name")
    return f"**{name}**" + (f" ({short})" if short else "")


def _fmt_list(keys: Iterable[str]) -> str:
    """Bullet-list of titles for the embed body. Empty iterable → "—"
    so the field never collapses (Discord refuses empty values)."""
    items = [_label_for(k) for k in keys]
    if not items:
        return "—"
    return "\n".join(f"• {item}" for item in items)


def _user_profile_url(user_id: int) -> str | None:
    """Best-effort link to the user's frontend profile page. Returns None
    when the server has no frontend URL configured (the embed then just
    omits the link instead of pointing at nothing)."""
    base = settings.web_url
    if not base or base == "/":
        return None
    return f"{base.rstrip('/')}/users/{user_id}"


def _build_embed(
    *,
    target_user: User,
    actor_username: str | None,
    added: list[str],
    removed: list[str],
) -> dict:
    """Build the Discord webhook payload for a title diff."""
    if added and removed:
        colour = _COLOUR_MIXED
        verb = "Titles updated"
    elif added:
        colour = _COLOUR_ADDED
        verb = "Title granted" if len(added) == 1 else "Titles granted"
    else:
        colour = _COLOUR_REMOVED
        verb = "Title revoked" if len(removed) == 1 else "Titles revoked"

    fields: list[dict] = []
    if added:
        fields.append({
            "name": f"➕ Granted ({len(added)})",
            "value": _fmt_list(added),
            "inline": True,
        })
    if removed:
        fields.append({
            "name": f"➖ Revoked ({len(removed)})",
            "value": _fmt_list(removed),
            "inline": True,
        })

    profile_url = _user_profile_url(target_user.id)
    description = (
        f"**[{target_user.username}]({profile_url})** (id `{target_user.id}`)"
        if profile_url
        else f"**{target_user.username}** (id `{target_user.id}`)"
    )

    embed: dict = {
        "title": verb,
        "description": description,
        "color": colour,
        "fields": fields,
    }

    # Avatar thumbnail is purely cosmetic — wrap in a guard so a bad URL
    # never makes Discord 400 the whole embed.
    avatar = (target_user.avatar_url or "").strip()
    if avatar.startswith(("http://", "https://")):
        embed["thumbnail"] = {"url": avatar}

    if actor_username:
        embed["footer"] = {"text": f"Updated by {actor_username}"}

    return {"embeds": [embed]}


async def notify_titles_changed(
    *,
    target_user: User,
    before: list[str],
    after: list[str],
    actor_username: str | None,
) -> None:
    """Fire-and-forget Discord notification for an admin title change.

    No-op when:
      - the webhook URL is unset (feature disabled)
      - before == after (admin saved without actually flipping any titles)
      - the HTTP call fails for any reason (logged, never raised)
    """
    url = (settings.discord_title_feed_webhook_url or "").strip()
    if not url:
        return

    before_set = set(before or [])
    after_set = set(after or [])
    added = sorted(after_set - before_set)
    removed = sorted(before_set - after_set)
    if not added and not removed:
        return

    payload = _build_embed(
        target_user=target_user,
        actor_username=actor_username,
        added=added,
        removed=removed,
    )

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json=payload)
    except Exception as exc:  # pragma: no cover — Discord must never block admin edits
        logger.warning(
            "Failed to forward title change for user_id={} to Discord: {}",
            target_user.id, exc,
        )
