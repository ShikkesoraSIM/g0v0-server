import math
from typing import TYPE_CHECKING, Any

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
    # author-port @ c2f5ac3
    if acc > 0.80:
        return 4.5 * (acc - 0.8) / math.pow(100 * (1 - acc) + math.pow(0.9, 20), 0.05)
    return 0.0


def _variety_multiplier(variety: float) -> float:
    # author-port @ c2f5ac3
    floor = 0.945
    cap = 1.055
    span = cap - floor
    v0 = 3.25
    k = 3.0
    return floor + span / (1.0 + math.exp(-k * (variety - v0)))


def _acc_multiplier(acc: float, acc_scalar: float) -> float:
    # author-port @ c2f5ac3
    sigmoid_scaler = 0.87 + 0.26 / (1.0 + math.exp(-20.0 * (acc_scalar - 1.0)))
    pow_acc = math.pow(acc, 20)
    return sigmoid_scaler * (2 * pow_acc - 1) + 2 - 2 * pow_acc


def _length_multiplier(total_notes: float, star_rating: float) -> float:
    # author-port @ c2f5ac3
    safe_total_notes = max(total_notes, 1.0)
    return 1.1 / (1.0 + math.sqrt(star_rating / (2.0 * safe_total_notes)))


def calculate_sunny_mania_pp(star_rating: float, score: "Score", diff_attrs: Any | None = None) -> float:
    """
    Server-side Sunny Rework (WIP) mania pp formula.

    Ported from vernonlim/osu branch author-port @ c2f5ac34625264846a4379e313b96fc4debd06ac.
    This is backend-only and keeps fallback defaults if some custom difficulty
    attributes are missing from the active calculator build.
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

    difficulty_value = 9.8 * math.pow(max(star_rating - 0.15, 0.05), 2.2) * proportion

    multiplier = 1.0
    if _has_mod(score.mods, "NF"):
        multiplier *= 0.75
    if _has_mod(score.mods, "EZ"):
        multiplier *= 0.90

    # If these attributes are not available in the current rosu build, keep sane
    # defaults so Sunny can still run without crashing.
    variety = float(getattr(diff_attrs, "variety", 3.25) or 3.25)
    acc_scalar = float(getattr(diff_attrs, "acc_scalar", 1.0) or 1.0)
    total_notes_raw = getattr(diff_attrs, "total_notes", None)
    total_notes = float(total_notes_raw) if total_notes_raw is not None else float(total_hits)

    total_value = (
        difficulty_value
        * multiplier
        * _variety_multiplier(variety)
        * _acc_multiplier(score_accuracy, acc_scalar)
        * _length_multiplier(total_notes, star_rating)
    )
    return max(0.0, total_value)
