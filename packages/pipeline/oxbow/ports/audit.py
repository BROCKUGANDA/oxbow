"""The AuditSink port: append, read the tip, walk the chain (02 §A, §F).

An audit sink is the one port whose contract cannot be satisfied by "return None and
carry on". Three properties have to hold for every implementation, null file or
Postgres, and the parametrised contract test checks all three:

* append-only — there is no update and no delete in the interface. A reversal is a
  new row referencing the original (plan §15), which only means something if the
  original is still there;
* the append refuses a stale tip rather than forking the chain;
* :meth:`AuditSink.verify` walks everything stored with the same pure arithmetic in
  :mod:`oxbow.audit.chain`, so ``make verify-audit`` and a packet export agree by
  construction rather than by coincidence.

The sink writes inside the caller's transaction — it does not own one. That is what
makes "the decision row, its audit row and its outbox row either all exist or none
do" true (02 §E).

Scope, stated because it is easy to get wrong: this port covers the *system* audit
stream. The decision chain is the same arithmetic over the ``decision`` table,
written by the decision repository, because a decision row carries reviewer
identity and money fields that an audit event has no business knowing.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from oxbow.audit.chain import ChainRow, ChainVerification, PendingChainRow


class AuditAppendError(RuntimeError):
    """The append would fork or skip a link, so it was refused."""


@runtime_checkable
class AuditSink(Protocol):
    """One append-only hash chain."""

    @property
    def sink_id(self) -> str:
        """Which store this is, for logs and the verify report."""
        ...

    def tip(self) -> ChainRow | None:
        """The highest row, or ``None`` when the chain is empty."""
        ...

    def append(self, row: PendingChainRow) -> ChainRow:
        """Persist one row.

        Raises :class:`AuditAppendError` when the row does not link to the current
        tip. Callers must not swallow it: a refused append means a concurrent writer
        moved the tip, and the transaction has to be retried.
        """
        ...

    def load(self, *, from_seq: int = 1) -> Sequence[ChainRow]:
        """Every row at or after ``from_seq``, in sequence order.

        Empty only when the chain genuinely has no rows in range. A sink that cannot
        reach its own store raises (03 §A rule 1); it does not return ``[]``.
        """
        ...

    def verify(self) -> ChainVerification:
        """Walk the stored chain and report the first broken link, by sequence number."""
        ...


__all__ = ["AuditAppendError", "AuditSink"]
