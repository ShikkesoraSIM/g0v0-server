"""drop supporter aura tier ids from equipped_aura column

After dropping the supporter loyalty tier system (bronze / silver /
gold), the only valid supporter aura id is now `supporter-aura`. Any
user whose `equipped_aura` still references one of the dropped ids
would render as null in the catalog (unknown id) — clear them so the
column only ever contains values the catalog actually serves.

The migration is data-only — it doesn't touch schema. We deliberately
DON'T attempt to rename old → new (e.g. "supporter-hearts-gold" →
"supporter-aura") because the dropped ids carried tier semantics that
no longer exist; remapping them would silently re-grant something
different. Better to clear and let the user re-equip from the picker.

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-04-28 06:00:00.000000

"""

from collections.abc import Sequence

from alembic import op


revision: str = "f7a8b9c0d1e2"
down_revision: str | Sequence[str] | None = "e6f7a8b9c0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Stored values that no longer correspond to a real catalog aura.
# `supporter-hearts` rolls forward to the new `supporter-aura` since it
# was the same default-pink visual; the tiered ones are dropped outright.
_DROPPED_AURA_IDS = (
    "supporter-hearts-bronze",
    "supporter-hearts-silver",
    "supporter-hearts-gold",
)


def upgrade() -> None:
    # Drop tier picks → revert to default (NULL → group resolver picks
    # the right thing on read).
    op.execute(
        f"""
        UPDATE lazer_users
        SET equipped_aura = NULL
        WHERE equipped_aura IN ({", ".join(repr(x) for x in _DROPPED_AURA_IDS)})
        """
    )
    # Roll the legacy "supporter-hearts" id forward to the new id so
    # nobody loses their pick. They had pink hearts equipped; they get
    # pink hearts equipped, just under the new key.
    op.execute(
        """
        UPDATE lazer_users
        SET equipped_aura = 'supporter-aura'
        WHERE equipped_aura = 'supporter-hearts'
        """
    )


def downgrade() -> None:
    # No-op rollback. Going back to the tier system would require
    # reconstructing per-user choices we no longer have, and the
    # "supporter-hearts" → "supporter-aura" mapping is one-way (we
    # don't know which tier picked what before).
    pass
