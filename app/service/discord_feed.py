"""Eventos de comunidad para el canal #feed de discord.

Companion de discord_account_feed.py / discord_title_feed.py: mismo webhook
(settings.discord_title_feed_webhook_url), mismo contrato fail-open (si discord
esta caido o la env no esta, el evento se pierde con un warning y NADA del
request path se rompe ni se enlentece).

Todos los notify_* son SINCRONICOS y toman valores planos (str/int), nunca
objetos ORM: los call sites snapshotean antes del commit (expire_on_commit
expira los objetos y tocarlos despues revienta en async). El POST real corre
en una task de fondo con referencia fuerte (sino el GC la puede matar).

Eventos: compra en la tienda, lobby de multi creado, playlist creada, daily
challenge del dia, beatmapset subido (BSS), team creado, nuevo #1 en un mapa.
Aparte: relay one-way del chat publico #osu (webhook PROPIO, apagado por
default; DISCORD_OSU_CHAT_WEBHOOK_URL para prenderlo).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.config import settings
from app.log import log

logger = log("DiscordFeed")

# colores por tipo de evento (paleta consistente con account/title feed)
_COLOUR_PURCHASE = 0xA78BFA  # violeta
_COLOUR_LOBBY = 0x58A6FF  # azul
_COLOUR_PLAYLIST = 0x9CCC65  # verde lima
_COLOUR_DAILY = 0xFFD36E  # ambar
_COLOUR_BSS = 0xFF8A65  # naranja
_COLOUR_TEAM = 0x4DB6AC  # teal
_COLOUR_NUMBER_ONE = 0xFFC107  # dorado

# referencia fuerte a las tasks en vuelo (asyncio solo guarda weakrefs).
_tasks: set[asyncio.Task] = set()


def _fire(coro) -> None:
    try:
        task = asyncio.get_running_loop().create_task(coro)
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    except RuntimeError:
        # sin loop corriendo (tests sincronicos): se pierde el evento, no importa.
        pass


async def _post(url: str, payload: dict[str, Any]) -> None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json=payload)
    except Exception as exc:
        logger.warning("Failed to post feed event to Discord: {}", exc)


def _feed(payload: dict[str, Any]) -> None:
    url = (settings.discord_title_feed_webhook_url or "").strip()
    if not url:
        return
    _fire(_post(url, payload))


def _profile(user_id: int) -> str:
    return f"{settings.web_url}users/{user_id}"


def _embed(title: str, description: str, colour: int, fields: list[dict] | None = None) -> dict[str, Any]:
    embed: dict[str, Any] = {"title": title, "description": description, "color": colour}
    if fields:
        embed["fields"] = fields
    return {"embeds": [embed]}


def _pretty_cosmetic(cosmetic_id: str) -> str:
    """'trail-rainbow-ribbon' -> 'Rainbow Ribbon trail' (mejor esfuerzo)."""
    cid = (cosmetic_id or "").strip()
    for prefix, kind in (("trail-", "trail"), ("namecolour-", "name colour"), ("aura-", "aura")):
        if cid.startswith(prefix):
            base = cid[len(prefix):].replace("-", " ").replace("_", " ").title()
            return f"{base} {kind}"
    return cid.replace("-", " ").replace("_", " ").title()


# ── eventos ──────────────────────────────────────────────────────────────────


def notify_cosmetic_purchase(*, username: str, user_id: int, cosmetic_id: str, price: int) -> None:
    _feed(_embed(
        "🛍️ Store purchase",
        f"**[{username}]({_profile(user_id)})** bought **{_pretty_cosmetic(cosmetic_id)}** for **{price:,}** points",
        _COLOUR_PURCHASE,
    ))


def notify_room_created(
    *,
    host_username: str,
    host_user_id: int,
    room_name: str,
    is_realtime: bool,
    map_count: int,
    has_password: bool,
) -> None:
    lock = " 🔒" if has_password else ""

    if is_realtime:
        _feed(_embed(
            "🎮 Multiplayer lobby up",
            f"**[{host_username}]({_profile(host_user_id)})** opened **{room_name}**{lock}",
            _COLOUR_LOBBY,
        ))
    else:
        maps = f"{map_count} map" + ("s" if map_count != 1 else "")
        _feed(_embed(
            "📜 New playlist",
            f"**[{host_username}]({_profile(host_user_id)})** created **{room_name}**{lock} ({maps})",
            _COLOUR_PLAYLIST,
        ))


def notify_daily_challenge(*, map_title: str, beatmapset_id: int | None, beatmap_id: int, mode: str, mods: str) -> None:
    if beatmapset_id:
        map_link = f"[{map_title}]({settings.web_url}beatmapsets/{beatmapset_id}#{mode}/{beatmap_id})"
    else:
        map_link = f"**{map_title}**"
    mods_part = f" with **{mods}**" if mods else ""
    _feed(_embed(
        "🗓️ Today's Daily Challenge is live",
        f"{map_link}{mods_part}",
        _COLOUR_DAILY,
    ))


def notify_beatmapset_uploaded(*, username: str, user_id: int, artist: str, title: str, beatmapset_id: int) -> None:
    _feed(_embed(
        "🎵 New beatmap uploaded",
        f"**[{username}]({_profile(user_id)})** uploaded "
        f"[{artist} - {title}]({settings.web_url}beatmapsets/{beatmapset_id})",
        _COLOUR_BSS,
    ))


def notify_team_created(*, username: str, user_id: int, team_name: str, short_name: str) -> None:
    _feed(_embed(
        "🚩 New team",
        f"**[{username}]({_profile(user_id)})** founded **[{short_name}] {team_name}**",
        _COLOUR_TEAM,
    ))


def notify_new_number_one(
    *,
    username: str,
    user_id: int,
    map_title: str,
    beatmap_url: str,
    pp: float | None,
    accuracy: float | None,
    dethroned_username: str | None,
) -> None:
    stats = []
    if pp:
        stats.append(f"{pp:.0f}pp")
    if accuracy is not None:
        stats.append(f"{accuracy * 100:.2f}%")
    stats_part = f" ({', '.join(stats)})" if stats else ""
    dethroned = f", dethroning **{dethroned_username}**" if dethroned_username else ""

    _feed(_embed(
        "👑 New #1",
        f"**[{username}]({_profile(user_id)})** took #1 on [{map_title}]({beatmap_url}){stats_part}{dethroned}",
        _COLOUR_NUMBER_ONE,
    ))


# ── relay del chat publico #osu (canal propio, apagado si la env no esta) ────


def relay_osu_chat(*, username: str, content: str) -> None:
    url = (getattr(settings, "discord_osu_chat_webhook_url", "") or "").strip()
    if not url:
        return

    text = (content or "").strip()
    if not text:
        return
    if len(text) > 1500:
        text = text[:1500] + "…"

    _fire(_post(url, {
        # el username del webhook toma el nombre del jugador: se lee como un chat.
        "username": username[:80],
        "content": text,
        # nada de pings desde el chat del juego + sin unfurl de links.
        "allowed_mentions": {"parse": []},
        "flags": 4,  # SUPPRESS_EMBEDS
    }))
