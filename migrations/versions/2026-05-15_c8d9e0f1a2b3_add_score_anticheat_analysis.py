"""add score_anticheat_analysis table

Cached anti-cheat verdict per score, one row per score, upserted on
every call to the external detection service. The admin replay
browser reads this table; without it, the panel would have to
re-analyse every replay to display its status.

Revision ID: b3c4d5e6f7a8
Revises: a7b8c9d0e1f2
Create Date: 2026-05-15 22:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c8d9e0f1a2b3"
down_revision: str | Sequence[str] | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "score_anticheat_analysis",
        sa.Column(
            "score_id",
            sa.BigInteger(),
            sa.ForeignKey("scores.id", ondelete="CASCADE"),
            primary_key=True,
            autoincrement=False,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("trust_factor_applied", sa.Float(), nullable=False, server_default="50"),
        sa.Column("detectors_fired", sa.JSON(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("replay_was_available", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("analyzer_version", sa.String(length=64), nullable=False, server_default="1"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("analyzed_at", sa.DateTime(), nullable=False),
    )
    op.create_index("idx_sa_analysis_user", "score_anticheat_analysis", ["user_id"])
    op.create_index(
        "idx_sa_analysis_verdict",
        "score_anticheat_analysis",
        ["verdict", "analyzed_at"],
    )
    op.create_index(
        "idx_sa_analysis_user_time",
        "score_anticheat_analysis",
        ["user_id", "analyzed_at"],
    )
    op.create_index("idx_sa_analysis_time", "score_anticheat_analysis", ["analyzed_at"])


def downgrade() -> None:
    op.drop_index("idx_sa_analysis_time", table_name="score_anticheat_analysis")
    op.drop_index("idx_sa_analysis_user_time", table_name="score_anticheat_analysis")
    op.drop_index("idx_sa_analysis_verdict", table_name="score_anticheat_analysis")
    op.drop_index("idx_sa_analysis_user", table_name="score_anticheat_analysis")
    op.drop_table("score_anticheat_analysis")
