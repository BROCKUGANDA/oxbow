"""Pricing an alert: ``EV_i = p_i * E_i * r - c_i - (1 - p_i) * f``.

This is the module that turns a probability into money, which is the argument of
the whole product (plan §11) and therefore the module that has to be honest about
what it multiplied. Three rules govern everything below:

* **Integers on the money side, named ratios on the other side.** ``p_i`` and
  ``r`` are converted once into integer micro-ratios (``money.ratio_to_micro``)
  and every product is exact integer arithmetic with a single stated rounding
  rule. A float ``0.35`` is not exactly ``0.35``, and a queue of a few thousand
  accounts multiplied by it would drift by more than the minor units a packet
  prints.
* **The sensitivity band is the return type, not an option.** Anything that hands
  money to a human goes out as a :class:`~oxbow.quant.economics.CurrencyFigure`
  built over the configured band, so the band cannot be dropped at a call site.
* **A negative expectation is not a candidate, and it is still reported.** The
  allocator sees only accounts with ``EV_i > 0``; the reason the rest exist is
  handed back as the break-even recovery rate rather than silently filtered
  away, because "nothing pays for itself at r = 0.35" and "there is no r that
  makes anything pay" are different answers.
* **A currency this configuration cannot price is refused and labelled, never
  converted.** ``config/economics.yaml`` declares exactly one ``currency``, and every cost
  term in it — the per-minute analyst price, the friction cost, the four-eyes threshold — is
  a minor-unit count of that one money. There is consequently no cost basis to price a
  second currency group against, so :func:`price_row` and :func:`price_exposures` hand the
  foreign rows back labelled rather than raising: ``CurrencyMismatchError`` is a quant error
  and not an API problem class, so on a route with no handler for it the refusal leaves as an
  unhandled 500 that names neither the account nor the assumption to change.

``CalibratedScore`` is the seam to P4: this layer takes a probability plus the
calibration bin's observed rate and sample size, and never imports the scoring
package. Calibration is load-bearing here for exactly the reason plan §10 states
it — this module multiplies those probabilities by money, so a miscalibrated p is
a wrong currency figure rather than a merely wrong ranking.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

import polars as pl

from oxbow.quant.economics import (
    CurrencyFigure,
    Economics,
    assumption_block,
    currency_figure,
    currency_figure_over_band,
)
from oxbow.quant.exposure import ExposureResult
from oxbow.quant.money import (
    MICRO,
    CurrencyMismatchError,
    Money,
    QuantError,
    ratio_to_micro,
    scale_div,
)

# The columns P4 hands over, named here and asserted in a test against the
# scoring layer's output, because a renamed column would otherwise be read as a
# missing probability and defaulted to something.
SCORE_COLUMNS: Final = (
    "account_key",
    "p_calibrated",
    "alert_class",
    "band_observed_rate",
    "band_n",
)

NO_INVESTMENT_MESSAGE: Final = "under these assumptions no investigation pays for itself"

# A break-even rate is reported to four decimals: a rate that only crosses at the
# third (a low-exposure, high-cost account) must not print as 0.000, because "r =
# 0.000" reads as "already profitable" and is the opposite of the finding.
_RATE_DECIMALS: Final = 4


class PricingError(QuantError):
    """Raised when an alert cannot be priced at all, rather than priced badly."""


@runtime_checkable
class _HasIterRows(Protocol):
    """The minimum a frame must offer to be read here.

    Declared structurally so this layer accepts a plain list of dicts in a unit
    test and a collected Polars frame in the pipeline without either importing the
    other.
    """

    def iter_rows(self, *, named: bool = ...) -> Iterable[Mapping[str, object]]: ...


@dataclass(frozen=True, slots=True)
class CalibratedScore:
    """One account's calibrated probability, with the evidence behind it.

    ``band_observed_rate`` and ``band_n`` travel because the confidence label a
    money figure inherits is "observed rate in this band: 71%, n=432" — plan §10
    forbids an invented adjective, and the EV below is downstream of the same bin.

    Both are optional because a fold whose calibration was refused has no rate and no
    population to report, and it still has an account to rank: 03 §H labels that state
    instead of dropping it, and ``score.calibration_kind`` carries the label into this
    layer. The two travel together — a rate without its ``n`` is an adjective and an
    ``n`` without its rate is a census of nothing — so ``None`` here means "both absent",
    which is the same pairing ``ck_score_calibration_pairing`` enforces in the table.
    ``p_calibrated`` stays required: the money layer cannot price an account with no
    probability at all, and substituting one it did not measure is the fabrication this
    file's own guards exist to stop.
    """

    account_key: str
    p_calibrated: float
    alert_class: str
    band_observed_rate: float | None
    band_n: int | None

    def __post_init__(self) -> None:
        if not self.account_key:
            raise PricingError("account_key is required to price an alert")
        for name, value in (
            ("p_calibrated", self.p_calibrated),
            ("band_observed_rate", self.band_observed_rate),
        ):
            if value is None:
                continue
            if not 0.0 <= value <= 1.0:
                raise PricingError(
                    f"{name} is {value} for {self.account_key}, outside [0, 1]. A "
                    "probability outside its own range is a calibration bug, and here "
                    "it is a currency-figure bug too."
                )
        if self.band_observed_rate is None and self.band_n is not None:
            raise PricingError(
                f"{self.account_key} reports band_n={self.band_n} with no observed rate. "
                "A population with no rate measured over it is the half of a pairing, and "
                "the pairing is the invariant."
            )
        if self.band_n is not None and self.band_n < 0:
            raise PricingError(f"band_n cannot be negative for {self.account_key}")

    @property
    def confidence_label(self) -> str:
        """The calibration statement that travels with a money figure."""
        if self.band_observed_rate is None or self.band_n is None:
            # The pipeline's own words, not a synonym invented here: this is the state
            # `CalibrationResult.confidence_label` names `uncalibrated`, and a money
            # figure built on an uncalibrated probability has to say so on its face.
            return (
                f"p={self.p_calibrated:.2f} from alert class {self.alert_class} "
                "(probabilities are uncalibrated: no observed rate was measured for the "
                "fold that scored this account)"
            )
        return (
            f"p={self.p_calibrated:.2f} from alert class {self.alert_class} (observed "
            f"rate in this band: {self.band_observed_rate:.0%}, n={self.band_n})"
        )


@dataclass(frozen=True, slots=True)
class AccountEV:
    """A priced alert: the money terms, their sum, and the ranking key.

    ``density_ratio`` is minor units of EV per analyst-minute. It is a *ratio*: the
    ranking key, never a rendered amount. Printing it as money would present a
    per-minute rate as if it were a sum, which is the figure nobody can reconcile
    against the queue total.
    """

    score: CalibratedScore
    exposure: Money
    review_minutes: int
    review_cost: Money
    expected_friction_cost: Money
    expected_intercept: Money
    ev: Money
    density_ratio: float

    @property
    def account_key(self) -> str:
        """The account this row prices."""
        return self.score.account_key

    @property
    def p_calibrated(self) -> float:
        """The calibrated probability behind this row."""
        return self.score.p_calibrated

    @property
    def is_worth_reviewing(self) -> bool:
        """Whether the review pays for itself under the configured default ``r``."""
        return self.ev.is_positive


def _joint_micro(p_calibrated: float, rate: float) -> int:
    """``p * r`` as one integer micro-ratio, so the exposure is scaled once.

    Scaling twice (``E * p`` then ``* r``) would round twice, and the two halves of
    one expected value would then disagree by a minor unit on some rows.
    """
    return scale_div(ratio_to_micro(p_calibrated) * ratio_to_micro(rate), MICRO)


def price_at_rate(row: AccountEV, rate: float, cfg: Economics) -> Money:
    """Re-price an already-priced row at a different recovery rate.

    Only ``expected_intercept`` depends on ``r``: the review cost is spent either
    way and the friction term is a function of ``p`` alone. The sensitivity band is
    built on this, which is what keeps the three rendered values consistent with
    one another instead of being re-derived three times.
    """
    if row.exposure.currency != cfg.currency:
        raise CurrencyMismatchError(
            f"cannot re-price {row.account_key} at r={rate}: its exposure is "
            f"{row.exposure} while {cfg.source_path.name} states costs in {cfg.currency}. "
            "A band figure whose terms are in two monies is not a sensitivity check."
        )
    intercept = row.exposure.scaled_by_micro(_joint_micro(row.p_calibrated, rate))
    return intercept - row.review_cost - row.expected_friction_cost


def _currency_mismatch_message(score: CalibratedScore, exposure: Money, cfg: Economics) -> str:
    """The one sentence both refusal arms print.

    Shared so that the raising and the labelled arm cannot drift into naming different
    problems for the same row.
    """
    return (
        f"exposure for {score.account_key} is {exposure} but "
        f"{cfg.source_path.name} prices review time in {cfg.currency}: EV would be a "
        "difference of two unrelated monies."
    )


def _priced(score: CalibratedScore, exposure: Money, cfg: Economics) -> AccountEV:
    """The §11 arithmetic, once, for an exposure already known to be in ``cfg.currency``.

    Reached only from `price_account` and `price_row`, so the two arms cannot disagree
    about what an account's EV is; they differ solely in what happens when the currency
    does not match.
    """
    minutes = cfg.minutes_for(score.alert_class)
    if minutes < cfg.analyst.min_review_minutes:
        raise PricingError(
            f"alert class {score.alert_class!r} for {score.account_key} is priced at "
            f"{minutes} minutes, below the floor {cfg.analyst.min_review_minutes}: m_i is "
            "the density denominator and must never be zero."
        )
    review_cost = Money(minutes * cfg.analyst.cost_per_minute_minor, cfg.currency)
    friction = cfg.friction_cost.scaled_by_micro(MICRO - ratio_to_micro(score.p_calibrated))
    intercept = exposure.scaled_by_micro(_joint_micro(score.p_calibrated, cfg.recovery.rate))
    ev = intercept - review_cost - friction
    return AccountEV(
        score=score,
        exposure=exposure,
        review_minutes=minutes,
        review_cost=review_cost,
        expected_friction_cost=friction,
        expected_intercept=intercept,
        ev=ev,
        density_ratio=ev.minor / minutes,
    )


@dataclass(frozen=True, slots=True)
class UnpriceableRow:
    """An exposure this configuration cannot price, with the reason carried on the row.

    Deliberately holds no money. An ``EV`` for a foreign-currency exposure could only be
    produced by inventing an FX rate or by borrowing a cost stated in another currency,
    and 02's money rules refuse both — so the honest object is the refusal, not a number.
    The row is returned rather than dropped so that the alerted set and the priced set
    still reconcile: a queue that silently shrinks by the unpriced accounts reads as "we
    found nothing" when the truth is "we could not price what we found", which is the
    exact ambiguity `price_exposures` already refuses on the missing-score side.
    """

    account_key: str
    exposure: Money
    configured_currency: str
    reason: str

    @property
    def label(self) -> str:
        """The short line a queue row or packet prints next to the gap."""
        return (
            f"unpriced: {self.exposure.currency} exposure against "
            f"{self.configured_currency} costs (no implicit FX)"
        )


@dataclass(frozen=True, slots=True)
class PricingOutcome:
    """What pricing a corpus of more than one currency actually produced.

    ``priced`` holds only rows in ``cfg.currency``; every other currency group lands in
    ``unpriceable``, labelled. ``groups`` is the evidence for the sentence a run report has
    to print anyway — this repository's own score stage reports the corpus's distinct
    currencies — so the mismatch is visible in the output rather than only in a traceback.
    """

    priced: tuple[AccountEV, ...]
    unpriceable: tuple[UnpriceableRow, ...]

    @property
    def groups(self) -> tuple[tuple[str, int], ...]:
        """``(currency, account count)`` over every exposure read, in currency order."""
        counts: dict[str, int] = {}
        for row in self.priced:
            counts[row.exposure.currency] = counts.get(row.exposure.currency, 0) + 1
        for row in self.unpriceable:
            counts[row.exposure.currency] = counts.get(row.exposure.currency, 0) + 1
        return tuple(sorted(counts.items()))

    @property
    def refused_note(self) -> str:
        """The honest headline for a partial price, or the clean-run sentence."""
        if not self.unpriceable:
            return f"all {len(self.priced)} accounts priced in {self._currency}."
        foreign = ", ".join(
            f"{code} ({count})" for code, count in self.groups if code != self._currency
        )
        return (
            f"{len(self.priced)} of {len(self.priced) + len(self.unpriceable)} accounts priced "
            f"in {self._currency}; {len(self.unpriceable)} refused for want of "
            f"{self._currency}-denominated costs: {foreign}. No exchange rate was applied."
        )

    @property
    def _currency(self) -> str:
        """The currency the priced rows are in, or the first refusal's own."""
        if self.priced:
            return self.priced[0].exposure.currency
        if self.unpriceable:
            return self.unpriceable[0].configured_currency
        return ""


def price_account(score: CalibratedScore, exposure: Money, cfg: Economics) -> AccountEV:
    """Apply the §11 formula to one account under the configured assumptions.

    Raises on a currency disagreement rather than converting: an exposure observed
    in one currency and a cost stated in another have no difference, and inventing
    the rate that would produce one is exactly the implicit FX the money rules
    forbid.

    This raising contract is the right one for a build that has committed to a single
    currency and must stop when a row contradicts it. A caller reading a corpus of
    unknown currency — a stored row, a landed packet — wants the mismatch as data about
    the row, and `price_row` is that arm: `CurrencyMismatchError` is a quant error, not
    an API problem class, so where a route has no handler for it the refusal leaves as a
    500 that names neither the account nor the assumption to fix.
    """
    if exposure.currency != cfg.currency:
        raise CurrencyMismatchError(_currency_mismatch_message(score, exposure, cfg))
    return _priced(score, exposure, cfg)


def price_row(
    score: CalibratedScore, exposure: Money, cfg: Economics
) -> AccountEV | UnpriceableRow:
    """Price one account, or refuse and label it. Never converts, never raises for currency.

    The non-raising arm of `price_account`, for callers that must keep going over a
    mixed-currency corpus: a foreign exposure becomes an :class:`UnpriceableRow` naming
    the account, both currencies and the reason, so the gap reaches the response as a
    labelled row instead of escaping as an unhandled server error. The sub-floor review
    time still raises — that is a registry/config contradiction on a row this currency
    *can* price, not a currency fact, and defaulting the minutes would price the account
    on a denominator the configuration rejects.
    """
    if exposure.currency != cfg.currency:
        return UnpriceableRow(
            account_key=score.account_key,
            exposure=exposure,
            configured_currency=cfg.currency,
            reason=_currency_mismatch_message(score, exposure, cfg),
        )
    return _priced(score, exposure, cfg)


def price_exposures(
    exposures: Sequence[ExposureResult],
    scores: Sequence[CalibratedScore],
    cfg: Economics,
) -> PricingOutcome:
    """Join exposures to calibrated scores and price each pair, grouped by currency.

    The join fails on either side missing. An account with a probability and no
    exposure prices at ``-c - (1-p)f``, i.e. negative and therefore invisible in
    the queue, which is indistinguishable in the output from "we found nothing
    worth reviewing" — the exact ambiguity 03 §A refuses.

    A currency disagreement is NOT part of that refusal set. Costs exist in exactly one
    currency in this configuration — `analyst.cost_per_minute_minor`,
    `friction_cost_minor` and `four_eyes.threshold_exposure_minor` are all declared as
    ``config/economics.yaml`` minor units of `currency` — so there is no cost basis to
    price a second currency group against, and "price per currency group" would mean
    inventing one. The group matching `cfg.currency` is priced; every other row is
    returned labelled in `PricingOutcome.unpriceable`. Silently converting is the one
    outcome neither arm offers.
    """
    by_account = {score.account_key: score for score in scores}
    priced: list[AccountEV] = []
    unpriceable: list[UnpriceableRow] = []
    for exposure in exposures:
        score = by_account.pop(exposure.account, None)
        if score is None:
            raise PricingError(
                f"{exposure.account} has an exposure but no calibrated probability, so it "
                "cannot be priced. Refusing rather than skipping keeps the alerted set "
                "and the priced set the same size."
            )
        row = price_row(score, exposure.exposure, cfg)
        if isinstance(row, UnpriceableRow):
            unpriceable.append(row)
        else:
            priced.append(row)
    if by_account:
        raise PricingError(
            f"these accounts have probabilities but no exposure: {sorted(by_account)}. "
            "A missing E_i is a window or graph question, not a zero."
        )
    return PricingOutcome(
        priced=tuple(sorted(priced, key=lambda row: (-row.density_ratio, row.account_key))),
        unpriceable=tuple(sorted(unpriceable, key=lambda row: row.account_key)),
    )


def scores_from_frame(frame: pl.DataFrame | _HasIterRows) -> tuple[CalibratedScore, ...]:
    """Read calibrated scores from a Polars frame or any iterable of row mappings.

    Ordered by ``account_key``, so the input order of the scoring layer cannot
    leak into a tie-break downstream.
    """
    rows = list(frame.iter_rows(named=True))
    if rows:
        missing = [column for column in SCORE_COLUMNS if column not in rows[0]]
        if missing:
            raise PricingError(
                f"the calibrated-score frame is missing {missing}; this layer expects "
                f"exactly {list(SCORE_COLUMNS)} and will not guess a probability."
            )
    scores = [_score_from_row(row) for row in rows]
    return tuple(sorted(scores, key=lambda score: score.account_key))


def _score_from_row(row: Mapping[str, object]) -> CalibratedScore:
    return CalibratedScore(
        account_key=str(row["account_key"]),
        p_calibrated=float(str(row["p_calibrated"])),
        alert_class=str(row["alert_class"]),
        band_observed_rate=float(str(row["band_observed_rate"])),
        band_n=int(str(row["band_n"])),
    )


def positive_ev_rows(rows: Iterable[AccountEV]) -> tuple[AccountEV, ...]:
    """The candidates: strictly positive EV, in density order.

    A row with EV exactly zero is excluded, not because it costs money but because
    it proves nothing: scheduling it spends analyst-minutes to move no expected
    value, and the capacity cutoff line would then land inside a run of identical
    numbers that the queue cannot rank by anything but name.
    """
    return tuple(
        sorted(
            (row for row in rows if row.ev.is_positive),
            key=lambda row: (-row.density_ratio, row.account_key),
        )
    )


# --- portfolio totals -----------------------------------------------------


def _selected(rows: Sequence[AccountEV], selected: Sequence[str] | None) -> tuple[AccountEV, ...]:
    """The priced rows for ``selected``, in the order the allocator chose them.

    ``selected=None`` means "every priced row", which is what the uncapped end of
    the frontier and the pre-allocation summary need.
    """
    if selected is None:
        return tuple(rows)
    index = {row.account_key: row for row in rows}
    missing = [key for key in selected if key not in index]
    if missing:
        raise PricingError(f"selected accounts are not priced: {sorted(missing)}")
    return tuple(index[key] for key in selected)


def ev_minor(
    rows: Sequence[AccountEV], selected: Sequence[str] | None, rate: float, cfg: Economics
) -> int:
    """Total expected value at one recovery rate, in minor units."""
    return sum((price_at_rate(row, rate, cfg).minor for row in _selected(rows, selected)), 0)


def expected_loss_avoided(
    rows: Sequence[AccountEV], selected: Sequence[str] | None, rate: float, cfg: Economics
) -> Money:
    """``sum p_i * E_i * r``: the gross figure the dashboard headline quotes.

    Gross by construction. Netting the review cost and the friction term off it and
    still calling the result "loss avoided" is the double-count the two separate
    figures exist to prevent.
    """
    total = sum(
        (
            row.exposure.scaled_by_micro(_joint_micro(row.p_calibrated, rate)).minor
            for row in _selected(rows, selected)
        ),
        0,
    )
    return Money(total, cfg.currency)


def review_cost_total(
    rows: Sequence[AccountEV], selected: Sequence[str] | None, cfg: Economics
) -> Money:
    """Analyst time actually spent on the set, at the configured per-minute price."""
    return Money(sum((row.review_cost.minor for row in _selected(rows, selected)), 0), cfg.currency)


def wrong_touch_friction(
    rows: Sequence[AccountEV], selected: Sequence[str] | None, cfg: Economics
) -> Money:
    """``sum (1 - p_i) * f``: the priced harm of the alerts that were wrong."""
    return Money(
        sum((row.expected_friction_cost.minor for row in _selected(rows, selected)), 0),
        cfg.currency,
    )


def captured_exposure(
    rows: Sequence[AccountEV], selected: Sequence[str] | None, cfg: Economics
) -> Money:
    """``sum E_i`` for the set. Independent of ``r`` by the definition of ``E_i``."""
    return Money(sum((row.exposure.minor for row in _selected(rows, selected)), 0), cfg.currency)


def review_minutes_total(rows: Sequence[AccountEV], selected: Sequence[str] | None) -> int:
    """Analyst-minutes consumed by the set, i.e. the capacity actually used."""
    return sum(row.review_minutes for row in _selected(rows, selected))


def expected_wrongly_touched_count(
    rows: Sequence[AccountEV], selected: Sequence[str] | None
) -> float:
    """``sum (1 - p_i)``: expected count of legitimate customers the set disturbs.

    A count, not money, and named for it. It is the third axis of the efficient
    frontier (plan §11: accounts reviewed, loss avoided, customers wrongly
    touched), and the reason it is an expectation rather than an integer is that
    the queue is chosen on probabilities, not on outcomes.
    """
    return sum(1.0 - row.p_calibrated for row in _selected(rows, selected))


# --- band figures ---------------------------------------------------------


def _figure(
    label: str,
    cfg: Economics,
    value_at: Callable[[float], Money],
    *,
    provenance: Sequence[str] = (),
) -> CurrencyFigure:
    """Build a rate-dependent band figure, so money cannot leave unlabelled.

    The recomputation note is part of the output: a reader comparing the three
    values needs to know they came from one formula evaluated three times, not from
    scaling one number.
    """
    block = assumption_block(cfg)
    note = (
        f"value recomputed at each of the {len(block.rates)} configured recovery rates "
        f"({', '.join(f'{rate:.2f}' for rate in block.rates)})"
    )
    return currency_figure_over_band(
        label,
        block,
        value_at,
        provenance=(*provenance, note),
    )


def ev_figure(
    rows: Sequence[AccountEV],
    selected: Sequence[str] | None,
    cfg: Economics,
    *,
    label: str = "Expected value of the recommended set",
) -> CurrencyFigure:
    """Net expected value of the set, over the band."""
    chosen = _selected(rows, selected)
    minutes = sum(row.review_minutes for row in chosen)
    return _figure(
        label,
        cfg,
        lambda rate: Money(ev_minor(rows, selected, rate, cfg), cfg.currency),
        provenance=(f"{len(chosen)} accounts reviewed, {minutes} analyst-minutes spent",),
    )


def expected_loss_avoided_figure(
    rows: Sequence[AccountEV],
    selected: Sequence[str] | None,
    cfg: Economics,
    *,
    label: str = "Expected loss avoided (gross p*E*r)",
) -> CurrencyFigure:
    """Gross loss avoided by the set, over the band."""
    return _figure(
        label,
        cfg,
        lambda rate: expected_loss_avoided(rows, selected, rate, cfg),
    )


def _constant_figure(
    label: str,
    cfg: Economics,
    value: Money,
    *,
    provenance: Sequence[str] = (),
) -> CurrencyFigure:
    """A money quantity that does not move with ``r``, still rendered over the band.

    The band is the invariant every currency figure carries, so a quantity that
    never depended on the recovery rate appears at all three coordinates with the
    same value — and says why, rather than letting a reader conclude that it was
    tested against the assumption and held.
    """
    block = assumption_block(cfg)
    note = "this quantity does not depend on r by definition; the band is shown because every currency figure carries it"
    return currency_figure(
        label, dict.fromkeys(block.rates, value), block, provenance=(*provenance, note)
    )


def exposure_figure(
    rows: Sequence[AccountEV],
    selected: Sequence[str] | None,
    cfg: Economics,
    *,
    label: str = "Exposure at risk inside the reviewed set",
) -> CurrencyFigure:
    """``sum E_i`` for the set, rendered with the band it never uses and saying so."""
    return _constant_figure(label, cfg, captured_exposure(rows, selected, cfg))


def cost_figure(
    rows: Sequence[AccountEV],
    selected: Sequence[str] | None,
    cfg: Economics,
    *,
    label: str = "Analyst time cost of the recommended set",
) -> CurrencyFigure:
    """Review cost of the set, priced out of minutes at the configured rate."""
    return _constant_figure(label, cfg, review_cost_total(rows, selected, cfg))


# --- the recovery rate that would change the answer ----------------------


@dataclass(frozen=True, slots=True)
class BreakEvenRecovery:
    """The ``r`` at which the first investigation starts paying for itself.

    Plan §11 requires the "no investigation pays for itself" message to arrive
    *with* the recovery rate that would change it. Solving
    ``p*E*r - c - (1-p)*f = 0`` gives ``r* = (c + (1-p)*f) / (p*E)``, and the
    portfolio answer is the minimum over accounts, because one profitable review
    is enough to make the queue non-empty.
    """

    rate: float | None
    account_key: str
    admissible: bool
    pays_at_default: bool
    message: str


def break_even_recovery(rows: Sequence[AccountEV], cfg: Economics) -> BreakEvenRecovery:
    """Solve for the break-even ``r`` and state it as a sentence.

    The rate comes out of integer micro-ratios and is converted to a float only at
    the end, so the number in the sentence is the number the arithmetic used. An
    inadmissible root — outside the configured open interval — is reported as
    inadmissible rather than clamped into the interval, because "no admissible r"
    is the finding and a clamped number would be a fabricated one.
    """
    lower = cfg.recovery.lower_exclusive
    upper = cfg.recovery.upper_exclusive
    default = cfg.recovery.rate
    best: tuple[float, str] | None = None
    pays = 0
    for row in sorted(rows, key=lambda candidate: candidate.account_key):
        if row.ev.is_positive:
            pays += 1
        p_micro = ratio_to_micro(row.p_calibrated)
        denominator = p_micro * row.exposure.minor
        if denominator <= 0:
            # p = 0 or E = 0: no recovery rate rescues an account that is either
            # certainly clean or has nothing interceptable.
            continue
        cost_minor = (row.review_cost + row.expected_friction_cost).minor
        rate_micro = scale_div(cost_minor * MICRO * MICRO, denominator)
        candidate_rate = rate_micro / MICRO
        if best is None or candidate_rate < best[0]:
            best = (candidate_rate, row.account_key)
    if best is None:
        return BreakEvenRecovery(
            rate=None,
            account_key="",
            admissible=False,
            pays_at_default=False,
            message=(
                f"{NO_INVESTMENT_MESSAGE}, and no recovery rate can change that: every "
                "candidate has p = 0 or E_i = 0, so there is nothing interceptable to "
                f"recover (configured r = {default:.2f})."
            ),
        )
    rate, account_key = best
    admissible = lower < rate < upper
    pays_at_default = rate < default
    if pays_at_default:
        outcome = (
            f"{pays} of {len(rows)} accounts already pay for themselves at r = "
            f"{default:.2f}, so the queue is not empty."
        )
    else:
        outcome = (
            f"{NO_INVESTMENT_MESSAGE} at the configured r = {default:.2f} "
            f"({pays} of {len(rows)} accounts pay for themselves)."
        )
    interval_note = (
        f"inside the admissible open interval ({lower:.2f}, {upper:.2f})"
        if admissible
        else f"outside the admissible open interval ({lower:.2f}, {upper:.2f}), so no "
        "recovery rate this product admits changes the answer"
    )
    return BreakEvenRecovery(
        rate=rate,
        account_key=account_key,
        admissible=admissible,
        pays_at_default=pays_at_default,
        message=(
            f"{outcome} The cheapest account to rescue is {account_key}: it breaks even at "
            f"r = {rate:.{_RATE_DECIMALS}f}, {interval_note}."
        ),
    )


__all__ = [
    "NO_INVESTMENT_MESSAGE",
    "SCORE_COLUMNS",
    "AccountEV",
    "BreakEvenRecovery",
    "CalibratedScore",
    "PricingError",
    "PricingOutcome",
    "UnpriceableRow",
    "break_even_recovery",
    "captured_exposure",
    "cost_figure",
    "ev_figure",
    "ev_minor",
    "expected_loss_avoided",
    "expected_loss_avoided_figure",
    "expected_wrongly_touched_count",
    "exposure_figure",
    "positive_ev_rows",
    "price_account",
    "price_at_rate",
    "price_exposures",
    "price_row",
    "review_cost_total",
    "review_minutes_total",
    "scores_from_frame",
    "wrong_touch_friction",
]
