"""Service for verifying client versions against known valid versions."""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from app.config import settings
from app.log import logger
from app.models.version import VersionCheckResult, VersionList
from app.path import CONFIG_DIR

import aiofiles
import httpx
from httpx import AsyncClient

HASHES_DIR = CONFIG_DIR / "client_versions.json"
OVERRIDES_DIR = CONFIG_DIR / "client_versions_overrides.json"
UNKNOWN_HASHES_DIR = CONFIG_DIR / "client_versions_unknown.json"


def _normalize_hash(client_hash: str) -> str:
    return (client_hash or "").strip().lower()


def _detect_os_from_user_agent(user_agent: str) -> str:
    ua = (user_agent or "").strip().lower()
    if not ua:
        return ""

    # Order matters: iOS UAs often contain "mac os x".
    if any(token in ua for token in ("iphone", "ipad", "ipod", "ios")):
        return "iOS"
    if "android" in ua:
        return "Android"
    if "windows" in ua or "win32" in ua or "win64" in ua:
        return "Windows"
    if any(token in ua for token in ("mac os", "macos", "darwin")):
        return "macOS"
    if "linux" in ua:
        return "Linux"
    if "freebsd" in ua:
        return "FreeBSD"

    return ""


class ClientVerificationService:
    """A service to verify client versions against known valid versions.

    Attributes:
        version_lists (list[VersionList]): A list of version lists fetched from remote sources.

    Methods:
        init(): Initialize the service by loading version data from disk and refreshing from remote.
        refresh(): Fetch the latest version lists from configured URLs and store them locally.
        load_from_disk(): Load version lists from the local JSON file.
        validate_client_version(client_version: str) -> VersionCheckResult: Validate a given client version against the known versions.
    """  # noqa: E501

    def __init__(self) -> None:
        self.original_version_lists: dict[str, list[VersionList]] = {}
        self.overrides: dict[str, tuple[str, str, str]] = {}
        self.unknown_hashes: dict[str, dict[str, Any]] = {}
        self.versions: dict[str, tuple[str, str, str]] = {}
        self._lock = asyncio.Lock()
        self._lazy_load_attempted = False
        self._lazy_refresh_attempted = False

    async def init(self) -> None:
        """Initialize the service by loading version data from disk and refreshing from remote."""
        await self.load_from_disk(first_load=True)
        await self.load_unknown_hashes_from_disk()
        await self.refresh()
        await self.load_from_disk()

    async def refresh(self) -> None:
        """Fetch the latest version lists from configured URLs and store them locally."""
        lists: dict[str, list[VersionList]] = self.original_version_lists.copy()
        async with AsyncClient() as client:
            for url in settings.client_version_urls:
                try:
                    resp = await client.get(url, timeout=10)
                    resp.raise_for_status()
                    data = resp.json()
                    if len(data) == 0:
                        logger.warning(f"Client version list from {url} is empty")
                        continue
                    lists[url] = data
                    logger.info(f"Fetched client version list from {url}, total {len(data)} clients")
                except httpx.TimeoutException:
                    logger.warning(f"Timeout when fetching client version list from {url}")
                except Exception as e:
                    logger.warning(f"Failed to fetch client version list from {url}: {e}")
        async with aiofiles.open(HASHES_DIR, "wb") as f:
            await f.write(json.dumps(lists).encode("utf-8"))

    async def load_from_disk(self, first_load: bool = False) -> None:
        """Load version lists from the local JSON file."""
        async with self._lock:
            self.versions.clear()
            self.overrides.clear()
            self.original_version_lists = {}
            try:
                remote_versions: dict[str, tuple[str, str, str]] = {}
                if HASHES_DIR.is_file():
                    async with aiofiles.open(HASHES_DIR, "rb") as f:
                        content = await f.read()
                        self.original_version_lists = json.loads(content.decode("utf-8"))
                        for version_list_group in self.original_version_lists.values():
                            for version_list in version_list_group:
                                for version_info in version_list["versions"]:
                                    for client_hash, os_name in version_info["hashes"].items():
                                        remote_versions[_normalize_hash(client_hash)] = (
                                            version_list["name"],
                                            version_info["version"],
                                            os_name,
                                        )
                elif not first_load:
                    logger.warning("Client version list file does not exist on disk")

                self.overrides = await self._load_overrides_from_disk()
                self.versions.update(remote_versions)
                self.versions.update(self.overrides)

                if not first_load:
                    remote_count = len(remote_versions)
                    override_count = len(self.overrides)
                    total = len(self.versions)
                    if total == 0:
                        logger.warning("Client version list is empty after loading from disk")
                    else:
                        logger.info(
                            "Loaded client versions from disk "
                            f"(remote={remote_count}, overrides={override_count}, total={total})"
                        )
            except Exception as e:
                logger.exception(f"Failed to load client version list from disk: {e}")

    async def _load_overrides_from_disk(self) -> dict[str, tuple[str, str, str]]:
        if not OVERRIDES_DIR.is_file():
            async with aiofiles.open(OVERRIDES_DIR, "wb") as f:
                await f.write(b"{}")
            return {}

        async with aiofiles.open(OVERRIDES_DIR, "rb") as f:
            content = await f.read()

        raw = json.loads(content.decode("utf-8") or "{}")
        if not isinstance(raw, dict):
            logger.warning("client_versions_overrides.json must be a JSON object")
            return {}

        overrides: dict[str, tuple[str, str, str]] = {}
        for hash_key, value in raw.items():
            client_hash = _normalize_hash(str(hash_key))
            if not client_hash or not isinstance(value, dict):
                continue

            client_name = str(value.get("client_name") or value.get("name") or "").strip()
            version = str(value.get("version") or "").strip()
            os_name = str(value.get("os") or value.get("platform") or "").strip()
            if not any((client_name, version, os_name)):
                continue

            overrides[client_hash] = (client_name, version, os_name)
        return overrides

    async def load_unknown_hashes_from_disk(self) -> None:
        async with self._lock:
            if not UNKNOWN_HASHES_DIR.is_file():
                async with aiofiles.open(UNKNOWN_HASHES_DIR, "wb") as f:
                    await f.write(b"{}")
                self.unknown_hashes = {}
                return

            try:
                async with aiofiles.open(UNKNOWN_HASHES_DIR, "rb") as f:
                    content = await f.read()
                raw = json.loads(content.decode("utf-8") or "{}")
                if not isinstance(raw, dict):
                    logger.warning("client_versions_unknown.json must be a JSON object")
                    self.unknown_hashes = {}
                    return
                self.unknown_hashes = {str(k).lower(): v for k, v in raw.items() if isinstance(v, dict)}
            except Exception as e:
                logger.warning(f"Failed to load unknown client hash list: {e}")
                self.unknown_hashes = {}

    async def _persist_unknown_hashes(self) -> None:
        payload = json.dumps(self.unknown_hashes, ensure_ascii=False, sort_keys=True, indent=2)
        async with aiofiles.open(UNKNOWN_HASHES_DIR, "wb") as f:
            await f.write(payload.encode("utf-8"))

    async def get_unknown_hashes(self) -> dict[str, dict[str, Any]]:
        async with self._lock:
            return dict(self.unknown_hashes)

    async def resolve_hash_input(self, raw_hash: str) -> tuple[str, list[str]]:
        """
        Resolve user-provided hash/prefix to a concrete known hash when possible.

        Returns:
            (resolved_hash, ambiguous_candidates)
        """
        normalized = _normalize_hash(raw_hash)
        if not normalized:
            return "", []

        async with self._lock:
            candidates = sorted(
                {
                    *[h for h in self.versions.keys() if h.startswith(normalized)],
                    *[h for h in self.unknown_hashes.keys() if h.startswith(normalized)],
                }
            )

        if not candidates:
            return normalized, []

        # Prefer a longer concrete hash when input is just a prefix.
        longer = [c for c in candidates if len(c) > len(normalized)]
        if len(longer) == 1:
            return longer[0], []
        if len(longer) > 1:
            return normalized, longer

        if len(candidates) == 1:
            return candidates[0], []

        return normalized, candidates

    async def get_version_signature_lookup(self) -> dict[tuple[str, str, str], str]:
        """Return a lookup of (client_name, version, os) -> hash.

        All keys are normalized to lowercase/trimmed strings.
        """
        async with self._lock:
            lookup: dict[tuple[str, str, str], str] = {}
            for client_hash, (client_name, version, os_name) in self.versions.items():
                signature = (
                    (client_name or "").strip().lower(),
                    (version or "").strip().lower(),
                    (os_name or "").strip().lower(),
                )
                if signature in lookup:
                    continue
                lookup[signature] = client_hash
            return lookup

    async def assign_hash_override(
        self,
        client_hash: str,
        *,
        client_name: str,
        version: str = "",
        os_name: str = "",
        remove_from_unknown: bool = True,
    ) -> None:
        normalized_hash = _normalize_hash(client_hash)
        if not normalized_hash:
            raise ValueError("client_hash is required")
        if not client_name.strip():
            raise ValueError("client_name is required")

        async with self._lock:
            resolved_os_name = os_name.strip()
            if not resolved_os_name:
                unknown_entry = self.unknown_hashes.get(normalized_hash) or {}
                resolved_os_name = str(unknown_entry.get("last_detected_os") or "").strip()
                if not resolved_os_name:
                    resolved_os_name = _detect_os_from_user_agent(
                        str(unknown_entry.get("last_user_agent") or "")
                    )

            raw: dict[str, Any] = {}
            if OVERRIDES_DIR.is_file():
                try:
                    async with aiofiles.open(OVERRIDES_DIR, "rb") as f:
                        content = await f.read()
                    parsed = json.loads(content.decode("utf-8") or "{}")
                    if isinstance(parsed, dict):
                        raw = parsed
                except Exception:
                    raw = {}

            raw[normalized_hash] = {
                "client_name": client_name.strip(),
                "version": version.strip(),
                "os": resolved_os_name,
            }

            async with aiofiles.open(OVERRIDES_DIR, "wb") as f:
                await f.write(json.dumps(raw, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"))

            self.overrides[normalized_hash] = (
                client_name.strip(),
                version.strip(),
                resolved_os_name,
            )
            self.versions[normalized_hash] = self.overrides[normalized_hash]

            if remove_from_unknown and normalized_hash in self.unknown_hashes:
                del self.unknown_hashes[normalized_hash]
                await self._persist_unknown_hashes()

    async def record_unknown_hash(
        self,
        client_hash: str,
        *,
        user_agent: str = "",
        user_id: int | None = None,
        source: str = "",
    ) -> None:
        normalized_hash = _normalize_hash(client_hash)
        if not normalized_hash:
            return

        now = datetime.now(tz=UTC).isoformat()
        user_agent_value = (user_agent or "").strip()[:180]
        detected_os = _detect_os_from_user_agent(user_agent_value)
        should_persist = False

        async with self._lock:
            if normalized_hash in self.versions:
                return

            entry = self.unknown_hashes.get(normalized_hash)
            if entry is None:
                self.unknown_hashes[normalized_hash] = {
                    "count": 1,
                    "first_seen": now,
                    "last_seen": now,
                    "last_user_id": user_id,
                    "last_user_agent": user_agent_value,
                    "last_detected_os": detected_os,
                    "last_source": source,
                }
                should_persist = True
            else:
                entry["count"] = int(entry.get("count", 0)) + 1
                entry["last_seen"] = now
                entry["last_user_id"] = user_id
                entry["last_user_agent"] = user_agent_value
                if detected_os:
                    entry["last_detected_os"] = detected_os
                entry["last_source"] = source
                should_persist = entry["count"] % 20 == 0

            if should_persist:
                await self._persist_unknown_hashes()

        if should_persist:
            logger.info(
                "Recorded unknown client hash "
                f"{normalized_hash[:12]}... (source={source}, user_id={user_id})"
            )

    async def validate_client_version(self, client_version: str) -> VersionCheckResult:
        """Validate a given client version against the known versions.

        Args:
            client_version (str): The client version string to validate.

        Returns:
            VersionCheckResult: The result of the validation.
        """
        if not self.versions and not self._lazy_load_attempted:
            self._lazy_load_attempted = True
            await self.load_from_disk()

        # If disk was empty/unavailable, try one network refresh lazily.
        if not self.versions and not self._lazy_refresh_attempted:
            self._lazy_refresh_attempted = True
            await self.refresh()
            await self.load_from_disk()

        client_hash = _normalize_hash(client_version)
        async with self._lock:
            if client_hash in self.versions:
                name, version, os_name = self.versions[client_hash]
                return VersionCheckResult(is_valid=True, client_name=name, version=version, os=os_name)
        if settings.check_client_version:
            return VersionCheckResult(is_valid=False)
        return VersionCheckResult(is_valid=True)


_client_verification_service: ClientVerificationService | None = None


def get_client_verification_service() -> ClientVerificationService:
    """Get the singleton instance of ClientVerificationService.

    Returns:
        ClientVerificationService: The singleton instance.
    """
    global _client_verification_service
    if _client_verification_service is None:
        _client_verification_service = ClientVerificationService()
    return _client_verification_service


async def init_client_verification_service() -> None:
    """Initialize the ClientVerificationService singleton."""
    service = get_client_verification_service()
    logger.info("Initializing ClientVerificationService...")
    await service.init()
