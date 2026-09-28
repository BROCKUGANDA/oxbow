"""A goAML transaction whose money was invented is a boundary failure, not a default.

The renderer used to write ``currency = str(txn.get("currency", "XXX"))`` and
``amount = int(txn.get("amount_minor", 0))``. ISO 4217's ``XXX`` means *no currency*, so a
canonical row that somehow lacked one emitted a filing to an intelligence unit asserting the
transaction had no currency; and a missing amount became a payment of zero. The canonical contract
declares both non-nullable, so the branches are unreachable-by-contract and wrong-if-reached —
03 §A rule 1 wants the loud failure, and DEV-005's whole no-implicit-FX rule exists to stop a
money figure whose currency was made up.

These are the two tests that fail if the defaults come back. They are written against the
transaction element the document actually carries, not against the helper's return value, because
the thing at stake is what a regulator-facing draft would say.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPO_ROOT / "apps", REPO_ROOT / "packages" / "pipeline"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from oxbow.adapters.goaml.xml import (  # noqa: E402
    GOAML_NAMESPACE,
    GoamlMoneyError,
    render_case_bundle,
)
from tests.unit.p9_fixtures import assumption_fixture, make_bundle  # noqa: E402

# Hand-set: 5,000.00 UGX in minor units, and the scale the document must state to make `500000`
# read as five thousand rather than five hundred thousand (the module header's own example).
AMOUNT_MINOR = 500_000
TXN: dict[str, Any] = {
    "txn_id": "paysim:tx0001",
    "amount_minor": AMOUNT_MINOR,
    "currency": "UGX",
    "event_ts_utc": None,
    "txn_type": "transfer",
    "src_account_key": "a1b2c3d4e5f6",
    "dst_account_key": "b2c3d4e5f6a7",
}


def _transactions(xml: bytes) -> list[ET.Element]:
    root = ET.fromstring(xml)
    return root.findall(f".//{{{GOAML_NAMESPACE}}}FinancialTransaction")


def _render(txn: dict[str, Any]) -> bytes:
    econ, block = assumption_fixture(REPO_ROOT)
    return render_case_bundle(make_bundle(economics=econ, block=block), [txn])


def test_a_whole_transaction_renders_with_its_currency_and_scale() -> None:
    """The happy path, asserted as bytes so the refusal tests mean something by contrast."""
    [element] = _transactions(_render(TXN))
    amount = element.find(f"{{{GOAML_NAMESPACE}}}TransactionAmount")

    assert amount is not None and amount.text == str(AMOUNT_MINOR)
    assert amount.get("currency") == "UGX", "the ISO attribute the amount is meaningless without"
    scale = amount.find(f"{{{GOAML_NAMESPACE}}}ValueScale")
    assert (
        scale is not None and scale.text == "-2"
    ), "minor units plus a declared scale is what makes 500000 mean 5,000.00"


@pytest.mark.parametrize("bad", [None, "  ", 12, ""])
def test_a_transaction_without_a_currency_is_refused_not_filed_as_no_currency(bad: Any) -> None:
    """`XXX` is a real ISO code and it means "no currency" — which is a claim, not a blank."""
    with pytest.raises(GoamlMoneyError, match="no currency") as caught:
        _render({**TXN, "currency": bad})

    message = str(caught.value)
    assert TXN["txn_id"] in message, "the refusal has to name the transaction it refuses"
    assert repr(bad) in message, message


@pytest.mark.parametrize("bad", [None, "500000", 5000.0, True])
def test_a_transaction_without_an_integer_amount_is_refused_not_filed_as_zero(bad: Any) -> None:
    """A missing amount rendered as 0 would state a payment that did not happen.

    ``0`` itself is NOT here, and that distinction is the point: a zero-value transaction is a real
    amount, while an *absent* one defaulted to zero. ``True`` is Python's ``int`` and DEV-005 names
    a boolean in a money column as the defect.
    """
    with pytest.raises(GoamlMoneyError, match="not an integer minor-unit amount") as caught:
        _render({**TXN, "amount_minor": bad})

    message = str(caught.value)
    assert TXN["txn_id"] in message, message
    assert repr(bad) in message, message


def test_a_zero_amount_is_a_transaction_and_renders() -> None:
    """Refusing the default must not become refusing the value.

    The old code's sin was inventing ``0`` for a missing amount; a row that genuinely says zero
    still has to render, or the fix would have traded one wrong answer for a blank filing.
    """
    [element] = _transactions(_render({**TXN, "amount_minor": 0}))
    amount = element.find(f"{{{GOAML_NAMESPACE}}}TransactionAmount")
    assert amount is not None and amount.text == "0" and amount.get("currency") == "UGX"


def test_a_refused_render_produces_no_bytes_for_the_sink_to_write() -> None:
    """Rendering is pure, so a refusal cannot hand the sink a half-built draft.

    The distinction matters: "raises" is safe, "raises after returning bytes" is not, because the
    sink writes whatever it is given. Asserted on the call, not on a file, because the renderer has
    no filesystem to corrupt — which is the property that makes the refusal enough.
    """
    econ, block = assumption_fixture(REPO_ROOT)
    bundle = make_bundle(economics=econ, block=block)

    with pytest.raises(GoamlMoneyError):
        render_case_bundle(bundle, [{**TXN, "currency": None}])

    # The same bundle renders whole afterwards: the refusal was about the transaction, not the run.
    assert len(_transactions(render_case_bundle(bundle, [TXN]))) == 1
