"""Torii-original PP penalty for degenerate low CS/OD osu! standard plays.

rosu's difficulty/PP does not punish tiny circle size / low overall difficulty
hard enough, so relax CS0/OD0 plays farm huge PP on otherwise-hard maps. This
applies a smooth multiplier on the EFFECTIVE (post-mod) circle size and overall
difficulty, osu! standard only (including the relax/autopilot variants), and
never touches Easy plays (honest easy mode, already PP-reduced by rosu).

    csFactor = 0.524 + 0.476 * clamp(CS / 2.5, 0, 1)   # CS>=2.5 -> 1.0, CS0 -> 0.524
    odFactor = 0.524 + 0.476 * clamp(OD / 4.0, 0, 1)   # OD>=4.0 -> 1.0, OD0 -> 0.524
    factor   = csFactor * odFactor                     # CS0 + OD0 -> ~0.275

Normal maps (CS>=2.5, OD>=4) and any Easy play come out at 1.0 (no change).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.mods import APIMod

# Knobs. Penalty starts ramping below these and bottoms out at the per-stat floor.
# Standard osu! only cares about the genuinely degenerate CS0/OD0 farm.
CS_THRESHOLD = 2.5
OD_THRESHOLD = 4.0

# Relax extends the ramp upward. With no tapping skill, lower CS (bigger targets,
# trivial to "washing machine" with a circular sweep) and lower OD (wider hit
# windows) are far easier to abuse, so a CS2.7/OD4.1 map sits comfortably above
# the standard thresholds yet still farms hard. Relax therefore penalises a wider
# band. Same floor as standard so the extreme CS0/OD0 case is unchanged.
CS_THRESHOLD_RX = 4.0
OD_THRESHOLD_RX = 6.0

STAT_FLOOR = 0.524     # standard per-stat min; CS0*OD0 ~= STAT_FLOOR**2 ~= 0.275
STAT_FLOOR_RX = 0.45   # relax bottoms out harder; CS0*OD0 ~= 0.45**2 ~= 0.20


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _stat_factor(value: float, threshold: float, floor: float = STAT_FLOOR) -> float:
    return floor + (1.0 - floor) * _clamp(value / threshold, 0.0, 1.0)


def _effective_cs_od(mods: "list[APIMod]", base_cs: float, base_od: float) -> tuple[float, float]:
    """Effective CS/OD after the mods that matter for this penalty.

    Easy is handled by the caller (excluded entirely). Hard Rock raises both
    (so the penalty naturally stops applying); Difficulty Adjust overrides to its
    absolute values (the CS0/OD0 case we are targeting). Rate mods don't change
    the CS/OD stat numbers, so they're ignored here.
    """
    cs, od = base_cs, base_od
    by_acr = {mod["acronym"]: mod for mod in mods}

    if "HR" in by_acr:
        cs = min(10.0, cs * 1.3)
        od = min(10.0, od * 1.4)

    if "DA" in by_acr:
        settings = by_acr["DA"].get("settings") or {}
        if settings.get("circle_size") is not None:
            cs = settings["circle_size"]
        if settings.get("overall_difficulty") is not None:
            od = settings["overall_difficulty"]

    return cs, od


def low_cs_od_pp_factor(
    base_ruleset_id: int,
    mods: "list[APIMod]",
    base_cs: float,
    base_od: float,
    is_relax: bool = False,
) -> float:
    """PP multiplier in [~0.275, 1.0] for low effective CS/OD; 1.0 when not applicable.

    ``base_ruleset_id`` is the base ruleset (0 for osu! / osu!relax / osu!autopilot).
    ``base_cs`` / ``base_od`` are the unmodded beatmap circle size / overall difficulty.
    ``is_relax`` widens the penalty band (see CS_THRESHOLD_RX / OD_THRESHOLD_RX).
    """
    if base_ruleset_id != 0:
        return 1.0
    if any(mod.get("acronym") == "EZ" for mod in mods):
        return 1.0

    cs, od = _effective_cs_od(mods, base_cs, base_od)
    cs_threshold = CS_THRESHOLD_RX if is_relax else CS_THRESHOLD
    od_threshold = OD_THRESHOLD_RX if is_relax else OD_THRESHOLD
    floor = STAT_FLOOR_RX if is_relax else STAT_FLOOR
    return _stat_factor(cs, cs_threshold, floor) * _stat_factor(od, od_threshold, floor)
