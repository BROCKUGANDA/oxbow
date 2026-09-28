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

import polars as pl

#: Either form polars accepts for a column's dtype: the class (``pl.Int64``, what every schema
#: in this repository writes) or a constructed instance (``pl.Datetime("us", "UTC")``, which a
#: parameterised dtype has to be).
type PolarsDtype = type[pl.DataType] | pl.DataType
