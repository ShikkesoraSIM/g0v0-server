"""
Repair script for missing `total_score_best_scores` rows + the user
statistics that depend on them (`ranked_score`, `maximum_combo`,
`grade_ss`/`ssh`/`s`/`sh`/`a`).

Symptom: a non-trivial number of users — including ones near the top of
the rankings — show `score: 0` on the leaderboard while having sane PP.
Their `lazer_user_statistics` row has `ranked_score = 0`, `maximum_combo
= 0`, all grade counts at 0, but `pp` and `total_score` are correct.

Root cause (data-only fix): `total_score_best_scores` (TSBS) is empty
for those (user, gamemode) combinations even though `scores` has plenty
of passed plays. _process_statistics in score.py only inserts a TSBS row
when `previous_score_best is None or previous_score_best_mod is None`
AND `score.passed and has_leaderboard` — and per-user-per-map, once
that branch is missed the TSBS row stays missing forever, because the
"update existing" branches also need a previous TSBS row to exist.

What this script does:
  1. For every (user_id, gamemode, beatmap_id, mod_set) combination in
     `scores` with passed=1, find the highest-scoring score and write it
     to `total_score_best_scores` (REPLACE-style insert keyed by score_id).
  2. Recompute `ranked_score`, `maximum_combo`, and the grade counters
     in `lazer_user_statistics` by aggregating the rebuilt TSBS table.
  3. Leave `pp`, `total_score`, `play_count`, `total_hits` alone — those
     are already correct (they update on a different code path).

Idempotent. Safe to re-run. Uses small per-user transactions so a single
weird row doesn't take down the whole repair.

Run:
  docker compose exec -T app python tools/repair_total_score_best_scores.py [--dry-run] [--user-id N]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Iterable

from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

# Set up the same DB engine the app uses.
from app.dependencies.database import engine


# Modes whose passed scores should populate TSBS. Mirrors the gamemode
# enum on the score column.
ALL_MODES = (
    "OSU", "TAIKO", "FRUITS", "MANIA",
    "OSURX", "OSUAP", "TAIKORX", "FRUITSRX",
    "SENTAKKI", "TAU", "RUSH", "HISHIGATA", "SOYOKAZE",
)


async def find_users_needing_repair(session: AsyncSession) -> list[tuple[int, str]]:
    """Return (user_id, gamemode) pairs where the user has passed scores
    but no TSBS rows for that mode."""
    rows = (await session.exec(text("""
        SELECT s.user_id, s.gamemode
        FROM scores s
        WHERE s.passed = 1 AND s.user_id IS NOT NULL
        GROUP BY s.user_id, s.gamemode
        HAVING NOT EXISTS (
            SELECT 1 FROM total_score_best_scores t
            WHERE t.user_id = s.user_id AND t.gamemode = s.gamemode
        )
    """))).all()
    return [(int(r[0]), str(r[1])) for r in rows]


async def rebuild_tsbs_for_user(
    session: AsyncSession, user_id: int, gamemode: str, dry_run: bool
) -> int:
    """For one (user, gamemode), find the best passed score per
    (beatmap, mod_set) and insert into TSBS. Returns rows inserted."""

    # Best score per (beatmap, mods) for this user. We use the JSON mods
    # column as-is for the grouping key — same string representation
    # already stored in TSBS. ROW_NUMBER picks the top score per group
    # so we don't insert dupes.
    rows = (await session.exec(text("""
        SELECT id, beatmap_id, gamemode, total_score, mods, `rank`
        FROM (
            SELECT
                s.id, s.beatmap_id, s.gamemode, s.total_score, s.mods, s.`rank`,
                ROW_NUMBER() OVER (
                    PARTITION BY s.beatmap_id, JSON_EXTRACT(s.mods, '$')
                    ORDER BY s.total_score DESC, s.id DESC
                ) AS rn
            FROM scores s
            WHERE s.user_id = :uid
              AND s.gamemode = :gm
              AND s.passed = 1
              AND s.beatmap_id IS NOT NULL
              AND EXISTS (SELECT 1 FROM beatmaps b WHERE b.id = s.beatmap_id)
        ) ranked
        WHERE rn = 1
    """), params={"uid": user_id, "gm": gamemode})).all()

    if not rows:
        return 0

    if dry_run:
        return len(rows)

    inserted = 0
    for r in rows:
        score_id = int(r[0])
        beatmap_id = int(r[1])
        gm = str(r[2])
        total_score = int(r[3])
        mods_json = r[4]  # already JSON string from the DB
        rank = str(r[5])

        # The TSBS table stores `mods` as a JSON list of mod-acronym
        # strings (same shape as `score_token.mods` after `mod_to_save`).
        # `scores.mods` in the DB is a list of full mod objects with
        # acronym + settings. Translate so we match the existing shape
        # the runtime code expects on read.
        try:
            full_mods = json.loads(mods_json) if mods_json else []
            mod_acronyms = sorted({m["acronym"] for m in full_mods if isinstance(m, dict) and "acronym" in m})
        except (json.JSONDecodeError, TypeError):
            mod_acronyms = []

        await session.exec(text("""
            INSERT INTO total_score_best_scores
                (user_id, score_id, beatmap_id, gamemode, total_score, mods, `rank`)
            VALUES
                (:uid, :sid, :bid, :gm, :ts, :mods, :rk)
            ON DUPLICATE KEY UPDATE
                user_id     = VALUES(user_id),
                beatmap_id  = VALUES(beatmap_id),
                gamemode    = VALUES(gamemode),
                total_score = VALUES(total_score),
                mods        = VALUES(mods),
                `rank`      = VALUES(`rank`)
        """), params={
            "uid": user_id, "sid": score_id, "bid": beatmap_id, "gm": gm,
            "ts": total_score, "mods": json.dumps(mod_acronyms), "rk": rank,
        })
        inserted += 1

    return inserted


async def recompute_user_stats(
    session: AsyncSession, user_id: int, gamemode: str, dry_run: bool
) -> dict:
    """Recompute ranked_score / grades / maximum_combo from TSBS."""

    agg = (await session.exec(text("""
        SELECT
            COALESCE(SUM(t.total_score), 0)      AS ranked_score,
            COALESCE(MAX(s.max_combo), 0)        AS maximum_combo,
            SUM(CASE WHEN t.`rank` = 'X'  THEN 1 ELSE 0 END) AS grade_ss,
            SUM(CASE WHEN t.`rank` = 'XH' THEN 1 ELSE 0 END) AS grade_ssh,
            SUM(CASE WHEN t.`rank` = 'S'  THEN 1 ELSE 0 END) AS grade_s,
            SUM(CASE WHEN t.`rank` = 'SH' THEN 1 ELSE 0 END) AS grade_sh,
            SUM(CASE WHEN t.`rank` = 'A'  THEN 1 ELSE 0 END) AS grade_a
        FROM total_score_best_scores t
        JOIN scores s ON s.id = t.score_id
        WHERE t.user_id = :uid AND t.gamemode = :gm
    """), params={"uid": user_id, "gm": gamemode})).first()

    if agg is None:
        return {}

    new_values = {
        "ranked_score": int(agg[0] or 0),
        "maximum_combo": int(agg[1] or 0),
        "grade_ss": int(agg[2] or 0),
        "grade_ssh": int(agg[3] or 0),
        "grade_s": int(agg[4] or 0),
        "grade_sh": int(agg[5] or 0),
        "grade_a": int(agg[6] or 0),
    }

    if dry_run:
        return new_values

    await session.exec(text("""
        UPDATE lazer_user_statistics
        SET ranked_score = :ranked_score,
            maximum_combo = :maximum_combo,
            grade_ss  = :grade_ss,
            grade_ssh = :grade_ssh,
            grade_s   = :grade_s,
            grade_sh  = :grade_sh,
            grade_a   = :grade_a
        WHERE user_id = :uid AND mode = :gm
    """), params={**new_values, "uid": user_id, "gm": gamemode})

    return new_values


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only, don't write")
    parser.add_argument("--user-id", type=int, default=None,
                        help="repair just one user (still scans all modes)")
    parser.add_argument("--mode", default=None,
                        help="restrict to a specific gamemode (e.g. OSU)")
    args = parser.parse_args(argv)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        if args.user_id is not None:
            modes_to_check: Iterable[tuple[int, str]] = [
                (args.user_id, m) for m in (ALL_MODES if not args.mode else [args.mode])
            ]
        else:
            print("Scanning for users with missing TSBS rows...", flush=True)
            modes_to_check = await find_users_needing_repair(session)
            if args.mode:
                modes_to_check = [m for m in modes_to_check if m[1] == args.mode]
            print(f"  Found {len(list(modes_to_check))} (user, mode) pairs to repair.")
            # The generator was consumed by len(); re-fetch.
            modes_to_check = await find_users_needing_repair(session)
            if args.mode:
                modes_to_check = [m for m in modes_to_check if m[1] == args.mode]

        total_inserted = 0
        users_touched = 0
        for user_id, gamemode in modes_to_check:
            inserted = await rebuild_tsbs_for_user(session, user_id, gamemode, args.dry_run)
            if inserted == 0 and args.user_id is None:
                # Skipping users with no passed scores in this mode (edge
                # case after we deliberately scanned for missing TSBS).
                continue
            stats = await recompute_user_stats(session, user_id, gamemode, args.dry_run)
            if not args.dry_run:
                await session.commit()
            users_touched += 1
            total_inserted += inserted
            print(
                f"  user {user_id} / {gamemode}: "
                f"{'WOULD insert' if args.dry_run else 'inserted'} {inserted} TSBS rows; "
                f"new stats: ranked_score={stats.get('ranked_score', '?')} "
                f"max_combo={stats.get('maximum_combo', '?')} "
                f"SS={stats.get('grade_ss', '?')} S={stats.get('grade_s', '?')} A={stats.get('grade_a', '?')}",
                flush=True,
            )

        print()
        print(f"Done. {'Would touch' if args.dry_run else 'Touched'} {users_touched} (user, mode) "
              f"pairs and {'would insert' if args.dry_run else 'inserted'} {total_inserted} TSBS rows.")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
