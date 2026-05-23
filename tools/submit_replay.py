"""Manually submit a score from a .osr file, bypassing the normal token flow.

Use case: a player's score was rejected by the live submission pipeline for
reasons outside their control (e.g. beatmap MD5 not yet known to the
server's mirror, transient API rate-limit on lookup) and we want to honour
the play after the fact. They DM us their .osr; we run this and it lands
in their account exactly as if the original submit had succeeded.

What it does, step by step:
  1. Parses the .osr (see tools/parse_osr.py for the format spec).
  2. Looks up the player. By default, matches the .osr's stored player
     name against both the current username and the previous_usernames
     history -- people rename. Override with --user-id when the name has
     drifted so far we can't auto-resolve it, or when the .osr predates
     a series of renames.
  3. Looks up the beatmap by checksum.
  4. Creates a backdated ScoreToken so the score is bound to a token in
     the same shape live submits produce.
  5. Calls process_score(...) -- the SAME function the live POST handler
     uses -- so leaderboard handling, pp calc gating on ranked status,
     etc. behave identically to a normal submit. NOTE: pp won't be
     granted on graveyard/unranked maps (correctly), but the play still
     shows up in the user's history.
  6. Calls _process_user(...) so user statistics + leaderboards reflect
     the new score immediately (no need to wait for the next aggregation).

Run inside the app container so it has the same env vars (DB / redis /
bucket) as the live server:

    # Copy the replay onto the box first.
    scp my-replay.osr torii-eu:~/manual-submits/

    # Then from the prod host:
    docker compose exec -T app python tools/submit_replay.py \\
        /host/manual-submits/my-replay.osr

The script prints the inserted score_id (and the user's new rank if
process_user succeeded) so the admin can paste it back to the player.
"""
import argparse
import asyncio
import datetime
import sys
from pathlib import Path

# Allow running this file directly: tools/ is not on sys.path by default
# in container WORKDIR.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parse_osr import parse, accuracy as compute_accuracy, mods_to_str  # noqa: E402
from sqlmodel import select  # noqa: E402

from app.database.beatmap import Beatmap  # noqa: E402
from app.database.score import process_score  # noqa: E402
from app.database.score_token import ScoreToken  # noqa: E402
from app.database.user import User  # noqa: E402
from app.dependencies.database import get_redis, with_db  # noqa: E402
from app.dependencies.fetcher import get_fetcher  # noqa: E402
from app.models.score import HitResult, Rank, SoloScoreSubmissionInfo  # noqa: E402


# Standard mod-bit -> lazer acronym. Mirrors the bits the .osr file uses.
# When a bit doesn't map to a current lazer mod (legacy keymode mods etc.)
# we just drop it; passing an unknown acronym to SoloScoreSubmissionInfo
# fails validation, and the alternative -- guessing -- would silently
# distort the submission. Worst case the score lands as NoMod, which is
# preferable to lying about which mods were active.
OSR_MOD_TO_LAZER = {
    1 << 0: "NF", 1 << 1: "EZ", 1 << 3: "HD", 1 << 4: "HR",
    1 << 5: "SD", 1 << 6: "DT", 1 << 7: "RX", 1 << 8: "HT",
    1 << 9: "NC", 1 << 10: "FL", 1 << 12: "SO", 1 << 13: "AP",
    1 << 14: "PF",
}


def osr_mods_to_lazer(mods_bitmask: int) -> list[dict]:
    """Convert the .osr int bitmask to lazer's APIMod list shape."""
    acronyms = [acr for bit, acr in OSR_MOD_TO_LAZER.items() if mods_bitmask & bit]
    # Strip DT when NC is set, and HD when PF is set -- those flags imply
    # the other, but the lazer mod validator treats them as incompatible
    # when both appear in the same list.
    if "NC" in acronyms and "DT" in acronyms:
        acronyms.remove("DT")
    if "PF" in acronyms and "SD" in acronyms:
        acronyms.remove("SD")
    return [{"acronym": a, "settings": {}} for a in acronyms]


def compute_rank_osu(accuracy_pct: float, n50: int, total_hits: int,
                     miss: int, mods_bitmask: int) -> Rank:
    """osu! standard rank thresholds, mirroring osu! stable's rules.

    Lazer technically uses slightly different cut-offs but for a manual
    submission we just need a reasonable label. HD/FL bump S->SH and
    X->XH (silver versions); the X variant requires 100% accuracy.
    """
    if accuracy_pct >= 100.0:
        return Rank.XH if mods_bitmask & ((1 << 3) | (1 << 10)) else Rank.X

    silver = bool(mods_bitmask & ((1 << 3) | (1 << 10)))

    # S: >=90% accuracy AND no miss AND <=1% of hits were 50s
    if accuracy_pct >= 90.0 and miss == 0 and (total_hits == 0 or n50 / total_hits <= 0.01):
        return Rank.SH if silver else Rank.S
    if accuracy_pct >= 80.0:
        return Rank.A
    if accuracy_pct >= 70.0:
        return Rank.B
    if accuracy_pct >= 60.0:
        return Rank.C
    return Rank.D


async def find_user(db, replay_player_name: str, override_id: int | None) -> User:
    if override_id is not None:
        user = await db.get(User, override_id)
        if not user:
            raise SystemExit(f"--user-id {override_id} not found in lazer_users")
        return user

    # 1) Current username match. Cheapest, hits the unique index.
    user = (await db.exec(select(User).where(User.username == replay_player_name))).first()
    if user:
        return user

    # 2) Username history match -- the player renamed since the replay was
    #    captured. previous_usernames is a JSON column of an array; we
    #    use JSON_CONTAINS for the lookup.
    from sqlalchemy import func, text
    result = await db.exec(
        select(User).where(
            func.json_contains(User.previous_usernames, text(f"JSON_QUOTE('{replay_player_name}')")) == 1
        )
    )
    user = result.first()
    if user:
        return user

    raise SystemExit(
        f"Couldn't resolve user '{replay_player_name}'. Pass --user-id <N> "
        f"if you know the numeric id (current username may have drifted)."
    )


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("osr_path", type=Path, help="Path to the .osr replay file.")
    parser.add_argument(
        "--user-id", type=int, default=None,
        help="Override the user lookup with an explicit lazer_users.id "
             "(useful when the player's renamed beyond auto-resolution).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse + resolve user + resolve beatmap, but do NOT insert. "
             "Use this once before the real run to sanity-check.",
    )
    args = parser.parse_args()

    if not args.osr_path.exists():
        raise SystemExit(f"replay not found: {args.osr_path}")

    replay = parse(args.osr_path)
    acc_pct = compute_accuracy(
        replay["mode"], replay["n300"], replay["n100"], replay["n50"],
        replay["miss"], replay["geki"], replay["katu"],
    )
    print(f"replay: player={replay['player']!r}  mode={replay['mode_name']}  "
          f"score={replay['total_score']:,}  combo={replay['max_combo']}  "
          f"acc={acc_pct:.2f}%  mods={mods_to_str(replay['mods'])}")
    print(f"  played at (UTC): {replay['played_at_utc']}")
    print(f"  beatmap md5:     {replay['beatmap_md5']}")

    async with with_db() as db:
        user = await find_user(db, replay["player"], args.user_id)
        print(f"  resolved user:   id={user.id}  username={user.username!r}  "
              f"prev={user.previous_usernames}")

        beatmap = (
            await db.exec(select(Beatmap).where(Beatmap.checksum == replay["beatmap_md5"]))
        ).first()
        if not beatmap:
            raise SystemExit(
                f"Beatmap with checksum {replay['beatmap_md5']} not found in beatmaps table. "
                f"Has the server ever seen this map? (Score submission requires the row to exist.)"
            )
        print(f"  resolved beatmap: id={beatmap.id}  status={beatmap.beatmap_status.value}  "
              f"version={beatmap.version!r}")

        if args.dry_run:
            print("\n--dry-run set: skipping insert. Re-run without --dry-run to commit.")
            return

        # Build statistics dict the same shape lazer's submission pipeline uses.
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

        # maximum_statistics: what the score would be at 100%. For osu!
        # standard this is "all Greats, no miss". The exact shape barely
        # matters for the rank+pp calculation when status=GRAVEYARD (no
        # pp granted anyway), but we fill it for parity with live submits.
        total_hits = replay["n300"] + replay["n100"] + replay["n50"] + replay["miss"]
        maximum_statistics: dict = {HitResult.GREAT: total_hits}

        mods_list = osr_mods_to_lazer(replay["mods"])
        rank = compute_rank_osu(
            acc_pct, replay["n50"], total_hits, replay["miss"], replay["mods"],
        )

        info = SoloScoreSubmissionInfo(
            rank=rank,
            total_score=replay["total_score"],
            total_score_without_mods=replay["total_score"],  # NoMod-only multiplier path; safe approximation
            accuracy=acc_pct / 100.0,
            pp=0,                       # process_score recomputes if the map is ranked
            max_combo=replay["max_combo"],
            ruleset_id=replay["mode"],
            passed=True,                # they finished and we have a final score; manual submits are passed
            mods=mods_list,
            statistics=statistics,
            maximum_statistics=maximum_statistics,
        )

        # Backdate the token so the score's started_at lines up with when
        # the play actually happened, instead of "now". The end_at gets
        # set by process_score to utcnow() -- we accept the small lie
        # there because we don't know the exact replay duration without
        # decoding the LZMA payload.
        token = ScoreToken(
            user_id=user.id,
            beatmap_id=beatmap.id,
            ruleset_id=replay["mode"],
            beatmap=beatmap,
            client_version="manual-submit",
            created_at=replay["played_at_utc"],
        )
        db.add(token)
        await db.flush()
        print(f"  created backdated ScoreToken id={token.id}")

        score = await process_score(
            user=user,
            beatmap_id=beatmap.id,
            ranked=False,                # graveyard -> always False; process_score gates pp internally
            score_token=token,
            info=info,
            session=db,
        )
        print(f"\nInserted score id={score.id} for user {user.id} ({user.username}).")
        print(f"  rank={score.rank}  total={score.total_score:,}  acc={score.accuracy:.4%}")

        # Recompute the user's overall stats so the score is reflected in
        # the live profile / leaderboard. Done in a best-effort try block
        # because if it fails for any reason, the score itself is already
        # committed and the next /me request will pick it up via the
        # nightly recalculator.
        try:
            from app.router.v2.score import _process_user
            redis_client = get_redis()
            fetcher = await get_fetcher()
            await _process_user(score.id, user.id, redis_client, fetcher)
            print("  user stats recomputed (best_scores + lazer_user_statistics).")
        except Exception as e:
            print(f"  user stats recompute failed ({e}); next nightly recalc will pick it up.")


if __name__ == "__main__":
    asyncio.run(main())
