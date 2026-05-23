"""Stand-alone .osr replay parser used for ad-hoc manual score submissions.

Usage:
    python tools/parse_osr.py <path-to-.osr>

Prints every field the score-submission path needs (player name, beatmap MD5,
counts, mods, online_score_id, etc.) so an admin can copy the numbers into
the manual submission flow without having to install an external osu! replay
library on the server box.
"""
import datetime
import struct
import sys
from pathlib import Path


def read_uleb128(f):
    result = 0
    shift = 0
    while True:
        b = f.read(1)[0]
        result |= (b & 0x7f) << shift
        if (b & 0x80) == 0:
            return result
        shift += 7


def read_str(f):
    marker = f.read(1)
    if not marker or marker[0] != 0x0b:
        return ""
    length = read_uleb128(f)
    return f.read(length).decode("utf-8", errors="replace")


def parse(path: Path) -> dict:
    with open(path, "rb") as f:
        mode = f.read(1)[0]
        version = struct.unpack("<i", f.read(4))[0]
        beatmap_md5 = read_str(f)
        player = read_str(f)
        replay_md5 = read_str(f)
        n300, n100, n50, geki, katu, miss = struct.unpack("<HHHHHH", f.read(12))
        total_score = struct.unpack("<i", f.read(4))[0]
        max_combo = struct.unpack("<H", f.read(2))[0]
        perfect = bool(f.read(1)[0])
        mods = struct.unpack("<i", f.read(4))[0]
        life_bar = read_str(f)
        timestamp_ticks = struct.unpack("<q", f.read(8))[0]
        replay_len = struct.unpack("<i", f.read(4))[0]
        replay_data_offset = f.tell()
        f.seek(replay_len, 1)
        online_score_id = struct.unpack("<q", f.read(8))[0]

    # Windows ticks -> UTC datetime (epoch is 0001-01-01).
    epoch = datetime.datetime(1, 1, 1)
    played_at = epoch + datetime.timedelta(microseconds=timestamp_ticks // 10)

    mode_names = ["osu", "taiko", "catch", "mania"]
    return dict(
        mode=mode,
        mode_name=mode_names[mode] if 0 <= mode <= 3 else f"unknown({mode})",
        version=version,
        beatmap_md5=beatmap_md5,
        player=player,
        replay_md5=replay_md5,
        n300=n300, n100=n100, n50=n50, geki=geki, katu=katu, miss=miss,
        total_score=total_score,
        max_combo=max_combo,
        perfect=perfect,
        mods=mods,
        life_bar=life_bar,
        timestamp_ticks=timestamp_ticks,
        played_at_utc=played_at,
        replay_len=replay_len,
        replay_data_offset=replay_data_offset,
        online_score_id=online_score_id,
        file_size=path.stat().st_size,
    )


# Mod bitmask -> short name. Standard osu! stable mod bits.
_MODS = {
    1 << 0: "NF", 1 << 1: "EZ", 1 << 2: "TD", 1 << 3: "HD",
    1 << 4: "HR", 1 << 5: "SD", 1 << 6: "DT", 1 << 7: "RX",
    1 << 8: "HT", 1 << 9: "NC", 1 << 10: "FL", 1 << 11: "AT",
    1 << 12: "SO", 1 << 13: "AP", 1 << 14: "PF",
    1 << 15: "4K", 1 << 16: "5K", 1 << 17: "6K", 1 << 18: "7K",
    1 << 19: "8K", 1 << 20: "FI", 1 << 21: "RD", 1 << 22: "CN",
    1 << 23: "TP", 1 << 24: "9K", 1 << 25: "CO", 1 << 26: "1K",
    1 << 27: "3K", 1 << 28: "2K", 1 << 29: "V2", 1 << 30: "MR",
}


def mods_to_str(mods: int) -> str:
    active = [name for bit, name in _MODS.items() if mods & bit]
    return "+".join(active) if active else "NoMod"


def accuracy(mode: int, n300: int, n100: int, n50: int, miss: int,
             geki: int, katu: int) -> float:
    """Standard osu! per-mode accuracy formulas."""
    if mode == 0:
        total = 300 * (n300 + n100 + n50 + miss)
        if not total:
            return 0.0
        return (50 * n50 + 100 * n100 + 300 * n300) / total * 100
    if mode == 1:
        total = 2 * (n300 + n100 + miss)
        if not total:
            return 0.0
        return (n100 + 2 * n300) / total * 100
    if mode == 2:
        total = n300 + n100 + n50 + miss + katu
        if not total:
            return 0.0
        return (n300 + n100 + n50) / total * 100
    if mode == 3:
        total = 300 * (n300 + n100 + n50 + miss + geki + katu)
        if not total:
            return 0.0
        return (50 * n50 + 100 * n100 + 200 * katu + 300 * (n300 + geki)) / total * 100
    return 0.0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: parse_osr.py <path-to-.osr>", file=sys.stderr)
        sys.exit(2)

    path = Path(sys.argv[1])
    d = parse(path)
    acc = accuracy(d["mode"], d["n300"], d["n100"], d["n50"], d["miss"], d["geki"], d["katu"])

    print(f"file size:        {d['file_size']:,} bytes")
    print(f"mode:             {d['mode_name']} ({d['mode']})")
    print(f"game version:     {d['version']}")
    print(f"beatmap MD5:      {d['beatmap_md5']}")
    print(f"player name:      {d['player']!r}")
    print(f"replay MD5:       {d['replay_md5']}")
    print(f"counts:           300={d['n300']}  100={d['n100']}  50={d['n50']}  "
          f"geki={d['geki']}  katu={d['katu']}  miss={d['miss']}")
    print(f"total score:      {d['total_score']:,}")
    print(f"max combo:        {d['max_combo']}")
    print(f"perfect/FC:       {d['perfect']}")
    print(f"mods (bitmask):   0x{d['mods']:08x} ({d['mods']})")
    print(f"mods decoded:     {mods_to_str(d['mods'])}")
    print(f"accuracy:         {acc:.4f}%")
    print(f"played at (UTC):  {d['played_at_utc'].isoformat()}")
    print(f"replay bytes:     {d['replay_len']:,} (LZMA, offset {d['replay_data_offset']})")
    print(f"online_score_id:  {d['online_score_id']}  "
          f"({'never submitted' if d['online_score_id'] == 0 else 'previously submitted'})")
