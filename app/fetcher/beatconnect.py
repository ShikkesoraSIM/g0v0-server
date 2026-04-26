from __future__ import annotations

from typing import Any

from app.config import settings
from app.database.beatmap import BeatmapDict
from app.database.beatmapset import BeatmapsetDict
from app.models.beatmap import BeatmapRankStatus
from app.models.score import GameMode


def beatconnect_enabled() -> bool:
    return bool(settings.beatconnect_api_token)


def beatconnect_headers() -> dict[str, str]:
    return {
        "Token": settings.beatconnect_api_token,
        "User-Agent": "ToriiBeatConnect/1.0 (+https://lazer.shikkesora.com)",
    }


def beatconnect_base_url() -> str:
    return str(settings.beatconnect_base_url).rstrip("/")


def _status_to_ranked(status: str | None) -> int:
    normalized = (status or "pending").strip().lower()
    return {
        "graveyard": int(BeatmapRankStatus.GRAVEYARD),
        "wip": int(BeatmapRankStatus.WIP),
        "pending": int(BeatmapRankStatus.PENDING),
        "ranked": int(BeatmapRankStatus.RANKED),
        "approved": int(BeatmapRankStatus.APPROVED),
        "qualified": int(BeatmapRankStatus.QUALIFIED),
        "loved": int(BeatmapRankStatus.LOVED),
    }.get(normalized, int(BeatmapRankStatus.PENDING))


def _mode_to_game_mode(mode: str | None) -> GameMode:
    normalized = (mode or "std").strip().lower()
    return {
        "osu": GameMode.OSU,
        "std": GameMode.OSU,
        "taiko": GameMode.TAIKO,
        "fruits": GameMode.FRUITS,
        "ctb": GameMode.FRUITS,
        "catch": GameMode.FRUITS,
        "mania": GameMode.MANIA,
    }.get(normalized, GameMode.OSU)


def beatconnect_beatmapset_to_dict(payload: dict[str, Any]) -> BeatmapsetDict:
    set_id = int(payload["id"])
    ranked = _status_to_ranked(payload.get("status"))
    beatmaps: list[BeatmapDict] = []

    for raw_beatmap in payload.get("beatmaps") or []:
        if not isinstance(raw_beatmap, dict):
            continue

        mode = _mode_to_game_mode(raw_beatmap.get("mode"))
        beatmaps.append(
            {
                "beatmapset_id": set_id,
                "difficulty_rating": float(raw_beatmap.get("difficulty") or 0.0),
                "id": int(raw_beatmap["id"]),
                "mode": mode,
                "total_length": int(raw_beatmap.get("total_length") or 0),
                "user_id": int(payload.get("user_id") or 0),
                "version": str(raw_beatmap.get("version") or "Unknown"),
                "url": f"{str(settings.web_url).rstrip('/')}/beatmaps/{int(raw_beatmap['id'])}",
                "accuracy": float(raw_beatmap.get("od") or 0.0),
                "ar": float(raw_beatmap.get("ar") or 0.0),
                "bpm": float(raw_beatmap.get("bpm") or payload.get("bpm") or 0.0),
                "checksum": None,
                "count_circles": int(raw_beatmap.get("count_circles") or 0),
                "count_sliders": int(raw_beatmap.get("count_sliders") or 0),
                "count_spinners": int(raw_beatmap.get("count_spinners") or 0),
                "cs": float(raw_beatmap.get("cs") or 0.0),
                "deleted_at": None,
                "drain": float(raw_beatmap.get("hp") or 0.0),
                "hit_length": int(raw_beatmap.get("total_length") or 0),
                "is_local": False,
                "last_updated": payload.get("last_updated"),
                "mode_int": int(mode),
                "ranked": ranked,
                "is_scoreable": ranked in (
                    int(BeatmapRankStatus.RANKED),
                    int(BeatmapRankStatus.APPROVED),
                    int(BeatmapRankStatus.QUALIFIED),
                    int(BeatmapRankStatus.LOVED),
                ),
                "max_combo": raw_beatmap.get("max_combo"),
                "status": str(payload.get("status") or "pending").lower(),
                "convert": False,
            }
        )

    raw_genre = payload.get("genre") if isinstance(payload.get("genre"), dict) else None
    raw_language = payload.get("language") if isinstance(payload.get("language"), dict) else None

    # BeatConnect sometimes omits genre/language entirely. The BeatmapsetUpdate
    # validator expects BeatmapTranslationText ({"name": str, "id": int}) — None
    # fails validation and used to spam ERROR logs every 1–2 s. Fall back to
    # "Unspecified"/id=1 (matching the osu-web Genre/Language enum default) so
    # the payload validates cleanly when BeatConnect didn't ship the field.
    genre: dict[str, Any] = (
        raw_genre if raw_genre else {"id": 1, "name": "Unspecified"}
    )
    language: dict[str, Any] = (
        raw_language if raw_language else {"id": 1, "name": "Unspecified"}
    )

    result: dict[str, Any] = {
        "id": set_id,
        "artist": str(payload.get("artist") or "Unknown"),
        "artist_unicode": str(payload.get("artist_unicode") or payload.get("artist") or "Unknown"),
        "covers": payload.get("covers"),
        "creator": str(payload.get("creator") or "Unknown"),
        "nsfw": bool(payload.get("nsfw") or False),
        "preview_url": payload.get("preview_url"),
        "source": str(payload.get("source") or ""),
        "spotlight": bool(payload.get("spotlight") or False),
        "title": str(payload.get("title") or "Unknown"),
        "title_unicode": str(payload.get("title_unicode") or payload.get("title") or "Unknown"),
        "track_id": payload.get("track_id"),
        "user_id": int(payload.get("user_id") or 0),
        "video": bool(payload.get("video") or False),
        "is_local": False,
        "current_nominations": None,
        "description": None,
        "pack_tags": payload.get("pack_tags") or [],
        "bpm": float(payload.get("bpm") or 0.0),
        "can_be_hyped": False,
        "discussion_locked": False,
        "last_updated": payload.get("last_updated"),
        "ranked_date": payload.get("ranked_date"),
        "storyboard": bool(payload.get("storyboard") or False),
        "submitted_date": payload.get("submitted_date"),
        "tags": str(payload.get("tags") or ""),
        "discussion_enabled": True,
        "status": str(payload.get("status") or "pending").lower(),
        "ranked": ranked,
        "favourite_count": int(payload.get("favourite_count") or 0),
        "genre_id": int((genre or {}).get("id") or 1),
        "language_id": int((language or {}).get("id") or 1),
        "play_count": int(payload.get("play_count") or 0),
        "availability": {"more_information": None, "download_disabled": False},
        "beatmaps": beatmaps,
        "genre": genre,
        "language": language,
        "ratings": [],
    }
    return result  # type: ignore[return-value]


def beatconnect_find_beatmap(
    payload: dict[str, Any],
    *,
    beatmap_id: int | None = None,
) -> BeatmapDict | None:
    beatmapset = beatconnect_beatmapset_to_dict(payload)
    for beatmap in beatmapset.get("beatmaps") or []:
        if beatmap_id is not None and int(beatmap["id"]) != beatmap_id:
            continue
        beatmap["beatmapset"] = beatmapset
        return beatmap
    return None
