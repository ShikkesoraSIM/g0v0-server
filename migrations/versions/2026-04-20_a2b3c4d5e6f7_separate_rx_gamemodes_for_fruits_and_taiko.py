"""separate RX gamemodes for fruits and taiko scores

Move all CTB (fruits) scores that have the RX mod to fruitsrx, and all taiko
scores that have the RX mod to taikorx.  These were historically stored under
the base gamemode when the server did not separate them.

Revision ID: a2b3c4d5e6f7
Revises: f1e2d3c4b5a6
Create Date: 2026-04-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2b3c4d5e6f7"
down_revision: str | Sequence[str] | None = "f1e2d3c4b5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSON_SEARCH returns non-NULL when the 'RX' acronym exists anywhere in the
# mods JSON array, meaning the score was played with Relax.
_RX_JSON = "JSON_SEARCH({mods_col}, 'one', 'RX', NULL, '$[*].acronym') IS NOT NULL"


def upgrade() -> None:
    """Move fruits+RX → fruitsrx and taiko+RX → taikorx in all score tables."""
    conn = op.get_bind()

    # scores — has its own mods column
    for base, rx in (("fruits", "fruitsrx"), ("taiko", "taikorx")):
        conn.execute(sa.text(
            f"UPDATE scores SET gamemode = '{rx}'"
            f" WHERE gamemode = '{base}' AND {_RX_JSON.format(mods_col='mods')}"
        ))

    # total_score_best_scores — also has its own mods column
    for base, rx in (("fruits", "fruitsrx"), ("taiko", "taikorx")):
        conn.execute(sa.text(
            f"UPDATE total_score_best_scores SET gamemode = '{rx}'"
            f" WHERE gamemode = '{base}' AND {_RX_JSON.format(mods_col='mods')}"
        ))

    # best_scores — no mods column; join with scores on score_id = scores.id
    for base, rx in (("fruits", "fruitsrx"), ("taiko", "taikorx")):
        conn.execute(sa.text(
            f"UPDATE best_scores b"
            f" JOIN scores s ON b.score_id = s.id"
            f" SET b.gamemode = '{rx}'"
            f" WHERE b.gamemode = '{base}'"
            f" AND {_RX_JSON.format(mods_col='s.mods')}"
        ))


def downgrade() -> None:
    """Revert fruitsrx → fruits and taikorx → taiko (loses RX distinction)."""
    conn = op.get_bind()

    for table in ("scores", "best_scores", "total_score_best_scores"):
        conn.execute(sa.text(
            f"UPDATE {table} SET gamemode = 'fruits' WHERE gamemode = 'fruitsrx'"
        ))
        conn.execute(sa.text(
            f"UPDATE {table} SET gamemode = 'taiko' WHERE gamemode = 'taikorx'"
        ))
