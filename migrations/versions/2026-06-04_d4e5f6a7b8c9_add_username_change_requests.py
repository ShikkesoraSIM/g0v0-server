"""add username_change_requests

Revision ID: d4e5f6a7b8c9
Revises: c8d9e0f1a2b3
Create Date: 2026-06-04 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "c8d9e0f1a2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_NAME = "username_change_requests"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if TABLE_NAME not in set(inspector.get_table_names()):
        op.create_table(
            TABLE_NAME,
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("current_username", sa.String(length=32), nullable=False),
            sa.Column("requested_username", sa.String(length=32), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("reject_reason", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("reviewed_at", sa.DateTime(), nullable=True),
            sa.Column("reviewed_by_id", sa.BigInteger(), nullable=True),
        )

    indexes = {index["name"] for index in inspector.get_indexes(TABLE_NAME)}
    wanted_indexes = {
        "ix_username_change_requests_user_id": ["user_id"],
        "ix_username_change_requests_requested_username": ["requested_username"],
        "ix_username_change_requests_status": ["status"],
        "ix_username_change_requests_created_at": ["created_at"],
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
        "ix_username_change_requests_user_id",
        "ix_username_change_requests_requested_username",
        "ix_username_change_requests_status",
        "ix_username_change_requests_created_at",
    ):
        if index_name in indexes:
            op.drop_index(index_name, table_name=TABLE_NAME)

    op.drop_table(TABLE_NAME)
