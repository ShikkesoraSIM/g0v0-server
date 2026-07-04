"""add torii replay renders (registro de renders o!rdr)

Una fila por render de replay pedido a o!rdr desde el cliente. Alimenta el
poller de estado del server y el anuncio de videos terminados en discord
(ToriiHalo, patron poll+dispatch como mod-alerts).

Idempotente: re-run / migracion interrumpida es seguro.

Revision ID: 1502b70ae8f5
Revises: d7e8f9a0b1c2
Create Date: 2026-07-05 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "1502b70ae8f5"
down_revision: str | Sequence[str] | None = "d7e8f9a0b1c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "torii_replay_renders" not in tables:
        op.create_table(
            "torii_replay_renders",
            sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
            sa.Column("ordr_render_id", sa.BigInteger, nullable=False),
            sa.Column("user_id", sa.BigInteger, nullable=False),
            sa.Column("score_id", sa.BigInteger, nullable=False),
            sa.Column("username", sa.VARCHAR(32), nullable=False),
            sa.Column("beatmap_title", sa.VARCHAR(255), nullable=False, server_default=""),
            sa.Column("resolution", sa.VARCHAR(16), nullable=False),
            sa.Column("skin", sa.VARCHAR(128), nullable=False),
            sa.Column("motion_blur", sa.Boolean, nullable=False, server_default=sa.text("0")),
            sa.Column("share", sa.Boolean, nullable=False, server_default=sa.text("1")),
            sa.Column("status", sa.VARCHAR(16), nullable=False, server_default="queued"),
            sa.Column("progress", sa.VARCHAR(160), nullable=False, server_default=""),
            sa.Column("video_url", sa.VARCHAR(255), nullable=True),
            sa.Column("error_code", sa.BigInteger, nullable=True),
            sa.Column("error_message", sa.VARCHAR(255), nullable=True),
            sa.Column("created_at", sa.DateTime, nullable=False),
            sa.Column("finished_at", sa.DateTime, nullable=True),
            sa.Column("dispatched_at", sa.DateTime, nullable=True),
        )

    # re-inspeccionar despues del posible create (los inspectors cachean)
    inspector = sa.inspect(bind)
    existing_indexes = {ix["name"] for ix in inspector.get_indexes("torii_replay_renders")}

    def ensure_index(name: str, cols: list[str], unique: bool = False) -> None:
        if name not in existing_indexes:
            op.create_index(name, "torii_replay_renders", cols, unique=unique)

    ensure_index("ix_torii_replay_renders_ordr_render_id", ["ordr_render_id"], unique=True)
    ensure_index("ix_torii_replay_renders_user_id", ["user_id"])
    ensure_index("ix_torii_replay_renders_score_id", ["score_id"])
    ensure_index("ix_torii_replay_renders_share", ["share"])
    ensure_index("ix_torii_replay_renders_status", ["status"])
    ensure_index("ix_torii_replay_renders_created_at", ["created_at"])
    ensure_index("ix_torii_replay_renders_dispatched_at", ["dispatched_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "torii_replay_renders" in set(inspector.get_table_names()):
        op.drop_table("torii_replay_renders")
