"""Minimal osu! replay (.osr) cursor reader for the relax washing-machine check.

We only need the cursor path (time, x, y) to see whether a relax play swept the
washable jump sections (Capa A) instead of aiming them. Keys / judgements are
irrelevant on relax (the auto-tap handles them), so this is deliberately tiny: it
decodes the LZMA replay block into cursor frames and nothing else.

Frame times are in beatmap (gameplay-clock) ms, the same base as hit-object times,
so rate mods (DT/HT) do not need correcting for alignment.
"""

from __future__ import annotations

import lzma
import struct
from dataclasses import dataclass

# Legacy mod bits we care about (the .osr header stores the legacy bitmask).
_MOD_HARD_ROCK = 1 << 4
_MOD_TOUCH_DEVICE = 1 << 22


@dataclass
class ReplayCursor:
    frames: list[tuple[float, float, float]]  # (time_ms, x, y)
    mods: int
    touch_device: bool


def _read_uleb(b: bytes, i: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        x = b[i]
        i += 1
        result |= (x & 0x7F) << shift
        if not (x & 0x80):
            break
        shift += 7
    return result, i


def _read_string(b: bytes, i: int) -> tuple[str, int]:
    if b[i] == 0x00:
        return "", i + 1
    # 0x0b marker, then ULEB length, then UTF-8 bytes
    i += 1
    length, i = _read_uleb(b, i)
    return b[i:i + length].decode("utf-8", "ignore"), i + length


def parse_replay_cursor(data: bytes) -> ReplayCursor | None:
    """Decode an .osr byte blob into cursor frames, or None if unreadable."""
    try:
        i = 0
        i += 1  # game mode byte
        i += 4  # version int
        _, i = _read_string(data, i)  # beatmap md5
        _, i = _read_string(data, i)  # player name
        _, i = _read_string(data, i)  # replay md5
        i += 12  # 6x short hit counts
        i += 4   # score int
        i += 2   # max combo short
        i += 1   # perfect byte
        mods = struct.unpack_from("<i", data, i)[0]
        i += 4
        _, i = _read_string(data, i)  # life bar graph
        i += 8   # timestamp long
        replay_len = struct.unpack_from("<i", data, i)[0]
        i += 4
        raw = data[i:i + replay_len]

        decoded = lzma.decompress(raw, format=lzma.FORMAT_ALONE).decode("ascii", "ignore")
        frames: list[tuple[float, float, float]] = []
        t = 0.0
        for part in decoded.split(","):
            if not part:
                continue
            f = part.split("|")
            if len(f) != 4 or f[0] == "-12345":
                continue
            try:
                dt = float(f[0])
                x = float(f[1])
                y = float(f[2])
            except ValueError:
                continue
            t += dt
            frames.append((t, x, y))

        if len(frames) < 2:
            return None
        return ReplayCursor(frames=frames, mods=mods, touch_device=bool(mods & _MOD_TOUCH_DEVICE))
    except Exception:
        return None
