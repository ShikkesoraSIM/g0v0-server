"""add td_play_style + td_classification_confidence columns to scores

Touchscreen play-style classification, populated by the
``/touchscreen/classify`` endpoint on osu-performance-server. Distinguishes
between two physically very different ways of using the TD-flagged mod:

* tapping each hit object discretely (FairTouchScreen — pp penalty
  should NOT apply)
* dragging one finger across the screen while side-tapping the other
  (drag-tap cheese — pp penalty applies, default)

The pp recalc pipeline reads this column when calling ``/performance`` on
the perf server; perf server strips the TD mod from the calculator's
input iff style == ``tap``, leaving the score's pp pretending it was
played on tablet/mouse. Every other value (drag, mixed, unknown) keeps
the TD mod and the current penalty.

Two columns are added:

* ``td_play_style`` (TINYINT, NOT NULL, default 0): enum-as-int. 0
  Unknown, 1 Tap, 2 Drag, 3 Mixed. Default 0 backfills all existing
  scores to "no verdict yet" — the conservative state where TD remains
  applied. A separate bulk-classify script populates the column for
  existing TD scores with replay files.
* ``td_classification_confidence`` (FLOAT, NULLABLE): confidence the
  classifier had in the verdict, [0..1]. NULL when never run. Kept
  separately so we can re-tune thresholds offline and re-derive the
  verdict from logged raw metrics without re-parsing the .osr.

Revision ID: a7b8c9d0e1f2
Revises: d1c0a51500b1
Create Date: 2026-05-10 22:30:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "a7b8c9d0e1f2"
down_revision: str | Sequence[str] | None = "d1c0a51500b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "scores",
        sa.Column(
            "td_play_style",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("0"),
            comment=(
                "Touchscreen play-style verdict from TouchScreenClassifier. "
                "0=Unknown (default, TD penalty applies), 1=Tap "
                "(FairTouchScreen — pp recalc strips TD), 2=Drag (TD "
                "penalty applies), 3=Mixed (treated as Drag, conservative)."
            ),
        ),
    )
    op.add_column(
        "scores",
        sa.Column(
            "td_classification_confidence",
            sa.Float(),
            nullable=True,
            comment=(
                "Classifier confidence in td_play_style, [0..1]. NULL when "
                "never run. Kept independently of the enum so the verdict "
                "can be re-derived under new thresholds without re-parsing "
                "the .osr."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("scores", "td_classification_confidence")
    op.drop_column("scores", "td_play_style")
