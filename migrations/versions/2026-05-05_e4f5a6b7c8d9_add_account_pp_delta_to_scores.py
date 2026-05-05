"""add account_pp_delta column to scores

Snapshot of how much this score changed the user's overall account-level
pp (i.e. ``UserStatistics.pp``) — captured at submission time so reads
are O(1). Compared to recomputing the marginal weighted contribution
on every pulse-endpoint poll, this trades 8 bytes / row of storage for
zero extra queries on the read path.

Default 0.0 + non-nullable. Existing scores backfill to 0.0 — the pulse
widget gates the secondary "+Xpp to total" line on ``delta >= 1.0``,
so historical scores simply render the play_pp line and skip the delta
suffix (the desired behaviour).

Revision ID: e4f5a6b7c8d9
Revises: c1d2e3f4a5b6
Create Date: 2026-05-05 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "e4f5a6b7c8d9"
down_revision: str | Sequence[str] | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "scores",
        sa.Column(
            "account_pp_delta",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0"),
            comment=(
                "Change in the user's UserStatistics.pp caused by this score, "
                "captured at submission time (pp_after - pp_before). Always "
                ">= 0; a score that didn't beat the user's previous best on "
                "the same beatmap stores 0. Read by the server-pulse endpoint "
                "for 'play_pp + (+Xpp to total)' UI."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("scores", "account_pp_delta")
