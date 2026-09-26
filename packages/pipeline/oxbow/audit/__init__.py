"""Append-only, hash-chained audit (02 §F, plan §13, plan §15).

Two things are chained: the decision table and the audit event table. Each row's
digest covers the previous row's digest, so removing or editing a row breaks
every link after it, and a reversal is a NEW row referencing the original rather
than an edit of it. Mutation destroys the integrity claim; this package is the
reason it cannot be quietly mutated.

Authority for the shape:

* 01 §B / plan §1: "append-only hash-chained audit" is a non-negotiable.
* 02 §F: the chain stores salted hashes and decision metadata only, so the right
  to erasure is satisfiable without breaking the chain — see :mod:`oxbow.audit.erasure`.
* plan §13: ``make verify-audit`` walks the chain and prints OK or the first broken
  link, naming the sequence number. :func:`verify_chain` returns that structure and
  :func:`render_verification` prints it, so the script, the CLI and the tests cannot
  disagree about what a green audit looks like.

Nothing here touches a database or a web framework: the arithmetic has to be the
same whether the rows came from Postgres, from a packet export, or from a fixture.
"""

from __future__ import annotations

from oxbow.audit.chain import (
    GENESIS_HASH,
    HASH_VERSION,
    AuditIntegrityError,
    BrokenLink,
    ChainRow,
    ChainVerification,
    PendingChainRow,
    append_row,
    canonical_json,
    compute_row_hash,
    render_verification,
    verify_chain,
)
from oxbow.audit.erasure import (
    ERASURE_ACTION,
    ErasureResult,
    PseudonymMapping,
    assert_erasure_payload_is_safe,
    build_erasure_audit_row,
    select_mappings_for_subject,
)

__all__ = [
    "ERASURE_ACTION",
    "GENESIS_HASH",
    "HASH_VERSION",
    "AuditIntegrityError",
    "BrokenLink",
    "ChainRow",
    "ChainVerification",
    "ErasureResult",
    "PendingChainRow",
    "PseudonymMapping",
    "append_row",
    "assert_erasure_payload_is_safe",
    "build_erasure_audit_row",
    "canonical_json",
    "compute_row_hash",
    "render_verification",
    "select_mappings_for_subject",
    "verify_chain",
]
