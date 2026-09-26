"""Pandera schema for raw PaySim rows, as they arrive from the CSV.

STRICT ON PURPOSE. 02 D and 01 B both say the same thing from opposite ends: a
source adapter yields canonical-valid rows or raises, and malformed input is
never repaired silently. A schema that quietly coerced ``"12.30"`` into a
float, or dropped a column it did not recognise, would let a corrupt corpus
through while every downstream number still looked reasonable.

So: unknown columns fail the batch and name themselves, ``coerce=False``, and
each check carries a short machine-readable ``error`` code so a quarantine
record says *which* rule failed rather than quoting a stack trace.

pandera.polars 0.20 notes, verified against the installed package rather than
assumed: ``Column`` accepts a ``checks=`` list (there is no positional
``Check``), and ``Check`` takes ``name=``/``error=`` but no ``description=``.
"""

from __future__ import annotations

import pandera.polars as pa
import polars as pl

# PaySim's exact header, READ FROM THE FILE rather than recalled. The real
# header is:
#   step,type,amount,nameOrig,oldbalanceOrg,newbalanceOrig,nameDest,
#   oldbalanceDest,newbalanceDest,isFraud,isFlaggedFraud
# Note the lower-case `b` in every balance column, and that `nameDest` sits
# AFTER the balance columns rather than beside nameOrig. A hand-written header
# with `oldBalanceOrig` and a tidy column order fails the strict check, which is
# exactly the failure this declaration is meant to prevent recurring.
PAYSIM_RAW_COLUMNS: tuple[str, ...] = (
    "step",
    "type",
    "amount",
    "nameOrig",
    "oldbalanceOrg",
    "newbalanceOrig",
    "nameDest",
    "oldbalanceDest",
    "newbalanceDest",
    "isFraud",
    "isFlaggedFraud",
)

# Carried but never read as a learning target. `isFlaggedFraud` fires 16 times
# in 6,362,620 rows on this corpus (measured in P1a, not assumed), which makes
# it unusable at any depth; the dataset card states this.
LABEL_CAVEAT = (
    "isFraud is a narrow simulator behaviour: an agent takes over an account and "
    "drains it via TRANSFER then CASH-OUT. isFlaggedFraud is a threshold, not "
    "ground truth, and is far too sparse here to train on."
)

# The five transaction types PaySim emits.
PAYSIM_TXN_TYPES: tuple[str, ...] = ("CASH_IN", "CASH_OUT", "DEBIT", "PAYMENT", "TRANSFER")


def _frame(data: object) -> pl.DataFrame:
    """Materialise whatever pandera hands a check function into a DataFrame."""
    lazy = getattr(data, "lazyframe", None)
    if lazy is not None:
        return lazy.collect()
    if isinstance(data, pl.DataFrame):
        return data
    if isinstance(data, pl.LazyFrame):
        return data.collect()
    raise TypeError(f"unsupported pandera check input: {type(data).__name__}")


def _bool_col(mask: pl.Series) -> pl.LazyFrame:
    """Wrap a boolean mask as the single-column LazyFrame a check must return.

    pandera 0.20 narrows the accepted return type with two ``@overload`` stubs
    in ``backends/polars/checks.py``: ``bool`` short-circuits, anything else
    must be a ``pl.LazyFrame``. A ``pl.DataFrame`` falls through to the
    catch-all overload and raises ``output type of check_fn not recognized``,
    and a ``pl.Series`` fails earlier still with ``'Series' object has no
    attribute 'collect_schema'``. Both were hit in this session before the
    backend source was read. Single-column naming matters too: the postprocessor
    selects the frame's first column, so the mask must be the only one.
    """
    return pl.DataFrame({"ok": mask}).lazy()


def _amount_format(data: object) -> pl.LazyFrame:
    """A non-negative decimal with at most two places, in either encoding the
    corpus actually uses.

    Checked on the string form, before any conversion. Parsing first and validating
    the float afterwards would accept ``"nan"`` and ``"inf"``, neither of which is
    money.

    The scientific alternative is not a loosening; it is the corpus. 5,650 of
    6,362,620 rows arrive as ``1.000191239E7`` rather than ``10001912.39`` because
    PaySim's writer emitted a float repr for the largest TRANSFER amounts (measured
    over the full file in P1b; the count is printed by ``oxbow ingest`` and recorded
    in ``data/DATASET_CARD.md``). Every one of those 2,444 distinct strings expands
    through ``Decimal`` to exactly two places, so the value is the same money in a
    different form and rejecting the form would quarantine 5,650 real transactions.
    The expansion is counted rather than assumed silent, and three-place values are
    still refused, because a third decimal is not a rounding decision we get to make.
    """
    f = _frame(data)
    return _bool_col(
        f["amount"].str.strip_chars().str.contains(r"^\d+(\.\d{1,2})?$|^\d+(\.\d+)?[Ee]\+?\d+$")
    )


def _no_self_transfer(data: object) -> pl.LazyFrame:
    """Originator and destination must differ.

    A self-transfer is a balance movement with no counterparty, so it has no
    edge to contribute to a graph. Rejecting it here means it cannot quietly
    become a node with degree zero that every graph query has to special-case.
    """
    f = _frame(data)
    return _bool_col(f["nameOrig"] != f["nameDest"])


def _nonnegative_balance(data: object) -> pl.LazyFrame:
    """Balances cannot be negative in this corpus.

    PaySim's balance columns are documented as internally inconsistent with
    `amount`. They are carried for the exposure proxy and are deliberately NOT
    reconciled against the amount: reconciling them would be precisely the
    silent repair this schema exists to prevent. A negative balance is still
    worth refusing, because it means the source changed shape.
    """
    f = _frame(data)
    return _bool_col(
        (f["oldbalanceOrg"] >= 0)
        & (f["newbalanceOrig"] >= 0)
        & (f["oldbalanceDest"] >= 0)
        & (f["newbalanceDest"] >= 0)
    )


def _fraud_is_binary(data: object) -> pl.LazyFrame:
    """Both label columns must be 0 or 1.

    A label outside {0, 1} is not a weaker signal, it is a different encoding,
    and accepting it would put an unlearnable target into the training set.
    """
    f = _frame(data)
    return _bool_col(f["isFraud"].is_in([0, 1]) & f["isFlaggedFraud"].is_in([0, 1]))


def _type_is_known(data: object) -> pl.LazyFrame:
    """`type` must be one of the five PaySim transaction types.

    An unrecognised type would flow into `txn_type` and then into features,
    where it becomes a category nobody has ever reasoned about. Failing here
    means the corpus changed and a human decides what the new type means.
    """
    f = _frame(data)
    return _bool_col(f["type"].is_in(list(PAYSIM_TXN_TYPES)))


raw_paysim_schema = pa.DataFrameSchema(
    {
        # `step` is a synthetic day counter, not a timestamp. It becomes a UTC
        # instant at the adapter boundary and never reaches a feature as a raw
        # number, because a model would learn "later day = more fraud" and that
        # is a simulator artefact rather than a finding.
        "step": pa.Column(pl.Int64, nullable=False),
        # Amount arrives as a decimal string. It is parsed to integer minor
        # units at the boundary, never carried as a float: 0.10 is not
        # representable in binary floating point, and money that cannot be
        # represented cannot be summed.
        "type": pa.Column(pl.String, nullable=False),
        "amount": pa.Column(pl.String, nullable=False),
        "nameOrig": pa.Column(pl.String, nullable=False),
        "nameDest": pa.Column(pl.String, nullable=False),
        "oldbalanceOrg": pa.Column(pl.Float64, nullable=False),
        "newbalanceOrig": pa.Column(pl.Float64, nullable=False),
        "oldbalanceDest": pa.Column(pl.Float64, nullable=False),
        "newbalanceDest": pa.Column(pl.Float64, nullable=False),
        "isFraud": pa.Column(pl.Int64, nullable=False),
        "isFlaggedFraud": pa.Column(pl.Int64, nullable=False),
    },
    # Frame-level checks, because that is the only level where `error=` is
    # accepted: `ComponentSchema.__init__` rejects the keyword outright, which
    # was found by constructing the schema rather than by reading its docs.
    # Each carries a machine-readable code so a quarantine record names the rule
    # it failed instead of quoting a stack trace.
    checks=[
        pa.Check(_amount_format, error="amount_format"),
        pa.Check(_no_self_transfer, error="self_transfer"),
        pa.Check(_nonnegative_balance, error="negative_balance"),
        pa.Check(_fraud_is_binary, error="label_not_binary"),
        pa.Check(_type_is_known, error="unknown_txn_type"),
    ],
    # strict=True: a column we do not know about is schema drift, and silently
    # ignoring it is how a renamed upstream column becomes an all-null feature
    # that nobody notices until a demo. The offending column names itself in the
    # error, which is what `test_unknown_column_fails_closed` asserts.
    strict=True,
    coerce=False,
    name="raw_paysim",
)

__all__ = [
    "LABEL_CAVEAT",
    "PAYSIM_RAW_COLUMNS",
    "PAYSIM_TXN_TYPES",
    "raw_paysim_schema",
]
