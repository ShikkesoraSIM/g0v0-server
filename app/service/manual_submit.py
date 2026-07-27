"""Manual score submission from a raw .osr replay.

Powers the admin "manual submit" panel: an admin uploads a player's .osr
(e.g. the player's score never reached the server because the submission
window was missed, a transient lookup failure, etc.) and we honour the
play after the fact by landing it exactly as a normal submit would.

This is the canonical, server-side path. The standalone CLI
(`tools/submit_replay.py`) predates it and carries its own copy of the
parser for offline / no-import-path use; the .osr binary layout is a
frozen historical format (osu! stable replay), so the two parsers can't
drift in any meaningful way. New behaviour should land HERE.

Two entry points:
  * preview(...)  -> resolve + describe, NO writes. Backs the dry-run UI.
  * commit(...)   -> insert the score via the same process_score(...) the
                     live POST handler uses, then refresh the user's stats.

Note on pp: like the CLI, commit() submits with ranked=False, so pp is
NOT granted inline even on ranked maps. The play still lands in history;
if pp should count, run the existing per-user PP recalc afterwards (the
admin maintenance page exposes that right next to this panel).
"""
from __future__ import annotations

import datetime
import io
import json
import lzma
import struct

from sqlalchemy import func, text
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database.beatmap import Beatmap
from app.database.score import process_score
from app.database.score_token import ScoreToken
from app.database.user import User
from app.log import log
from app.models.score import GameMode, HitResult, Rank, SoloScoreSubmissionInfo

logger = log("ManualSubmit")


# ──────────────────────────────────────────────────────────────────────────
# .osr parsing (bytes in, dict out). Mirrors tools/parse_osr.py — the format
# is frozen, so this is a straight port operating on an in-memory buffer
# instead of a file path.
# ──────────────────────────────────────────────────────────────────────────


class ReplayParseError(ValueError):
    """Raised when the uploaded bytes aren't a well-formed .osr."""


def _read_uleb128(f: io.BytesIO) -> int:
    result = 0
    shift = 0
    while True:
        chunk = f.read(1)
        if not chunk:
            raise ReplayParseError("truncated ULEB128 while reading replay header")
        b = chunk[0]
        result |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return result
        shift += 7


def _read_str(f: io.BytesIO) -> str:
    marker = f.read(1)
    if not marker or marker[0] != 0x0B:
        return ""
    length = _read_uleb128(f)
    return f.read(length).decode("utf-8", errors="replace")


_MODE_NAMES = ["osu", "taiko", "catch", "mania"]

# Replays from this version on carry a LegacyReplaySoloScoreInfo blob appended
# after the legacy body (see LegacyScoreEncoder in the client).
_LAZER_BLOCK_MIN_VERSION = 30000001


def _read_lazer_block(f: io.BytesIO, version: int) -> dict | None:
    """The lazer score info appended to the end of the .osr, if there is one.

    This block is the ONLY place a replay records what the mods really were.
    The legacy header has a single DT bit and no way to say "1.15x", so a rate
    changed play read from the header alone comes back as full 1.5x Double Time
    and scores accordingly. Reading this is the difference between submitting
    the play someone actually made and inventing a much harder one.
    """
    if version < _LAZER_BLOCK_MIN_VERSION:
        return None

    raw = f.read(4)

    # Old or hand-made files can simply stop here, which is not an error.
    if len(raw) < 4:
        return None

    length = struct.unpack("<i", raw)[0]

    if length <= 0:
        return None

    blob = f.read(length)

    if len(blob) < length:
        return None

    try:
        # LZMA-alone, the same framing the client writes.
        decoded = lzma.decompress(blob, format=lzma.FORMAT_ALONE).decode("ascii")
        parsed = json.loads(decoded)
    except (lzma.LZMAError, ValueError, UnicodeDecodeError) as exc:
        # A replay we cannot read here still submits from the legacy header, so
        # this is worth a note rather than a failure.
        logger.warning("manual submit: could not read the lazer block in this replay (%s)", exc)
        return None

    return parsed if isinstance(parsed, dict) else None


def parse_replay(data: bytes) -> dict:
    """Decode a .osr byte blob into the fields the submission path needs."""
    try:
        f = io.BytesIO(data)
        mode = f.read(1)[0]
        version = struct.unpack("<i", f.read(4))[0]
        beatmap_md5 = _read_str(f)
        player = _read_str(f)
        replay_md5 = _read_str(f)
        n300, n100, n50, geki, katu, miss = struct.unpack("<HHHHHH", f.read(12))
        total_score = struct.unpack("<i", f.read(4))[0]
        max_combo = struct.unpack("<H", f.read(2))[0]
        perfect = bool(f.read(1)[0])
        mods = struct.unpack("<i", f.read(4))[0]
        _life_bar = _read_str(f)
        timestamp_ticks = struct.unpack("<q", f.read(8))[0]
        replay_len = struct.unpack("<i", f.read(4))[0]
        f.seek(replay_len, 1)
        online_score_id = struct.unpack("<q", f.read(8))[0]
        lazer_info = _read_lazer_block(f, version)
    except (IndexError, struct.error) as exc:
        raise ReplayParseError(f"not a valid .osr file: {exc}") from exc

    if not (0 <= mode <= 3):
        raise ReplayParseError(f"unknown ruleset byte in replay: {mode}")
    if not beatmap_md5:
        raise ReplayParseError("replay has no beatmap checksum")

    # Windows ticks (epoch 0001-01-01, 100ns units) -> UTC datetime.
    played_at = datetime.datetime(1, 1, 1) + datetime.timedelta(microseconds=timestamp_ticks // 10)

    return dict(
        mode=mode,
        mode_name=_MODE_NAMES[mode],
        version=version,
        beatmap_md5=beatmap_md5,
        player=player,
        replay_md5=replay_md5,
        n300=n300, n100=n100, n50=n50, geki=geki, katu=katu, miss=miss,
        total_score=total_score,
        max_combo=max_combo,
        perfect=perfect,
        mods=mods,
        timestamp_ticks=timestamp_ticks,
        played_at_utc=played_at,
        replay_len=replay_len,
        online_score_id=online_score_id,
        lazer_info=lazer_info,
    )


# ──────────────────────────────────────────────────────────────────────────
# Derived values
# ──────────────────────────────────────────────────────────────────────────

# osr mod-bit -> lazer acronym. Bits without a current lazer mod (legacy
# keymode flags etc.) are dropped rather than guessed; worst case the score
# lands as NoMod, which beats lying about which mods were active.
_OSR_MOD_TO_LAZER = {
    1 << 0: "NF", 1 << 1: "EZ", 1 << 3: "HD", 1 << 4: "HR",
    1 << 5: "SD", 1 << 6: "DT", 1 << 7: "RX", 1 << 8: "HT",
    1 << 9: "NC", 1 << 10: "FL", 1 << 12: "SO", 1 << 13: "AP",
    1 << 14: "PF",
}

_MODS_DISPLAY = {
    1 << 0: "NF", 1 << 1: "EZ", 1 << 2: "TD", 1 << 3: "HD",
    1 << 4: "HR", 1 << 5: "SD", 1 << 6: "DT", 1 << 7: "RX",
    1 << 8: "HT", 1 << 9: "NC", 1 << 10: "FL", 1 << 11: "AT",
    1 << 12: "SO", 1 << 13: "AP", 1 << 14: "PF", 1 << 29: "V2",
}


def mods_to_acronyms(mods_bitmask: int, lazer_info: dict | None = None) -> list[str]:
    """Human-readable mod list for the preview panel.

    Shows the same thing the commit will actually submit, settings included, so
    an admin approving "DT" is never approving a different play than the one on
    screen. A rate is spelled out because it is the whole point: "DT 1.15x" and
    "DT" are wildly different scores.
    """
    labels = []

    for mod in osr_mods_to_lazer(mods_bitmask, lazer_info):
        rate = (mod.get("settings") or {}).get("speed_change")
        labels.append(f"{mod['acronym']} {rate}x" if rate is not None else mod["acronym"])

    return labels


def osr_mods_to_lazer(mods_bitmask: int, lazer_info: dict | None = None) -> list[dict]:
    """The mods the play was actually set with, in lazer's APIMod shape.

    When the replay carries a lazer block its mod list wins outright: it is the
    real thing, settings and all. The bitmask below is a lossy fallback for
    genuinely legacy replays, and it CANNOT express a custom rate. Deriving
    "DT" from it for a lazer replay means submitting a 1.15x play as a 1.5x one,
    which is exactly how a 543pp score once landed as 1967pp.
    """
    if lazer_info is not None:
        mods = lazer_info.get("mods")

        if isinstance(mods, list):
            cleaned = [m for m in mods if isinstance(m, dict) and isinstance(m.get("acronym"), str)]

            # An empty list is a real answer here: it means the play was NoMod.
            if len(cleaned) == len(mods):
                return [{"acronym": m["acronym"], "settings": m.get("settings") or {}} for m in cleaned]

        logger.warning("manual submit: the lazer block had an unreadable mod list, falling back to the legacy bits")

    acronyms = [acr for bit, acr in _OSR_MOD_TO_LAZER.items() if mods_bitmask & bit]
    # NC implies DT and PF implies SD; lazer's validator rejects the pair.
    if "NC" in acronyms and "DT" in acronyms:
        acronyms.remove("DT")
    if "PF" in acronyms and "SD" in acronyms:
        acronyms.remove("SD")
    return [{"acronym": a, "settings": {}} for a in acronyms]


def accuracy(mode: int, n300: int, n100: int, n50: int, miss: int, geki: int, katu: int) -> float:
    """Standard per-mode accuracy formulas (matches tools/parse_osr.py)."""
    if mode == 0:
        total = 300 * (n300 + n100 + n50 + miss)
        return (50 * n50 + 100 * n100 + 300 * n300) / total * 100 if total else 0.0
    if mode == 1:
        total = 2 * (n300 + n100 + miss)
        return (n100 + 2 * n300) / total * 100 if total else 0.0
    if mode == 2:
        total = n300 + n100 + n50 + miss + katu
        return (n300 + n100 + n50) / total * 100 if total else 0.0
    if mode == 3:
        total = 300 * (n300 + n100 + n50 + miss + geki + katu)
        return (50 * n50 + 100 * n100 + 200 * katu + 300 * (n300 + geki)) / total * 100 if total else 0.0
    return 0.0


def compute_rank_osu(accuracy_pct: float, n50: int, total_hits: int, miss: int, mods_bitmask: int) -> Rank:
    """osu! standard rank thresholds (HD/FL bump S/X to their silver variants)."""
    silver = bool(mods_bitmask & ((1 << 3) | (1 << 10)))
    if accuracy_pct >= 100.0:
        return Rank.XH if silver else Rank.X
    if accuracy_pct >= 90.0 and miss == 0 and (total_hits == 0 or n50 / total_hits <= 0.01):
        return Rank.SH if silver else Rank.S
    if accuracy_pct >= 80.0:
        return Rank.A
    if accuracy_pct >= 70.0:
        return Rank.B
    if accuracy_pct >= 60.0:
        return Rank.C
    return Rank.D


# ──────────────────────────────────────────────────────────────────────────
# Resolution (no raising — callers decide how to surface a miss)
# ──────────────────────────────────────────────────────────────────────────


async def resolve_user(session: AsyncSession, player_name: str, override_id: int | None) -> User | None:
    if override_id is not None:
        return await session.get(User, override_id)

    # Current username (hits the unique index).
    user = (await session.exec(select(User).where(User.username == player_name))).first()
    if user:
        return user

    # Username-history match — the player renamed since the replay was captured.
    if player_name:
        result = await session.exec(
            select(User).where(
                func.json_contains(User.previous_usernames, text(f"JSON_QUOTE('{player_name}')")) == 1
            )
        )
        user = result.first()
        if user:
            return user
    return None


async def resolve_beatmap(session: AsyncSession, beatmap_md5: str) -> Beatmap | None:
    return (await session.exec(select(Beatmap).where(Beatmap.checksum == beatmap_md5))).first()


def _build_submission_info(replay: dict, acc_pct: float) -> SoloScoreSubmissionInfo:
    statistics: dict = {
        HitResult.GREAT: replay["n300"],
        HitResult.OK: replay["n100"],
        HitResult.MEH: replay["n50"],
        HitResult.MISS: replay["miss"],
    }
    if replay["geki"]:
        statistics[HitResult.PERFECT] = replay["geki"]
    if replay["katu"]:
        statistics[HitResult.GOOD] = replay["katu"]

    total_hits = replay["n300"] + replay["n100"] + replay["n50"] + replay["miss"]
    maximum_statistics: dict = {HitResult.GREAT: total_hits}
    rank = compute_rank_osu(acc_pct, replay["n50"], total_hits, replay["miss"], replay["mods"])

    return SoloScoreSubmissionInfo(
        rank=rank,
        total_score=replay["total_score"],
        total_score_without_mods=replay["total_score"],  # NoMod-multiplier path; safe approximation
        accuracy=acc_pct / 100.0,
        pp=0,  # process_score recomputes if granted; manual submits stay at 0
        max_combo=replay["max_combo"],
        ruleset_id=replay["mode"],
        passed=True,  # a final score in hand means they finished
        mods=osr_mods_to_lazer(replay["mods"], replay.get("lazer_info")),
        statistics=statistics,
        maximum_statistics=maximum_statistics,
    )


# ──────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────


async def preview(session: AsyncSession, data: bytes, override_user_id: int | None) -> dict:
    """Parse + resolve + describe. No DB writes. Drives the dry-run UI."""
    replay = parse_replay(data)
    acc_pct = accuracy(
        replay["mode"], replay["n300"], replay["n100"], replay["n50"],
        replay["miss"], replay["geki"], replay["katu"],
    )

    user = await resolve_user(session, replay["player"], override_user_id)
    beatmap = await resolve_beatmap(session, replay["beatmap_md5"])

    warnings: list[str] = []
    if user is None:
        warnings.append(
            f"Could not resolve player '{replay['player']}'. Provide a user id to submit."
            if override_user_id is None
            else f"User id {override_user_id} does not exist."
        )
    if beatmap is None:
        warnings.append(
            "This beatmap has never been seen by the server, so the score can't be attached. "
            "The map's leaderboard has to exist first."
        )
    if replay["online_score_id"] and replay["online_score_id"] != 0:
        warnings.append(
            "This replay carries an online score id (it was submitted somewhere before) — "
            "submitting may create a duplicate of an existing score."
        )
    warnings.append(
        "Manual submit does not grant pp inline. The play lands in history; "
        "run a per-user PP recalc afterwards if the map is ranked and pp should count."
    )

    return {
        "can_submit": user is not None and beatmap is not None,
        "player_name": replay["player"],
        "mode": replay["mode_name"],
        "total_score": replay["total_score"],
        "max_combo": replay["max_combo"],
        "accuracy": round(acc_pct, 2),
        "mods": mods_to_acronyms(replay["mods"], replay.get("lazer_info")),
        "played_at": replay["played_at_utc"].isoformat(),
        "counts": {
            "great": replay["n300"], "ok": replay["n100"], "meh": replay["n50"],
            "miss": replay["miss"], "geki": replay["geki"], "katu": replay["katu"],
        },
        "resolved_user": (
            {"id": user.id, "username": user.username} if user is not None else None
        ),
        "resolved_beatmap": (
            {
                "id": beatmap.id,
                "version": beatmap.version,
                "status": beatmap.beatmap_status.value,
            }
            if beatmap is not None else None
        ),
        "warnings": warnings,
    }


async def commit(
    session: AsyncSession,
    data: bytes,
    override_user_id: int | None,
    redis,
    fetcher,
    actor_label: str = "manual-submit",
) -> dict:
    """Insert the score via the live process_score path, then refresh stats.

    Raises ValueError (-> 400) for a malformed replay or an unresolvable
    user/beatmap; the caller maps those to HTTP errors.
    """
    replay = parse_replay(data)
    acc_pct = accuracy(
        replay["mode"], replay["n300"], replay["n100"], replay["n50"],
        replay["miss"], replay["geki"], replay["katu"],
    )

    user = await resolve_user(session, replay["player"], override_user_id)
    if user is None:
        raise ValueError(
            f"Could not resolve player '{replay['player']}'."
            if override_user_id is None
            else f"User id {override_user_id} does not exist."
        )

    beatmap = await resolve_beatmap(session, replay["beatmap_md5"])
    if beatmap is None:
        raise ValueError(
            f"Beatmap {replay['beatmap_md5']} is not known to the server; can't attach the score."
        )

    info = _build_submission_info(replay, acc_pct)

    # Snapshot everything we'll report BEFORE process_score runs. process_score
    # commits internally, and a commit expires the ORM instances
    # (expire_on_commit). Reading an expired attribute afterward triggers an
    # implicit *sync* lazy-load, which fails under the async session with
    # "MissingGreenlet". So capture the already-loaded user/beatmap fields here,
    # and reload the freshly-created score explicitly (awaited) below.
    user_id = user.id
    username = user.username
    bm_id = beatmap.id
    bm_version = beatmap.version
    mods_acr = mods_to_acronyms(replay["mods"], replay.get("lazer_info"))

    # Backdate the token's created_at to when the play actually happened.
    # ScoreToken.ruleset_id is the string-backed GameMode enum (not the raw
    # int byte) — process_score derives the canonical mode for the Score row
    # from info.ruleset_id + mods, so the token only needs the base ruleset.
    token = ScoreToken(
        user_id=user_id,
        beatmap_id=bm_id,
        ruleset_id=GameMode.from_int(replay["mode"]),
        beatmap=beatmap,
        client_version=actor_label,
        created_at=replay["played_at_utc"],
    )
    session.add(token)
    await session.flush()

    score = await process_score(
        user=user,
        beatmap_id=bm_id,
        ranked=False,  # never grant pp inline; recalc handles ranked pp separately
        score_token=token,
        info=info,
        session=session,
    )

    # process_score committed -> the score's attributes are expired. Reload it
    # in the async context (awaited) before reading any column, then pull the
    # values into plain locals so the rest of the function touches no ORM state.
    await session.refresh(score)
    score_id = score.id
    score_acc = float(score.accuracy)
    score_rank = str(score.rank)
    score_total = int(score.total_score)

    # Keep the .osr. A manually submitted score used to land without one, which
    # left it unwatchable, unrenderable and, worse, impossible to audit: when one
    # of these turned out to have the wrong mods there was nothing left to check
    # it against. Best-effort, because the score itself is already committed and
    # a storage hiccup should not undo an accepted play.
    try:
        from app.dependencies.storage import get_storage_service

        storage = get_storage_service()
        replay_path = f"replays/{score_id}_{bm_id}_{user_id}_lazer_replay.osr"

        await storage.write_file(replay_path, data, "application/x-osu-replay")

        score.has_replay = True
        await session.commit()

        logger.info(f"manual submit: stored the replay for score {score_id} at {replay_path}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"manual submit: could not store the replay for score {score_id}: {exc}")

    try:
        from app.router.v2.score import _process_user
        await _process_user(score_id, user_id, redis, fetcher)
    except Exception as exc:  # noqa: BLE001 — score is already committed; stats are best-effort
        logger.warning(f"manual submit: user-stat recompute failed for {user_id}: {exc}")

    logger.info(
        f"manual submit: score {score_id} created for user {user_id} ({username}) "
        f"on beatmap {bm_id} via {actor_label}"
    )

    return {
        "score_id": score_id,
        "user_id": user_id,
        "username": username,
        "beatmap_id": bm_id,
        "beatmap_version": bm_version,
        "accuracy": round(score_acc * 100, 2),
        "rank": score_rank,
        "total_score": score_total,
        "mods": mods_acr,
        "new_global_rank": None,
    }
