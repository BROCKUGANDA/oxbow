"""A real ``backtest.interfaces.Scorer``: P6's seam, fitted one fold at a time.

WHAT THIS MODULE IS AND WHY IT IS THE MISSING HOP. ``oxbow.models.run.P4bScorer`` already
shows the shape of a fit — concat the fold's three slices, build a fold-scoped
:class:`~oxbow.scoring.frame.TrainingFrame`, run :meth:`FoldModelRunner.run_fold`, read the
calibrated column back off the scored rows. What it does *not* do is stamp the ``role``
column from the harness's own train/validation/test assignment, and that is exactly what
makes a fold honest: the caller (the P6 harness) decides which rows were fitting data and
which rows were scored, and this scorer records that decision in the artefact instead of
letting ``build_training_frame`` re-derive a role from ``fold`` + ``as_of_ts`` and quietly
disagreeing with the caller about what was held out.

THE ONE IMPORT DECISION, STATED. This module imports ``oxbow.backtest.interfaces`` for the
value types it must return (:class:`~oxbow.backtest.interfaces.AccountScore`,
:class:`~oxbow.backtest.interfaces.ScoreResult`) and for the ``Scorer`` protocol it
satisfies. That import is safe in one direction only: ``backtest.interfaces`` reads stdlib
and polars and imports nothing from ``oxbow.models``, so ``models -> backtest.interfaces``
is not a cycle, and the ``backtest`` package's ``__init__`` is a docstring. The dependency
is on a leaf of pure data types, not on the harness, the folds, or the metrics. Everything
that actually computes — the runner, the scorecard, the calibration, the leakage guard — is
reached through ``oxbow.models``. The rule the rest of the layer obeys (models never
imports the *harness*) is preserved: this scorer does not know folds exist, it is handed
three frames and told which was which.

MONEY (DEV-005): nothing here multiplies an amount. The outputs are a float probability and
two measured quantities — the calibration band's observed rate and its sample size — read
straight off the rows :func:`oxbow.models.run.annotate_rows` published. When a band carries
no observed evidence (``band_n == 0``, which is the refusal path in
:mod:`oxbow.models.calibration`) the rate is reported as ``0.0`` *with* ``band_n == 0``:
zero accounts observed is the honest statement, and no invented rate is put in its place.

DETERMINISM: rows are ordered ``(event_ts_utc, txn_id)`` upstream and this module adds no
reordering, no wall clock and no set iteration — the mapping is keyed by ``account_key`` and
the frame it reads is already queue-ordered by ``(-p_fused, account_key)``.
"""

from __future__ import annotations

import math
from typing import Final

import polars as pl

from oxbow.backtest.interfaces import AccountScore, ScoreResult
from oxbow.models.errors import FrameContractViolationError
from oxbow.models.run import (
    BAND_N_COLUMN,
    BAND_OBSERVED_RATE_COLUMN,
    EVALUATION_SLICE_KEY,
    P_FUSED_COLUMN,
    P_SCORECARD_COLUMN,
    FoldModelRunner,
    FoldRun,
    build_fold_training_frame,
)
from oxbow.scoring.frame import (
    ALLOWED_ATTACHED_COLUMNS,
    COL_ACCOUNT_KEY,
    COL_AS_OF_TS,
    COL_FOLD,
    COL_ROLE,
    META_COLUMNS,
    ROLE_TEST,
    ROLE_TRAIN,
    ROLE_VALIDATION,
    TrainingFrame,
)

CALIBRATED_COLUMN: Final = "calibrated"

#: The per-rule severity features the flags below are derived from. Each is a published
#: ``rule_field`` column the fold-scoped rules provider stamped onto the matrix, so reading
#: them is not models/ importing the rules layer — the rule engine already emitted the
#: severity, and a flag is "did any account in this fold have that rule fire".
_CYCLE_SEVERITY_COLUMN: Final = "rule_r4_cycle_severity"
_FAN_IN_SEVERITY_COLUMN: Final = "rule_r2_fan_in_severity"
_FAN_OUT_SEVERITY_COLUMN: Final = "rule_r3_fan_out_severity"


class FrameRuleHitProvider:
    """A ``models.inputs.RuleHitProvider`` that derives the fusion flags from the frame.

    ``FoldModelRunner`` calls ``attach_rule_hits(scored, provider)`` and requires
    ``cycle_flag`` / ``fan_flag`` (they are fusion inputs). Those two are not published
    features, so the provider supplies them; every other rule column the fusion needs is
    already on the frame, and a provider that re-emitted them would risk a version skew the
    seam deliberately refuses. Deriving a flag from a per-rule severity is a comparison, not
    a re-fit, so nothing here recomputes a rule.
    """

    def rule_hits(self, frame: pl.DataFrame) -> pl.DataFrame:
        out = (
            frame.select([COL_ACCOUNT_KEY, COL_AS_OF_TS])
            if COL_AS_OF_TS in frame.columns
            else frame.select([COL_ACCOUNT_KEY])
        )
        return out.with_columns(
            _flag(frame, _CYCLE_SEVERITY_COLUMN, "cycle_flag"),
            _fan_flag(frame, "fan_flag"),
        )


def _flag(frame: pl.DataFrame, column: str, name: str) -> pl.Series:
    if column not in frame.columns:
        return pl.Series(name, [0] * frame.height, dtype=pl.Int8)
    severity = frame.get_column(column).cast(pl.Float64).fill_null(0.0)
    return (severity > 0.0).cast(pl.Int8).alias(name)


def _fan_flag(frame: pl.DataFrame, name: str) -> pl.Series:
    columns = [
        column
        for column in (_FAN_IN_SEVERITY_COLUMN, _FAN_OUT_SEVERITY_COLUMN)
        if column in frame.columns
    ]
    if not columns:
        return pl.Series(name, [0] * frame.height, dtype=pl.Int8)
    max_horizontal = pl.max_horizontal(
        *[pl.col(column).cast(pl.Float64).fill_null(0.0) for column in columns]
    )
    return (
        frame.select(max_horizontal.alias("_fan_max"))
        .get_column("_fan_max")
        .gt(0.0)
        .cast(pl.Int8)
        .alias(name)
    )


def stamp_role(frame: pl.DataFrame, role: str) -> pl.DataFrame:
    """Stamp the role the *caller's assignment* gave these rows.

    This is the whole point of the seam: the harness sliced the corpus into train,
    validation and scored, and the frame must say so. ``build_training_frame`` would
    otherwise derive a role from ``fold`` + ``as_of_ts`` and disagree with the caller about
    which rows were held out — a role nobody assigned is a leak wearing a column name.
    """
    if role not in (ROLE_TRAIN, ROLE_VALIDATION, ROLE_TEST):
        raise FrameContractViolationError(
            f"unknown role {role!r}; a fold slice is train, validation or test"
        )
    if COL_FOLD not in frame.columns:
        raise FrameContractViolationError(
            "the frame carries no fold column; the splits module assigns it and this scorer "
            "refuses to invent one so a fold cannot be named in the artefact"
        )
    return frame.with_columns(pl.lit(role, dtype=pl.String).alias(COL_ROLE))


def _project(frame: pl.DataFrame, allowed: set[str]) -> pl.DataFrame:
    """Keep only the columns the scoring contract declares, dropping harness extras.

    The backtest corpus carries economic columns (``exposure_minor``, ``review_cost_minor``,
    ``review_minutes``, ``amount_minor``) the harness needs but the feature spec does not, and
    ``oxbow.scoring.frame`` fails closed on an undeclared column. This selects the intersection
    in the frame's own column order so no feature is invented or dropped by accident.
    """
    return frame.select([column for column in frame.columns if column in allowed])


def _fold_index(scored: pl.DataFrame) -> int:
    """A fold label for the leakage message, read off the corpus's ``fold`` column.

    The harness masks its own folds by ``as_of`` against the split boundaries, so the scored
    slice it hands over can legitimately straddle several bridge-folds; this value is only a
    label threaded into :func:`assert_no_leakage`'s message, never a boundary the scorer
    computes. It refuses only on an empty slice, which is a real contract break.
    """
    values = sorted({int(value) for value in scored.get_column(COL_FOLD).to_list()})
    if not values:
        raise FrameContractViolationError("the scored slice is empty; there is nothing to score")
    return values[-1]


class FixedFoldSlicesProvider:
    """Hand the runner the three slices this scorer was already given, roles intact.

    Structurally the same provider ``oxbow.models.run`` keeps private for its own
    :class:`~oxbow.models.run.P4bScorer`: it computes no boundaries (the caller did) and
    refuses to invent any, it just presents a ``{train, validation, evaluation}`` mapping so
    :meth:`FoldModelRunner.run_fold` can run its leakage guard against the slices the
    harness actually used. The ``fold`` argument is echoed for the guard's message.
    """

    def __init__(
        self,
        train: pl.DataFrame,
        validation: pl.DataFrame,
        evaluation: pl.DataFrame,
        *,
        embargo_days: int,
    ) -> None:
        self._train = train
        self._validation = validation
        self._evaluation = evaluation
        self._embargo_days = int(embargo_days)

    def fold_slices(
        self, frame: TrainingFrame, fold: int, evaluation_role: str
    ) -> dict[str, pl.DataFrame]:
        del frame, fold, evaluation_role
        return {
            ROLE_TRAIN: self._train,
            ROLE_VALIDATION: self._validation,
            EVALUATION_SLICE_KEY: self._evaluation,
        }

    def embargo_days(self) -> int:
        return self._embargo_days


class WalkForwardScorer:
    """A ``Scorer`` (per ``oxbow.backtest.interfaces``) backed by the P4b fold runner.

    Constructed once with a fitted-configuration :class:`FoldModelRunner`; its
    :meth:`score` is called once per fold by the harness and always returns a probability
    for every account in the ``scored`` slice — the harness refuses a missing score rather
    than price an account at a silent zero, and this scorer never produces one.
    """

    def __init__(
        self,
        runner: FoldModelRunner,
        *,
        embargo_days: int | None = None,
    ) -> None:
        self._runner = runner
        self._embargo_days = (
            runner.split_cfg.embargo_days if embargo_days is None else int(embargo_days)
        )

    def score(
        self,
        *,
        train: pl.DataFrame,
        validation: pl.DataFrame,
        scored: pl.DataFrame,
        feature_spec_hash: str,
        seed: int,
    ) -> ScoreResult:
        """Fit on ``train`` + ``validation``, score ``scored``, return calibrated per-account p.

        Raises on a feature-spec mismatch: the harness hands over the corpus's declared
        ``feature_spec_hash`` and this scorer refuses to score a frame the model's feature
        set does not describe (plan §8, 02 §B seam 3).
        """
        stamped_train = stamp_role(train, ROLE_TRAIN)
        stamped_validation = stamp_role(validation, ROLE_VALIDATION)
        stamped_scored = stamp_role(scored, ROLE_TEST)
        allowed = self._allowed_columns()
        stamped_train = _project(stamped_train, allowed)
        stamped_validation = _project(stamped_validation, allowed)
        stamped_scored = _project(stamped_scored, allowed)
        fold = _fold_index(stamped_scored)

        rows = pl.concat([stamped_train, stamped_validation, stamped_scored])
        frame = build_fold_training_frame(
            rows,
            self._runner.feature_registry,
            self._runner.scorecard_cfg,
            provenance=self._runner.provenance,
            split_cfg=self._runner.split_cfg,
        )
        if frame.feature_spec_hash != feature_spec_hash:
            raise FrameContractViolationError(
                f"the harness declared feature_spec_hash {feature_spec_hash[:16]}… but the "
                f"fold frame carries {frame.feature_spec_hash[:16]}…: scoring with a feature "
                "set the model never saw produces confident nonsense, so it is refused "
                "(plan §8 / 02 §B seam 3)"
            )

        provider = FixedFoldSlicesProvider(
            stamped_train,
            stamped_validation,
            stamped_scored,
            embargo_days=self._embargo_days,
        )
        run = self._runner.run_fold(
            frame,
            fold,
            provider=provider,
            evaluation_role=ROLE_TEST,
            seed=seed,
        )
        entries = self._account_scores(run, fold=fold)
        return ScoreResult(
            scores=entries,
            model_version=self._model_version(run, fold=fold),
            feature_spec_hash=frame.feature_spec_hash,
        )

    # -- mapping --------------------------------------------------------------

    def _allowed_columns(self) -> set[str]:
        """The column set the scoring contract declares: meta + features + role + attached."""
        return (
            set(META_COLUMNS)
            | set(self._runner.feature_registry.names)
            | {COL_ROLE}
            | set(ALLOWED_ATTACHED_COLUMNS)
        )

    def _account_scores(self, run: FoldRun, *, fold: int) -> dict[str, AccountScore]:
        if not isinstance(run, FoldRun):  # pragma: no cover - defensive against a runner swap
            raise FrameContractViolationError("run_fold returned an unexpected type")
        evaluation = run.scored.filter(pl.col(COL_ROLE) == ROLE_TEST)
        if evaluation.height == 0:
            raise FrameContractViolationError(
                f"fold {fold}: the runner produced no scored test rows to map; a fold that "
                "was fit but never scored is a contract break, not an empty result"
            )
        entries: dict[str, AccountScore] = {}
        for row in evaluation.iter_rows(named=True):
            key = str(row[COL_ACCOUNT_KEY])
            entries[key] = AccountScore(
                account_key=key,
                p_calibrated=self._probability(row, account_key=key),
                **self._band_evidence(row),
            )
        return entries

    @staticmethod
    def _probability(row: dict[str, object], *, account_key: str) -> float:
        """The calibrated p, with one documented fallback and no invented number.

        ``p_fused`` is the calibrated probability whenever the fold calibrated, and the
        prior-corrected scorecard probability whenever it degraded to scorecard-and-rules
        (both are inside ``[0, 1]``). The one case it can leave that interval is a fold that
        fitted a fused score but whose calibration was refused, where ``p_fused`` carries the
        raw meta-learner output: there the defensible probability is the audited scorecard
        channel, never a rescaled raw score.
        """
        fused = float(row[P_FUSED_COLUMN])
        if 0.0 <= fused <= 1.0:
            return fused
        scorecard = row.get(P_SCORECARD_COLUMN)
        if scorecard is not None and 0.0 <= float(scorecard) <= 1.0:
            return float(scorecard)
        raise FrameContractViolationError(
            f"no probability in [0, 1] is available for {account_key}: fused={fused!r} is "
            "outside the unit interval and the scorecard channel is missing or invalid, so "
            "any number emitted here would be invented (03 A rule 2)."
        )

    @staticmethod
    def _band_evidence(row: dict[str, object]) -> dict[str, float | int]:
        """The band's observed rate and its sample size, or an explicit 'no evidence'.

        ``annotate_rows`` publishes ``band_observed_rate`` / ``band_n`` from the calibration
        bin the prediction landed in. On a refused or uncalibrated fold the rate column is
        NaN and the count is zero; that is reported as ``band_n == 0`` with a zero rate
        rather than as a made-up observed frequency, because Module C multiplies the
        probability by money and a fabricated rate is a fabricated currency figure.
        """
        raw_rate = row.get(BAND_OBSERVED_RATE_COLUMN)
        raw_n = row.get(BAND_N_COLUMN)
        n = 0 if raw_n is None else int(raw_n)
        if n <= 0 or raw_rate is None or not math.isfinite(float(raw_rate)):
            return {"band_observed_rate": 0.0, "band_n": 0}
        return {"band_observed_rate": float(raw_rate), "band_n": n}

    @staticmethod
    def _model_version(run: object, *, fold: int) -> str:
        from oxbow.models.run import FoldRun

        assert isinstance(run, FoldRun)  # narrowed above
        if run.lineage is not None and run.lineage.model_version is not None:
            return str(run.lineage.model_version)
        if run.gbm is not None:
            return run.gbm.fingerprint()[:12]
        return f"fold-{fold}-{run.mode}"


__all__ = [
    "FixedFoldSlicesProvider",
    "FrameRuleHitProvider",
    "WalkForwardScorer",
    "stamp_role",
]
