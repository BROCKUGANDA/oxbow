"""Money as integer minor units, plus the rounding rule that keeps it that way.

DEV-005 makes ``amount_minor: int64`` a hard constraint rather than a preference,
and this module is where the constraint becomes inconvenient enough to be felt:
there is no ``Money * float`` here. Scaling money is possible only through an
explicit integer *micro-ratio*, so a rate can be applied to an amount without a
binary float ever entering the sum.

The reason is arithmetic rather than stylistic. ``0.1 + 0.2 != 0.3`` in IEEE-754,
so a float pipeline cannot be trusted to reconcile to the minor unit, and the
failure is invisible: it drifts by fractions of a unit on some rows and surfaces
months later as a packet total that does not add up (03 A rule 2: never let an
unknown become a zero, and never let it become a plausible float instead).

Two decisions are recorded here because they are load-bearing:

* Rounding of ``amount * ratio`` is *round half up* on the floor-division
  remainder (``scale_div``). It is stated once so every figure in the product
  shares one tie-breaking rule instead of inheriting ``round``'s banker's
  rounding in one place and ``int()`` truncation in another.
* A currency figure — the major-unit string a human reads — is produced by
  exactly one caller, ``quant/economics.py``'s renderer. ``to_major_text`` lives
  here because it needs ``minor_units_per_major``, and a test asserts that no
  other module in the package reaches for it. That is what turns "every currency
  figure carries its assumption line" from a convention into a dependency.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, Self

# One million micro-units per unit. Every real-valued rate the quant layer
# multiplies into money is first expressed as an integer count of these, so
# `amount * rate` is integer arithmetic end to end.
MICRO: Final = 1_000_000

# ISO-4217 codes are three uppercase letters; an empty or lowercase code is a
# data-quality bug that must not survive into a total.
_CURRENCY_LEN: Final = 3


class QuantError(RuntimeError):
    """Base class for quant-layer failures that must not be caught broadly.

    01 §G makes "catching a broad exception and returning an empty list" a
    rejection trigger, because the UI then renders an empty state for a server
    bug. Every quant error is a subclass of this one type, so callers can catch
    a quant failure precisely and never widen to ``Exception``.
    """


class CurrencyMismatchError(QuantError):
    """Raised when money of two currencies would have been added.

    02 §money rules: currency is part of every amount, aggregations group by
    currency or fail, and there is no implicit FX. Silently summing EUR cents and
    UGX cents yields a number that is wrong in a way no downstream check can see.
    """


@dataclass(frozen=True, slots=True)
class Money:
    """An amount in integer minor units, tagged with its currency.

    The tag is not decoration. ``Money(500, "UGX")`` and ``Money(500, "EUR")``
    are different quantities that happen to share digits, and the only reason
    ``amount_minor: int64`` is safe to sum is that the currency travels with it.
    """

    minor: int
    currency: str

    def __post_init__(self) -> None:
        if isinstance(self.minor, bool) or not isinstance(self.minor, int):
            raise CurrencyMismatchError(
                f"money must be integer minor units, got {type(self.minor).__name__} "
                f"{self.minor!r} ({self.currency}). Float money is a defect, not a "
                "style choice (DEV-005)."
            )
        if len(self.currency) != _CURRENCY_LEN or not self.currency.isalpha():
            raise CurrencyMismatchError(
                f"currency must be a 3-letter ISO code, got {self.currency!r}"
            )
        object.__setattr__(self, "currency", self.currency.upper())

    @classmethod
    def zero(cls, currency: str) -> Self:
        """The additive identity for ``currency``."""
        return cls(0, currency)

    @property
    def is_positive(self) -> bool:
        """Strictly greater than zero."""
        return self.minor > 0

    @property
    def is_zero(self) -> bool:
        """Exactly zero."""
        return self.minor == 0

    def __add__(self, other: Money) -> Money:
        if self.currency != other.currency:
            raise CurrencyMismatchError(
                f"cannot add {self.currency} {self.minor} to {other.currency} "
                f"{other.minor}: no implicit FX (02 money rules)."
            )
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        return self.__add__(-other)

    def __neg__(self) -> Money:
        return Money(-self.minor, self.currency)

    def _comparable(self, other: Money) -> bool:
        if self.currency != other.currency:
            raise CurrencyMismatchError(
                f"cannot order {self.currency} against {other.currency}: two monies have "
                "no greater-than, and a sort that mixes them silently invents an FX rate."
            )
        return True

    def __lt__(self, other: Money) -> bool:
        return self._comparable(other) and self.minor < other.minor

    def __le__(self, other: Money) -> bool:
        return self._comparable(other) and self.minor <= other.minor

    def __gt__(self, other: Money) -> bool:
        return self._comparable(other) and self.minor > other.minor

    def __ge__(self, other: Money) -> bool:
        return self._comparable(other) and self.minor >= other.minor

    def scaled_by_micro(self, ratio_micro: int) -> Money:
        """Multiply by ``ratio_micro / MICRO``, rounding half up.

        The only way to scale an amount in this module. A ratio expressed in
        micro-units is an integer, so an exposure of 1e9 minor units times a
        recovery rate of 0.35 is exact integer arithmetic rather than a float
        product that happens to print as 349999999.98.
        """
        if isinstance(ratio_micro, bool) or not isinstance(ratio_micro, int):
            raise CurrencyMismatchError(
                f"ratio_micro must be an integer count of micro-units, got "
                f"{type(ratio_micro).__name__}. Convert a float rate with "
                "ratio_to_micro() at the boundary, explicitly."
            )
        return Money(scale_div(self.minor * ratio_micro, MICRO), self.currency)

    def __str__(self) -> str:
        """Minor-unit debug form, deliberately not a currency figure.

        It says ``minor`` so a log line can never be mistaken for a rendered
        amount, and so a rendered amount can only come from the renderer.
        """
        return f"{self.minor} minor {self.currency}"


def scale_div(numerator: int, denominator: int) -> int:
    """Integer division rounded half up, on the floor-division remainder.

    Stated as a rule rather than inherited from ``round``: banker's rounding
    would make ``EV`` depend on which side of even a tie falls on, and
    truncation would bias every expected value downward by up to one minor unit
    per account, which across a few thousand accounts is a visible and wrong
    difference between the queue total and the sum of the rows.
    """
    if denominator == 0:
        raise QuantError("scale_div denominator must be non-zero")
    quotient, remainder = divmod(numerator, denominator)
    if 2 * abs(remainder) >= denominator:
        # `divmod` already floors when the sign differs; lift by one so the
        # tie and everything above it move away from negative infinity.
        return quotient + 1
    return quotient


def ratio_to_micro(value: float) -> int:
    """Convert a real-valued rate into an integer count of micro-units.

    The single sanctioned boundary where a float enters the money arithmetic,
    and it converts *out* of float immediately. Non-finite input is refused:
    ``nan`` and ``inf`` would otherwise turn into an absurd integer instead of an
    error, and an absurd EV is worse than a failed run because it still ranks.

    ``floor(x + 0.5)`` rather than ``round``: Python's ``round`` is banker's
    rounding, so a rate written as ``0.125`` would land on an even micro-count
    and ``0.375`` on an odd one. Same rule as ``scale_div``, one tie behaviour
    for the whole layer.
    """
    if not math.isfinite(value):
        raise QuantError(f"rate must be finite, got {value!r}")
    return math.floor(value * MICRO + 0.5)


def sum_money(items: list[Money] | tuple[Money, ...], *, empty_currency: str) -> Money:
    """Total money, failing loudly on a mixed-currency pile.

    ``exposure`` and ``cost`` totals are aggregations of many rows, and one row
    carrying a different currency is schema drift rather than a rounding
    question (03 §A: fail loud at the boundary).
    """
    total = Money.zero(empty_currency)
    for item in items:
        total = total + item
    return total


def decimals_for_base(per_major: int) -> int:
    """The decimal exponent a base of ``per_major`` minor units implies.

    A base and an exponent are different numbers and reading one as the other is the
    whole 10^100 bug class: ``Money.decimals`` is the exponent, while
    ``config/economics.yaml`` declares the base, because the base is what an operator
    reasons about. Anything that renders money needs the exponent, so every call site
    converts here rather than inventing its own.

    Refuses a base that is not an exact power of ten. Rounding an exponent into
    existence would silently render 1,234,567 minor units as ``12,345.67`` under one
    base and ``123.4567`` under a near-miss, and a figure that depends on which is not
    a figure.
    """
    if per_major < 1:
        raise QuantError(f"minor_units_per_major must be >= 1, got {per_major}")
    exponent, place = 0, 1
    while place < per_major:
        place *= 10
        exponent += 1
    if place != per_major:
        raise QuantError(
            f"minor_units_per_major={per_major} is not an exact power of ten, so it has "
            "no decimal exponent and money would render at a guessed scale"
        )
    return exponent


def to_major_text(minor: int, currency: str, per_major: int) -> str:
    """Render minor units as a grouped major-unit string.

    Renderer-only by design: ``quant/economics.py`` is the single caller, and
    ``test_currency_figure_has_only_the_renderer_as_its_producer`` enforces it by
    source scan. Division happens here, at render time, and the integer minor
    units are what was summed, so totals still match their rendered rows
    (03 money rules).

    ASCII thousands separators on purpose: a non-breaking thin space is unprintable
    on this project's Windows console (cp1252) and in a packet font subset, and a
    figure that raises on the way to the screen is not a rendered figure.
    """
    if per_major < 1:
        raise QuantError(f"minor_units_per_major must be >= 1, got {per_major}")
    whole, frac = divmod(abs(minor), per_major)
    digits = f"{whole:,}"
    frac_text = "" if per_major == 1 else f".{frac:0>{len(str(per_major - 1))}}"
    sign = "-" if minor < 0 else ""
    return f"{sign}{digits}{frac_text} {currency}"


__all__ = [
    "MICRO",
    "CurrencyMismatchError",
    "Money",
    "QuantError",
    "decimals_for_base",
    "ratio_to_micro",
    "scale_div",
    "sum_money",
    "to_major_text",
]
