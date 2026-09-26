"""The warehouse adapter family: Postgres for the product, files for the demo.

Both implement ``ports/warehouse.py`` and pass the same round-trip contract test,
which is the mechanism behind "the API reads what the pipeline wrote" without the
pipeline needing a database to be up.
"""

from __future__ import annotations

__all__: list[str] = []
