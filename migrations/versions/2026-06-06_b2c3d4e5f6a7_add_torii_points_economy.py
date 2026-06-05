"""add torii points economy (balance + ledger + access codes)

The earned-only points system:

  * `lazer_users.points` — cached running balance. Earned by playing (daily
    play + streak, new top plays, daily challenge, medals) and by redeeming
    staff-issued access codes. Spent in the cosmetics store. NEVER buyable.
  * `torii_point_transactions` — append-only ledger; the source of truth for
    balances and history. Every earn/spend is one row.
  * `torii_access_codes` — redeemable codes that grant points (bug-report
    rewards, event payouts, giveaways).
  * `torii_access_code_redemptions` — who redeemed which code; UNIQUE
    (code_id, user_id) is the hard guarantee a code can't be redeemed twice
    by the same user.

Idempotent so a re-run / interrupted migration is safe.

Revision ID: b2c3d4e5f6a7
Revises: f6a7b8c9d0e1
Create Date: 2026-06-06 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "b2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # 1) balance column on lazer_users
    if "lazer_users" in tables:
        columns = {c["name"] for c in inspector.get_columns("lazer_users")}
        if "points" not in columns:
            op.add_column(
                "lazer_users",
                sa.Column(
                    "points",
                    sa.Integer,
                    nullable=False,
                    server_default="0",
                    comment=(
                        "Earned-only Torii points balance (cached running total; "
                        "authoritative history is torii_point_transactions). "
                        "Never buyable with money."
                    ),
                ),
            )

    # 2) ledger
    if "torii_point_transactions" not in tables:
        op.create_table(
            "torii_point_transactions",
            sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
            sa.Column(
                "user_id",
                sa.BigInteger,
                sa.ForeignKey("lazer_users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            # > 0 earned, < 0 spent.
            sa.Column("amount", sa.Integer, nullable=False),
            sa.Column("reason", sa.String(32), nullable=False),
            # Free-form traceability ref: score_id, achievement_id, code, etc.
            sa.Column("ref", sa.String(128), nullable=True),
            # Idempotency hint (pre-checked in the service; indexed, not unique).
            sa.Column("idempotency_key", sa.String(160), nullable=True),
            sa.Column("balance_after", sa.Integer, nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_torii_point_transactions_user_id", "torii_point_transactions", ["user_id"])
        op.create_index("ix_torii_point_transactions_created_at", "torii_point_transactions", ["created_at"])
        op.create_index(
            "ix_torii_point_transactions_idempotency_key", "torii_point_transactions", ["idempotency_key"]
        )
        # Composite for the per-user history endpoint (newest first).
        op.create_index("ix_torii_point_tx_user", "torii_point_transactions", ["user_id", "created_at"])

    # 3) access codes
    if "torii_access_codes" not in tables:
        op.create_table(
            "torii_access_codes",
            sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
            sa.Column("code", sa.String(64), nullable=False),
            sa.Column("amount", sa.Integer, nullable=False, server_default="0"),
            sa.Column("note", sa.String(256), nullable=True),
            sa.Column("max_uses", sa.Integer, nullable=False, server_default="1"),
            sa.Column("uses", sa.Integer, nullable=False, server_default="0"),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_by", sa.BigInteger, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_torii_access_codes_code", "torii_access_codes", ["code"], unique=True)

    # 4) redemptions (one per code+user)
    if "torii_access_code_redemptions" not in tables:
        op.create_table(
            "torii_access_code_redemptions",
            sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
            sa.Column(
                "code_id",
                sa.BigInteger,
                sa.ForeignKey("torii_access_codes.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "user_id",
                sa.BigInteger,
                sa.ForeignKey("lazer_users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("code_id", "user_id", name="uq_torii_access_code_user"),
        )
        op.create_index(
            "ix_torii_access_code_redemptions_code_id", "torii_access_code_redemptions", ["code_id"]
        )
        op.create_index(
            "ix_torii_access_code_redemptions_user_id", "torii_access_code_redemptions", ["user_id"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "torii_access_code_redemptions" in tables:
        op.drop_table("torii_access_code_redemptions")
    if "torii_access_codes" in tables:
        op.drop_table("torii_access_codes")
    if "torii_point_transactions" in tables:
        op.drop_table("torii_point_transactions")
    if "lazer_users" in tables:
        columns = {c["name"] for c in inspector.get_columns("lazer_users")}
        if "points" in columns:
            op.drop_column("lazer_users", "points")
