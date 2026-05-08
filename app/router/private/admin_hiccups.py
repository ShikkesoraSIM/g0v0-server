"""Admin dashboard + APIs for the Torii hiccup-report archive.

All endpoints sit under ``/api/private/admin/hiccups`` and require an
admin user (the same ``require_admin`` helper the rest of the admin
router uses). Public client uploads land at
``POST /api/v2/torii/hiccup-reports`` (see ``torii_hiccup_reports.py``);
this file is purely the read side.

Surfaces
--------
``GET  /admin/hiccups/``            HTML dashboard. Self-contained: filter bar
                                    drives every other endpoint via HTMX,
                                    Chart.js draws the charts, no SPA build.

``GET  /admin/hiccups/list``        Paginated JSON list of records, filterable.
``GET  /admin/hiccups/causes``      Top likely_cause buckets.
``GET  /admin/hiccups/histogram``   frame_ms distribution (5 buckets).
``GET  /admin/hiccups/timeseries``  Hiccups per hour over the time range.
``GET  /admin/hiccups/{id}``        One record's full payload (modal expansion).
``GET  /admin/hiccups/export.csv``  CSV download of filtered set.
``GET  /admin/hiccups/export.json`` JSON download of filtered set.

Filters
-------
All read endpoints accept the same query parameters so the dashboard can
share a single filter dict across them:

* ``since`` — ISO 8601 lower bound on ``captured_at`` (default 7 d ago).
* ``until`` — ISO 8601 upper bound on ``captured_at`` (default now).
* ``min_frame_ms`` — only rows with ``frame_ms >= this`` (default 33).
* ``cause`` — substring match on ``likely_cause`` (default any).
* ``user_id`` — exact match on ``user_id`` (default any).
* ``device_hash`` — prefix match on ``device_hash`` (default any).
* ``platform`` — exact match on ``platform`` (default any).
* ``version`` — exact match on ``osu_version`` (default any).
* ``screen`` — exact match on ``current_screen`` (default any).

Pagination uses ``cursor`` (an ``id`` from the previous page; descending
ID order) + ``limit`` (default 100, max 500). For large exports the CSV
endpoint streams without pagination — it's gated to admins.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import and_, func
from sqlmodel import col, select

from app.database.torii_hiccup_report import ToriiHiccupReport
from app.database.user import User
from app.dependencies.database import Database
from app.dependencies.user import UserAndToken, get_client_user_and_token
from app.utils import utcnow

from .admin import require_admin
from .router import router


# ── Filter parsing ──────────────────────────────────────────────────────────


def _parse_filters(
    since: str | None,
    until: str | None,
    min_frame_ms: float,
    cause: str | None,
    user_id: int | None,
    device_hash: str | None,
    platform: str | None,
    version: str | None,
    screen: str | None,
) -> dict[str, Any]:
    """Normalise the query-string filters into a dict the SQL helpers consume.

    Returns a dict with parsed datetimes + string filters. Defaults: a 7-day
    window ending now, frame_ms >= 33 (the same threshold the client uses
    by default — keeps the dashboard's default view in sync with what the
    client considers a hiccup).
    """
    now = utcnow()

    parsed_since = _parse_dt(since) or now - timedelta(days=7)
    parsed_until = _parse_dt(until) or now

    if parsed_since >= parsed_until:
        raise HTTPException(status_code=400, detail="`since` must be before `until`.")

    return {
        "since": parsed_since,
        "until": parsed_until,
        "min_frame_ms": float(min_frame_ms),
        "cause": cause.strip() if cause else None,
        "user_id": user_id,
        "device_hash": device_hash.strip() if device_hash else None,
        "platform": platform.strip() if platform else None,
        "version": version.strip() if version else None,
        "screen": screen.strip() if screen else None,
    }


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        # `datetime.fromisoformat` handles both naive and TZ-aware strings.
        # We strip TZ to match the column type (no-tz DateTime) so the
        # comparison is on the same basis the writer uses.
        v = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if v.tzinfo is not None:
            v = v.astimezone(tz=None).replace(tzinfo=None)
        return v
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Bad ISO datetime: {raw!r}")


def _apply_filters(stmt, f: dict[str, Any]):
    """Tack the filter dict onto a select() statement."""
    stmt = stmt.where(
        col(ToriiHiccupReport.captured_at) >= f["since"],
        col(ToriiHiccupReport.captured_at) <= f["until"],
        col(ToriiHiccupReport.frame_ms) >= f["min_frame_ms"],
    )
    if f["cause"]:
        stmt = stmt.where(col(ToriiHiccupReport.likely_cause).contains(f["cause"]))
    if f["user_id"] is not None:
        stmt = stmt.where(ToriiHiccupReport.user_id == f["user_id"])
    if f["device_hash"]:
        stmt = stmt.where(col(ToriiHiccupReport.device_hash).startswith(f["device_hash"]))
    if f["platform"]:
        stmt = stmt.where(ToriiHiccupReport.platform == f["platform"])
    if f["version"]:
        stmt = stmt.where(ToriiHiccupReport.osu_version == f["version"])
    if f["screen"]:
        stmt = stmt.where(ToriiHiccupReport.current_screen == f["screen"])
    return stmt


# ── JSON: paginated list ────────────────────────────────────────────────────


@router.get(
    "/admin/hiccups/list",
    name="List Torii hiccup reports (admin)",
    tags=["Torii", "Admin"],
)
async def list_hiccups(
    session: Database,
    user_and_token: Annotated[UserAndToken, Depends(get_client_user_and_token)],
    cursor: int | None = Query(default=None, description="Return rows with id < cursor (descending). Omit for first page."),
    limit: int = Query(default=100, ge=1, le=500),
    since: str | None = None,
    until: str | None = None,
    min_frame_ms: float = 33.0,
    cause: str | None = None,
    user_id: int | None = None,
    device_hash: str | None = None,
    platform: str | None = None,
    version: str | None = None,
    screen: str | None = None,
):
    await require_admin(session, user_and_token)
    f = _parse_filters(since, until, min_frame_ms, cause, user_id, device_hash, platform, version, screen)

    stmt = select(ToriiHiccupReport)
    stmt = _apply_filters(stmt, f)
    if cursor is not None:
        stmt = stmt.where(ToriiHiccupReport.id < cursor)
    stmt = stmt.order_by(col(ToriiHiccupReport.id).desc()).limit(limit + 1)

    rows = list((await session.exec(stmt)).all())
    has_more = len(rows) > limit
    rows = rows[:limit]

    return {
        "items": [_row_to_dict(r) for r in rows],
        "has_more": has_more,
        "next_cursor": rows[-1].id if has_more and rows else None,
    }


def _row_to_dict(r: ToriiHiccupReport) -> dict[str, Any]:
    return {
        "id": r.id,
        "user_id": r.user_id,
        "device_hash": r.device_hash[:12] if r.device_hash else None,  # tease only — full not needed in the table
        "device_hash_full": r.device_hash,
        "session_id": r.session_id,
        "captured_at": r.captured_at.isoformat() if r.captured_at else None,
        "received_at": r.received_at.isoformat() if r.received_at else None,
        "frame_ms": r.frame_ms,
        "thread": r.thread,
        "likely_cause": r.likely_cause,
        "api_state": r.api_state,
        "logged_in": r.logged_in,
        "current_screen": r.current_screen,
        "visible_overlays": r.visible_overlays,
        "gen0_count": r.gen0_count,
        "gen1_count": r.gen1_count,
        "gen2_count": r.gen2_count,
        "gen0_delta": r.gen0_delta,
        "gen1_delta": r.gen1_delta,
        "gen2_delta": r.gen2_delta,
        "total_memory_mb": r.total_memory_mb,
        "recent_events": r.recent_events,
        "osu_version": r.osu_version,
        "platform": r.platform,
        "cpu_arch": r.cpu_arch,
    }


# ── JSON: aggregations ──────────────────────────────────────────────────────


@router.get(
    "/admin/hiccups/causes",
    name="Top hiccup causes (admin)",
    tags=["Torii", "Admin"],
)
async def causes_aggregation(
    session: Database,
    user_and_token: Annotated[UserAndToken, Depends(get_client_user_and_token)],
    since: str | None = None,
    until: str | None = None,
    min_frame_ms: float = 33.0,
    cause: str | None = None,
    user_id: int | None = None,
    device_hash: str | None = None,
    platform: str | None = None,
    version: str | None = None,
    screen: str | None = None,
    top: int = Query(default=10, ge=1, le=50),
):
    await require_admin(session, user_and_token)
    f = _parse_filters(since, until, min_frame_ms, cause, user_id, device_hash, platform, version, screen)

    stmt = select(
        ToriiHiccupReport.likely_cause,
        func.count(ToriiHiccupReport.id).label("n"),
    )
    stmt = _apply_filters(stmt, f).group_by(ToriiHiccupReport.likely_cause).order_by(func.count(ToriiHiccupReport.id).desc()).limit(top)

    rows = list((await session.exec(stmt)).all())
    total = sum(n for _, n in rows) or 1
    return {
        "buckets": [{"cause": cause, "count": n, "pct": round(n / total * 100, 1)} for cause, n in rows],
        "total": total,
    }


@router.get(
    "/admin/hiccups/histogram",
    name="Hiccup frame_ms histogram (admin)",
    tags=["Torii", "Admin"],
)
async def histogram(
    session: Database,
    user_and_token: Annotated[UserAndToken, Depends(get_client_user_and_token)],
    since: str | None = None,
    until: str | None = None,
    min_frame_ms: float = 33.0,
    cause: str | None = None,
    user_id: int | None = None,
    device_hash: str | None = None,
    platform: str | None = None,
    version: str | None = None,
    screen: str | None = None,
):
    """5-bucket fixed histogram of frame_ms, log-spaced for human reading.

    Buckets: [33,50), [50,100), [100,200), [200,500), [500,inf). Picked so
    each bucket spans roughly one perceptual category — "slightly choppy",
    "noticeable stutter", "user complains in chat", "user thinks the game
    crashed", "user starts task manager".
    """
    await require_admin(session, user_and_token)
    f = _parse_filters(since, until, min_frame_ms, cause, user_id, device_hash, platform, version, screen)

    bucket_edges = [33.0, 50.0, 100.0, 200.0, 500.0]  # < first edge is dropped (we filter min_frame_ms anyway)
    bucket_labels = ["33–50 ms", "50–100 ms", "100–200 ms", "200–500 ms", "500 ms+"]
    counts = [0, 0, 0, 0, 0]

    # One scan + bucket-side accumulation. With our indexes this is fast
    # enough for the dashboard's typical 7-day window; if the table grows
    # past a few million rows we'd switch to a CASE-WHEN aggregate.
    stmt = select(ToriiHiccupReport.frame_ms)
    stmt = _apply_filters(stmt, f)
    for (frame_ms,) in (await session.exec(stmt)).all():
        if frame_ms < bucket_edges[1]:
            counts[0] += 1
        elif frame_ms < bucket_edges[2]:
            counts[1] += 1
        elif frame_ms < bucket_edges[3]:
            counts[2] += 1
        elif frame_ms < bucket_edges[4]:
            counts[3] += 1
        else:
            counts[4] += 1

    return {"labels": bucket_labels, "counts": counts}


@router.get(
    "/admin/hiccups/timeseries",
    name="Hiccup time series (admin)",
    tags=["Torii", "Admin"],
)
async def timeseries(
    session: Database,
    user_and_token: Annotated[UserAndToken, Depends(get_client_user_and_token)],
    since: str | None = None,
    until: str | None = None,
    min_frame_ms: float = 33.0,
    cause: str | None = None,
    user_id: int | None = None,
    device_hash: str | None = None,
    platform: str | None = None,
    version: str | None = None,
    screen: str | None = None,
    bucket: str = Query(default="hour", pattern="^(hour|day)$"),
):
    """Hiccup count over time, bucketed hourly or daily.

    The bucket key drops to the start of the period for stable labels;
    daily for week+ ranges, hourly otherwise (same defaults the dashboard
    auto-picks).
    """
    await require_admin(session, user_and_token)
    f = _parse_filters(since, until, min_frame_ms, cause, user_id, device_hash, platform, version, screen)

    if bucket == "day":
        # Truncate to day boundary in UTC. MySQL's DATE() works here.
        bucket_expr = func.date(ToriiHiccupReport.captured_at)
    else:
        # Hour bucket — DATE_FORMAT for portability.
        bucket_expr = func.date_format(ToriiHiccupReport.captured_at, "%Y-%m-%d %H:00:00")

    stmt = select(
        bucket_expr.label("bucket"),
        func.count(ToriiHiccupReport.id).label("n"),
    )
    stmt = _apply_filters(stmt, f).group_by("bucket").order_by("bucket")
    rows = list((await session.exec(stmt)).all())
    return {
        "bucket": bucket,
        "points": [{"t": str(b), "n": n} for b, n in rows],
    }


# ── JSON: single record (for modal) ─────────────────────────────────────────


@router.get(
    "/admin/hiccups/{record_id}",
    name="Get one hiccup record (admin)",
    tags=["Torii", "Admin"],
)
async def get_one(
    record_id: int,
    session: Database,
    user_and_token: Annotated[UserAndToken, Depends(get_client_user_and_token)],
):
    await require_admin(session, user_and_token)
    row = await session.get(ToriiHiccupReport, record_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Hiccup record not found")
    return _row_to_dict(row)


# ── Exports (CSV / JSON) ────────────────────────────────────────────────────


@router.get(
    "/admin/hiccups/export.csv",
    name="Export hiccups as CSV (admin)",
    tags=["Torii", "Admin"],
)
async def export_csv(
    session: Database,
    user_and_token: Annotated[UserAndToken, Depends(get_client_user_and_token)],
    since: str | None = None,
    until: str | None = None,
    min_frame_ms: float = 33.0,
    cause: str | None = None,
    user_id: int | None = None,
    device_hash: str | None = None,
    platform: str | None = None,
    version: str | None = None,
    screen: str | None = None,
    limit: int = Query(default=10_000, ge=1, le=100_000),
):
    await require_admin(session, user_and_token)
    f = _parse_filters(since, until, min_frame_ms, cause, user_id, device_hash, platform, version, screen)

    stmt = (
        select(ToriiHiccupReport)
        .order_by(col(ToriiHiccupReport.captured_at).desc())
        .limit(limit)
    )
    stmt = _apply_filters(stmt, f)
    rows = list((await session.exec(stmt)).all())

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "user_id", "device_hash", "session_id", "captured_at", "received_at",
        "frame_ms", "thread", "likely_cause", "api_state", "logged_in",
        "current_screen", "visible_overlays_json",
        "gen0_count", "gen1_count", "gen2_count",
        "gen0_delta", "gen1_delta", "gen2_delta",
        "total_memory_mb", "recent_events_json",
        "osu_version", "platform", "cpu_arch",
    ])
    for r in rows:
        writer.writerow([
            r.id, r.user_id, r.device_hash, r.session_id,
            r.captured_at.isoformat() if r.captured_at else "",
            r.received_at.isoformat() if r.received_at else "",
            r.frame_ms, r.thread, r.likely_cause,
            r.api_state or "", r.logged_in if r.logged_in is not None else "",
            r.current_screen or "",
            json.dumps(r.visible_overlays) if r.visible_overlays else "",
            r.gen0_count or "", r.gen1_count or "", r.gen2_count or "",
            r.gen0_delta or "", r.gen1_delta or "", r.gen2_delta or "",
            r.total_memory_mb or "",
            json.dumps(r.recent_events) if r.recent_events else "",
            r.osu_version or "", r.platform or "", r.cpu_arch or "",
        ])

    filename = f"torii-hiccups-{f['since'].strftime('%Y%m%d')}-to-{f['until'].strftime('%Y%m%d')}.csv"
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/admin/hiccups/export.json",
    name="Export hiccups as JSON (admin)",
    tags=["Torii", "Admin"],
)
async def export_json(
    session: Database,
    user_and_token: Annotated[UserAndToken, Depends(get_client_user_and_token)],
    since: str | None = None,
    until: str | None = None,
    min_frame_ms: float = 33.0,
    cause: str | None = None,
    user_id: int | None = None,
    device_hash: str | None = None,
    platform: str | None = None,
    version: str | None = None,
    screen: str | None = None,
    limit: int = Query(default=10_000, ge=1, le=100_000),
):
    await require_admin(session, user_and_token)
    f = _parse_filters(since, until, min_frame_ms, cause, user_id, device_hash, platform, version, screen)

    stmt = (
        select(ToriiHiccupReport)
        .order_by(col(ToriiHiccupReport.captured_at).desc())
        .limit(limit)
    )
    stmt = _apply_filters(stmt, f)
    rows = list((await session.exec(stmt)).all())

    payload = json.dumps(
        {"items": [_row_to_dict(r) for r in rows], "count": len(rows)},
        default=str, indent=2,
    )
    filename = f"torii-hiccups-{f['since'].strftime('%Y%m%d')}-to-{f['until'].strftime('%Y%m%d')}.json"
    return StreamingResponse(
        iter([payload]),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── HTML dashboard (single self-contained page) ─────────────────────────────


@router.get(
    "/admin/hiccups/",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="Torii hiccup admin dashboard",
)
async def hiccups_dashboard(
    session: Database,
    user_and_token: Annotated[UserAndToken, Depends(get_client_user_and_token)],
):
    await require_admin(session, user_and_token)
    # Hardcoded HTML so we don't pull in a template engine just for this. The
    # filter form fires HTMX requests against the JSON endpoints above; the
    # client-side JS renders Chart.js charts from the responses. CDN-loaded
    # HTMX + Chart.js + Inter font keeps the page completely self-contained
    # as long as the admin browser has internet (it does — they just hit
    # this URL through one).
    return HTMLResponse(_DASHBOARD_HTML)


# Massive HTML literal — kept as a module-level constant so the route handler
# stays scannable. CSS is intentionally inlined; the dashboard is not part of
# the public site, and shipping a separate stylesheet would mean another
# StaticFiles mount for a single page.
_DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Torii hiccup reports</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<script src="https://unpkg.com/htmx.org@1.9.10"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0c0e2a;
    --panel: #15112c;
    --panel-2: #1c1d3a;
    --border: rgba(255,255,255,.10);
    --hairline: rgba(255,255,255,.07);
    --pink: #ff66b3;
    --cyan: #69d7ff;
    --gain: #8bffcf;
    --loss: #ff8f9c;
    --amber: #ffd36e;
    --ink-1: rgba(255,255,255,.94);
    --ink-2: rgba(255,255,255,.62);
    --ink-3: rgba(255,255,255,.42);
  }
  * { box-sizing: border-box }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--ink-1); font-family: Inter, system-ui, sans-serif; }
  body { padding: 24px; min-height: 100vh; }
  .wrap { max-width: 1400px; margin: 0 auto; }

  h1 { font-size: 22px; font-weight: 700; margin: 0 0 4px; letter-spacing: -.01em; }
  .subtitle { color: var(--ink-3); font-size: 13px; margin-bottom: 24px; }

  /* Filter bar */
  .filter-card {
    background: var(--panel-2);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 14px 16px;
    margin-bottom: 16px;
  }
  .filter-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; }
  .filter-row label { display: block; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .08em; color: var(--ink-3); margin-bottom: 4px; }
  .filter-row input, .filter-row select {
    width: 100%; background: var(--panel); border: 1px solid var(--border); color: var(--ink-1);
    padding: 7px 10px; border-radius: 8px; font: inherit; font-size: 13px;
  }
  .filter-row input:focus, .filter-row select:focus { outline: 1px solid var(--cyan); border-color: var(--cyan); }
  .filter-actions { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
  button.btn, a.btn {
    background: var(--pink); color: white; border: 0; padding: 8px 14px; border-radius: 8px;
    font: inherit; font-size: 13px; font-weight: 600; cursor: pointer; text-decoration: none; display: inline-flex; align-items: center; gap: 6px;
  }
  button.btn:hover, a.btn:hover { filter: brightness(1.1) }
  button.btn.ghost, a.btn.ghost { background: var(--panel); color: var(--ink-1); border: 1px solid var(--border); }
  button.btn.ghost:hover, a.btn.ghost:hover { background: var(--panel-2); }

  /* Charts row */
  .charts-row { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; margin-bottom: 16px; }
  .chart-card {
    background: var(--panel-2); border: 1px solid var(--border); border-radius: 14px; padding: 14px 16px;
    min-height: 220px; position: relative;
  }
  .chart-card h3 {
    font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .08em;
    color: var(--ink-3); margin: 0 0 8px;
  }
  .chart-card canvas { max-height: 180px !important; }
  @media (max-width: 1080px) { .charts-row { grid-template-columns: 1fr; } }

  /* Records table */
  .records-card { background: var(--panel-2); border: 1px solid var(--border); border-radius: 14px; padding: 14px 16px; }
  .records-card h3 { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .08em; color: var(--ink-3); margin: 0 0 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; font-family: 'JetBrains Mono', monospace; }
  th { text-align: left; font-weight: 600; padding: 8px 8px; border-bottom: 1px solid var(--border); color: var(--ink-3); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; font-family: Inter, sans-serif; }
  td { padding: 8px 8px; border-bottom: 1px solid var(--hairline); vertical-align: top; }
  tr:hover td { background: rgba(255,255,255,.02); cursor: pointer; }
  .frame-ms { font-weight: 600; }
  .frame-ms.warn { color: var(--amber); }
  .frame-ms.bad  { color: var(--loss); }
  .frame-ms.huge { color: var(--pink); }
  .cause { color: var(--ink-2); }
  .cause-gc  { color: var(--loss); }
  .cause-api { color: var(--amber); }
  .cause-stall { color: var(--pink); }
  .pager { display: flex; justify-content: space-between; align-items: center; margin-top: 10px; color: var(--ink-3); font-size: 12px; }
  .empty { padding: 32px; text-align: center; color: var(--ink-3); }

  /* Detail modal */
  .modal-back { position: fixed; inset: 0; background: rgba(0,0,0,.65); display: none; align-items: center; justify-content: center; padding: 24px; z-index: 100; }
  .modal-back.shown { display: flex; }
  .modal {
    background: var(--panel-2); border: 1px solid var(--border); border-radius: 14px;
    padding: 20px; max-width: 720px; width: 100%; max-height: 80vh; overflow: auto;
  }
  .modal h3 { margin: 0 0 6px; font-size: 16px; }
  .modal pre { background: var(--panel); padding: 12px; border-radius: 8px; font-size: 12px; line-height: 1.6; overflow-x: auto; max-height: 50vh; color: var(--ink-2); }
  .modal-close {
    background: transparent; border: 0; color: var(--ink-3); font-size: 22px; float: right;
    cursor: pointer; line-height: 1; padding: 0; margin: -2px 0 0 0;
  }
</style>
</head>
<body>

<div class="wrap">
  <h1>Torii hiccup reports</h1>
  <p class="subtitle">Frame stalls reported by Torii clients with the hiccup logger enabled. Click a row for the full record.</p>

  <!-- Filter bar -->
  <form class="filter-card" id="filters" onsubmit="event.preventDefault(); refreshAll();">
    <div class="filter-row">
      <div><label>Since</label><input type="datetime-local" name="since"></div>
      <div><label>Until</label><input type="datetime-local" name="until"></div>
      <div><label>Min frame ms</label><input type="number" name="min_frame_ms" value="33" min="0" step="1"></div>
      <div><label>Cause contains</label><input type="text" name="cause" placeholder="e.g. GC pause"></div>
      <div><label>User ID</label><input type="number" name="user_id" min="1"></div>
      <div><label>Version</label><input type="text" name="version" placeholder="v2026.509.0-lazer"></div>
      <div><label>Platform</label>
        <select name="platform">
          <option value="">any</option>
          <option>Windows</option><option>macOS</option><option>Linux</option>
          <option>iOS</option><option>Android</option>
        </select>
      </div>
      <div><label>Screen</label><input type="text" name="screen" placeholder="SongSelectV2"></div>
      <div><label>Device hash</label><input type="text" name="device_hash" placeholder="prefix"></div>
    </div>
    <div class="filter-actions">
      <button type="submit" class="btn">Apply filters</button>
      <button type="button" class="btn ghost" onclick="resetFilters()">Reset</button>
      <a id="csv-btn"  class="btn ghost" target="_blank" rel="noopener">Export CSV</a>
      <a id="json-btn" class="btn ghost" target="_blank" rel="noopener">Export JSON</a>
    </div>
  </form>

  <!-- Charts -->
  <div class="charts-row">
    <div class="chart-card">
      <h3>Top causes</h3>
      <canvas id="chart-causes"></canvas>
    </div>
    <div class="chart-card">
      <h3>Frame_ms distribution</h3>
      <canvas id="chart-histogram"></canvas>
    </div>
    <div class="chart-card">
      <h3>Hiccups over time</h3>
      <canvas id="chart-timeseries"></canvas>
    </div>
  </div>

  <!-- Records table -->
  <div class="records-card">
    <h3>Records</h3>
    <table>
      <thead><tr>
        <th>Captured</th><th>User</th><th>Device</th><th>ms</th>
        <th>Cause</th><th>Screen</th><th>Version</th><th>Plat</th>
      </tr></thead>
      <tbody id="records-body"><tr><td colspan="8" class="empty">Loading…</td></tr></tbody>
    </table>
    <div class="pager">
      <span id="pager-status"></span>
      <button id="pager-more" type="button" class="btn ghost" style="display:none" onclick="loadMore()">Load more →</button>
    </div>
  </div>
</div>

<!-- Detail modal -->
<div class="modal-back" id="modal-back" onclick="if(event.target.id==='modal-back') closeModal();">
  <div class="modal">
    <button class="modal-close" onclick="closeModal()">&times;</button>
    <h3 id="modal-title">Hiccup record</h3>
    <pre id="modal-body">Loading…</pre>
  </div>
</div>

<script>
const BASE = '/api/private/admin/hiccups';

// charts
let causesChart, histogramChart, timeseriesChart;

// pager state
let nextCursor = null;

function getFilters() {
  const f = new FormData(document.getElementById('filters'));
  const obj = {};
  for (const [k, v] of f.entries()) if (v) obj[k] = v;
  return obj;
}

function qs(extra = {}) {
  return new URLSearchParams({...getFilters(), ...extra}).toString();
}

function resetFilters() {
  document.getElementById('filters').reset();
  document.querySelector('input[name=min_frame_ms]').value = 33;
  refreshAll();
}

async function fetchJson(path, params={}) {
  const r = await fetch(`${BASE}${path}?${qs(params)}`, {credentials: 'include'});
  if (!r.ok) throw new Error(`${path} ${r.status}`);
  return r.json();
}

async function refreshAll() {
  await Promise.all([refreshCauses(), refreshHistogram(), refreshTimeseries(), refreshRecords({reset: true})]);
  syncExportLinks();
}

function syncExportLinks() {
  document.getElementById('csv-btn').href  = `${BASE}/export.csv?${qs()}`;
  document.getElementById('json-btn').href = `${BASE}/export.json?${qs()}`;
}

async function refreshCauses() {
  const data = await fetchJson('/causes', {top: 8});
  const labels = data.buckets.map(b => b.cause.length > 32 ? b.cause.slice(0,29)+'…' : b.cause);
  const counts = data.buckets.map(b => b.count);
  if (causesChart) causesChart.destroy();
  const ctx = document.getElementById('chart-causes').getContext('2d');
  causesChart = new Chart(ctx, {
    type: 'bar',
    data: { labels, datasets: [{ data: counts, backgroundColor: '#ff66b3' }] },
    options: {
      indexAxis: 'y',
      plugins: { legend: { display: false }, tooltip: { callbacks: {
        label: c => `${c.formattedValue} hiccups (${data.buckets[c.dataIndex].pct}%)`
      } } },
      scales: { x: { grid: { color: 'rgba(255,255,255,.06)' }, ticks: { color: 'rgba(255,255,255,.5)' } },
                y: { grid: { display: false }, ticks: { color: 'rgba(255,255,255,.7)', font: { size: 11 } } } },
      maintainAspectRatio: false,
    }
  });
}

async function refreshHistogram() {
  const data = await fetchJson('/histogram');
  if (histogramChart) histogramChart.destroy();
  const ctx = document.getElementById('chart-histogram').getContext('2d');
  histogramChart = new Chart(ctx, {
    type: 'bar',
    data: { labels: data.labels, datasets: [{ data: data.counts,
      backgroundColor: ['#69d7ff','#ffd36e','#ff8f9c','#ff66b3','#ff66b3'] }] },
    options: {
      plugins: { legend: { display: false } },
      scales: { x: { grid: { display: false }, ticks: { color: 'rgba(255,255,255,.7)', font: { size: 11 } } },
                y: { grid: { color: 'rgba(255,255,255,.06)' }, ticks: { color: 'rgba(255,255,255,.5)' }, beginAtZero: true } },
      maintainAspectRatio: false,
    }
  });
}

async function refreshTimeseries() {
  // Auto-pick day vs hour based on range
  const f = getFilters();
  let bucket = 'hour';
  if (f.since && f.until) {
    const span = (new Date(f.until) - new Date(f.since)) / 86400000;
    if (span > 7) bucket = 'day';
  }
  const data = await fetchJson('/timeseries', { bucket });
  const labels = data.points.map(p => p.t);
  const counts = data.points.map(p => p.n);
  if (timeseriesChart) timeseriesChart.destroy();
  const ctx = document.getElementById('chart-timeseries').getContext('2d');
  timeseriesChart = new Chart(ctx, {
    type: 'line',
    data: { labels, datasets: [{ data: counts, borderColor: '#69d7ff', backgroundColor: 'rgba(105,215,255,.18)', fill: true, tension: 0.25, borderWidth: 2, pointRadius: 0 }] },
    options: {
      plugins: { legend: { display: false } },
      scales: { x: { grid: { display: false }, ticks: { color: 'rgba(255,255,255,.5)', font: { size: 10 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 } },
                y: { grid: { color: 'rgba(255,255,255,.06)' }, ticks: { color: 'rgba(255,255,255,.5)' }, beginAtZero: true } },
      maintainAspectRatio: false,
    }
  });
}

async function refreshRecords(opts = {}) {
  const params = { limit: 100 };
  if (opts.reset) {
    nextCursor = null;
    document.getElementById('records-body').innerHTML = '<tr><td colspan="8" class="empty">Loading…</td></tr>';
  } else if (nextCursor) {
    params.cursor = nextCursor;
  }
  const data = await fetchJson('/list', params);
  const tbody = document.getElementById('records-body');
  if (opts.reset) tbody.innerHTML = '';
  if (data.items.length === 0 && opts.reset) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty">No hiccups match the current filters.</td></tr>';
  }
  for (const r of data.items) {
    const tr = document.createElement('tr');
    tr.onclick = () => openModal(r.id);
    const fmsClass = r.frame_ms >= 500 ? 'huge' : r.frame_ms >= 200 ? 'bad' : r.frame_ms >= 100 ? 'warn' : '';
    const causeClass = r.likely_cause.includes('GC') ? 'cause-gc' :
                       r.likely_cause.includes('API') ? 'cause-api' :
                       r.likely_cause.includes('stall') ? 'cause-stall' : '';
    tr.innerHTML = `
      <td>${(r.captured_at || '').replace('T', ' ').slice(0, 19)}</td>
      <td>${r.user_id ?? '<span style="color:var(--ink-3)">anon</span>'}</td>
      <td style="color:var(--ink-3); font-size: 11px">${r.device_hash || ''}</td>
      <td class="frame-ms ${fmsClass}">${r.frame_ms.toFixed(1)}</td>
      <td class="cause ${causeClass}">${r.likely_cause}</td>
      <td>${r.current_screen ?? '<span style="color:var(--ink-3)">–</span>'}</td>
      <td>${r.osu_version ?? ''}</td>
      <td>${r.platform ?? ''}</td>`;
    tbody.appendChild(tr);
  }
  nextCursor = data.next_cursor;
  document.getElementById('pager-more').style.display = data.has_more ? 'inline-flex' : 'none';
  document.getElementById('pager-status').textContent =
    `${tbody.querySelectorAll('tr').length} ${data.has_more ? 'rows shown (more available)' : 'rows total'}`;
}

function loadMore() { refreshRecords(); }

async function openModal(id) {
  document.getElementById('modal-title').textContent = `Hiccup #${id}`;
  document.getElementById('modal-body').textContent = 'Loading…';
  document.getElementById('modal-back').classList.add('shown');
  try {
    const r = await fetch(`${BASE}/${id}`, {credentials: 'include'});
    const data = await r.json();
    document.getElementById('modal-body').textContent = JSON.stringify(data, null, 2);
  } catch (e) {
    document.getElementById('modal-body').textContent = `Failed: ${e.message}`;
  }
}

function closeModal() { document.getElementById('modal-back').classList.remove('shown'); }

document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

// Defaults: last 7d
(function initDefaults() {
  const now = new Date();
  const weekAgo = new Date(now - 7*86400000);
  const fmt = d => d.toISOString().slice(0,16);
  document.querySelector('input[name=since]').value = fmt(weekAgo);
  document.querySelector('input[name=until]').value = fmt(now);
})();

refreshAll();
</script>
</body>
</html>
"""
