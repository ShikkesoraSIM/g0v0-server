"""add equipped_name_colour column to lazer_users

Stores each user's chosen BOUGHT name-colour cosmetic id (e.g. "name-crimson")
so it can be broadcast to every client, the same way equipped_aura already is.
NULL means "no bought colour equipped" — clients fall back to the group/role
colour. Validated against ownership when set (PATCH /me/equipped-name-colour),
so the stored value is always a cosmetic the user owns.

Idempotent so a re-run / interrupted migration is safe.

Revision ID: c010c0a1b2d3
Revises: 0c0e7ed0a1b2
Create Date: 2026-06-10 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c010c0a1b2d3"
down_revision: str | Sequence[str] | None = "0c0e7ed0a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("lazer_users")}

    if "equipped_name_colour" not in columns:
        op.add_column(
            "lazer_users",
            sa.Column(
                "equipped_name_colour",
                sa.String(length=64),
                nullable=True,
                comment=(
                    "Bought name-colour cosmetic id the user has equipped (e.g. "
                    "'name-crimson'), broadcast to all clients. NULL = none equipped "
                    "(clients fall back to the group/role colour)."
                ),
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("lazer_users")}

    if "equipped_name_colour" in columns:
        op.drop_column("lazer_users", "equipped_name_colour")
