"""add torii_hiccup_reports table

Storage for the opt-in client-side hiccup logger. Each row is one
captured frame stall (>= ~33 ms) uploaded by a Torii client, with
context (API state, GC stats, recent breadcrumb events, build info) so
the admin dashboard can correlate hiccups across users / versions /
platforms.

See app/database/torii_hiccup_report.py for the SQLModel definition +
the rationale on each column.

Revision ID: d1c0a51500b1
Revises: e4f5a6b7c8d9
Create Date: 2026-05-09 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "d1c0a51500b1"
down_revision: str | Sequence[str] | None = "e4f5a6b7c8d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "torii_hiccup_reports",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        # Identity
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("lazer_users.id", ondelete="SET NULL"),
            nullable=True,
            comment="Logged-in user at capture time. NULL for anonymous reports.",
        ),
        sa.Column(
            "device_hash",
            sa.String(length=64),
            nullable=False,
            comment="SHA-256 of the client's machine identity. Stable per install.",
        ),
        sa.Column(
            "session_id",
            sa.String(length=32),
            nullable=False,
            comment="Per-game-session ID. Clusters all hiccups from one session.",
        ),
        # Timing
        sa.Column(
            "captured_at",
            sa.DateTime(),
            nullable=False,
            comment="Wall-clock UTC time on the client when the hiccup happened.",
        ),
        sa.Column(
            "received_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            comment="Server-side insert time. Reveals upload-delay patterns.",
        ),
        # The hiccup itself
        sa.Column("frame_ms", sa.Float(), nullable=False),
        sa.Column("thread", sa.String(length=16), nullable=False),
        sa.Column(
            "likely_cause",
            sa.String(length=128),
            nullable=False,
            comment="Heuristic guess from the client.",
        ),
        # Context
        sa.Column("api_state", sa.String(length=16), nullable=True),
        sa.Column("logged_in", sa.Boolean(), nullable=True),
        sa.Column("current_screen", sa.String(length=64), nullable=True),
        sa.Column("visible_overlays", sa.JSON(), nullable=True),
        # GC / memory
        sa.Column("gen0_count", sa.Integer(), nullable=True),
        sa.Column("gen1_count", sa.Integer(), nullable=True),
        sa.Column("gen2_count", sa.Integer(), nullable=True),
        sa.Column("gen0_delta", sa.Integer(), nullable=True),
        sa.Column("gen1_delta", sa.Integer(), nullable=True),
        sa.Column("gen2_delta", sa.Integer(), nullable=True),
        sa.Column("total_memory_mb", sa.Integer(), nullable=True),
        # Activity context
        sa.Column(
            "recent_events",
            sa.JSON(),
            nullable=True,
            comment="Ring-buffer snapshot of breadcrumb events at capture time.",
        ),
        # Build / platform
        sa.Column("osu_version", sa.String(length=32), nullable=True),
        sa.Column("platform", sa.String(length=32), nullable=True),
        sa.Column("cpu_arch", sa.String(length=16), nullable=True),
    )

    # Indexes — see torii_hiccup_report.py for the rationale on each.
    op.create_index(
        "ix_torii_hiccup_reports_user",
        "torii_hiccup_reports",
        ["user_id", "captured_at"],
    )
    op.create_index(
        "ix_torii_hiccup_reports_device",
        "torii_hiccup_reports",
        ["device_hash", "captured_at"],
    )
    op.create_index(
        "ix_torii_hiccup_reports_session",
        "torii_hiccup_reports",
        ["session_id"],
    )
    op.create_index(
        "ix_torii_hiccup_reports_cause",
        "torii_hiccup_reports",
        ["likely_cause", "captured_at"],
    )
    op.create_index(
        "ix_torii_hiccup_reports_received",
        "torii_hiccup_reports",
        ["received_at"],
    )
    op.create_index(
        "ix_torii_hiccup_reports_version",
        "torii_hiccup_reports",
        ["osu_version", "captured_at"],
    )
    op.create_index(
        "ix_torii_hiccup_reports_frame_ms",
        "torii_hiccup_reports",
        ["frame_ms"],
    )


def downgrade() -> None:
    op.drop_index("ix_torii_hiccup_reports_frame_ms", table_name="torii_hiccup_reports")
    op.drop_index("ix_torii_hiccup_reports_version", table_name="torii_hiccup_reports")
    op.drop_index("ix_torii_hiccup_reports_received", table_name="torii_hiccup_reports")
    op.drop_index("ix_torii_hiccup_reports_cause", table_name="torii_hiccup_reports")
    op.drop_index("ix_torii_hiccup_reports_session", table_name="torii_hiccup_reports")
    op.drop_index("ix_torii_hiccup_reports_device", table_name="torii_hiccup_reports")
    op.drop_index("ix_torii_hiccup_reports_user", table_name="torii_hiccup_reports")
    op.drop_table("torii_hiccup_reports")
