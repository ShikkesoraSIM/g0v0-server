"""POST /api/v2/torii/hiccup-reports — batch ingest for client-side hiccup captures.

Backed by ``app.database.torii_hiccup_report.ToriiHiccupReport``. The
client (osu! Torii) writes hiccup records to a local JSONL file
unconditionally when its hiccup logger toggle is ON, and *additionally*
batches them up and POSTs them here when the user has flipped the
"Share with Torii devs" sub-toggle.

Design constraints
------------------
* **Anonymous-friendly.** The most useful hiccups to receive are the
  ones that fire on the login screen / before login completes — exactly
  when the client has no auth token. So this endpoint accepts unauthed
  POSTs and stores ``user_id = NULL`` for those rows. The
  ``device_hash`` field is then the only identity tie. Authenticated
  POSTs additionally fill ``user_id`` so the dashboard can group by
  username for known users.

* **Batch-only.** A single hiccup is rare; in practice a session that
  lags will produce 5–50 records before the batch is shipped (every 30 s
  client-side). Accepting a batch keeps the request count cheap on the
  ingest side and lets the rate limiter target *batches*, not records.

* **Bounded everything.** Max records per batch is hard-capped at 50; any
  extra are silently dropped (the client can split into multiple
  requests). Each individual field has length / range bounds. We trust
  the client's clock for ``captured_at`` (the dashboard uses
  ``received_at`` for ingest ordering anyway).

* **No 4xx for a partial batch.** If 47 records validate and 3 fail, we
  insert the 47 and return ``{accepted: 47, dropped: 3}``. Returning
  400 for a single bad record would force the client to retry the whole
  batch, which on a flaky network multiplies traffic.

Why not put this under ``/admin/...``?
The dashboard live there (``/admin/hiccups/...``); ingest stays under the
public ``/api/v2/torii/...`` namespace because that's what the client
already knows how to talk to (same base URL as briefing / pulse). Auth
gating happens per-endpoint.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends
from pydantic import BaseModel, Field, field_validator

from app.database.torii_hiccup_report import ToriiHiccupReport
from app.database.user import User
from app.dependencies.database import Database
from app.dependencies.user import get_optional_user

from .router import router


logger = logging.getLogger(__name__)


# ── Tunables ────────────────────────────────────────────────────────────────

# Hard cap on a single batch. Clients should split larger backlogs into
# multiple requests; the rate limiter then naturally throttles them.
MAX_RECORDS_PER_BATCH = 50

# How far in the past a record's captured_at can be. Backlogs that span
# longer than this (e.g. user offline for a week then comes back) are
# clamped to "received_at" for storage so the dashboard's time-range
# filters don't have to special-case ancient outliers.
MAX_CAPTURED_AGE = timedelta(days=14)

# Sanity bounds on frame_ms. Anything outside this is almost certainly
# bogus (negative time or a literal hour) and is rejected.
MIN_FRAME_MS = 1.0
MAX_FRAME_MS = 60_000.0  # 60 seconds — already absurd, but lets a deadlocked client send its eventual recovery


# ── Pydantic request schemas ────────────────────────────────────────────────


class RecentEventDTO(BaseModel):
    """One breadcrumb in the client's recent-events ring buffer."""

    kind: str = Field(max_length=32)
    detail: str = Field(max_length=256)
    at_utc: datetime


class HiccupRecordDTO(BaseModel):
    """One captured frame stall as the client serialises it.

    Field names match the JSONL schema the client writes to disk, so a
    user can copy-paste a line out of their local capture and POST it
    here unchanged.
    """

    # Required core
    ts: datetime = Field(description="Wall-clock UTC time on the client when the hiccup fired.")
    frame_ms: float = Field(ge=MIN_FRAME_MS, le=MAX_FRAME_MS)
    thread: str = Field(max_length=16)
    likely_cause: str = Field(max_length=128)

    # Optional context
    api_state: str | None = Field(default=None, max_length=16)
    logged_in: bool | None = None
    current_screen: str | None = Field(default=None, max_length=64)
    visible_overlays: list[str] | None = Field(default=None, max_length=32)

    # GC / memory
    gen0_count: int | None = Field(default=None, ge=0)
    gen1_count: int | None = Field(default=None, ge=0)
    gen2_count: int | None = Field(default=None, ge=0)
    gen0_delta: int | None = Field(default=None, ge=0, le=10_000)
    gen1_delta: int | None = Field(default=None, ge=0, le=10_000)
    gen2_delta: int | None = Field(default=None, ge=0, le=10_000)
    total_memory_mb: int | None = Field(default=None, ge=0, le=131_072)

    # Activity context
    recent_events: list[RecentEventDTO] | None = Field(default=None, max_length=32)

    @field_validator("ts")
    @classmethod
    def _ensure_utc(cls, v: datetime) -> datetime:
        # Strip tz so SQLAlchemy's DateTime (no-tz) accepts it. Trusting the
        # client to send UTC; the dashboard documents this in its tooltip.
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        return v


class HiccupBatchRequest(BaseModel):
    """Body of POST /api/v2/torii/hiccup-reports.

    Header fields (session_id, device_hash, build / platform info) are
    promoted out of the per-record DTO because they're constant across
    every record in a single batch — saving ~150 bytes per record over
    the wire.
    """

    session_id: str = Field(max_length=32)
    device_hash: str = Field(min_length=16, max_length=64)
    osu_version: str | None = Field(default=None, max_length=32)
    platform: str | None = Field(default=None, max_length=32)
    cpu_arch: str | None = Field(default=None, max_length=16)

    records: list[HiccupRecordDTO] = Field(min_length=1, max_length=MAX_RECORDS_PER_BATCH)


class HiccupBatchResponse(BaseModel):
    accepted: int
    dropped: int


# ── Endpoint ────────────────────────────────────────────────────────────────


@router.post(
    "/torii/hiccup-reports",
    tags=["Torii"],
    response_model=HiccupBatchResponse,
    name="Submit Torii hiccup-report batch",
    description=(
        "Ingest a batch of frame-stall captures from the Torii client's hiccup logger. "
        "Auth is optional: authenticated POSTs link to the user_id, anonymous POSTs "
        "store user_id=NULL and rely on device_hash for identity. Rejects invalid records "
        "individually (returns accepted/dropped counts) rather than failing the whole batch."
    ),
)
async def submit_hiccup_reports(
    body: HiccupBatchRequest,
    session: Database,
    current_user: Annotated[User | None, Depends(get_optional_user)] = None,
) -> HiccupBatchResponse:
    accepted = 0
    dropped = 0
    now_utc = datetime.utcnow()
    cutoff = now_utc - MAX_CAPTURED_AGE

    user_id = current_user.id if current_user is not None else None

    rows: list[ToriiHiccupReport] = []
    for record in body.records:
        # Drop obviously-bogus future timestamps (>1 h ahead — clock skew is
        # OK; fabricated future stamps are not).
        if record.ts > now_utc + timedelta(hours=1):
            dropped += 1
            continue

        # Clamp very old timestamps to MAX_CAPTURED_AGE so the dashboard
        # doesn't show events from "user's old session 3 weeks ago" as if
        # they happened today. Drop instead of clamp would silently lose
        # legitimate offline-backlog data; clamping preserves the row but
        # caps how far back it appears.
        captured_at = record.ts if record.ts >= cutoff else cutoff

        rows.append(
            ToriiHiccupReport(
                user_id=user_id,
                device_hash=body.device_hash,
                session_id=body.session_id,
                captured_at=captured_at,
                frame_ms=record.frame_ms,
                thread=record.thread,
                likely_cause=record.likely_cause,
                api_state=record.api_state,
                logged_in=record.logged_in,
                current_screen=record.current_screen,
                visible_overlays=record.visible_overlays,
                gen0_count=record.gen0_count,
                gen1_count=record.gen1_count,
                gen2_count=record.gen2_count,
                gen0_delta=record.gen0_delta,
                gen1_delta=record.gen1_delta,
                gen2_delta=record.gen2_delta,
                total_memory_mb=record.total_memory_mb,
                recent_events=(
                    [event.model_dump(mode="json") for event in record.recent_events]
                    if record.recent_events is not None
                    else None
                ),
                osu_version=body.osu_version,
                platform=body.platform,
                cpu_arch=body.cpu_arch,
            )
        )
        accepted += 1

    if rows:
        session.add_all(rows)
        await session.commit()

    if dropped:
        logger.info(
            "torii.hiccup ingest: dropped=%d accepted=%d device=%s session=%s",
            dropped, accepted, body.device_hash[:8], body.session_id,
        )

    return HiccupBatchResponse(accepted=accepted, dropped=dropped)
