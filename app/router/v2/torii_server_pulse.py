"""GET /api/v2/torii/server-pulse — live server activity snapshot.

Powers the toolbar pulse widget on the Torii client. Returns a tight
snapshot of "what's happening on the server right now" so the client
doesn't have to assemble the same view by combining a half-dozen
existing endpoints client-side:

  * currently_playing  — count of in-flight score sessions (score
                         tokens with no submitted score yet, capped to
                         "started in the last 10 min" so a crashed
                         client's stale token doesn't inflate the
                         number).
  * plays_last_minute  — submitted scores in the last 1 min.
  * plays_last_5min    — submitted scores in the last 5 min. Used for
                         the secondary "X plays/min average" stat in
                         the popover.
  * online_users       — distinct online presences in Redis. Reuses the
                         set-first / SCAN-fallback strategy already
                         shared by the v1 public_user count endpoint
                         and the admin overview.
  * top_map            — most-played beatmap in the last 5 min, with
                         enough beatmapset metadata (covers, title,
                         artist, version, creator) for the client to
                         render a card without a follow-up lookup.
  * sparkline          — 12 buckets × 1 minute of play counts (oldest
                         first) for the popover's micro-graph. 12×1min
                         was picked over e.g. 60×1s because most maps
                         span 1.5–3 min, so submission times are
                         clumpy at second granularity — minute buckets
                         show the actual trend instead of bouncing.

Caching
-------
Result is cached in Redis under ``torii:server_pulse:v1`` for 10s. The
client polls every 60s normally / 10s while the popover is open, so:
  - most polls hit warm cache (low DB load even with many concurrent
    clients on the same server),
  - a 10s-stale snapshot is fine for "live activity" semantics — a
    play landing now showing up 8s late doesn't break the illusion,
  - the cache TTL is also a natural ceiling on update cadence; if 200
    clients all poll at the same wall-clock second, exactly one of
    them computes the snapshot, the rest read the cache.

Auth
----
Public endpoint (no auth required). Online user count + currently
playing count are already exposed publicly via the website
(/admin/system-status, /home, etc.); top map and play counts are
derived from already-public score data. Nothing here leaks anything
the user can't already see by clicking around.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc as sa_desc
from sqlmodel import col, func, select

from app.database.beatmap import Beatmap
from app.database.beatmapset import Beatmapset
from app.database.score import Score
from app.database.score_token import ScoreToken
from app.dependencies.database import Database, get_redis

from .router import router


# --- Tunables -----------------------------------------------------------------

# Cache key + TTL. Bumping the schema version (the trailing ``v1``)
# invalidates all clients' cached snapshots in one move when the
# response shape changes — older clients then see a fresh recompute
# instead of trying to deserialise an incompatible cached blob.
_PULSE_CACHE_KEY = "torii:server_pulse:v1"
_PULSE_CACHE_TTL_SECONDS = 10

# In-flight cap. score_tokens with no score_id may legitimately stick
# around for the duration of the longest possible map (~10 min for
# extreme marathons). Beyond that it's almost certainly a crashed /
# disconnected client whose token will never be reconciled, and
# counting it inflates "currently playing" indefinitely.
_IN_FLIGHT_MAX_AGE_MIN = 10

# Sparkline shape. 12 minute-buckets balances "enough history to see a
# trend" against "fits in a small popover" against "small enough query
# window that it's cheap". Per upstream test runs ~3000 plays in 12min
# is the absolute upper bound during a tournament-finals event; that's
# a single index-backed range scan, well within budget.
_SPARKLINE_BUCKETS = 12
_SPARKLINE_BUCKET_SECONDS = 60


# --- Endpoint -----------------------------------------------------------------


@router.get(
    "/torii/server-pulse",
    tags=["Torii"],
    name="Torii server pulse",
    description=(
        "Live snapshot of server activity for the client toolbar pulse "
        "widget. Cached server-side for 10s; intended polling cadence is "
        "60s normal / 10s while the popover is open."
    ),
)
async def get_server_pulse(session: Database) -> dict[str, Any]:
    redis = get_redis()

    # Try the cached snapshot first. Wrap in a try/except so a Redis
    # outage degrades to a slow direct path rather than a 500.
    try:
        cached = await redis.get(_PULSE_CACHE_KEY)
        if cached:
            return json.loads(cached)
    except Exception:
        pass

    snapshot = await _compute_pulse_snapshot(session, redis)

    # Cache write is best-effort. If Redis is down or hiccups, we
    # already have a valid snapshot to return — no point failing the
    # request just because the next caller will recompute.
    try:
        await redis.set(_PULSE_CACHE_KEY, json.dumps(snapshot), ex=_PULSE_CACHE_TTL_SECONDS)
    except Exception:
        pass

    return snapshot


# --- Snapshot composition -----------------------------------------------------


async def _compute_pulse_snapshot(session, redis) -> dict[str, Any]:
    """Pull the live numbers from DB + Redis. ~5 lightweight queries."""
    now = datetime.now(timezone.utc)
    one_min_ago = now - timedelta(minutes=1)
    five_min_ago = now - timedelta(minutes=5)
    in_flight_cutoff = now - timedelta(minutes=_IN_FLIGHT_MAX_AGE_MIN)

    in_flight = (
        await session.exec(
            select(func.count())
            .select_from(ScoreToken)
            .where(col(ScoreToken.score_id).is_(None))
            .where(ScoreToken.created_at >= in_flight_cutoff)
        )
    ).one()

    plays_1m = (
        await session.exec(
            select(func.count())
            .select_from(Score)
            .where(Score.ended_at >= one_min_ago)
        )
    ).one()

    plays_5m = (
        await session.exec(
            select(func.count())
            .select_from(Score)
            .where(Score.ended_at >= five_min_ago)
        )
    ).one()

    sparkline = await _compute_sparkline_buckets(session, now)
    top_map = await _compute_top_map(session, five_min_ago)
    online = await _count_online_users(redis)

    return {
        "captured_at": now.isoformat(),
        "currently_playing": int(in_flight or 0),
        "plays_last_minute": int(plays_1m or 0),
        "plays_last_5min": int(plays_5m or 0),
        "online_users": int(online or 0),
        "top_map": top_map,
        "sparkline": {
            "bucket_seconds": _SPARKLINE_BUCKET_SECONDS,
            "bucket_count": _SPARKLINE_BUCKETS,
            "buckets": sparkline,
        },
    }


async def _compute_sparkline_buckets(session, now: datetime) -> list[int]:
    """Return ``_SPARKLINE_BUCKETS`` ints, oldest first, plays per bucket.

    Bucketing happens in Python rather than in SQL because:
      a) the row count is tiny (a heavy server peaks ~3k rows in 12 min;
         most servers see <500), so the bandwidth cost is trivial;
      b) keeps the SQL portable across Postgres / MySQL / SQLite without
         needing date_trunc / DATE_FORMAT polymorphism;
      c) makes the bucket alignment trivially correct — the snapshot's
         ``now`` is the only reference point, so the last bucket is
         always "the most recent minute" even if the request landed
         mid-second.
    """
    window_seconds = _SPARKLINE_BUCKETS * _SPARKLINE_BUCKET_SECONDS
    window_start = now - timedelta(seconds=window_seconds)

    rows = (
        await session.exec(
            select(Score.ended_at).where(Score.ended_at >= window_start)
        )
    ).all()

    buckets = [0] * _SPARKLINE_BUCKETS
    for ended_at in rows:
        if ended_at is None:
            continue
        # Ensure tz-aware comparison; some legacy rows may have been
        # written with naive timestamps. Treat naive as UTC (matches
        # what ``utcnow()`` in app.utils produces).
        if ended_at.tzinfo is None:
            ended_at = ended_at.replace(tzinfo=timezone.utc)

        delta_seconds = (now - ended_at).total_seconds()
        if delta_seconds < 0 or delta_seconds >= window_seconds:
            continue

        # bucket_idx_from_end: 0 = newest, _SPARKLINE_BUCKETS-1 = oldest.
        # We invert to chronological order so the client renders left-
        # to-right naturally (oldest on the left, newest on the right).
        bucket_idx_from_end = int(delta_seconds // _SPARKLINE_BUCKET_SECONDS)
        idx = _SPARKLINE_BUCKETS - 1 - bucket_idx_from_end
        if 0 <= idx < _SPARKLINE_BUCKETS:
            buckets[idx] += 1

    return buckets


async def _compute_top_map(session, since: datetime) -> dict[str, Any] | None:
    """Most-played beatmap since ``since``, with full beatmapset metadata.

    Two queries:
      1. GROUP BY beatmap_id, ORDER BY count DESC, LIMIT 1.
      2. Fetch the matching Beatmap + Beatmapset rows for metadata.

    Returns ``None`` when no plays at all have landed in the window —
    the client treats that as "the gates are quiet" and shows a calm
    empty state rather than fabricating a placeholder.
    """
    play_count_label = "play_count"
    row = (
        await session.exec(
            select(Score.beatmap_id, func.count(Score.id).label(play_count_label))
            .where(Score.ended_at >= since)
            .where(col(Score.beatmap_id).is_not(None))
            .group_by(Score.beatmap_id)
            .order_by(sa_desc(play_count_label))
            .limit(1)
        )
    ).first()

    if not row:
        return None

    beatmap_id, play_count = row
    if beatmap_id is None:
        return None

    beatmap = (
        await session.exec(select(Beatmap).where(Beatmap.id == beatmap_id))
    ).first()
    if beatmap is None:
        # Beatmap row missing — possible if the beatmap was wiped after
        # a score landed. Skip it rather than surfacing a half-shaped
        # card the client can't render meaningfully.
        return None

    beatmapset = (
        await session.exec(
            select(Beatmapset).where(Beatmapset.id == beatmap.beatmapset_id)
        )
    ).first()
    if beatmapset is None:
        return None

    # Covers come back as a Pydantic model on the DB row; downstream
    # JSON serialisation only handles dict-shaped values, so unpack
    # explicitly here. Fall back to None when the column is null —
    # the client renders a placeholder card in that case.
    covers_payload: dict[str, Any] | None = None
    if beatmapset.covers is not None:
        try:
            covers_payload = beatmapset.covers.model_dump()
        except AttributeError:
            # Already a dict (some code paths assign raw dict)
            covers_payload = dict(beatmapset.covers) if beatmapset.covers else None

    return {
        "beatmap_id": int(beatmap_id),
        "beatmapset_id": int(beatmap.beatmapset_id),
        "title": beatmapset.title or "",
        "title_unicode": beatmapset.title_unicode or beatmapset.title or "",
        "artist": beatmapset.artist or "",
        "artist_unicode": beatmapset.artist_unicode or beatmapset.artist or "",
        "version": beatmap.version or "",
        "creator": beatmapset.creator or "",
        "covers": covers_payload,
        "play_count_5min": int(play_count or 0),
        "ruleset_id": int(getattr(beatmap, "mode", 0) or 0),
        "star_rating": float(getattr(beatmap, "difficulty_rating", 0.0) or 0.0),
    }


# --- Online presence ----------------------------------------------------------


async def _count_online_users(redis) -> int:
    """Count distinct online presences from Redis.

    Strategy mirrors :func:`app.router.private.admin._count_online_users`
    and :func:`app.router.v1.public_user._count_online_users` exactly so
    all three surfaces report the same number.

    Set-first (``metadata:online_users_set``) is the fast path — O(1)
    SCARD vs an O(N) SCAN. SCAN is the fallback for environments that
    haven't migrated to the explicit set yet.
    """
    try:
        if await redis.exists("metadata:online_users_set"):
            return int(await redis.scard("metadata:online_users_set"))
    except Exception:
        pass

    try:
        cursor = 0
        count = 0
        for _ in range(500):  # bounded — protects against runaway scan
            cursor, keys = await redis.scan(cursor, match="metadata:online:*", count=1000)
            count += len(keys)
            if cursor == 0:
                break
        return count
    except Exception:
        return 0
