"""Torii hiccup-report storage.

Backs the opt-in client-side hiccup logger:
the client (osu! Torii) records frames slower than ~33 ms locally as
JSONL, and — if the user has also opted into "Share with Torii devs" —
batch-uploads them to ``POST /api/v2/torii/hiccup-reports``. This module
defines the SQL table the server writes those records into.

Schema design notes
-------------------
* **Identity is dual.** ``user_id`` is set when the client was logged in
  at the time the hiccup was captured; otherwise the row is anonymous and
  only ``device_hash`` ties it back to the same install. We deliberately
  do not require ``user_id`` so anonymous bug reports still land — a user
  hitting login-screen freezes literally cannot be logged in to send the
  report.

* **No nested table for events / overlays.** ``recent_events`` and
  ``visible_overlays`` are both small (≤ 16 entries) and only ever read as
  a unit together with the row, so we store them as JSON columns rather
  than splitting into joined tables. Queries that filter on event content
  use ``JSON_CONTAINS`` / virtual columns when needed; the dashboard's
  default views aggregate on indexed columns only.

* **Both `captured_at` and `received_at`.** Captured-at is the wall-clock
  time on the client when the hiccup happened (UTC, sent by the client);
  received-at is when the server inserted the row. The gap between them
  reveals upload-delay patterns (e.g. a user offline for hours then
  bursting their backlog).

* **Indexes are deliberate, not exhaustive.** The dashboard's hot
  queries are: per-user-recent, per-cause-recent, per-version,
  threshold-filtered ("show me everything > 200 ms"). Each gets an
  index. Anything else gets table-scanned — fine for a low-volume admin
  dashboard.
"""

from datetime import datetime

from app.utils import utcnow

from sqlalchemy import Boolean, Column, DateTime, Float, Index, Integer, JSON, String
from sqlmodel import BigInteger, Field, ForeignKey, SQLModel


class ToriiHiccupReport(SQLModel, table=True):
    """One captured frame-stall record uploaded by a Torii client."""

    __tablename__: str = "torii_hiccup_reports"
    __table_args__ = (
        # Per-user timeline (admin dashboard "show me what user X reported")
        Index("ix_torii_hiccup_reports_user", "user_id", "captured_at"),
        # Per-device timeline for anonymous reports
        Index("ix_torii_hiccup_reports_device", "device_hash", "captured_at"),
        # Group records from the same game session together (a single
        # session_id pre-batched on the client end)
        Index("ix_torii_hiccup_reports_session", "session_id"),
        # Cause aggregation ("which cause is trending?")
        Index("ix_torii_hiccup_reports_cause", "likely_cause", "captured_at"),
        # Recent-firehose view ordered by ingest time
        Index("ix_torii_hiccup_reports_received", "received_at"),
        # "What broke in v2026.509.0?" version-pinned regression hunting
        Index("ix_torii_hiccup_reports_version", "osu_version", "captured_at"),
        # Threshold queries "show me only the >200 ms stalls"
        Index("ix_torii_hiccup_reports_frame_ms", "frame_ms"),
    )

    id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, primary_key=True, autoincrement=True),
    )

    # ── Identity ────────────────────────────────────────────────────────

    user_id: int | None = Field(
        default=None,
        sa_column=Column(
            BigInteger,
            ForeignKey("lazer_users.id", ondelete="SET NULL"),
            nullable=True,
            comment="Logged-in user at capture time. NULL for anonymous reports (login-screen freezes, opted-out users, etc.).",
        ),
    )
    device_hash: str = Field(
        sa_column=Column(
            String(64),
            nullable=False,
            comment="SHA-256 of the client's machine identity (Win32 MachineGuid / /etc/machine-id / iOS identifierForVendor). Stable per install; lets us correlate reports from the same machine across user logouts.",
        ),
    )
    session_id: str = Field(
        sa_column=Column(
            String(32),
            nullable=False,
            comment="Per-game-session ID generated on the client when the hiccup logger starts. Lets us cluster all hiccups from one play session together in the dashboard.",
        ),
    )

    # ── Timing ──────────────────────────────────────────────────────────

    captured_at: datetime = Field(
        sa_column=Column(
            DateTime,
            nullable=False,
            comment="Wall-clock UTC time on the client when the hiccup happened. Trusted from the client; obvious skew (>1h vs server) is fine — we use received_at for ingest ordering.",
        ),
    )
    received_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(
            DateTime,
            nullable=False,
            comment="Server-side insert time. The (received_at - captured_at) gap reveals how long batches were buffered on the client (e.g. offline for hours).",
        ),
    )

    # ── The hiccup itself ───────────────────────────────────────────────

    frame_ms: float = Field(
        sa_column=Column(
            Float,
            nullable=False,
            comment="Wall-clock ms the offending frame took. Threshold for capture is ~33 ms (sub-30 fps) by default but the client may send anything; the dashboard re-filters on this column.",
        ),
    )
    thread: str = Field(
        sa_column=Column(
            String(16),
            nullable=False,
            comment="Which framework thread exceeded the threshold. v1 always 'Update' (only the update thread is instrumented client-side); kept widened for future Draw/Audio expansion.",
        ),
    )
    likely_cause: str = Field(
        sa_column=Column(
            String(128),
            nullable=False,
            comment="Heuristic guess from the client at what caused the stall (e.g. 'Gen2 GC pause', 'API state changed to Offline 12 ms ago'). Used as the default group-by axis on the dashboard.",
        ),
    )

    # ── Context ─────────────────────────────────────────────────────────

    api_state: str | None = Field(
        default=None,
        sa_column=Column(String(16), nullable=True, comment="APIState bindable value at capture time. Online / Offline / Connecting / etc."),
    )
    logged_in: bool | None = Field(
        default=None,
        sa_column=Column(Boolean, nullable=True, comment="api.IsLoggedIn at capture time. NULL only if the client couldn't determine."),
    )
    current_screen: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True, comment="ScreenStack.CurrentScreen type name at capture time. NULL until the client wires screen events."),
    )
    visible_overlays: list[str] | None = Field(
        default=None,
        sa_column=Column(JSON, nullable=True, comment="Type names of any visible OsuFocusedOverlayContainer at capture time. NULL until wired."),
    )

    # ── GC + memory at capture time ─────────────────────────────────────

    gen0_count: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    gen1_count: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    gen2_count: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    gen0_delta: int | None = Field(default=None, sa_column=Column(Integer, nullable=True, comment="Number of Gen-0 collections that fired during the offending frame. Non-zero values are a strong signal that GC caused the stall."))
    gen1_delta: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    gen2_delta: int | None = Field(default=None, sa_column=Column(Integer, nullable=True, comment="Gen2 collections during the frame. Even one is a multi-ms stall — basically always the cause when set."))
    total_memory_mb: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))

    # ── Activity context ────────────────────────────────────────────────

    recent_events: list[dict] | None = Field(
        default=None,
        sa_column=Column(
            JSON,
            nullable=True,
            comment="Snapshot of the client-side ring buffer of recent breadcrumb events at capture time (max 16 entries). Each entry: {kind, detail, at_utc}. Most useful field on the row — tells you what was happening just before the stall.",
        ),
    )

    # ── Build / platform ────────────────────────────────────────────────

    osu_version: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True, comment="osu! Torii client version (the v2026.MMDD.N-lazer tag). Lets us pin regressions to a specific release."),
    )
    platform: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True, comment="OS family — Windows / macOS / Linux / iOS / Android. Useful for mobile-only regression hunting."),
    )
    cpu_arch: str | None = Field(
        default=None,
        sa_column=Column(String(16), nullable=True, comment="x64 / arm64 / x86. Useful for catching ARM-specific JIT pessimisations or arm64-only regressions."),
    )
