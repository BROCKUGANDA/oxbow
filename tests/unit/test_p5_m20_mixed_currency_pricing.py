"""M20: pricing is single-currency while the landed corpus is not.

``config/economics.yaml`` declares ``currency: UGX``, and every cost term in it —
``analyst.cost_per_minute_minor``, ``friction_cost_minor``,
``four_eyes.threshold_exposure_minor`` — is a minor-unit count of that one money. The run this
repository actually landed is not in UGX: the score stage prints
``label carried outside the matrix: True | currencies: EUR``. So every exposure in the stored
``economics`` rows disagrees with the currency the costs are stated in, and the pricing layer
has to answer that question one way or another.

It used to answer by raising ``CurrencyMismatchError``. That refusal is correct arithmetic and
wrong plumbing at the same time: the error is a ``QuantError``, which is a ``RuntimeError``,
and the API's problem-document handlers cover ``OxbowError``, request validation and HTTP
exceptions — not this. So on the two routes that price stored rows the refusal reaches the
catch-all and the user gets a 500 that names neither the account nor the assumption to fix.

The behaviour these tests hold is: refuse the row and label it. Price per currency group is
NOT offered here as an option, because it is not available — pricing an EUR exposure against
UGX analyst minutes would mean inventing an EUR cost basis, which is the implicit FX the money
rules forbid. Silently converting is the one outcome no arm of this layer provides, and one
test below exists solely to keep it that way.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from oxbow.config import find_repo_root
from oxbow.quant.economics import Economics, load_economics
from oxbow.quant.ev import (
    AccountEV,
    CalibratedScore,
    PricingOutcome,
    UnpriceableRow,
    price_account,
    price_exposures,
    price_row,
)
from oxbow.quant.exposure import ExposureResult
from oxbow.quant.money import CurrencyMismatchError, Money

REPO_ROOT: Final = Path(find_repo_root())
for _extra in (REPO_ROOT / "apps", REPO_ROOT / "apps" / "api"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from api.problems import OxbowError, register_problem_handlers  # noqa: E402

# The two currencies the finding is about: the one the costs are declared in, and the one the
# landed corpus arrived in.
CONFIGURED: Final = "UGX"
CORPUS: Final = "EUR"
T0: Final = datetime(2024, 3, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def cfg() -> Economics:
    """The shipped assumptions, unmodified — the finding is about this file, not a variant."""
    return load_economics(REPO_ROOT)


def _exposure(account: str, minor: int, currency: str) -> ExposureResult:
    outflow = Money(minor, currency)
    return ExposureResult(
        account=account,
        cluster=(account,),
        first_trigger_ts=T0,
        window_end=T0 + timedelta(hours=24),
        window_hours=24,
        hops=1,
        outflow=outflow,
        inflow=Money(minor * 2, currency),
        exposure=outflow,
        edges_in_window=1,
        capped_by_inflow=False,
    )


def _score(account: str, p_calibrated: float = 0.8) -> CalibratedScore:
    return CalibratedScore(account, p_calibrated, "C", p_calibrated, 400)


def _mixed_corpus() -> tuple[list[ExposureResult], list[CalibratedScore]]:
    """Two accounts priced in the configured money, three in the currency that landed."""
    rows = [
        _exposure("acct_ugx_a", 500_000_000, CONFIGURED),
        _exposure("acct_ugx_b", 120_000_000, CONFIGURED),
        _exposure("acct_eur_c", 4_800_000, CORPUS),
        _exposure("acct_eur_d", 900_000, CORPUS),
        _exposure("acct_eur_e", 12_000_000, CORPUS),
    ]
    return rows, [_score(row.account) for row in rows]


# --- the fix: refuse and label, do not raise, do not convert ----------------


def test_a_foreign_currency_group_is_refused_and_labelled_not_raised(cfg: Economics) -> None:
    """A mixed-currency corpus comes back priced where it can be and labelled where it can't.

    Before this change `price_exposures` raised `CurrencyMismatchError` on the first EUR row,
    so nothing came back at all — not the two UGX accounts it could have priced, and not a
    reason a reviewer could act on. Both halves matter: the honest answer is partial, and the
    gap is data.
    """
    assert (
        cfg.currency == CONFIGURED
    ), "the finding is about this declaration; if it moved, so did the test"

    exposures, scores = _mixed_corpus()
    outcome = price_exposures(exposures, scores, cfg)

    assert isinstance(outcome, PricingOutcome)
    assert [row.account_key for row in outcome.priced] == [
        "acct_ugx_a",
        "acct_ugx_b",
    ], "the configured-currency group must be priced, and in density order"
    assert [row.account_key for row in outcome.unpriceable] == [
        "acct_eur_c",
        "acct_eur_d",
        "acct_eur_e",
    ]
    # Every alerted account is accounted for: nothing silently dropped.
    assert len(outcome.priced) + len(outcome.unpriceable) == len(exposures)

    refused = outcome.unpriceable[0]
    assert isinstance(refused, UnpriceableRow)
    assert refused.exposure.currency == CORPUS
    assert refused.configured_currency == CONFIGURED
    assert CORPUS in refused.reason and CONFIGURED in refused.reason
    assert "economics.yaml" in refused.reason, "the label must name the assumption to change"
    assert "difference of two unrelated monies" in refused.reason
    assert "unpriced" in refused.label and "no implicit FX" in refused.label

    # The groups are reported, because the run's own report already prints the corpus's
    # currencies and the reader has to be able to see which one did not get priced.
    assert outcome.groups == ((CORPUS, 3), (CONFIGURED, 2))
    assert "3 refused" in outcome.refused_note
    assert "No exchange rate was applied." in outcome.refused_note

    # Grouping changes nothing about the arithmetic that does run: the priced rows are
    # bit-identical to pricing each one directly.
    for row in outcome.priced:
        direct = price_account(_score(row.account_key), row.exposure, cfg)
        assert repr(direct.ev.minor) == repr(row.ev.minor)
        assert repr(direct.density_ratio) == repr(row.density_ratio)


def test_no_amount_is_converted_into_the_configured_currency(cfg: Economics) -> None:
    """The refusal carries the foreign money untouched, and holds out no EV to total.

    The tempting wrong fix is to price the EUR row anyway and stamp UGX on the result. This
    asserts the three things that make that impossible to do quietly: the exposure keeps its
    own currency and digits, the unpriceable row has no money field at all, and totalling
    across the two groups still raises.
    """
    exposures, scores = _mixed_corpus()
    outcome = price_exposures(exposures, scores, cfg)

    refused = {row.account_key: row for row in outcome.unpriceable}
    assert refused["acct_eur_e"].exposure == Money(12_000_000, CORPUS)
    assert not hasattr(refused["acct_eur_e"], "ev"), "a refusal must not carry an expected value"
    assert not hasattr(refused["acct_eur_e"], "expected_intercept")

    priced_minor = {row.account_key: row.exposure for row in outcome.priced}
    assert all(amount.currency == CONFIGURED for amount in priced_minor.values())

    # Adding the two groups together is still refused by the money layer, so no downstream
    # total can quietly bridge them.
    foreign = refused["acct_eur_c"].exposure
    own = next(iter(priced_minor.values()))
    with pytest.raises(CurrencyMismatchError, match="no implicit FX"):
        _ = own + foreign

    # And the raising arm is unchanged: a caller that has committed to one currency still
    # stops on a contradicting row rather than receiving a labelled result it might ignore.
    with pytest.raises(CurrencyMismatchError, match="two unrelated monies"):
        price_account(_score("acct_eur_c"), Money(4_800_000, CORPUS), cfg)
    assert isinstance(
        price_row(_score("acct_eur_c"), Money(4_800_000, CORPUS), cfg), UnpriceableRow
    )
    assert isinstance(
        price_row(_score("acct_ugx_a"), Money(500_000_000, CONFIGURED), cfg), AccountEV
    )


# --- the symptom: a refusal the HTTP layer has no shape for -----------------


def _app_with(
    pricing: Callable[[CalibratedScore, Money, Economics], object], economics: Economics
) -> TestClient:
    """A bare app carrying the project's real problem handlers, with one pricing route.

    Deliberately not the whole API: the claim under test is about which exception type the
    handlers cover, and a route that calls `price_account` on a stored EUR exposure reproduces
    it without a database in the way.
    """
    app = FastAPI()
    register_problem_handlers(app)
    exposures, scores = _mixed_corpus()

    @app.get("/price/{account_key}")
    def _route(account_key: str) -> dict[str, object]:
        exposure = next(row for row in exposures if row.account == account_key)
        score = next(row for row in scores if row.account_key == account_key)
        result = pricing(score, exposure.exposure, economics)
        if isinstance(result, UnpriceableRow):
            return {"priced": False, "label": result.label, "reason": result.reason}
        return {"priced": True, "ev_minor": result.ev.minor, "currency": result.ev.currency}

    return TestClient(app, raise_server_exceptions=False)


def test_the_raising_arm_still_leaves_as_a_bare_500_and_the_labelled_arm_does_not(
    cfg: Economics,
) -> None:
    """The 500 is measured, not read off a table of registered handlers.

    Two routes, one difference: which arm of the pricing layer they call. `CurrencyMismatchError`
    is a ``QuantError`` and the registered handlers cover ``OxbowError`` / request validation /
    HTTP exceptions, so the refusal falls through to the catch-all: status 500,
    ``internal-error``, a detail that says only that the request failed inside the API, and not
    one word about which two monies disagreed. The traceback does reach the log, which is the
    operator's half of the story; the caller's half — my currency, the config's, or a bad row —
    is gone. The labelled arm answers 200 with both currencies and the reason, which is the
    sentence an analyst can act on.
    """
    assert not issubclass(CurrencyMismatchError, OxbowError), (
        "if the API now has a problem class for a currency refusal, this route's 500 is gone "
        "and the finding has moved: wire the caller to `price_row` and re-point this test"
    )

    def raising(score: CalibratedScore, exposure: Money, economics: Economics) -> AccountEV:
        return price_account(score, exposure, economics)

    def labelling(score: CalibratedScore, exposure: Money, economics: Economics) -> object:
        return price_row(score, exposure, economics)

    broken = _app_with(raising, cfg)
    response = broken.get("/price/acct_eur_c")
    assert response.status_code == 500, (
        f"the bare 500 is the finding; if the API now handles this, wire price_row out of it "
        f"instead — got {response.status_code}"
    )
    assert "internal-error" in response.text
    generic = response.json()
    assert "failed inside the API" in generic["detail"]
    assert "EUR" not in response.text and "UGX" not in response.text, (
        "the 500 must not name the currencies; if it does, the API has grown a handler for "
        "this refusal and the finding has moved to the caller wiring"
    )

    fixed = _app_with(labelling, cfg)
    refused = fixed.get("/price/acct_eur_c")
    assert refused.status_code == 200, refused.text
    body = refused.json()
    assert body["priced"] is False
    assert CORPUS in body["label"] and CONFIGURED in body["label"]
    assert "no implicit FX" in body["label"]
    # The same route prices the configured group normally, so the label is not a blanket no.
    priced = fixed.get("/price/acct_ugx_a")
    assert priced.status_code == 200, priced.text
    assert priced.json() == {
        "priced": True,
        "ev_minor": price_account(
            _score("acct_ugx_a"), Money(500_000_000, CONFIGURED), cfg
        ).ev.minor,
        "currency": CONFIGURED,
    }
