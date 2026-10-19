from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.database._base import DatabaseModel
from app.database.beatmapset import BeatmapsetModel
from app.fetcher.beatconnect import beatconnect_beatmapset_to_dict
from app.fetcher.beatmap import (
    BeatmapFetcher,
    adapter as beatmap_adapter,
)
from app.fetcher.beatmapset import (
    BeatmapsetFetcher,
    adapter as beatmapset_adapter,
)

from pydantic import ValidationError


class BeatmapsetBlacklistTest(unittest.IsolatedAsyncioTestCase):
    async def test_summary_loads_blacklist_asynchronously(self):
        session = object()
        load = AsyncMock(return_value=[SimpleNamespace(id=123), SimpleNamespace(id=124)])

        class AwaitableBeatmaps:
            @property
            def beatmaps(self):
                return load()

        class LazyBeatmapset:
            awaitable_attrs = AwaitableBeatmaps()

            @property
            def beatmaps(self):
                raise AssertionError("no leer una relacion lazy sin await")

        with (
            patch.object(DatabaseModel, "transform", AsyncMock(return_value={"id": 456, "covers": None})),
            patch("app.database.beatmapset.async_object_session", return_value=session),
            patch("app.database.beatmap.get_all_banned_beatmap_ids", AsyncMock(return_value={123})) as banned,
        ):
            result = await BeatmapsetModel.transform(LazyBeatmapset())

        assert result["has_blacklisted_difficulties"]
        assert not result["is_blacklisted"]
        banned.assert_awaited_once_with(session)
        load.assert_awaited_once()

    async def test_full_response_preserves_blacklist_without_loading_relation(self):
        payload = {"id": 456, "covers": None, "beatmaps": [{"id": 123, "is_blacklisted": True}]}
        with patch.object(DatabaseModel, "transform", AsyncMock(return_value=payload)):
            result = await BeatmapsetModel.transform(object(), session=object())
        assert result["is_blacklisted"]
        assert result["has_blacklisted_difficulties"]


def remote_set() -> dict:
    return beatconnect_beatmapset_to_dict(
        {
            "id": 456,
            "status": "ranked",
            "artist": "artist",
            "title": "title",
            "creator": "mapper",
            "beatmaps": [{"id": 123, "mode": "std", "version": "Hard"}],
        }
    )


class BeatmapMetadataTest(unittest.TestCase):
    def test_remote_set_validates_after_normalization(self):
        payload = remote_set()
        with self.assertRaises(ValidationError):  # noqa: PT027
            beatmapset_adapter.validate_python(payload)

        result = beatmapset_adapter.validate_python(BeatmapsetFetcher._ensure_local_flags(payload))
        assert result["id"] == 456
        assert result["beatmaps"][0]["ranked"] == 1
        assert not result["beatmaps"][0]["is_blacklisted"]

    def test_single_difficulty_validates_after_normalization(self):
        payload = remote_set()
        beatmap = deepcopy(payload["beatmaps"][0])
        beatmap["beatmapset"] = payload
        with self.assertRaises(ValidationError):  # noqa: PT027
            beatmap_adapter.validate_python(beatmap)

        result = beatmap_adapter.validate_python(BeatmapFetcher._inject_torii_defaults(beatmap))
        assert result["id"] == 123
        assert result["ranked"] == 1
        assert not result["is_blacklisted"]

    def test_existing_flags_are_preserved(self):
        payload = remote_set()
        payload.update(is_local=True, ai=True, is_blacklisted=True, has_blacklisted_difficulties=True)
        payload["beatmaps"][0].update(is_local=True, is_blacklisted=True)
        normalized = BeatmapsetFetcher._ensure_local_flags(payload)
        assert normalized["is_blacklisted"]
        assert normalized["has_blacklisted_difficulties"]
        assert normalized["beatmaps"][0]["is_blacklisted"]
        assert normalized["is_local"]
        assert normalized["ai"]

        beatmap = deepcopy(normalized["beatmaps"][0])
        beatmap["beatmapset"] = normalized
        BeatmapFetcher._inject_torii_defaults(beatmap)
        assert beatmap["is_blacklisted"]
        assert beatmap["beatmapset"]["is_blacklisted"]


class MissingChecksumTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_missing_checksums_are_filled(self):
        payload = remote_set()
        payload["beatmaps"][0]["checksum"] = "existing"
        payload["beatmaps"].append({"id": 124, "checksum": None})
        fetcher = object.__new__(BeatmapsetFetcher)
        fetcher.request_api = AsyncMock(
            return_value={
                "id": 456,
                "beatmaps": [
                    {"id": 123, "checksum": "different"},
                    {"id": 124, "checksum": "missing"},
                    {"id": 999, "checksum": "unrelated"},
                ],
            }
        )

        await fetcher._enrich_missing_checksums_from_api(456, payload)
        assert payload["beatmaps"][0]["checksum"] == "existing"
        assert payload["beatmaps"][1]["checksum"] == "missing"
        assert len(payload["beatmaps"]) == 2
        fetcher.request_api.assert_awaited_once()

    async def test_complete_set_does_not_call_official_api(self):
        payload = remote_set()
        payload["beatmaps"][0]["checksum"] = "existing"
        fetcher = object.__new__(BeatmapsetFetcher)
        fetcher.request_api = AsyncMock()
        await fetcher._enrich_missing_checksums_from_api(456, payload)
        fetcher.request_api.assert_not_awaited()

    async def test_wrong_set_is_ignored(self):
        payload = remote_set()
        fetcher = object.__new__(BeatmapsetFetcher)
        fetcher.request_api = AsyncMock(return_value={"id": 999, "beatmaps": [{"id": 123, "checksum": "wrong"}]})
        await fetcher._enrich_missing_checksums_from_api(456, payload)
        assert payload["beatmaps"][0]["checksum"] is None

    async def test_outage_preserves_other_metadata(self):
        payload = remote_set()
        original = deepcopy(payload)
        fetcher = object.__new__(BeatmapsetFetcher)
        fetcher.request_api = AsyncMock(side_effect=TimeoutError())
        await fetcher._enrich_missing_checksums_from_api(456, payload)
        assert payload == original


if __name__ == "__main__":
    unittest.main()
