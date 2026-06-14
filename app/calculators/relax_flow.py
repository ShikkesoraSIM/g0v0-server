"""Torii-original relax "washing machine" pp nerf.

On relax there is no tapping skill, so osu! pp collapses to aim + a little
accuracy. rosu still scores the aim of a map as if you had to precisely move and
stop on every object, but on relax you can hold a smooth circular cursor sweep
(the "washing machine") and let the auto-tap catch every object that falls under
the path. Maps built from wide, same-direction flow (squares, loops, spaced
streams with a steady rhythm) are trivial to wash, so their aim pp is wildly
overpaid on relax. Sharp back-and-forth (snap) aim cannot be washed and stays
honest.

This module reads the raw beatmap geometry and produces a washability score in
[0, 1]: high when the path is gentle, same-direction and rhythmically steady (easy
to wash), low when it reverses sharply or its rhythm is jumpy. The score feeds a
bounded aim-pp multiplier, applied to relax scores only.

Pure geometry, so it is independent of rate / CS / AR mods and can be cached per
beatmap. Easy plays and non-relax scores never reach here.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

# Tuning knobs (calibrated against the live relax farm leaderboard).
FLOW_DIRECTION_FLIP_PENALTY = 0.65  # turn that reverses rotation direction is harder to wash
RHYTHM_SIMILAR_LO = 0.8            # consecutive gaps within [LO, HI] ratio count as "steady"
RHYTHM_SIMILAR_HI = 1.25
RHYTHM_WEIGHT = 0.25               # how much steady rhythm modulates the angle score
VELOCITY_CAP = 5.0                 # osu!px per ms, clamp so spinners/teleports don't dominate

# Nerf shaping: only clearly washable maps get hit, ramping to MAX_NERF.
WASH_LO = 0.58                     # below this -> no nerf
WASH_HI = 0.90                     # at/above this -> full MAX_NERF
MAX_NERF = 0.38                    # strongest aim cut (38%) on a perfectly washable map

# Circle-size gate: washing only works with big targets. Small circles need real
# precision no matter how flowy the geometry, so the nerf fades out as effective
# CS climbs. (Effective = post DA/HR; Part 1 handles the low-CS side separately.)
CS_GATE_LO = 5.0                   # at/below: full geometric nerf
CS_GATE_HI = 7.0                   # at/above: no flow nerf (tiny circles, honest aim)


@dataclass
class _Obj:
    x: float
    y: float
    t: float


def _parse_hit_objects(beatmap_raw: str) -> list[_Obj]:
    """Pull circle / slider-head positions + times from a raw .osu file.

    Slider bodies and ticks are follow-aim on relax, so the head is the aim point
    we care about. Spinners have no aim position and are skipped.
    """
    objs: list[_Obj] = []
    in_section = False

    for line in beatmap_raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("["):
            in_section = line == "[HitObjects]"
            continue
        if not in_section:
            continue

        parts = line.split(",")
        if len(parts) < 4:
            continue
        try:
            x = float(parts[0])
            y = float(parts[1])
            t = float(parts[2])
            obj_type = int(parts[3])
        except ValueError:
            continue

        if obj_type & 8:  # spinner: no meaningful aim position
            continue

        objs.append(_Obj(x, y, t))

    return objs


def washability(beatmap_raw: str) -> float:
    """Geometry-only washability of a map in [0, 1] (higher = easier to wash)."""
    objs = _parse_hit_objects(beatmap_raw)
    if len(objs) < 4:
        return 0.0

    # Per-turn flow score, weighted by how much aim that turn actually demands.
    total_w = 0.0
    total_flow = 0.0
    prev_sign = 0
    gaps: list[float] = []

    for i in range(1, len(objs) - 1):
        a = objs[i - 1]
        b = objs[i]
        c = objs[i + 1]

        ax, ay = b.x - a.x, b.y - a.y
        bx, by = c.x - b.x, c.y - b.y
        la = math.hypot(ax, ay)
        lb = math.hypot(bx, by)
        dt_a = max(b.t - a.t, 1.0)
        dt_b = max(c.t - b.t, 1.0)
        gaps.append(b.t - a.t)

        if la < 1.0 or lb < 1.0:  # stacked / negligible movement, no aim demand
            prev_sign = 0
            continue

        # Turn angle: 0 = continue straight, pi = full reversal. Wide/gentle turns
        # are washable, reversals are not.
        dot = ax * bx + ay * by
        cross = ax * by - ay * bx
        phi = abs(math.atan2(cross, dot))
        flow = math.cos(phi / 2.0)  # 1.0 straight -> 0.0 reversed

        # A washing machine spins one way. Flipping rotation direction between
        # turns breaks the sweep, so penalise sign changes.
        sign = 1 if cross >= 0 else -1
        if prev_sign != 0 and sign != prev_sign:
            flow *= FLOW_DIRECTION_FLIP_PENALTY
        prev_sign = sign

        # Weight by aim demand (velocity through the turn) so big fast flow, which
        # is where the overpaid pp lives, dominates the average.
        vel = 0.5 * (la / dt_a + lb / dt_b)
        w = min(vel, VELOCITY_CAP)

        total_w += w
        total_flow += w * flow

    if total_w <= 0.0:
        return 0.0

    angle_score = total_flow / total_w

    # Rhythm steadiness: a metronomic gap pattern is easy to keep circling to;
    # bursts and pauses are not. Fraction of consecutive gaps that stay similar.
    steady = 0
    pairs = 0
    for j in range(1, len(gaps)):
        g0, g1 = gaps[j - 1], gaps[j]
        if g0 <= 0 or g1 <= 0:
            continue
        pairs += 1
        ratio = g1 / g0
        if RHYTHM_SIMILAR_LO <= ratio <= RHYTHM_SIMILAR_HI:
            steady += 1
    rhythm = (steady / pairs) if pairs else 0.0

    return angle_score * ((1.0 - RHYTHM_WEIGHT) + RHYTHM_WEIGHT * rhythm)


def _smoothstep(lo: float, hi: float, x: float) -> float:
    if x <= lo:
        return 0.0
    if x >= hi:
        return 1.0
    t = (x - lo) / (hi - lo)
    return t * t * (3.0 - 2.0 * t)


def relax_flow_aim_nerf(beatmap_raw: str, aim_fraction: float, effective_cs: float = 4.0) -> tuple[float, float]:
    """PP multiplier in [1 - MAX_NERF, 1.0] for a relax score on this map.

    ``aim_fraction`` is aim_pp / (aim + speed + acc + fl), so accuracy-carried
    scores keep more of their pp than pure aim farms. ``effective_cs`` is the
    post-mod circle size; the nerf fades out on small circles (high CS) where the
    sweep can't reach. Returns (multiplier, wash) where ``wash`` is the raw
    washability for logging / calibration.
    """
    wash = washability(beatmap_raw)
    cs_gate = 1.0 - _smoothstep(CS_GATE_LO, CS_GATE_HI, effective_cs)
    nerf = (
        MAX_NERF
        * _smoothstep(WASH_LO, WASH_HI, wash)
        * max(0.0, min(1.0, aim_fraction))
        * cs_gate
    )
    return 1.0 - nerf, wash


# --- Capa A: washable JUMP sections (for replay confirmation) ------------------

JUMP_SPACING_MIN = 90.0   # osu!px; below this is a stream/stack, not a jump
SECTION_FLOW_MIN = 0.45   # per-turn flow floor to count as washable (wide-ish angle)
MIN_SECTION_OBJS = 5      # a section needs at least this many objects to matter


@dataclass
class JumpSection:
    """A run of consecutive washable jumps: where, how much it matters, and the
    objects in it (so the replay layer can check if the cursor actually swept it).
    """
    t_start: float
    t_end: float
    weight: float                          # washable aim demand (velocity * flow)
    objects: list[tuple[float, float, float]]  # (time, x, y) of each object in the run


def jump_sections(beatmap_raw: str) -> list[JumpSection]:
    """Segment the map into washable jump runs.

    A jump is a movement of at least JUMP_SPACING_MIN px; a *washable* jump also
    turns gently and keeps its rotation direction (so a smooth sweep covers it).
    Sharp reversals and stream/slider density break a run. Pure geometry, so it is
    rate/CS independent and cacheable per beatmap.
    """
    objs = _parse_hit_objects(beatmap_raw)
    if len(objs) < MIN_SECTION_OBJS:
        return []

    sections: list[JumpSection] = []
    run: list[tuple[int, float]] = []  # (object index, per-turn weight)
    prev_sign = 0

    def flush():
        if len(run) >= MIN_SECTION_OBJS:
            idxs = [i for i, _ in run]
            pts = [(objs[i].t, objs[i].x, objs[i].y) for i in idxs]
            sections.append(JumpSection(
                t_start=pts[0][0],
                t_end=pts[-1][0],
                weight=sum(w for _, w in run),
                objects=pts,
            ))

    for i in range(1, len(objs) - 1):
        a, b, c = objs[i - 1], objs[i], objs[i + 1]
        ax, ay = b.x - a.x, b.y - a.y
        bx, by = c.x - b.x, c.y - b.y
        la = math.hypot(ax, ay)
        lb = math.hypot(bx, by)

        is_jump = la >= JUMP_SPACING_MIN and lb >= JUMP_SPACING_MIN
        if not is_jump:
            flush()
            run = []
            prev_sign = 0
            continue

        dot = ax * bx + ay * by
        cross = ax * by - ay * bx
        phi = abs(math.atan2(cross, dot))
        flow = math.cos(phi / 2.0)
        sign = 1 if cross >= 0 else -1

        flips = prev_sign != 0 and sign != prev_sign
        washable = flow >= SECTION_FLOW_MIN and not flips
        prev_sign = sign

        if not washable:
            flush()
            run = []
            continue

        dt_a = max(b.t - a.t, 1.0)
        dt_b = max(c.t - b.t, 1.0)
        vel = min(0.5 * (la / dt_a + lb / dt_b), VELOCITY_CAP)
        run.append((i, vel * flow))

    flush()
    return sections


# --- Capa B: replay confirmation (did THIS player actually wash it?) -----------

HIT_WINDOW_MS = 100.0   # search this far around each object time for closest cursor approach
OFFSET_LO = 0.35        # closest approach (in radii) below this = aimed (centre)
OFFSET_HI = 0.85        # above this = washed (rim graze / outside)
SPEED_LO = 0.30         # px/ms at closest approach below this = dwell (aimed)
SPEED_HI = 1.20         # above this = fast pass-through (washed)


def wash_confidence(
    sections: list[JumpSection],
    frames: list[tuple[float, float, float]],
    effective_cs: float,
    touch_device: bool = False,
) -> float:
    """How much the player actually washed the washable jump sections, in [0, 1].

    For each object in a section we find the cursor's closest approach within the
    hit window and how fast it was moving there. Washing grazes the rim at speed
    (high); aiming lands near centre and slows (low). Aggregated per section and
    weighted by section importance. Returns 0 when there is nothing washable or no
    replay, so honest aimers and missing replays are never nerfed.
    """
    if not sections or len(frames) < 2:
        return 0.0

    cr = max(54.4 - 4.48 * effective_cs, 4.0)
    ts = [f[0] for f in frames]

    total_w = 0.0
    total_c = 0.0
    for sec in sections:
        signals: list[float] = []
        for (ot, ox, oy) in sec.objects:
            lo = bisect.bisect_left(ts, ot - HIT_WINDOW_MS)
            hi = bisect.bisect_right(ts, ot + HIT_WINDOW_MS)
            best_d = None
            best_k = None
            for k in range(max(0, lo - 1), min(len(frames), hi + 1)):
                d = math.hypot(frames[k][1] - ox, frames[k][2] - oy)
                if best_d is None or d < best_d:
                    best_d = d
                    best_k = k
            if best_d is None or best_k is None:
                continue

            min_offset = best_d / cr
            vel = 0.0
            if 0 < best_k < len(frames):
                dt = max(frames[best_k][0] - frames[best_k - 1][0], 1.0)
                dd = math.hypot(
                    frames[best_k][1] - frames[best_k - 1][1],
                    frames[best_k][2] - frames[best_k - 1][2],
                )
                vel = dd / dt

            offset_sig = _smoothstep(OFFSET_LO, OFFSET_HI, min_offset)
            # Touch replays have unreliable frame velocity, so lean on offset only.
            speed_sig = 1.0 if touch_device else _smoothstep(SPEED_LO, SPEED_HI, vel)
            signals.append(offset_sig * (0.7 + 0.3 * speed_sig))

        if signals:
            total_w += sec.weight
            total_c += sec.weight * (sum(signals) / len(signals))

    return (total_c / total_w) if total_w > 0 else 0.0
