"""add profile_media_reviews

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-04 00:00:01.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "e5f6a7b8c9d0"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_NAME = "profile_media_reviews"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if TABLE_NAME not in set(inspector.get_table_names()):
        op.create_table(
            TABLE_NAME,
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("media_type", sa.String(length=16), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("storage_path", sa.String(length=512), nullable=True),
            sa.Column("filehash", sa.String(length=128), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("is_current", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("reviewed_at", sa.DateTime(), nullable=True),
            sa.Column("reviewed_by_id", sa.BigInteger(), nullable=True),
        )

    indexes = {index["name"] for index in inspector.get_indexes(TABLE_NAME)}
    wanted_indexes = {
        "ix_profile_media_reviews_user_id": ["user_id"],
        "ix_profile_media_reviews_media_type": ["media_type"],
        "ix_profile_media_reviews_status": ["status"],
        "ix_profile_media_reviews_is_current": ["is_current"],
        "ix_profile_media_reviews_created_at": ["created_at"],
    }
    for index_name, columns in wanted_indexes.items():
        if index_name not in indexes:
            op.create_index(index_name, TABLE_NAME, columns)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if TABLE_NAME not in set(inspector.get_table_names()):
        return

    indexes = {index["name"] for index in inspector.get_indexes(TABLE_NAME)}
    for index_name in (
        "ix_profile_media_reviews_user_id",
        "ix_profile_media_reviews_media_type",
        "ix_profile_media_reviews_status",
        "ix_profile_media_reviews_is_current",
        "ix_profile_media_reviews_created_at",
    ):
        if index_name in indexes:
            op.drop_index(index_name, table_name=TABLE_NAME)

    op.drop_table(TABLE_NAME)
