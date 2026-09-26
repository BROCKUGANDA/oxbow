"""The response envelope: ``{data, meta}`` and nothing else (standing user rule).

The rule this file enforces: an operational payload returns ``data`` and ``meta``,
status lives in the HTTP status code, and there is no ``success`` boolean anywhere. A
200 body that says ``{"success": false}`` is a second, contradictory status channel —
the client then has to check two things and they can disagree, which is why one unwrap
point in the client is only possible when there is only one place status can live.

FastAPI's generic response model gives the schema for free: every route declares
``Envelope[X]``, the generated TypeScript client sees the concrete shape per route, and
the union for the error branch comes from ``ProblemDetail`` declared in the same
operation. The doctrine test in ``tests/unit/test_p7_envelope.py`` walks the OpenAPI
document and fails if any 2xx schema grows a ``success`` or ``error`` property, so the
rule cannot be re-broken by a route added later.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

DataT = TypeVar("DataT")

# The keys a 2xx envelope is allowed to carry at its top level. Enforced by the test,
# declared here so the test names the rule rather than inventing it.
ENVELOPE_KEYS: Final = frozenset({"data", "meta"})


class AssumptionLine(BaseModel):
    """One economic assumption a money figure in this response depends on.

    Rendering this next to the figure is what makes "every currency figure carries its
    recovery-rate band and assumption line" (00 §B) a property of the API rather than a
    discipline the UI has to remember.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    value: float | int | str
    source: str = Field(description="Which config file or run record this came from.")
    note: str | None = None


class Money(BaseModel):
    """Minor units plus currency, always together.

    There is no float field here and no unit-less amount in the API: a number without
    its currency is not a money figure, and a float is not a minor unit (DEV-005).
    """

    model_config = ConfigDict(extra="forbid")

    minor: int = Field(description="Integer minor units.")
    currency: str = Field(min_length=3, max_length=3, description="ISO 4217 code.")
    decimals: int = Field(default=2, description="Minor units per major unit, from config.")

    @property
    def major(self) -> float:
        """Rendering aid for the server side only; never serialised into a response."""
        return self.minor / (10**self.decimals)


class Meta(BaseModel):
    """Correlation and provenance every response carries.

    ``provenance`` is on every response because a fixture run and a pipeline run render
    identically otherwise, and the difference is the difference between a measurement
    and a demo (plan §19: no fabricated numbers presented as real).
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str | None = None
    trace_id: str | None = None
    model_version: str | None = None
    provenance: str | None = None
    generated_at: datetime | None = None
    assumptions: list[AssumptionLine] = Field(default_factory=list)
    degraded: bool = False
    degraded_reason: str | None = None
    disclaimer: str


class PageMeta(Meta):
    """Server-side paging and sorting, so virtualisation never needs the whole set."""

    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    total: int = Field(ge=0, description="Rows matching the filter, before paging.")
    next_offset: int | None = Field(
        default=None, description="Offset to request next, or None at the end of the set."
    )
    sort: str
    order: str = Field(pattern="^(asc|desc)$")


class Envelope(BaseModel, Generic[DataT]):
    """The only success shape this API has."""

    model_config = ConfigDict(extra="forbid")

    data: DataT
    meta: Meta


class PageEnvelope(BaseModel, Generic[DataT]):
    """A list route's envelope: same two keys, paging in ``meta``."""

    model_config = ConfigDict(extra="forbid")

    data: list[DataT]
    meta: PageMeta


def envelope(data: Any, **meta_fields: Any) -> dict[str, Any]:
    """Build the ``{data, meta}`` pair without any caller remembering the shape.

    Keeping construction in one function is what stops a route adding a third top-level
    key "just this once", which is how an envelope quietly becomes a success flag.
    """
    return {"data": data, "meta": meta_fields}


__all__ = [
    "ENVELOPE_KEYS",
    "AssumptionLine",
    "Envelope",
    "Meta",
    "Money",
    "PageEnvelope",
    "PageMeta",
    "envelope",
]
