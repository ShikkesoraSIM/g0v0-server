import asyncio
import datetime
from typing import TYPE_CHECKING, TypedDict, cast

from app.models.mods import APIMod
from app.models.performance import (
    DifficultyAttributes,
    DifficultyAttributesUnion,
    ManiaDifficultyAttributes,
    ManiaPerformanceAttributes,
    OsuDifficultyAttributes,
    OsuPerformanceAttributes,
    PerformanceAttributes,
    PerformanceAttributesUnion,
    TaikoDifficultyAttributes,
    TaikoPerformanceAttributes,
)
from app.models.score import GameMode

from ._base import (
    AvailableModes,
    CalculateError,
    DifficultyError,
    PerformanceCalculator as BasePerformanceCalculator,
    PerformanceError,
)

from httpx import AsyncClient, HTTPError
from pydantic import TypeAdapter

if TYPE_CHECKING:
    from app.database.score import Score


class AvailableRulesetResp(TypedDict):
    has_performance_calculator: list[str]
    has_difficulty_calculator: list[str]
    loaded_rulesets: list[str]


class PerformanceServerPerformanceCalculator(BasePerformanceCalculator):
    def __init__(self, server_url: str = "http://localhost:5225", **kwargs) -> None:  # noqa: ARG002
        self.server_url = server_url

        self._available_modes: AvailableModes | None = None
        self._modes_lock = asyncio.Lock()
        self._today = datetime.date.today()

    async def init(self):
        await self.get_available_modes()

    def _process_modes(self, modes: AvailableRulesetResp) -> AvailableModes:
        performance_modes = {
            m for mode in modes["has_performance_calculator"] if (m := GameMode.parse(mode)) is not None
        }
        difficulty_modes = {m for mode in modes["has_difficulty_calculator"] if (m := GameMode.parse(mode)) is not None}
        if GameMode.OSU in performance_modes:
            performance_modes.add(GameMode.OSURX)
            performance_modes.add(GameMode.OSUAP)
        if GameMode.TAIKO in performance_modes:
            performance_modes.add(GameMode.TAIKORX)
        if GameMode.FRUITS in performance_modes:
            performance_modes.add(GameMode.FRUITSRX)

        return AvailableModes(
            has_performance_calculator=performance_modes,
            has_difficulty_calculator=difficulty_modes,
        )

    async def get_available_modes(self) -> AvailableModes:
        # https://github.com/GooGuTeam/osu-performance-server#get-available_rulesets
        if self._available_modes is not None and self._today == datetime.date.today():
            return self._available_modes
        async with self._modes_lock, AsyncClient() as client:
            try:
                resp = await client.get(f"{self.server_url}/available_rulesets")
                if resp.status_code != 200:
                    raise CalculateError(f"Failed to get available modes: {resp.text}")
                modes = cast(AvailableRulesetResp, resp.json())
                result = self._process_modes(modes)

                self._available_modes = result
                self._today = datetime.date.today()
                return result
            except HTTPError as e:
                raise CalculateError(f"Failed to get available modes: {e}") from e
            except Exception as e:
                raise CalculateError(f"Unknown error: {e}") from e

    async def calculate_performance(self, beatmap_raw: str, score: "Score") -> PerformanceAttributes:
        # https://github.com/GooGuTeam/osu-performance-server#post-performance
        #
        # FairTouchScreen passthrough: the perf server strips the TD mod
        # from its calculator input iff td_play_style == "tap". The verdict
        # itself was reached server-side by the classifier (see
        # classify_touchscreen below) and persisted on the score row. Any
        # other value (drag / mixed / unknown / 0) leaves TD in place and
        # the existing penalty applies. Field is omitted entirely when the
        # column says "Unknown" so the perf server treats it as a normal
        # request — there's no behavioural difference vs. omitting, but
        # keeps the wire format compact.
        td_play_style_value = _td_play_style_to_wire(getattr(score, "td_play_style", 0))

        async with AsyncClient(timeout=15) as client:
            try:
                request_body = {
                    "beatmap_id": score.beatmap_id,
                    "beatmap_file": beatmap_raw,
                    "checksum": score.map_md5,
                    "accuracy": score.accuracy,
                    "combo": score.max_combo,
                    "mods": score.mods,
                    "statistics": {
                        "great": score.n300,
                        "ok": score.n100,
                        "meh": score.n50,
                        "miss": score.nmiss,
                        "perfect": score.ngeki,
                        "good": score.nkatu,
                        "large_tick_hit": score.nlarge_tick_hit or 0,
                        "large_tick_miss": score.nlarge_tick_miss or 0,
                        "small_tick_hit": score.nsmall_tick_hit or 0,
                        "slider_tail_hit": score.nslider_tail_hit or 0,
                    },
                    "ruleset": score.gamemode.to_base_ruleset().value,
                }
                if td_play_style_value is not None:
                    request_body["td_play_style"] = td_play_style_value

                resp = await client.post(
                    f"{self.server_url}/performance",
                    json=request_body,
                )
                if resp.status_code != 200:
                    raise PerformanceError(f"Failed to calculate performance: {resp.text}")
                payload = resp.json()
                base_mode = score.gamemode.to_base_ruleset()
                if base_mode == GameMode.OSU:
                    try:
                        return TypeAdapter(OsuPerformanceAttributes).validate_python(payload)
                    except Exception:
                        # Some performance-server builds omit advanced osu fields.
                        # Fallback to generic payload to avoid dropping score PP pipeline.
                        return TypeAdapter(PerformanceAttributesUnion).validate_python(payload)
                if base_mode == GameMode.TAIKO:
                    return TypeAdapter(TaikoPerformanceAttributes).validate_python(payload)
                if base_mode == GameMode.MANIA:
                    return TypeAdapter(ManiaPerformanceAttributes).validate_python(payload)
                return TypeAdapter(PerformanceAttributesUnion).validate_python(payload)
            except HTTPError as e:
                raise PerformanceError(f"Failed to calculate performance: {e}") from e
            except Exception as e:
                raise CalculateError(f"Unknown error: {e}") from e

    async def calculate_difficulty(
        self, beatmap_raw: str, mods: list[APIMod] | None = None, gamemode: GameMode | None = None
    ) -> DifficultyAttributes:
        # https://github.com/GooGuTeam/osu-performance-server#post-difficulty
        async with AsyncClient(timeout=15) as client:
            try:
                resp = await client.post(
                    f"{self.server_url}/difficulty",
                    json={
                        "beatmap_file": beatmap_raw,
                        "mods": mods or [],
                        "ruleset": gamemode.value if gamemode else None,
                    },
                )
                if resp.status_code != 200:
                    raise DifficultyError(f"Failed to calculate difficulty: {resp.text}")
                payload = resp.json()
                base_mode = gamemode.to_base_ruleset() if gamemode is not None else None
                if base_mode == GameMode.OSU:
                    try:
                        return TypeAdapter(OsuDifficultyAttributes).validate_python(payload)
                    except Exception:
                        # Keep working with partial osu difficulty payloads.
                        return TypeAdapter(DifficultyAttributesUnion).validate_python(payload)
                if base_mode == GameMode.TAIKO:
                    return TypeAdapter(TaikoDifficultyAttributes).validate_python(payload)
                if base_mode == GameMode.MANIA:
                    return TypeAdapter(ManiaDifficultyAttributes).validate_python(payload)
                return TypeAdapter(DifficultyAttributesUnion).validate_python(payload)
            except HTTPError as e:
                raise DifficultyError(f"Failed to calculate difficulty: {e}") from e
            except Exception as e:
                raise DifficultyError(f"Unknown error: {e}") from e


    async def classify_touchscreen(
        self,
        replay_bytes: bytes,
        beatmap_raw: str,
        beatmap_id: int | None = None,
        score_id: int | None = None,
    ) -> "TouchScreenClassifyResult":
        """Ask the performance server to decide whether a TD-tagged osu!
        replay is a discrete-tap play (FairTouchScreen) or drag-tap cheese.

        Returns a verdict + confidence + raw metric bag. Callers should
        persist the verdict to ``scores.td_play_style`` and the confidence
        to ``scores.td_classification_confidence`` so the pp pipeline can
        consult them downstream without re-parsing the replay.

        Raises :class:`CalculateError` on a server-side failure. Callers
        treat that as "couldn't classify" — they keep the score's existing
        column values (typically 0=Unknown), which means the conservative
        TD penalty stays applied.
        """
        import base64

        async with AsyncClient(timeout=15) as client:
            try:
                resp = await client.post(
                    f"{self.server_url}/touchscreen/classify",
                    json={
                        "replay_file": base64.b64encode(replay_bytes).decode("ascii"),
                        "beatmap_file": beatmap_raw,
                        "beatmap_id": beatmap_id,
                        "score_id": score_id,
                    },
                )
                if resp.status_code != 200:
                    raise CalculateError(
                        f"Touchscreen classifier returned {resp.status_code}: {resp.text}"
                    )
                payload = resp.json()
                return TouchScreenClassifyResult(
                    style=str(payload.get("style", "unknown")).lower(),
                    confidence=float(payload.get("confidence", 0.0)),
                    metrics=payload.get("metrics", {}) or {},
                )
            except HTTPError as e:
                raise CalculateError(f"Touchscreen classify HTTP error: {e}") from e


class TouchScreenClassifyResult(TypedDict):
    """Shape of :meth:`PerformanceServerPerformanceCalculator.classify_touchscreen`'s return.

    ``style`` is one of ``"tap"``, ``"drag"``, ``"mixed"``, or ``"unknown"``
    (lower-cased on the wire); the helpers below convert between the wire
    form and the int enum stored on ``scores.td_play_style``.
    """

    style: str
    confidence: float
    metrics: dict[str, float]


# ─────────────────────────────────────────────────────────────────────────
# Enum mapping helpers. The DB stores td_play_style as a SmallInteger
# (cheap, indexable, easy to compare in WHERE clauses) while the perf
# server's JSON wire format uses lowercase strings (matches the C# enum
# name). These two helpers are the single conversion point so we don't
# scatter magic-number-to-name mappings across the codebase.
# ─────────────────────────────────────────────────────────────────────────

# Values match the C# enum in PerformanceServer/TouchScreen/TouchScreenPlayStyle.cs
# and the migration's documented mapping. Don't reorder.
TD_PLAY_STYLE_UNKNOWN = 0
TD_PLAY_STYLE_TAP = 1
TD_PLAY_STYLE_DRAG = 2
TD_PLAY_STYLE_MIXED = 3

_TD_WIRE_TO_INT: dict[str, int] = {
    "unknown": TD_PLAY_STYLE_UNKNOWN,
    "tap": TD_PLAY_STYLE_TAP,
    "drag": TD_PLAY_STYLE_DRAG,
    "mixed": TD_PLAY_STYLE_MIXED,
}

_TD_INT_TO_WIRE: dict[int, str] = {v: k for k, v in _TD_WIRE_TO_INT.items()}


def td_play_style_from_wire(style: str) -> int:
    """Translate ``"tap"`` → 1, ``"drag"`` → 2, etc. Anything unrecognised
    becomes 0 (Unknown) so the column always has a clean integer."""
    return _TD_WIRE_TO_INT.get(style.lower(), TD_PLAY_STYLE_UNKNOWN)


def _td_play_style_to_wire(value: int | None) -> str | None:
    """Translate the stored int back to the perf server's wire string,
    returning None for Unknown / NULL / out-of-range so the caller can
    elide the field from the request body entirely (less noise on the
    wire, matches what a pre-migration request would look like)."""
    if not value:
        return None
    return _TD_INT_TO_WIRE.get(int(value)) or None


PerformanceCalculator = PerformanceServerPerformanceCalculator
