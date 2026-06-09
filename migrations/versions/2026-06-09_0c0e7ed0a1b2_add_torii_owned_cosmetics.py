"""add torii owned cosmetics (server-authoritative ownership)

Records which cosmetics a user owns, so purchases (and later grants) persist
server-side and across devices instead of living only in the client config. A
unique (user_id, cosmetic_id) keeps ownership idempotent.

Idempotent so a re-run / interrupted migration is safe.

Revision ID: 0c0e7ed0a1b2
Revises: 91f7ac0de5b0
Create Date: 2026-06-09 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0c0e7ed0a1b2"
down_revision: str | Sequence[str] | None = "91f7ac0de5b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "torii_owned_cosmetics" not in tables:
        op.create_table(
            "torii_owned_cosmetics",
            sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
            sa.Column(
                "user_id",
                sa.BigInteger,
                sa.ForeignKey("lazer_users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("cosmetic_id", sa.String(128), nullable=False),
            sa.Column("source", sa.String(32), nullable=True),
            sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_torii_owned_cosmetics_user_id", "torii_owned_cosmetics", ["user_id"])
        op.create_unique_constraint(
            "uq_torii_owned_user_cosmetic", "torii_owned_cosmetics", ["user_id", "cosmetic_id"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "torii_owned_cosmetics" in set(inspector.get_table_names()):
        op.drop_table("torii_owned_cosmetics")
