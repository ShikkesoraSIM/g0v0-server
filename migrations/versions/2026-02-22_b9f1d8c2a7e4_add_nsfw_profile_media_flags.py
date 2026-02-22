"""add nsfw profile media flags

Revision ID: b9f1d8c2a7e4
Revises: f4a2c9d1e6b7
Create Date: 2026-02-22 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "b9f1d8c2a7e4"
down_revision: str | Sequence[str] | None = "f4a2c9d1e6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "lazer_users" in tables:
        user_columns = {col["name"] for col in inspector.get_columns("lazer_users")}
        if "avatar_nsfw" not in user_columns:
            op.add_column(
                "lazer_users",
                sa.Column("avatar_nsfw", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            )
        if "cover_nsfw" not in user_columns:
            op.add_column(
                "lazer_users",
                sa.Column("cover_nsfw", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            )

    pref_table = "userpreference" if "userpreference" in tables else "user_preference" if "user_preference" in tables else None
    if pref_table is not None:
        pref_columns = {col["name"] for col in inspector.get_columns(pref_table)}
        if "profile_media_show_nsfw" not in pref_columns:
            op.add_column(
                pref_table,
                sa.Column("profile_media_show_nsfw", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            )
        # Safety backfill for legacy rows: default is OFF for everyone.
        op.execute(sa.text(f"UPDATE {pref_table} SET profile_media_show_nsfw = 0 WHERE profile_media_show_nsfw IS NULL"))

    # Safety backfill for legacy rows: profile media flags default OFF.
    if "lazer_users" in tables:
        op.execute(sa.text("UPDATE lazer_users SET avatar_nsfw = 0 WHERE avatar_nsfw IS NULL"))
        op.execute(sa.text("UPDATE lazer_users SET cover_nsfw = 0 WHERE cover_nsfw IS NULL"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    pref_table = "userpreference" if "userpreference" in tables else "user_preference" if "user_preference" in tables else None
    if pref_table is not None:
        pref_columns = {col["name"] for col in inspector.get_columns(pref_table)}
        if "profile_media_show_nsfw" in pref_columns:
            op.drop_column(pref_table, "profile_media_show_nsfw")

    if "lazer_users" in tables:
        user_columns = {col["name"] for col in inspector.get_columns("lazer_users")}
        if "cover_nsfw" in user_columns:
            op.drop_column("lazer_users", "cover_nsfw")
        if "avatar_nsfw" in user_columns:
            op.drop_column("lazer_users", "avatar_nsfw")
