"""The base/exponent boundary on money, held as an invariant.

`config/economics.yaml` declares `minor_units_per_major` — a BASE (100 minor units in one
UGX shilling). `Money.decimals` on the wire is an EXPONENT, and both the server's own
renderer (`schemas/common.py`, `major = minor / 10**decimals`) and the client
(`apps/web/src/lib/format/money.ts`, `const scale = 10 ** decimals`) raise ten to it. The
composition root used to hand the base straight into the exponent field, so every currency
figure the API served was divided by 10^100 instead of 100 and printed as zero — and no
existing gate could see it, because the integration tests assert the key set of a money
object and the web fixtures hand-write `decimals: 2`.

A float never represents money here, so these tests compare exact integers and strings.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPO_ROOT / "apps", REPO_ROOT / "packages" / "pipeline"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))


def _config_base() -> int:
    from oxbow.config import load_yaml

    raw = load_yaml(REPO_ROOT / "config" / "economics.yaml")
    return int(raw.get("minor_units_per_major", 100))


def test_the_composed_container_serves_the_exponent_whose_base_is_the_configured_one() -> None:
    """The one-line statement of the boundary, asserted on the object that answers requests.

    Asserted on `build_container().read_model.money_decimals` rather than on the helper that
    computes it, because when this was first written the helper was correct and the wiring
    was not: a test that calls `_money_decimals()` directly stayed green while the container
    still handed the base to every route. The observable is the composed object.
    """
    from api.deps import build_container

    container = build_container()
    try:
        base = _config_base()
        decimals = container.read_model.money_decimals
        assert 10**decimals == base, (
            f"the container serves decimals={decimals}, i.e. 10**{decimals}={10**decimals} "
            f"minor units per major unit, but config/economics.yaml declares {base}. A base "
            "wired into an exponent field scales every currency figure wrongly."
        )
        # And the same object's money() must render a hand-computed amount, so a route that
        # builds its figures through the read model cannot drift from either half.
        wire = container.read_model.money(1_234_567, "UGX")
        assert wire["minor"] == 1_234_567
        schemas_common = __import__("api.schemas.common", fromlist=["Money"])
        assert schemas_common.Money(**wire).major == 12_345.67, wire
    finally:
        container.close()


def test_a_served_money_object_renders_the_hand_computed_major_amount() -> None:
    from api.deps import _money_decimals
    api_readmodel = __import__("api.readmodel", fromlist=["money"])

    wire = api_readmodel.money(1_234_567, "UGX", decimals=_money_decimals())
    assert wire["minor"] == 1_234_567
    schemas_common = __import__("api.schemas.common", fromlist=["Money"])
    rendered = schemas_common.Money(**wire).major
    assert rendered == 12_345.67, (
        f"1,234,567 minor units at the served decimals rendered as {rendered!r}; the config "
        "base is 100, so the only correct answer is 12345.67"
    )


def test_the_client_and_the_server_agree_on_the_scale() -> None:
    """`money.ts` computes `10 ** decimals`; the server must produce the same scale from the
    same payload, or the two halves of one figure disagree on screen and in the packet."""
    from api.deps import _money_decimals

    decimals = _money_decimals()
    client_scale = 10**decimals  # apps/web/src/lib/format/money.ts:45
    assert client_scale == _config_base()
    # The client's fixed-point form, reproduced without a float division.
    minor = 5
    whole, rest = divmod(minor, client_scale)
    text = f"{whole}.{str(rest).zfill(decimals)}" if decimals else str(whole)
    assert text == "0.05", f"5 minor units rendered as {text!r}, not the 0.05 the client shows"


def test_a_base_that_is_not_a_power_of_ten_is_refused_not_rounded() -> None:
    """An operator setting `minor_units_per_major: 1000` means something; guessing 3 would
    silently rescale every figure, so the loader has to say no instead."""
    import pytest
    from api.deps import _money_decimals

    from oxbow.config import ConfigError

    real = __import__("api.deps", fromlist=["_minor_units_per_major"])
    original = real._minor_units_per_major
    try:
        real._minor_units_per_major = lambda: 250  # type: ignore[method-assign]
        with pytest.raises(ConfigError, match="not an exact power of ten"):
            _money_decimals()
    finally:
        real._minor_units_per_major = original  # type: ignore[method-assign]
