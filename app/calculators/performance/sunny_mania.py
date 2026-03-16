import math
from typing import TYPE_CHECKING

from app.models.mods import APIMod

if TYPE_CHECKING:
    from app.database.score import Score


def _has_mod(mods: list[APIMod], acronym: str) -> bool:
    target = acronym.upper()
    return any((mod.get("acronym") or "").upper() == target for mod in mods)


def _mania_custom_accuracy(score: "Score") -> float:
    # Mapping used by osu!mania laser score fields:
    # Perfect -> ngeki, Great -> n300, Good -> nkatu, Ok -> n100, Meh -> n50, Miss -> nmiss
    count_perfect = max(score.ngeki, 0)
    count_great = max(score.n300, 0)
    count_good = max(score.nkatu, 0)
    count_ok = max(score.n100, 0)
    count_meh = max(score.n50, 0)
    count_miss = max(score.nmiss, 0)

    total_hits = count_perfect + count_great + count_good + count_ok + count_meh + count_miss
    if total_hits <= 0:
        return 0.0

    weighted = (
        count_perfect * 305
        + count_great * 300
        + count_good * 200
        + count_ok * 100
        + count_meh * 50
    )
    return weighted / (total_hits * 305.0)


def _performance_proportion(acc: float) -> float:
    if acc > 0.99:
        return (1.00 - 0.85) * (acc - 0.99) / 0.01 + 0.85
    if acc > 0.96:
        return (0.85 - 0.64) * (acc - 0.96) / 0.03 + 0.64
    if acc > 0.80:
        return (0.64 - 0.00) * (acc - 0.80) / 0.16
    return 0.0


def calculate_sunny_mania_pp(star_rating: float, score: "Score") -> float:
    """
    Server-side Sunny Rework (WIP) mania pp formula.

    Notes:
    - Uses Sunny's public pp curve and accuracy weighting.
    - Expects star_rating from the active difficulty calculator.
    - This intentionally keeps implementation backend-only and toggleable.
    """
    count_perfect = max(score.ngeki, 0)
    count_great = max(score.n300, 0)
    count_good = max(score.nkatu, 0)
    count_ok = max(score.n100, 0)
    count_meh = max(score.n50, 0)
    count_miss = max(score.nmiss, 0)
    total_hits = count_perfect + count_great + count_good + count_ok + count_meh + count_miss
    if total_hits <= 0:
        return 0.0

    score_accuracy = _mania_custom_accuracy(score)
    proportion = _performance_proportion(score_accuracy)

    # Difficulty part
    difficulty_value = (
        math.pow(max(star_rating - 0.15, 0.05), 2.2)
        * 1.1
        / (1.0 + (1.5 / math.sqrt(total_hits)))
        * proportion
        * 1.18
    )

    multiplier = 8.0
    if _has_mod(score.mods, "NF"):
        multiplier *= 0.75
    if _has_mod(score.mods, "EZ"):
        multiplier *= 0.5

    return max(0.0, difficulty_value * multiplier)
