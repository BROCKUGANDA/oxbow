"""The one type alias polars schemas need, declared here because polars says to.

Every canonical, feature and ledger frame in this pipeline declares its schema rather than
letting polars infer one — ``_RULE_HIT_SCHEMA``'s comment records the measured run that died
because inference sampled a hundred ungrouped rule hits and typed ``overlap_group`` as ``Null``.
A schema is therefore a ``Mapping[str, <something>>`` whose values are written as ``pl.Utf8``,
``pl.Int64``: the *classes*, which polars accepts alongside instances.

``polars.DataType`` alone describes an instance, so annotating a schema that way makes mypy
reject every entry of the literal (``[dict-item]``, 19 errors in one file). polars' own private
alias for the union is ``polars._typing.PolarsDataType``, and importing it earns a deprecation
note that says plainly: define your own. This is that definition, in one place, so the schemas
are annotated with what they hold instead of each module restating the union.
"""

from __future__ import annotations

from datetime import datetime

import polars as pl

#: Either form polars accepts for a column's dtype: the class (``pl.Int64``, what every schema
#: in this repository writes) or a constructed instance (``pl.Datetime("us", "UTC")``, which a
#: parameterised dtype has to be).
type PolarsDtype = type[pl.DataType] | pl.DataType


def as_moment(value: object) -> datetime | None:
    """Narrow one polars column reduction to a timestamp, or say it was not one.

    ``column.min()`` / ``.max()`` / ``.item()`` answer with a union over every type a column can
    hold — ``int | float | Decimal | date | time | timedelta | str | bytes | ndarray | list |
    None`` — and callers that then write ``.isoformat()`` or interpolate it into a message get a
    type error *and* a runtime one: ``f"{b'2014-01-02'}"`` renders as ``b'2014-01-02'``, which
    reads like a timestamp and is not one.

    Deliberately returns ``None`` instead of raising: the caller knows which frame it read and
    which error its layer publishes (``FoldError`` for a fold plan, ``GrainBridgeError`` for a
    bridged frame), and a shared leaf cannot. Every caller here turns the ``None`` into a refusal
    that names the column and the value.
    """
    return value if isinstance(value, datetime) else None
