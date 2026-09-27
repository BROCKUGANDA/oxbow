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
    P_FUSED_RAW_COLUMN,
    P_GBM_COLUMN,
    P_GBM_NO_GRAPH_COLUMN,
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
    COL_SPEC_HASH,
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


#: One ablation profile per score column, each produced by a DIFFERENT fitted object inside
#: the same fold fit — the WOE logistic, the booster on every feature, the booster refitted
#: without the graph groups, the meta-learner, and the calibrated meta-learner. A profile is
#: a request, not a preference: :meth:`WalkForwardScorer.score` reads exactly that column and
#: refuses the row when the fold did not produce it, because a fallback would report the full
#: stack's probability under a label that says a smaller model made it (DEV-027).
PROFILES: Final[dict[str, str]] = {
    "scorecard": P_SCORECARD_COLUMN,
    "gbm": P_GBM_COLUMN,
    "gbm_no_graph": P_GBM_NO_GRAPH_COLUMN,
    "fused_uncalibrated": P_FUSED_RAW_COLUMN,
    "calibrated": P_FUSED_COLUMN,
}
DEFAULT_PROFILE: Final = "calibrated"


class ProfileUnavailableError(FrameContractViolationError):
    """The fold did not produce the column a profile asked for, so the row is not measurable."""


class WalkForwardScorer:
    """A ``Scorer`` (per ``oxbow.backtest.interfaces``) backed by the P4b fold runner.

    Constructed once with a fitted-configuration :class:`FoldModelRunner`; its
    :meth:`score` is called once per fold by the harness and always returns a probability
    for every account in the ``scored`` slice — the harness refuses a missing score rather
    than price an account at a silent zero, and this scorer never produces one.

    ``profile`` selects which of the fold's fitted channels answers the harness, so one fold
    fit can serve the model-ablation rows honestly: each row reads the column ITS model made.
    The default is ``"calibrated"``, which is the production configuration and the number the
    rest of the packet quotes.
    """

    def __init__(
        self,
        runner: FoldModelRunner,
        *,
        embargo_days: int | None = None,
        profile: str = DEFAULT_PROFILE,
    ) -> None:
        if profile not in PROFILES:
            raise ProfileUnavailableError(
                f"unknown ablation profile {profile!r}; the fitted channels are "
                f"{sorted(PROFILES)}, and a profile that names nothing would score the row off "
                "whichever column happened to be present"
            )
        self._runner = runner
        self.profile = profile
        self._embargo_days = (
            runner.split_cfg.embargo_days if embargo_days is None else int(embargo_days)
        )

    def fold_run(
        self,
        *,
        train: pl.DataFrame,
        validation: pl.DataFrame,
        scored: pl.DataFrame,
        feature_spec_hash: str,
        seed: int,
    ) -> FoldRun:
        """Fit this fold's whole stack once and return the run, with every channel's column.

        This is the unit of work an ablation row costs. :meth:`score` reads one column out of
        what it returns, so a shared cache can answer six rows from one call and each row's
        number still comes from the model its label names.

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
        return self._runner.run_fold(
            frame,
            fold,
            provider=provider,
            evaluation_role=ROLE_TEST,
            seed=seed,
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
        """Fit the fold and answer with the profile's own column — never another channel's."""
        run = self.fold_run(
            train=train,
            validation=validation,
            scored=scored,
            feature_spec_hash=feature_spec_hash,
            seed=seed,
        )
        return self.scores_from(run, profile=self.profile)

    @classmethod
    def scores_from(cls, run: FoldRun, *, profile: str) -> ScoreResult:
        """Project one fold's fitted stack onto the probability column a profile names.

        Public because the ablation cache holds the :class:`FoldRun` and asks this six times
        over one fit. It is a projection, not a fallback: the column is read and nothing else
        is consulted, so two rows can only report the same number if they asked for the same
        column — which the ablation table's own distinctness check then catches.
        """
        if profile not in PROFILES:
            raise ProfileUnavailableError(
                f"unknown ablation profile {profile!r}; the fitted channels are {sorted(PROFILES)}"
            )
        entries = cls._account_scores(run, profile=profile)
        hashes = set(run.scored.get_column(COL_SPEC_HASH).unique().to_list())
        if len(hashes) != 1:
            raise FrameContractViolationError(
                f"fold {run.fold}: its scored rows carry {sorted(hashes)} — a fold fitted on one "
                "feature spec and scored on another cannot be reported as one number"
            )
        return ScoreResult(
            scores=entries,
            # Which profile produced it is recorded per row in the payload's
            # `ablation_profiles` (keyed by row_id) rather than stitched in here: the string
            # lands in a VARCHAR(128) column and a lineage version already fills most of it.
            model_version=cls._model_version(run, fold=run.fold),
            feature_spec_hash=str(hashes.pop()),
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

    @classmethod
    def _account_scores(cls, run: FoldRun, *, profile: str) -> dict[str, AccountScore]:
        column = PROFILES[profile]
        evaluation = run.scored.filter(pl.col(COL_ROLE) == ROLE_TEST)
        if evaluation.height == 0:
            raise FrameContractViolationError(
                f"fold {run.fold}: the runner produced no scored test rows to map; a fold that "
                "was fit but never scored is a contract break, not an empty result"
            )
        if column not in run.scored.columns:
            raise ProfileUnavailableError(
                f"fold {run.fold}: profile {profile!r} asks for {column!r} and the fold did not "
                f"publish it. Its scoring_mode is {run.mode!r} and it recorded "
                f"{dict(run.channel_skips) or 'no channel skips'} — the channels it refused are "
                "reported, not replaced with another channel's number"
            )
        entries: dict[str, AccountScore] = {}
        for row in evaluation.iter_rows(named=True):
            key = str(row[COL_ACCOUNT_KEY])
            entries[key] = AccountScore(
                account_key=key,
                p_calibrated=cls._probability(
                    row, profile=profile, column=column, account_key=key, fold=run.fold
                ),
                **cls._band_evidence(row),
            )
        return entries

    @staticmethod
    def _probability(
        row: dict[str, object],
        *,
        profile: str,
        column: str,
        account_key: str,
        fold: int,
    ) -> float:
        """The profile's own number, or a named refusal — one column, no fallback.

        DEV-027's imitation was possible because one scorer answered for eight labels and the
        reader could not tell which channel produced a row. Each row now declares the column it
        reads, so a fold that did not fit that channel stops the row instead of quietly
        borrowing the full stack's probability.
        """
        raw = row.get(column)
        if raw is None:
            raise ProfileUnavailableError(
                f"fold {fold}, account {account_key}: {column!r} (profile {profile!r}) is null, "
                "and pricing a queue off a missing score would book it as a zero"
            )
        value = float(raw)
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ProfileUnavailableError(
                f"fold {fold}, account {account_key}: {column!r} (profile {profile!r}) is "
                f"{value!r}, outside [0, 1] — the economics multiply it by exposure, so it has "
                "to be a probability and this profile is not one on this fold"
            )
        return value

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
    def _model_version(run: FoldRun, *, fold: int) -> str:
        """Which fitted object produced the fold's rows, in one string the artifact can carry."""
        if run.lineage is not None and run.lineage.model_version is not None:
            return str(run.lineage.model_version)
        if run.gbm is not None:
            return run.gbm.fingerprint()[:12]
        return f"fold-{fold}-{run.mode}"


__all__ = [
    "DEFAULT_PROFILE",
    "PROFILES",
    "FixedFoldSlicesProvider",
    "FrameRuleHitProvider",
    "ProfileUnavailableError",
    "WalkForwardScorer",
    "stamp_role",
]
