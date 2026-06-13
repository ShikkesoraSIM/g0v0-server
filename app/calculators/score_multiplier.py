"""Server-side port of the client per-ruleset ScoreMultiplierCalculator.

osu! moved mod score multipliers into a per-ruleset calculator
(ppy/osu#37818). The server stores ``total_score_without_mods`` (the raw,
multiplier-free score) for every score, so the post-rebalance total score is
simply ``round(total_score_without_mods * multiplier(mods, beatmap))``. Because
``total_score_without_mods`` carries no multiplier, a single "current values"
(V2) pass reproduces exactly what the new client computes -- there is no need
for the client's V1 fallback here, and every score is treated as post-rebalance
(mania key mods 0.9x, taiko/catch/mania classic 1.0x).

Mirrors:
  osu.Game.Rulesets.Osu.Scoring.OsuScoreMultiplierCalculatorV2
    (with Torii's custom difficulty adjust: AR/HP never change score, only
     LOWERING CS or OD penalises, so CS0/OD0 floors at 0.1x)
  osu.Game.Rulesets.{Taiko,Catch,Mania}.Scoring.*ScoreMultiplierCalculator

Used by the total-score recalc and by submission-time validation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.mods import APIMod


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _setting(mod: APIMod, key: str, default):
    settings = mod.get("settings") or {}
    value = settings.get(key, default)
    return default if value is None else value


def _uses_default_config(mod: APIMod) -> bool:
    # "UsesDefaultConfiguration" on the client == no non-default settings.
    return not (mod.get("settings") or {})


# --------------------------------------------------------------------------- #
#  osu! standard (mirrors OsuScoreMultiplierCalculatorV2)
# --------------------------------------------------------------------------- #

def _osu_double_time(speed: float) -> float:
    value = int(speed * 10) / 10.0
    penalty = 0.01 if value != 1.5 and value != 1.0 else 0.0
    return (value - 1) * 0.46 + 1 - penalty


def _osu_half_time(speed: float) -> float:
    return int(speed * 20) / 20.0 * 1.4 - 0.5


def _osu_hidden(mod: APIMod, other_mods_provide_timing_info: bool) -> float:
    value = 1.04
    if _setting(mod, "only_fade_approach_circles", False):
        value -= 0.02
    if other_mods_provide_timing_info:
        value -= 0.02
    return value


def _osu_flashlight(mod: APIMod) -> float:
    value = max(1.02, min(1.2, 1.2 - 0.2 * (_setting(mod, "size_multiplier", 1.0) - 1)))
    if not _setting(mod, "combo_based_size", True):
        value = 1 + (value - 1) / 5
    return value


def _osu_easy(mod: APIMod) -> float:
    # 0.8x base, minus 0.1x per extra retry over the default of 2, floored at 0.4x.
    return max(0.4, 0.8 - max(0, 0.1 * (_setting(mod, "retries", 2) - 2)))


def _osu_deflate(mod: APIMod) -> float:
    return 1.0 - max(0, 0.02 * (_setting(mod, "start_scale", 2.0) - 2.0))


def _osu_magnetised(mod: APIMod) -> float:
    return 0.7 - _setting(mod, "attraction_strength", 0.5) * 0.6


def _osu_time_ramp(mod: APIMod, default_final: float) -> float:
    initial = _setting(mod, "initial_rate", 1.0)
    final = _setting(mod, "final_rate", default_final)
    lo, hi = min(initial, final), max(initial, final)
    lo_m = _osu_half_time(lo) if lo < 1 else _osu_double_time(lo)
    hi_m = _osu_half_time(hi) if hi < 1 else _osu_double_time(hi)
    return 0.8 * lo_m + 0.2 * hi_m


def _osu_classic(mod: APIMod) -> float:
    return 0.985 if _setting(mod, "classic_note_lock", True) else 0.96


def _osu_difficulty_adjust(mod: APIMod, base_cs: float, base_od: float) -> float:
    # Torii custom (diverges from upstream's symmetric penalty): AR and HP never
    # change the multiplier, and only LOWERING CS or OD is penalised, by the
    # absolute resulting value. CS0 + OD0 bottoms out at the 0.1x floor; raising
    # any stat stays neutral.
    selected_cs = _setting(mod, "circle_size", base_cs)
    selected_od = _setting(mod, "overall_difficulty", base_od)
    cs_mult = _clamp(selected_cs / 4.0, 0.2, 1.0) if selected_cs < base_cs else 1.0
    od_mult = _clamp(selected_od / 5.0, 0.2, 1.0) if selected_od < base_od else 1.0
    return max(0.1, cs_mult * od_mult)


def _osu_tables(base_cs: float, base_od: float):
    blinds = 1.24
    singles: dict[str, Callable[[APIMod], float] | float] = {
        "EZ": _osu_easy,
        "NF": 0.5,
        "HT": lambda m: _osu_half_time(_setting(m, "speed_change", 0.75)),
        "DC": lambda m: _osu_half_time(_setting(m, "speed_change", 0.75)),
        "HR": 1.09,
        "DT": lambda m: _osu_double_time(_setting(m, "speed_change", 1.5)),
        "NC": lambda m: _osu_double_time(_setting(m, "speed_change", 1.5)),
        "HD": lambda m: _osu_hidden(m, False),
        "TC": 1.02,
        "FL": _osu_flashlight,
        "BL": blinds,
        "TP": 0.01,
        "DA": lambda m: _osu_difficulty_adjust(m, base_cs, base_od),
        "CL": _osu_classic,
        "RD": 0.7,
        "RX": 0.1,
        "AP": 0.1,
        "SO": 0.95,
        "DF": _osu_deflate,
        "WU": lambda m: _osu_time_ramp(m, 1.5),
        "WD": lambda m: _osu_time_ramp(m, 0.75),
        "AD": 0.7,
        "MG": _osu_magnetised,
        "AS": 0.1,
        "SY": 0.99,
    }
    combinations: list[tuple[tuple[str, ...], Callable[[dict[str, APIMod]], float]]] = [
        (("HD", "BL"), lambda by: blinds),
        (("HD", "WG"), lambda by: _osu_hidden(by["HD"], True)),
        (("HD", "GR"), lambda by: _osu_hidden(by["HD"], True)),
        (("HD", "DF"), lambda by: _osu_hidden(by["HD"], True) * _osu_deflate(by["DF"])),
        (("HD", "RP"), lambda by: _osu_hidden(by["HD"], True)),
        (("HD", "DP"), lambda by: _osu_hidden(by["HD"], True)),
        (("TC", "BL"), lambda by: blinds),
        (("FL", "FR"), lambda by: 1 + (_osu_flashlight(by["FL"]) - 1) / 2),
    ]
    return singles, combinations


# --------------------------------------------------------------------------- #
#  taiko / catch / mania  (the older rate curve, kept by upstream)
# --------------------------------------------------------------------------- #

def _rate_adjust(speed: float) -> float:
    value = int(speed * 10) / 10.0 - 1
    return 1 + value / 5 if speed >= 1 else 0.6 + value


def _default_cfg_mult(default_value: float):
    return lambda m: default_value if _uses_default_config(m) else 1.0


def _taiko_tables():
    singles: dict[str, Callable[[APIMod], float] | float] = {
        "EZ": 0.5,
        "NF": 0.5,
        "HT": lambda m: _rate_adjust(_setting(m, "speed_change", 0.75)),
        "DC": lambda m: _rate_adjust(_setting(m, "speed_change", 0.75)),
        "SR": 0.6,
        "HR": _default_cfg_mult(1.06),
        "DT": lambda m: _rate_adjust(_setting(m, "speed_change", 1.5)),
        "NC": lambda m: _rate_adjust(_setting(m, "speed_change", 1.5)),
        "HD": _default_cfg_mult(1.06),
        "FL": _default_cfg_mult(1.12),
        "DA": 0.5,
        "CL": 1.0,
        "CS": 0.9,
        "RX": 0.1,
        "WU": 0.5,
        "WD": 0.5,
        "AS": 0.5,
    }
    return singles, []


def _catch_tables():
    singles: dict[str, Callable[[APIMod], float] | float] = {
        "EZ": 0.5,
        "NF": 0.5,
        "HT": lambda m: _rate_adjust(_setting(m, "speed_change", 0.75)),
        "DC": lambda m: _rate_adjust(_setting(m, "speed_change", 0.75)),
        "HR": _default_cfg_mult(1.12),
        "DT": lambda m: _rate_adjust(_setting(m, "speed_change", 1.5)),
        "NC": lambda m: _rate_adjust(_setting(m, "speed_change", 1.5)),
        "HD": _default_cfg_mult(1.06),
        "FL": _default_cfg_mult(1.12),
        "DA": 0.5,
        "CL": 1.0,
        "RX": 0.1,
        "WU": 0.5,
        "WD": 0.5,
    }
    return singles, []


def _mania_tables():
    singles: dict[str, Callable[[APIMod], float] | float] = {
        "EZ": 0.5,
        "NF": 0.5,
        "HT": lambda m: _rate_adjust(_setting(m, "speed_change", 0.75)),
        "DC": lambda m: _rate_adjust(_setting(m, "speed_change", 0.75)),
        "NR": 0.9,
        "DA": 0.5,
        "CL": 1.0,
        "CS": 0.9,
        "HO": 0.9,
        "WU": 0.5,
        "WD": 0.5,
        "AS": 0.5,
    }
    for keys in range(1, 11):
        singles[f"{keys}K"] = 0.9
    return singles, []


# --------------------------------------------------------------------------- #
#  generic evaluator (mirrors ScoreMultiplierCalculator.CalculateFor)
# --------------------------------------------------------------------------- #

def _evaluate(mods: list[APIMod], singles, combinations) -> float:
    by_acr: dict[str, APIMod] = {mod["acronym"]: mod for mod in mods}
    if not by_acr:
        return 1.0

    remaining = set(by_acr.keys())
    result = 1.0

    if len(by_acr) > 1:
        for combo_acrs, fn in combinations:
            if remaining.issuperset(combo_acrs):
                result *= fn(by_acr)
                remaining.difference_update(combo_acrs)

    for acr in list(remaining):
        fn = singles.get(acr)
        if fn is None:
            continue
        result *= fn(by_acr[acr]) if callable(fn) else fn

    return result


def score_multiplier(
    ruleset_id: int,
    mods: list[APIMod],
    base_cs: float = 5.0,
    base_od: float = 5.0,
) -> float:
    """Score multiplier for ``mods`` in the given ruleset (post-rebalance values).

    ``base_cs`` / ``base_od`` are the beatmap's pre-mod circle size / overall
    difficulty (only consumed by osu! difficulty adjust).
    """
    if ruleset_id == 0:
        singles, combinations = _osu_tables(base_cs, base_od)
    elif ruleset_id == 1:
        singles, combinations = _taiko_tables()
    elif ruleset_id == 2:
        singles, combinations = _catch_tables()
    elif ruleset_id == 3:
        singles, combinations = _mania_tables()
    else:
        return 1.0

    return _evaluate(mods, singles, combinations)


def recompute_total_score(
    ruleset_id: int,
    mods: list[APIMod],
    total_score_without_mods: int,
    base_cs: float = 5.0,
    base_od: float = 5.0,
) -> int:
    """``round(total_score_without_mods * multiplier)`` (banker's rounding, as the client)."""
    multiplier = score_multiplier(ruleset_id, mods, base_cs, base_od)
    return int(round(total_score_without_mods * multiplier))
