"""add torii gifts (staff-sent points / cosmetic gifts)

A gift is a staff-queued reward for a specific player, claimed after a play.
One row per gift; claimed_at guards against double-claim.

Idempotent so a re-run / interrupted migration is safe.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-09 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "e5f6a7b8c9d0"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "torii_gifts" not in tables:
        op.create_table(
            "torii_gifts",
            sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
            sa.Column(
                "recipient_id",
                sa.BigInteger,
                sa.ForeignKey("lazer_users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("points", sa.Integer, nullable=False, server_default="0"),
            sa.Column("grant_cosmetics", sa.String(1024), nullable=True),
            sa.Column("message", sa.String(256), nullable=True),
            sa.Column("sender", sa.String(64), nullable=True),
            sa.Column("created_by", sa.BigInteger, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_torii_gifts_recipient_id", "torii_gifts", ["recipient_id"])
        op.create_index("ix_torii_gifts_created_at", "torii_gifts", ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "torii_gifts" in tables:
        op.drop_table("torii_gifts")
