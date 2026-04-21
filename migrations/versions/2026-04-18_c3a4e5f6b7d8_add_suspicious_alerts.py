"""add suspicious alerts

Revision ID: c3a4e5f6b7d8
Revises: b7c8d9e0f1a2
Create Date: 2026-04-18 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c3a4e5f6b7d8"
down_revision: str | Sequence[str] | None = "b7c8d9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_NAME = "suspicious_alerts"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if TABLE_NAME not in set(inspector.get_table_names()):
        op.create_table(
            TABLE_NAME,
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("kind", sa.String(length=64), nullable=False),
            sa.Column("severity", sa.String(length=16), nullable=False),
            sa.Column("fingerprint", sa.String(length=128), nullable=False),
            sa.Column("user_id", sa.BigInteger(), nullable=True),
            sa.Column("score_id", sa.BigInteger(), nullable=True),
            sa.Column("beatmap_id", sa.BigInteger(), nullable=True),
            sa.Column("title", sa.String(length=200), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("metadata", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("dispatched_at", sa.DateTime(), nullable=True),
            sa.Column("resolved_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("fingerprint", name="uq_suspicious_alert_fingerprint"),
        )

    indexes = {index["name"] for index in inspector.get_indexes(TABLE_NAME)}
    wanted_indexes = {
        "ix_suspicious_alerts_kind": ["kind"],
        "ix_suspicious_alerts_severity": ["severity"],
        "ix_suspicious_alerts_fingerprint": ["fingerprint"],
        "ix_suspicious_alerts_user_id": ["user_id"],
        "ix_suspicious_alerts_score_id": ["score_id"],
        "ix_suspicious_alerts_beatmap_id": ["beatmap_id"],
        "ix_suspicious_alerts_created_at": ["created_at"],
        "ix_suspicious_alerts_dispatched_at": ["dispatched_at"],
        "ix_suspicious_alerts_resolved_at": ["resolved_at"],
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
        "ix_suspicious_alerts_kind",
        "ix_suspicious_alerts_severity",
        "ix_suspicious_alerts_fingerprint",
        "ix_suspicious_alerts_user_id",
        "ix_suspicious_alerts_score_id",
        "ix_suspicious_alerts_beatmap_id",
        "ix_suspicious_alerts_created_at",
        "ix_suspicious_alerts_dispatched_at",
        "ix_suspicious_alerts_resolved_at",
    ):
        if index_name in indexes:
            op.drop_index(index_name, table_name=TABLE_NAME)

    op.drop_table(TABLE_NAME)
