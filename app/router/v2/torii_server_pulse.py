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
from app.database.user import User
from app.dependencies.database import Database, get_redis

from .router import router


# --- Tunables -----------------------------------------------------------------

# Cache key + TTL. Bumping the schema version (the trailing ``v1``)
# invalidates all clients' cached snapshots in one move when the
# response shape changes — older clients then see a fresh recompute
# instead of trying to deserialise an incompatible cached blob.
# Bumped to v2 when the response schema gained top_maps / mode_breakdown /
# recent_plays — older clients that pulled a v1 cache during the upgrade
# would have deserialised the unfamiliar fields cleanly (their DTO has
# defensive defaults), but bumping the cache key is the cleaner cut.
# v4: recent_plays now includes recently-submitted scores too (with
# pp / accuracy / rank metadata), so the Live Plays feed stays
# populated even on quiet servers. Mixed feed sorted by event time
# desc, deduped by user_id (most-recent event wins).
_PULSE_CACHE_KEY = "torii:server_pulse:v4"
_PULSE_CACHE_TTL_SECONDS = 10

# Carousel page sizes. Picked together with the 380px popover width:
# 5 top maps fit as a vertical list with covers (~52px each); 8 recent
# plays fit as a denser two-line-per-row list (~38px each); breakdown
# is just up to 4 modes (osu/taiko/catch/mania) so the cap is implicit.
_TOP_MAPS_LIMIT = 5
_RECENT_PLAYS_LIMIT = 8

# How long a SUBMITTED score stays in the recent_plays feed after it
# ends. Bigger than the in-flight cutoff (5 min) so that on quiet
# servers the live feed doesn't go empty seconds after the last play
# completes — there's still recent context to look at.
_SUBMITTED_VISIBILITY_MIN = 15

# In-flight cap. score_tokens with no score_id may legitimately stick
# around for the duration of the longest possible map. Most maps are
# 1.5–4 min, with extreme marathons rarely above ~7 min — anything
# older is almost certainly a crashed / disconnected / page-closed
# client whose token will never be reconciled, and counting it
# inflates "currently playing" indefinitely.
#
# Tightened from 10 → 5 min after upstream feedback that the snapshot
# was reporting ~12 in-flight when only ~3 distinct users were really
# playing — a couple of retries per user and a few stale tokens from
# earlier in the day were both in the count. 5 min is short enough to
# discard those, long enough to comfortably cover any normal play.
_IN_FLIGHT_MAX_AGE_MIN = 5

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
    """Pull the live numbers from DB + Redis.

    Single shared in-flight token fetch
    -----------------------------------
    `currently_playing`, `mode_breakdown`, and `recent_plays` are all
    derived from the same set of in-flight score tokens — and they
    all need the SAME deduplication rule (one row per user, keeping
    the most recent token) so the three numbers tell a consistent
    story. Doing it once here and threading the deduped list through
    each helper avoids the alternative of three separate dedup passes
    that could drift apart.
    """
    now = datetime.now(timezone.utc)
    one_min_ago = now - timedelta(minutes=1)
    five_min_ago = now - timedelta(minutes=5)
    in_flight_cutoff = now - timedelta(minutes=_IN_FLIGHT_MAX_AGE_MIN)

    # Pull all in-flight tokens within the cutoff in one query, ordered
    # newest first so dedup picks up the latest token per user.
    in_flight_tokens = (
        await session.exec(
            select(ScoreToken)
            .where(col(ScoreToken.score_id).is_(None))
            .where(ScoreToken.created_at >= in_flight_cutoff)
            .order_by(sa_desc(ScoreToken.created_at))
        )
    ).all()

    # Dedup by user_id keeping the most-recent token (the iteration
    # is in created_at desc order, so the first time we see a user_id
    # is the one we keep). Skips tokens with null user_id defensively.
    seen_users: set[int] = set()
    deduped_tokens: list[ScoreToken] = []
    for tok in in_flight_tokens:
        if tok.user_id is None:
            continue
        if tok.user_id in seen_users:
            continue
        seen_users.add(tok.user_id)
        deduped_tokens.append(tok)

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
    top_maps = await _compute_top_maps(session, five_min_ago)
    mode_breakdown = _compute_mode_breakdown_from_tokens(deduped_tokens)
    recent_plays = await _compute_mixed_recent_plays(session, deduped_tokens, now)
    online = await _count_online_users(redis)

    # ``top_map`` (singular) preserved for backward compatibility with
    # the v1 client DTO; new clients use ``top_maps`` (plural) for the
    # Hot Maps page.
    top_map_singular = top_maps[0] if top_maps else None

    return {
        "captured_at": now.isoformat(),
        # Distinct-user count, not raw token count. This matches what
        # users intuit by "currently playing" — one human is one play
        # in progress, regardless of how many retry-tokens they've
        # opened. See _IN_FLIGHT_MAX_AGE_MIN cutoff for the upper
        # bound on staleness.
        "currently_playing": len(deduped_tokens),
        "plays_last_minute": int(plays_1m or 0),
        "plays_last_5min": int(plays_5m or 0),
        "online_users": int(online or 0),
        "top_map": top_map_singular,
        "top_maps": top_maps,
        "mode_breakdown": mode_breakdown,
        "recent_plays": recent_plays,
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


async def _compute_top_maps(session, since: datetime) -> list[dict[str, Any]]:
    """Top ``_TOP_MAPS_LIMIT`` most-played beatmaps since ``since``, with full
    beatmapset metadata each.

    Two-stage query:
      1. GROUP BY beatmap_id, ORDER BY count DESC, LIMIT N.
      2. Single batched fetch of the matching Beatmap + Beatmapset rows
         (one query per table, IN-list match on the IDs from stage 1).

    Empty list when no plays have landed in the window — the client
    treats it as a calm "gates are quiet" empty state rather than
    fabricating placeholders.
    """
    play_count_label = "play_count"
    rows = (
        await session.exec(
            select(Score.beatmap_id, func.count(Score.id).label(play_count_label))
            .where(Score.ended_at >= since)
            .where(col(Score.beatmap_id).is_not(None))
            .group_by(Score.beatmap_id)
            .order_by(sa_desc(play_count_label))
            .limit(_TOP_MAPS_LIMIT)
        )
    ).all()

    if not rows:
        return []

    beatmap_ids = [int(r[0]) for r in rows if r[0] is not None]
    if not beatmap_ids:
        return []

    play_counts: dict[int, int] = {int(r[0]): int(r[1] or 0) for r in rows if r[0] is not None}

    beatmaps_list = (
        await session.exec(select(Beatmap).where(col(Beatmap.id).in_(beatmap_ids)))
    ).all()
    beatmaps_by_id = {bm.id: bm for bm in beatmaps_list}

    beatmapset_ids = [bm.beatmapset_id for bm in beatmaps_list if bm.beatmapset_id is not None]
    beatmapsets_list = (
        await session.exec(select(Beatmapset).where(col(Beatmapset.id).in_(beatmapset_ids)))
    ).all() if beatmapset_ids else []
    beatmapsets_by_id = {bs.id: bs for bs in beatmapsets_list}

    out: list[dict[str, Any]] = []
    for beatmap_id in beatmap_ids:  # preserve order from rank
        beatmap = beatmaps_by_id.get(beatmap_id)
        if beatmap is None:
            continue

        beatmapset = beatmapsets_by_id.get(beatmap.beatmapset_id)
        if beatmapset is None:
            continue

        covers_payload: dict[str, Any] | None = None
        if beatmapset.covers is not None:
            try:
                covers_payload = beatmapset.covers.model_dump()
            except AttributeError:
                covers_payload = dict(beatmapset.covers) if beatmapset.covers else None

        out.append({
            "beatmap_id": int(beatmap_id),
            "beatmapset_id": int(beatmap.beatmapset_id),
            "title": beatmapset.title or "",
            "title_unicode": beatmapset.title_unicode or beatmapset.title or "",
            "artist": beatmapset.artist or "",
            "artist_unicode": beatmapset.artist_unicode or beatmapset.artist or "",
            "version": beatmap.version or "",
            "creator": beatmapset.creator or "",
            "covers": covers_payload,
            "play_count_5min": play_counts.get(beatmap_id, 0),
            "ruleset_id": int(getattr(beatmap, "mode", 0) or 0),
            "star_rating": float(getattr(beatmap, "difficulty_rating", 0.0) or 0.0),
        })

    return out


def _compute_mode_breakdown_from_tokens(deduped_tokens: list[ScoreToken]) -> dict[str, int]:
    """Per-ruleset count of currently-playing distinct users, derived from
    the already-deduped (one-token-per-user) list.

    Returned as a string-keyed dict (``"0"`` osu / ``"1"`` taiko / ``"2"``
    catch / ``"3"`` mania) because JSON object keys are always strings —
    the client casts back to int when rendering. Modes with zero are
    absent from the dict; the client treats missing as zero.
    """
    out: dict[str, int] = {}
    for tok in deduped_tokens:
        ruleset_value = tok.ruleset_id
        if ruleset_value is None:
            continue
        # ruleset_id is a GameMode enum on the model. Cast to its int via
        # .value when available; else int() coerces an int directly.
        try:
            mode_int = int(ruleset_value.value)  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            try:
                mode_int = int(ruleset_value)
            except (TypeError, ValueError):
                continue
        key = str(mode_int)
        out[key] = out.get(key, 0) + 1
    return out


async def _compute_mixed_recent_plays(
    session,
    deduped_tokens: list[ScoreToken],
    now: datetime,
) -> list[dict[str, Any]]:
    """Mixed feed of in-flight plays + recently-submitted scores, deduped
    by user (most-recent event per user wins). Powers the carousel's
    "Live Plays" page.

    Why mix
    -------
    On a quiet server (one or two players), the in-flight-only feed
    goes empty seconds after each play ends, leaving the page looking
    dead between maps. Including SUBMITTED scores from the last
    ``_SUBMITTED_VISIBILITY_MIN`` minutes keeps the feed populated with
    recent context — and adds the more interesting data (pp gained,
    accuracy, grade) that an in-flight token can't carry yet.

    Dedup rule
    ----------
    Per user, keep the most-recent EVENT (whether that's an in-flight
    token or a submitted score). Two reasons:
      1. A user who's started a new attempt is the active context —
         their old finished score from 8 minutes ago shouldn't crowd
         out the "they're playing right now" event.
      2. Avoids the previous "lovinflowin appears 4 times in a row
         from retry-spam tokens" failure mode.

    Output schema
    -------------
    Each entry has a ``status`` field:
      * "playing"   — in-flight token; carries ``started_seconds_ago``.
      * "submitted" — landed score; carries ``score_id``, ``pp``,
                       ``accuracy``, ``rank``, ``max_combo``,
                       ``submitted_seconds_ago``.

    Common fields (user / beatmap context) are shared.
    """
    submitted_cutoff = now - timedelta(minutes=_SUBMITTED_VISIBILITY_MIN)

    # Pull recent submitted scores. Limit defensively to 4× the carousel
    # limit so we have headroom to dedup against in-flight tokens
    # without losing coverage. ORDER BY ended_at desc.
    submitted_scores = (
        await session.exec(
            select(Score)
            .where(Score.ended_at >= submitted_cutoff)
            .order_by(sa_desc(Score.ended_at))
            .limit(_RECENT_PLAYS_LIMIT * 4)
        )
    ).all()

    # Build a unified event list: (user_id, sort_time, kind, payload)
    # kind = "token" or "score". sort_time is the more-recent of
    # ScoreToken.created_at / Score.ended_at — that's what determines
    # which event wins in the per-user dedup.
    events: list[tuple[int, datetime, str, Any]] = []

    for token in deduped_tokens:  # already user-deduped, newest first
        if token.user_id is None or token.created_at is None:
            continue
        ts = token.created_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        events.append((int(token.user_id), ts, "token", token))

    for score in submitted_scores:
        if score.user_id is None or score.ended_at is None:
            continue
        ts = score.ended_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        events.append((int(score.user_id), ts, "score", score))

    if not events:
        return []

    # Newest first, then dedup by user_id.
    events.sort(key=lambda e: e[1], reverse=True)

    seen_users: set[int] = set()
    deduped_events: list[tuple[int, datetime, str, Any]] = []
    for ev in events:
        if ev[0] in seen_users:
            continue
        seen_users.add(ev[0])
        deduped_events.append(ev)

    # Take the carousel limit
    deduped_events = deduped_events[:_RECENT_PLAYS_LIMIT]

    # Batch-fetch users + beatmaps + beatmapsets for all referenced IDs.
    user_ids = list({e[0] for e in deduped_events})

    beatmap_ids = list({
        getattr(e[3], "beatmap_id", None)
        for e in deduped_events
    } - {None})

    users_list = (
        await session.exec(select(User).where(col(User.id).in_(user_ids)))
    ).all() if user_ids else []
    users_by_id = {u.id: u for u in users_list}

    beatmaps_list = (
        await session.exec(select(Beatmap).where(col(Beatmap.id).in_(beatmap_ids)))
    ).all() if beatmap_ids else []
    beatmaps_by_id = {bm.id: bm for bm in beatmaps_list}

    beatmapset_ids = list({bm.beatmapset_id for bm in beatmaps_list if bm.beatmapset_id is not None})
    beatmapsets_list = (
        await session.exec(select(Beatmapset).where(col(Beatmapset.id).in_(beatmapset_ids)))
    ).all() if beatmapset_ids else []
    beatmapsets_by_id = {bs.id: bs for bs in beatmapsets_list}

    out: list[dict[str, Any]] = []
    for user_id, ts, kind, payload in deduped_events:
        user = users_by_id.get(user_id)

        beatmap_id = getattr(payload, "beatmap_id", None)
        beatmap = beatmaps_by_id.get(beatmap_id) if beatmap_id is not None else None
        beatmapset = beatmapsets_by_id.get(beatmap.beatmapset_id) if beatmap is not None else None

        ruleset_int = _ruleset_to_int(getattr(payload, "ruleset_id", None) if kind == "token" else getattr(payload, "gamemode", None))

        # Common skeleton; status-specific fields filled below.
        entry: dict[str, Any] = {
            "user_id": int(user_id),
            "username": user.username if user is not None else "",
            "avatar_url": getattr(user, "avatar_url", "") if user is not None else "",
            "beatmap_id": int(beatmap_id or 0),
            "beatmapset_id": int(beatmap.beatmapset_id) if beatmap is not None else 0,
            "title": beatmapset.title if beatmapset is not None else "",
            "title_unicode": (beatmapset.title_unicode or beatmapset.title) if beatmapset is not None else "",
            "artist": beatmapset.artist if beatmapset is not None else "",
            "version": beatmap.version if beatmap is not None else "",
            "ruleset_id": ruleset_int,
        }

        if kind == "token":
            entry["status"] = "playing"
            entry["started_seconds_ago"] = max(0, int((now - ts).total_seconds()))
            # Keep submitted-only fields absent rather than zeroing them
            # — distinguishes "playing now" from "just submitted with 0
            # pp" cleanly client-side.
        else:  # kind == "score"
            entry["status"] = "submitted"
            entry["submitted_seconds_ago"] = max(0, int((now - ts).total_seconds()))
            entry["score_id"] = int(getattr(payload, "id", 0) or 0)
            entry["pp"] = float(getattr(payload, "pp", 0.0) or 0.0)
            entry["accuracy"] = float(getattr(payload, "accuracy", 0.0) or 0.0)
            entry["max_combo"] = int(getattr(payload, "max_combo", 0) or 0)

            rank_value = getattr(payload, "rank", None)
            entry["rank"] = _stringify_rank(rank_value)

        out.append(entry)

    return out


def _ruleset_to_int(value: Any) -> int:
    """Coerce a ruleset/gamemode value (enum or int or None) to its int
    representation. 0 = osu, 1 = taiko, 2 = catch, 3 = mania.
    """
    if value is None:
        return 0
    try:
        return int(value.value)  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0


def _stringify_rank(value: Any) -> str:
    """Coerce a Rank enum (or string, or None) to its display string —
    "SS", "S", "A", "B", "C", "D", or "F". Empty string for None.
    """
    if value is None:
        return ""
    try:
        # Rank enum has .value as the string display
        v = value.value if hasattr(value, "value") else value
        return str(v).upper()
    except Exception:
        return ""


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
