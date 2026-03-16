"""add client hash and label to login logs

Revision ID: d2c4f7a8b1e0
Revises: 9a7d3b4c5e6f
Create Date: 2026-03-14 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "d2c4f7a8b1e0"
down_revision: str | Sequence[str] | None = "9a7d3b4c5e6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_columns_if_missing(table_name: str) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns(table_name)}

    if "client_hash" not in columns:
        op.add_column(table_name, sa.Column("client_hash", sa.String(length=128), nullable=True))

    if "client_label" not in columns:
        op.add_column(table_name, sa.Column("client_label", sa.String(length=255), nullable=True))


def _drop_columns_if_present(table_name: str) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns(table_name)}

    if "client_label" in columns:
        op.drop_column(table_name, "client_label")
    if "client_hash" in columns:
        op.drop_column(table_name, "client_hash")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "user_login_log" in tables:
        _add_columns_if_missing("user_login_log")
    elif "userloginlog" in tables:
        _add_columns_if_missing("userloginlog")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "user_login_log" in tables:
        _drop_columns_if_present("user_login_log")
    elif "userloginlog" in tables:
        _drop_columns_if_present("userloginlog")
