"""add client_version to scores and score_tokens

Revision ID: 9a7d3b4c5e6f
Revises: c1b7d9e4a6f2
Create Date: 2026-03-13 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "9a7d3b4c5e6f"
down_revision: str | Sequence[str] | None = "c1b7d9e4a6f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "scores" in tables:
        score_columns = {col["name"] for col in inspector.get_columns("scores")}
        if "client_version" not in score_columns:
            op.add_column("scores", sa.Column("client_version", sa.String(length=255), nullable=True))

    if "score_tokens" in tables:
        token_columns = {col["name"] for col in inspector.get_columns("score_tokens")}
        if "client_version" not in token_columns:
            op.add_column("score_tokens", sa.Column("client_version", sa.String(length=255), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "score_tokens" in tables:
        token_columns = {col["name"] for col in inspector.get_columns("score_tokens")}
        if "client_version" in token_columns:
            op.drop_column("score_tokens", "client_version")

    if "scores" in tables:
        score_columns = {col["name"] for col in inspector.get_columns("scores")}
        if "client_version" in score_columns:
            op.drop_column("scores", "client_version")
