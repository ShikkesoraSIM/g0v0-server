"""add donations table + supporter loyalty fields on lazer_users

Schema for the Ko-fi (and future Stripe / other-provider) donation flow:

  * `donations` — one row per inbound donation event from any provider.
    Idempotent on (provider, provider_transaction_id) so duplicate
    webhook deliveries are no-ops.
  * `lazer_users.total_supporter_months` — cumulative supporter time
    granted across ALL donations linked to this user. Drives the
    server-side loyalty tier mapping in torii_groups (1+ → supporter,
    6+ → bronze, 12+ → silver, 36+ → gold).
  * `lazer_users.kofi_display_name` — optional override for matching
    inbound Ko-fi `from_name` values to a Torii account when the donor
    forgot to put @username in the message field.

The `donations` table is auditable (provider message id, amount, donor
display name, message, public flag) so the public "supporters" page
can render donor walls without digging through provider dashboards.

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-04-28 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "e6f7a8b9c0d1"
down_revision: str | Sequence[str] | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "donations",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        # Nullable: anonymous / unmatched donations live here until an
        # admin links them via the queue or the donor edits their
        # kofi_display_name and we re-run the matcher.
        sa.Column(
            "user_id",
            sa.BigInteger,
            sa.ForeignKey("lazer_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Provider discriminator — "kofi" today, "stripe" / "paypal_direct"
        # tomorrow without schema changes.
        sa.Column("provider", sa.String(32), nullable=False),
        # Idempotency key from the provider (Ko-fi's kofi_transaction_id
        # for example). Combined with provider for the unique constraint
        # so two providers can't collide if their id namespaces overlap.
        sa.Column("provider_transaction_id", sa.String(128), nullable=False),
        # Some providers also surface a separate message_id used for
        # webhook retry deduping. Optional — kept for audit.
        sa.Column("provider_message_id", sa.String(128), nullable=True),
        # Stored in cents to avoid float drift. 100 = $1.00.
        sa.Column("amount_cents", sa.Integer, nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("is_recurring", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("is_first_recurring", sa.Boolean, nullable=False, server_default=sa.false()),
        # Provider-specific tier name (Ko-fi memberships have these).
        sa.Column("tier_name", sa.String(64), nullable=True),
        # Donor name AS THEY ENTERED IT on the provider — used both for
        # matching (vs kofi_display_name) and for display on the public
        # supporters page when the donor opted to be public.
        sa.Column("donor_display_name", sa.String(128), nullable=True),
        sa.Column("donor_message", sa.Text, nullable=True),
        # Mirrors the provider's `is_public` field — when false, the
        # donor message must NOT be displayed in any public list.
        sa.Column("donor_message_is_public", sa.Boolean, nullable=False, server_default=sa.false()),
        # Optional, stored encrypted in some providers — we keep the
        # field for receipts but never display.
        sa.Column("donor_email", sa.String(254), nullable=True),
        # How many months of supporter time this donation contributed.
        # = floor(amount_usd / dollars_per_month). 0 for shop orders or
        # other types that don't grant supporter status.
        sa.Column("months_granted", sa.Integer, nullable=False, server_default="0"),
        sa.Column("received_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "provider",
            "provider_transaction_id",
            name="uq_donations_provider_txn",
        ),
    )
    op.create_index("ix_donations_user_id", "donations", ["user_id"])
    op.create_index("ix_donations_received_at", "donations", ["received_at"])
    op.create_index("ix_donations_provider", "donations", ["provider"])

    op.add_column(
        "lazer_users",
        sa.Column(
            "total_supporter_months",
            sa.Integer,
            nullable=False,
            server_default="0",
            comment=(
                "Cumulative supporter months across all donations for this user. "
                "Drives the loyalty-tier group resolution in torii_groups "
                "(1/6/12/36 → supporter / bronze / silver / gold)."
            ),
        ),
    )
    op.add_column(
        "lazer_users",
        sa.Column(
            "kofi_display_name",
            sa.String(length=128),
            nullable=True,
            comment=(
                "Optional override matching the user's Ko-fi 'from_name' to this "
                "Torii account. Used by the webhook matcher when the donor's "
                "Ko-fi display name differs from their Torii username and they "
                "forgot to include @username in the message field."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("lazer_users", "kofi_display_name")
    op.drop_column("lazer_users", "total_supporter_months")
    op.drop_index("ix_donations_provider", table_name="donations")
    op.drop_index("ix_donations_received_at", table_name="donations")
    op.drop_index("ix_donations_user_id", table_name="donations")
    op.drop_table("donations")
