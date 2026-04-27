from typing import Any, cast

from app.config import settings
from app.database.beatmap import BeatmapDict, BeatmapModel
from app.log import fetcher_logger

from ._base import BaseFetcher
from .beatconnect import beatconnect_base_url, beatconnect_enabled, beatconnect_find_beatmap, beatconnect_headers

from httpx import AsyncClient
from pydantic import TypeAdapter
from sqlmodel import select

logger = fetcher_logger("BeatmapFetcher")
adapter = TypeAdapter(
    BeatmapModel.generate_typeddict(
        (
            "checksum",
            "accuracy",
            "ar",
            "bpm",
            "convert",
            "count_circles",
            "count_sliders",
            "count_spinners",
            "cs",
            "deleted_at",
            "drain",
            "hit_length",
            "is_scoreable",
            "last_updated",
            "mode_int",
            "ranked",
            "url",
            "max_combo",
            "beatmapset",
        )
    )
)


class BeatmapFetcher(BaseFetcher):
    @staticmethod
    def _to_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    async def _get_beatmap_from_osu_direct(
        self,
        beatmap_id: int | None = None,
        beatmap_checksum: str | None = None,
    ) -> BeatmapDict | None:
        if beatmap_id is None and not beatmap_checksum:
            return None

        params: dict[str, Any] = {"limit": 100}
        if beatmap_id is not None:
            params["b"] = beatmap_id
        if beatmap_checksum:
            params["h"] = beatmap_checksum

        headers = {
            "User-Agent": "ToriiBeatmapFetcher/1.0 (+https://lazer.shikkesora.com)",
            "Accept": "application/json,*/*;q=0.8",
        }

        try:
            async with AsyncClient(timeout=8.0, follow_redirects=True) as client:
                resp = await client.get("https://osu.direct/api/get_beatmaps", params=params, headers=headers)

            if resp.status_code >= 400:
                logger.debug(
                    "osu.direct get_beatmaps failed for id={} md5={} with status {}",
                    beatmap_id,
                    beatmap_checksum,
                    resp.status_code,
                )
                return None

            payload = resp.json()
            if not isinstance(payload, list):
                return None

            requested_md5 = (beatmap_checksum or "").lower()
            for row in payload:
                if not isinstance(row, dict):
                    continue

                row_id = self._to_int(row.get("beatmap_id"), 0)
                row_md5 = str(row.get("file_md5") or "").lower()

                if beatmap_id is not None and row_id != beatmap_id:
                    continue
                if requested_md5 and row_md5 != requested_md5:
                    continue

                ranked = self._to_int(row.get("approved", row.get("ranked")), 0)
                if ranked not in (-2, -1, 0, 1, 2, 3, 4):
                    ranked = 0

                row_set_id = self._to_int(row.get("beatmapset_id"), 0)
                if row_id <= 0 or row_set_id <= 0:
                    continue

                beatmap_payload: dict[str, Any] = {
                    "id": row_id,
                    "beatmapset_id": row_set_id,
                    "difficulty_rating": self._to_float(row.get("difficultyrating"), 0.0),
                    "mode": self._to_int(row.get("mode"), 0),
                    "total_length": self._to_int(row.get("total_length"), 0),
                    "user_id": self._to_int(row.get("creator_id"), 0),
                    "version": str(row.get("version") or "Unknown"),
                    "url": f"{str(settings.web_url).rstrip('/')}/beatmaps/{row_id}",
                    "checksum": row.get("file_md5"),
                    "accuracy": self._to_float(row.get("diff_overall"), 0.0),
                    "ar": self._to_float(row.get("diff_approach"), 0.0),
                    "bpm": self._to_float(row.get("bpm"), 0.0),
                    "convert": bool(self._to_int(row.get("convert"), 0)),
                    "count_circles": self._to_int(row.get("count_normal"), 0),
                    "count_sliders": self._to_int(row.get("count_slider"), 0),
                    "count_spinners": self._to_int(row.get("count_spinner"), 0),
                    "cs": self._to_float(row.get("diff_size"), 0.0),
                    "drain": self._to_float(row.get("diff_drain"), 0.0),
                    "hit_length": self._to_int(row.get("hit_length"), 0),
                    "last_updated": row.get("last_update"),
                    "mode_int": self._to_int(row.get("mode"), 0),
                    "ranked": ranked,
                    "is_scoreable": ranked in (1, 2, 3, 4),
                    "max_combo": self._to_int(row.get("max_combo"), 0),
                    # Keep extra fields for fallback beatmapset bootstrap in Beatmap.get_or_fetch.
                    "artist": row.get("artist"),
                    "title": row.get("title"),
                    "creator": row.get("creator"),
                    "source": row.get("source"),
                    "tags": row.get("tags"),
                    "video": bool(self._to_int(row.get("video"), 0)),
                }

                logger.info(
                    "Beatmap {} resolved via osu.direct fallback (set {}, status {})",
                    row_id,
                    row_set_id,
                    ranked,
                )
                return cast(BeatmapDict, beatmap_payload)

        except Exception as e:
            logger.debug("osu.direct beatmap fallback failed for id={} md5={}: {}", beatmap_id, beatmap_checksum, e)

        return None

    async def _get_beatmap_from_beatconnect(
        self,
        beatmap_id: int | None = None,
        beatmap_checksum: str | None = None,
    ) -> BeatmapDict | None:
        if not beatconnect_enabled() or beatmap_id is None:
            return None

        beatmapset_id: int | None = None
        try:
            from app.database import Beatmap
            from app.dependencies.database import with_db

            async with with_db() as session:
                beatmap = await session.get(Beatmap, beatmap_id)
                if beatmap is not None:
                    beatmapset_id = beatmap.beatmapset_id
                elif beatmap_checksum:
                    beatmap = (
                        await session.exec(
                            select(Beatmap).where(Beatmap.checksum == beatmap_checksum)  # type: ignore[name-defined]
                        )
                    ).first()
                    if beatmap is not None:
                        beatmapset_id = beatmap.beatmapset_id
        except Exception as e:
            logger.debug("BeatConnect beatmap pre-resolution failed for id={} md5={}: {}", beatmap_id, beatmap_checksum, e)

        if beatmapset_id is None:
            return None

        try:
            async with AsyncClient(timeout=12.0, follow_redirects=True) as client:
                resp = await client.get(
                    f"{beatconnect_base_url()}/api/beatmap/{beatmapset_id}/",
                    headers=beatconnect_headers(),
                )

            if resp.status_code == 404:
                return None

            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, dict):
                return None

            return beatconnect_find_beatmap(payload, beatmap_id=beatmap_id)
        except Exception as e:
            logger.debug("BeatConnect beatmap lookup failed for id={} md5={}: {}", beatmap_id, beatmap_checksum, e)
            return None

    @staticmethod
    def _inject_torii_defaults(payload: Any) -> Any:
        """Inject defaults for Torii-specific fields the upstream APIs don't return.

        The official osu! API (and most mirrors) don't know about Torii's
        ``is_local`` flag. Without these defaults, ``adapter.validate_python``
        rejects every primary lookup and we fall through to a more limited
        mirror response, which results in incomplete metadata in the client
        (grey diffs, "unknown" labels, etc.).
        """
        if not isinstance(payload, dict):
            return payload
        payload.setdefault("is_local", False)
        beatmapset = payload.get("beatmapset")
        if isinstance(beatmapset, dict):
            beatmapset.setdefault("is_local", False)
        return payload

    async def get_beatmap(self, beatmap_id: int | None = None, beatmap_checksum: str | None = None) -> BeatmapDict:
        if beatmap_id:
            params = {"id": beatmap_id}
        elif beatmap_checksum:
            params = {"checksum": beatmap_checksum}
        else:
            raise ValueError("Either beatmap_id or beatmap_checksum must be provided.")
        logger.opt(colors=True).debug(f"get_beatmap: <y>{params}</y>")

        beatconnect_payload = await self._get_beatmap_from_beatconnect(beatmap_id, beatmap_checksum)
        if beatconnect_payload is not None:
            return adapter.validate_python(self._inject_torii_defaults(beatconnect_payload))  # pyright: ignore[reportReturnType]

        try:
            primary_payload = await self.request_api(
                "https://osu.ppy.sh/api/v2/beatmaps/lookup",
                params=params,
            )
            return adapter.validate_python(self._inject_torii_defaults(primary_payload))  # pyright: ignore[reportReturnType]
        except Exception as e:
            logger.warning(
                "Primary beatmap lookup failed for id={} md5={} ({})",
                beatmap_id,
                beatmap_checksum,
                e,
            )
            fallback = await self._get_beatmap_from_osu_direct(beatmap_id, beatmap_checksum)
            if fallback is not None:
                return fallback
            raise
