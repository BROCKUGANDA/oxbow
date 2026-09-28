"""score: a refused calibration becomes a labelled state, not a dropped row

Revision ID: 0003_calibration_kind
Revises: 0002_integrity
Create Date: 2026-09-28

Why this migration exists, in one paragraph. ``score.observed_rate`` and
``score.calibration_n`` were NOT NULL, which is correct as a claim ("a confidence figure
without its ``n`` is an adjective") and wrong as a consequence. A fold whose calibration
was refused -- fewer validation positives than
``config/model.yaml``'s ``calibration.min_positives_for_calibration`` -- has a real fused
score and no measured rate to put beside it. The loader therefore refused the row, every
fold of the landed run refused, and the queue the analyst opens came up empty: an absent
score is indistinguishable on screen from an account nothing scored, which is exactly the
unknown-becoming-a-zero failure plan 03 §A rule 2 forbids. Plan 03 §H names the intended
behaviour instead: "calibration is refused and the UI says probabilities are uncalibrated."

So the invariant moves from *the column* to *the pairing*. ``calibration_kind`` holds the
producer's own vocabulary -- the ``kind`` field ``CalibrationResult.confidence_label``
already emits -- and ``ck_score_calibration_pairing`` requires that a calibrated row carry
its rate, its band and a positive ``n`` with no note, and that an uncalibrated row carry
none of them and a note explaining the refusal. Neither shape can be half-populated, so
the adjective still cannot be stored; the score now can be.

Existing rows are backfilled as ``calibrated_band`` because the constraint they were
written under could not have produced any other kind: a row in this table before this
revision had a non-null rate and ``calibration_n > 0`` or the loader rejected it. The
backfill is a CASE rather than a constant so a row that somehow violates that is left
visible as ``uncalibrated`` and fails the pairing check, instead of being asserted sound.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_calibration_kind"
down_revision: str | None = "0002_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The columns that stop being required. Each one is a *measurement* of the calibration, so
# all four are absent together when there was no calibration to measure.
_NOW_OPTIONAL: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("calibrated_probability", sa.Double()),
    ("calibration_band", sa.String(length=8)),
    ("observed_rate", sa.Double()),
    ("calibration_n", sa.Integer()),
)


def upgrade() -> None:
    op.add_column(
        "score",
        sa.Column("calibration_kind", sa.String(length=15), nullable=True),
    )
    op.add_column("score", sa.Column("calibration_note", sa.Text(), nullable=True))

    op.execute(
        """
        UPDATE score
           SET calibration_kind = CASE
                    WHEN observed_rate IS NOT NULL
                     AND calibration_n IS NOT NULL
                     AND calibration_n > 0
                    THEN 'calibrated_band'
                    ELSE 'uncalibrated'
                 END
         WHERE calibration_kind IS NULL
        """
    )
    # An uncalibrated row must explain itself and a calibrated one must not, and the
    # backfill can produce the former without a note. The reason belongs to the fold, not
    # to the account, so a backfilled row states where it came from rather than going
    # silent on a CHECK the writer cannot see.
    op.execute(
        """
        UPDATE score
           SET calibration_note = 'row predates 0003: calibration was never recorded for it'
         WHERE calibration_kind = 'uncalibrated' AND calibration_note IS NULL
        """
    )

    op.alter_column("score", "calibration_kind", nullable=False)
    for name, type_ in _NOW_OPTIONAL:
        op.alter_column("score", name, existing_type=type_, nullable=True)

    op.create_check_constraint(
        "ck_score_calibration_kind",
        "score",
        "calibration_kind IN ('calibrated_band', 'uncalibrated')",
    )
    op.create_check_constraint(
        "ck_score_calibration_pairing",
        "score",
        "(calibration_kind = 'calibrated_band' "
        " AND calibrated_probability IS NOT NULL"
        " AND calibration_band IS NOT NULL"
        " AND observed_rate IS NOT NULL"
        " AND calibration_n > 0"
        " AND calibration_note IS NULL)"
        " OR (calibration_kind = 'uncalibrated'"
        " AND calibrated_probability IS NULL"
        " AND calibration_band IS NULL"
        " AND observed_rate IS NULL"
        " AND calibration_n IS NULL"
        " AND calibration_note IS NOT NULL)",
    )

    # The reasoning goes where someone inspecting the schema will find it, which is 0002's
    # convention and the reason this revision is hand-written.
    op.execute(
        "COMMENT ON COLUMN score.calibration_kind IS "
        "'calibrated_band | uncalibrated, mirroring confidence_label.kind in "
        "oxbow.models.calibration. uncalibrated means the fold was refused calibration by "
        "config/model.yaml calibration.min_positives_for_calibration; the score is real, "
        "the observed rate is not, and the UI must say so (plan 03 §H, plan §12.8 "
        "degraded-not-broken).'"
    )
    op.execute(
        "COMMENT ON CONSTRAINT ck_score_calibration_pairing ON score IS "
        "'A confidence figure without its n is an adjective, and an uncalibrated row "
        "without its refusal reason is an unexplained gap. The pairing stores both shapes "
        "and forbids a half-populated one.'"
    )


def downgrade() -> None:
    # Every uncalibrated row becomes a deletion, because the pre-0003 schema cannot hold
    # it and dropping the measurement columns it lacks would violate the old NOT NULLs.
    # That is the same information loss the loader was already causing, made explicit.
    op.execute("DELETE FROM score WHERE calibration_kind = 'uncalibrated'")
    op.drop_constraint("ck_score_calibration_pairing", "score", type_="check")
    op.drop_constraint("ck_score_calibration_kind", "score", type_="check")
    for name, type_ in _NOW_OPTIONAL:
        op.alter_column("score", name, existing_type=type_, nullable=False)
    op.drop_column("score", "calibration_note")
    op.drop_column("score", "calibration_kind")
