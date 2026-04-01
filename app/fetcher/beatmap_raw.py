import asyncio
import hashlib
import re
import zipfile
from io import BytesIO
from typing import Any

from app.log import fetcher_logger

from ._base import BaseFetcher

from httpx import AsyncClient, HTTPError, Limits
import redis.asyncio as redis

urls = [
    "https://osu.direct/api/osu/{beatmap_id}",        # osu.direct
    "https://b.ppy.sh/osu/{beatmap_id}",              # old bancho endpoint
    "https://osu.ppy.sh/osu/{beatmap_id}",            # official (rate limited — last resort)
    "https://old.ppy.sh/osu/{beatmap_id}",            # legacy official fallback
]

logger = fetcher_logger("BeatmapRawFetcher")


class NoBeatmapError(Exception):
    """Beatmap 不存在异常"""

    pass


class BeatmapRawFetcher(BaseFetcher):
    def __init__(self, client_id: str = "", client_secret: str = "", **kwargs):
        # BeatmapRawFetcher does not require OAuth, pass empty values to base class.
        super().__init__(client_id, client_secret, **kwargs)
        # Shared HTTP client and connection pool.
        self._client: AsyncClient | None = None
        # De-duplicate concurrent requests.
        self._pending_requests: dict[int, asyncio.Future[str]] = {}
        self._request_lock = asyncio.Lock()
        self._request_headers = {
            "User-Agent": "ToriiBeatmapRawFetcher/1.0 (+https://lazer.shikkesora.com)",
            "Accept": "text/plain,application/octet-stream;q=0.9,*/*;q=0.8",
        }

    async def _get_client(self) -> AsyncClient:
        """Get or create a shared HTTP client."""
        if self._client is None:
            limits = Limits(
                max_keepalive_connections=20,
                max_connections=50,
                keepalive_expiry=30.0,
            )
            self._client = AsyncClient(
                timeout=10.0,
                limits=limits,
                follow_redirects=True,
            )
        return self._client

    async def close(self):
        """Close HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _extract_beatmap_id_from_osu(content: str) -> int | None:
        match = re.search(r"(?mi)^BeatmapID:\s*(\d+)\s*$", content)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    @staticmethod
    def _is_valid_osu_payload(content: str) -> bool:
        if not content or not content.strip():
            return False

        lowered = content.lstrip().lower()
        if lowered.startswith("<!doctype html") or lowered.startswith("<html"):
            return False

        has_header = "osu file format v" in content.lower()
        has_hitobjects = "[HitObjects]" in content
        return has_header and has_hitobjects

    def _extract_osu_from_osz_bytes(
        self,
        osz_bytes: bytes,
        beatmap_id: int,
        checksum: str | None,
    ) -> str | None:
        try:
            with zipfile.ZipFile(BytesIO(osz_bytes)) as zf:
                osu_files = [name for name in zf.namelist() if name.lower().endswith(".osu")]
                if not osu_files:
                    return None

                # First pass: exact BeatmapID match.
                for osu_name in osu_files:
                    raw_bytes = zf.read(osu_name)
                    text = raw_bytes.decode("utf-8-sig", errors="ignore")
                    parsed_id = self._extract_beatmap_id_from_osu(text)
                    if parsed_id == beatmap_id and self._is_valid_osu_payload(text):
                        return text

                # Second pass: checksum match (for malformed/missing BeatmapID).
                if checksum:
                    expected = checksum.lower()
                    for osu_name in osu_files:
                        raw_bytes = zf.read(osu_name)
                        md5 = hashlib.md5(raw_bytes, usedforsecurity=False).hexdigest().lower()
                        if md5 == expected:
                            text = raw_bytes.decode("utf-8-sig", errors="ignore")
                            if self._is_valid_osu_payload(text):
                                return text

                # Last resort: one-difficulty set.
                if len(osu_files) == 1:
                    text = zf.read(osu_files[0]).decode("utf-8-sig", errors="ignore")
                    if self._is_valid_osu_payload(text):
                        return text
        except Exception:
            return None
        return None

    async def _resolve_from_osu_direct(self, beatmap_id: int) -> tuple[int | None, str | None]:
        client = await self._get_client()
        try:
            resp = await client.get(
                "https://osu.direct/api/get_beatmaps",
                params={"b": beatmap_id},
                headers=self._request_headers,
            )
            if resp.status_code >= 400:
                return None, None

            payload: Any = resp.json()
            if not isinstance(payload, list):
                return None, None

            exact = None
            for row in payload:
                if not isinstance(row, dict):
                    continue
                try:
                    if int(row.get("beatmap_id", 0)) == beatmap_id:
                        exact = row
                        break
                except Exception:
                    continue

            if not exact:
                return None, None

            beatmapset_id = exact.get("beatmapset_id")
            file_md5 = exact.get("file_md5")
            return int(beatmapset_id), (str(file_md5) if file_md5 else None)
        except Exception:
            return None, None

    async def _resolve_beatmap_context(self, beatmap_id: int) -> tuple[int | None, str | None, bool]:
        beatmapset_id: int | None = None
        checksum: str | None = None
        is_local = False

        # Try local DB first.
        try:
            from app.database import Beatmap
            from app.dependencies.database import with_db

            async with with_db() as session:
                beatmap = await session.get(Beatmap, beatmap_id)
                if beatmap is not None:
                    beatmapset_id = beatmap.beatmapset_id
                    checksum = beatmap.checksum
                    is_local = bool(beatmap.is_local)
                    return beatmapset_id, checksum, is_local
        except Exception as e:
            logger.debug(f"Failed to resolve local beatmap context for {beatmap_id}: {e}")

        # Fall back to official API lookup (available on Fetcher combined class).
        if hasattr(self, "get_beatmap"):
            try:
                data = await self.get_beatmap(beatmap_id=beatmap_id)  # type: ignore[attr-defined]
                beatmapset_id = data.get("beatmapset_id")
                checksum = data.get("checksum")
                return int(beatmapset_id) if beatmapset_id is not None else None, checksum, False
            except Exception as e:
                logger.debug(f"Official beatmap lookup failed for {beatmap_id}: {e}")

        # Last fallback: osu.direct lookup endpoint.
        sid, md5 = await self._resolve_from_osu_direct(beatmap_id)
        return sid, md5, False

    async def _fetch_local_osz_beatmap_raw(
        self,
        beatmap_id: int,
        beatmapset_id: int | None,
        checksum: str | None,
    ) -> str | None:
        if beatmapset_id is None:
            return None

        try:
            from app.dependencies.storage import get_storage_service

            storage = get_storage_service()
            file_path = f"beatmapsets/{beatmapset_id}.osz"
            if not await storage.is_exists(file_path):
                return None

            osz_bytes = await storage.read_file(file_path)
            return self._extract_osu_from_osz_bytes(osz_bytes, beatmap_id, checksum)
        except Exception as e:
            logger.debug(f"Local .osz fallback failed for beatmap {beatmap_id}: {e}")
            return None

    def _build_archive_urls(self, beatmapset_id: int) -> list[str]:
        # Official first, then mirrors from the existing download service.
        candidates = [f"https://osu.ppy.sh/beatmapsets/{beatmapset_id}/download?noVideo=1"]

        try:
            from app.service.beatmap_download_service import download_service

            candidates.extend(download_service.get_download_urls(beatmapset_id, no_video=True, is_china=False))
            candidates.extend(download_service.get_download_urls(beatmapset_id, no_video=True, is_china=True))
        except Exception as e:
            logger.debug(f"Could not load beatmap download service URLs: {e}")

        # Keep order and remove duplicates.
        deduped: list[str] = []
        seen: set[str] = set()
        for url in candidates:
            if url and url not in seen:
                deduped.append(url)
                seen.add(url)
        return deduped

    async def _fetch_from_archive_fallback(
        self,
        beatmap_id: int,
        beatmapset_id: int | None,
        checksum: str | None,
    ) -> str | None:
        if beatmapset_id is None:
            return None

        client = await self._get_client()
        archive_urls = self._build_archive_urls(beatmapset_id)
        for archive_url in archive_urls:
            try:
                logger.debug(f"Archive fallback fetch for beatmap {beatmap_id}: {archive_url}")
                resp = await client.get(archive_url, headers=self._request_headers, timeout=25.0)
                if resp.status_code >= 400:
                    continue

                body = resp.content
                if not body:
                    continue

                content_type = (resp.headers.get("Content-Type") or "").lower()
                if not body.startswith(b"PK") and "zip" not in content_type and "octet-stream" not in content_type:
                    continue

                raw = self._extract_osu_from_osz_bytes(body, beatmap_id, checksum)
                if raw is not None:
                    logger.debug(f"Archive fallback succeeded for beatmap {beatmap_id} via {archive_url}")
                    return raw
            except Exception as e:
                logger.debug(f"Archive fallback source failed for beatmap {beatmap_id}: {archive_url} ({e})")

        return None

    async def get_beatmap_raw(self, beatmap_id: int) -> str:
        future: asyncio.Future[str] | None = None

        # Check if there is already an in-flight request for this beatmap.
        async with self._request_lock:
            if beatmap_id in self._pending_requests:
                logger.debug(f"Beatmap {beatmap_id} request already in progress, waiting...")
                future = self._pending_requests[beatmap_id]

        # If another coroutine already started this request, await it.
        if future is not None:
            try:
                return await future
            except Exception as e:
                logger.warning(f"Waiting for beatmap {beatmap_id} failed: {e}")
                future = None

        # Create a new in-flight request future.
        async with self._request_lock:
            if beatmap_id in self._pending_requests:
                future = self._pending_requests[beatmap_id]
                if future is not None:
                    try:
                        return await future
                    except Exception as e:
                        logger.debug(f"Concurrent request for beatmap {beatmap_id} failed: {e}")

            future = asyncio.get_event_loop().create_future()
            self._pending_requests[beatmap_id] = future

        try:
            result = await self._fetch_beatmap_raw(beatmap_id)
            if not future.done():
                future.set_result(result)
            return result
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            raise
        except Exception as e:
            if not future.done():
                future.set_exception(e)
            return await future
        finally:
            async with self._request_lock:
                self._pending_requests.pop(beatmap_id, None)

    async def _fetch_beatmap_raw(self, beatmap_id: int) -> str:
        client = await self._get_client()
        last_error = None
        beatmapset_id, checksum, is_local = await self._resolve_beatmap_context(beatmap_id)
        expected_checksum = checksum.lower() if checksum else None

        # Local maps: use local .osz first. This avoids relying on external mirrors for local uploads.
        local_raw = await self._fetch_local_osz_beatmap_raw(beatmap_id, beatmapset_id, checksum)
        if local_raw is not None:
            logger.debug(f"Successfully fetched local beatmap raw for {beatmap_id} (set {beatmapset_id})")
            return local_raw

        for url_template in urls:
            req_url = url_template.format(beatmap_id=beatmap_id)
            try:
                logger.opt(colors=True).debug(f"get_beatmap_raw: <y>{req_url}</y>")
                resp = await client.get(req_url, headers=self._request_headers)

                if resp.status_code >= 400:
                    logger.warning(f"Beatmap {beatmap_id} from {req_url}: HTTP {resp.status_code}")
                    last_error = NoBeatmapError(f"HTTP {resp.status_code}")
                    continue

                raw_bytes = resp.content
                body = raw_bytes.decode("utf-8-sig", errors="ignore")
                if not body or not body.strip():
                    logger.warning(f"Beatmap {beatmap_id} from {req_url}: empty response")
                    last_error = NoBeatmapError("Empty response")
                    continue

                if not self._is_valid_osu_payload(body):
                    logger.warning(f"Beatmap {beatmap_id} from {req_url}: invalid/non-osu payload")
                    last_error = NoBeatmapError("Invalid payload")
                    continue

                parsed_id = self._extract_beatmap_id_from_osu(body)
                if parsed_id is not None and parsed_id != beatmap_id:
                    logger.warning(
                        f"Beatmap {beatmap_id} from {req_url}: returned mismatched BeatmapID={parsed_id}"
                    )
                    last_error = NoBeatmapError("Mismatched BeatmapID")
                    continue

                if expected_checksum:
                    payload_checksum = hashlib.md5(raw_bytes, usedforsecurity=False).hexdigest().lower()
                    if payload_checksum != expected_checksum:
                        if is_local:
                            # Local beatmaps can collide with upstream IDs; checksum must match.
                            logger.warning(
                                "Beatmap {} from {}: checksum mismatch for local map (expected {}, got {})",
                                beatmap_id,
                                req_url,
                                expected_checksum,
                                payload_checksum,
                            )
                            last_error = NoBeatmapError("Checksum mismatch")
                            continue

                        # For upstream maps, checksum can drift when DB metadata is stale.
                        # Prefer using a valid payload over dropping PP calculation.
                        logger.warning(
                            "Beatmap {} from {}: checksum mismatch (expected {}, got {}), accepting payload",
                            beatmap_id,
                            req_url,
                            expected_checksum,
                            payload_checksum,
                        )

                logger.debug(f"Successfully fetched beatmap {beatmap_id} from {req_url}")
                return body

            except Exception as e:
                logger.warning(f"Error fetching beatmap {beatmap_id} from {req_url}: {e}")
                last_error = e
                continue

        # Final fallback: resolve through beatmapset archives and extract the matching .osu.
        archive_raw = await self._fetch_from_archive_fallback(beatmap_id, beatmapset_id, checksum)
        if archive_raw is not None:
            logger.debug(
                "Successfully fetched beatmap {} from archive fallback (set {}, local={})",
                beatmap_id,
                beatmapset_id,
                is_local,
            )
            return archive_raw

        error_msg = f"Failed to fetch beatmap {beatmap_id} from all sources"
        if last_error and isinstance(last_error, NoBeatmapError):
            raise last_error
        raise HTTPError(error_msg) from last_error

    async def get_or_fetch_beatmap_raw(self, redis: redis.Redis, beatmap_id: int) -> str:
        from app.config import settings

        cache_key = f"beatmap:{beatmap_id}:raw"
        cache_expire = settings.beatmap_cache_expire_hours * 60 * 60

        if await redis.exists(cache_key):
            content = await redis.get(cache_key)
            if content:
                await redis.expire(cache_key, cache_expire)
                return content

        raw = await self.get_beatmap_raw(beatmap_id)
        await redis.set(cache_key, raw, ex=cache_expire)
        return raw
