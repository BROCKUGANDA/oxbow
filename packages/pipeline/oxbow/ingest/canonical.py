"""Canonical event construction: the single boundary where raw becomes trusted.

Everything downstream — features, graph, rules, models, backtest — reads the
canonical frame produced here and nothing else. That is the whole point of this
module: there is exactly one place in the pipeline where a float amount, a
wall-clock guess, or an un-hashed account name could leak in.

Three rules from the governing documents meet here and are worth stating together,
because they are easy to satisfy one at a time and break the third:

* Money is ``amount_minor: int64`` and nothing else. 00 A forbids float money end
  to end. ``0.10`` has no exact binary representation, so a float pipeline cannot
  be trusted to sum to the cent; a minor-unit pipeline can.
* Time is a real UTC instant plus a separate ``local_hour`` column. 03 C forbids
  mixing the two. An ODD_HOUR_SHIFT rule written against UTC flags an entire
  East-African morning as suspicious, which is a false positive manufactured
  entirely by the modelling choice.
* Accounts are salted hashes, never names. 03 D requires a per-deployment salt that
  is not committed, so a hash from one run cannot be reversed by an attacker
  holding a list of candidate account names.

THIS FILE ALSO HOLDS THE VECTORISED PRIMITIVES. The previous revision of the PaySim
adapter walked all 6,362,620 rows in a Python ``iter_rows`` loop, which put a
single-source ingest at roughly twenty minutes and made ``make ingest`` unusable as
a gate command. Everything expensive here now happens either as a Polars
expression over the whole frame, or once over a *distinct* key set and joined back:
PaySim has 9,073,900 distinct account names across 6,362,620 rows (measured in
DEV-011's run, not recalled), so the distinct set is larger than the row count and
hashing it once is both the only tractable route and the only correct one.

The column list is not declared here: it comes from
``oxbow.contracts.canonical_v1``, which is the single source of truth (DEV-012 and
the seam this module closes). It is re-exported because every existing reader
imports ``CANONICAL_COLUMNS`` from here.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from functools import partial
from typing import Final
from zoneinfo import ZoneInfo

import polars as pl

from oxbow.contracts.canonical_v1 import (
    ACCOUNT_KEY_LENGTH,
    CANONICAL_COLUMNS,
    CANONICAL_DTYPES,
    PERSISTED_CANONICAL_COLUMNS,
    UTC_MICROS,
)
from oxbow.dtypes import PolarsDtype
from oxbow.identity import is_ulid, new_ulid

# Amounts in PaySim are EUR to the cent. Not inferred: the dataset card states it and
# it is stated here rather than left implicit, because currency is part of every
# amount downstream and an implicit one is an implicit FX conversion.
PAYSIM_CURRENCY: Final = "EUR"

# The declared account-key width, re-exported under the name the earlier modules and
# ``config/sources.yaml`` use. 12 hex characters is 48 bits.
ACCOUNT_KEY_PREFIX_LEN: Final = ACCOUNT_KEY_LENGTH

# A decimal with at most two fractional places. This is the shape money arrives in in
# both corpora and the shape ``parse_amount_minor`` accepts without rounding.
DECIMAL_2DP_PATTERN: Final = r"^\d+(\.\d{1,2})?$"

# PaySim's CSV writer emitted float reprs for the largest TRANSFER amounts, so 5,650
# rows of 6,362,620 arrive as ``1.000191239E7`` rather than ``10001912.39`` (measured
# in this session; see the dataset card). The form is different, the value is exact.
# Accepting it is a documented parse, not a repair: it is counted, printed, and every
# expansion is re-checked against DECIMAL_2DP_PATTERN before it becomes an integer.
SCIENTIFIC_DECIMAL_PATTERN: Final = r"^\d+(?:\.\d+)?[Ee]\+?\d+$"

# Chunk size for the process pool. Small enough that a nine-million-name corpus
# spreads across every worker, large enough that pickling overhead stays under a
# second per chunk.
_HASH_CHUNK_MIN: Final = 50_000


class CanonicalizationError(RuntimeError):
    """Raised when a row cannot be turned into a canonical event.

    Boundary failure, loud on purpose (03 A rule 1). The alternative — coerce what
    we can and drop the rest without counting — is how a corpus silently becomes 40%
    smaller and nobody notices until a demo.
    """


@dataclass(frozen=True, slots=True)
class RunIdentity:
    """Per-run identity material.

    ``run_salt`` must come from the environment (``.env``), never from ``config/``,
    because ``config/`` is committed. ``batch_id`` is 12 hex characters and is
    asserted unique per run.

    ``run_id`` is a ULID (DEV-003) and is carried here rather than inside any frame:
    DEV-012 keeps run-stamped fields out of the persisted Parquet bytes so
    ``make verify-determinism`` can compare two runs at all. The batch manifest and
    the warehouse table are where ``run_id`` lives.
    """

    run_salt: str
    batch_id: str
    run_id: str = ""

    def __post_init__(self) -> None:
        if not self.run_salt or len(self.run_salt) < 16:
            raise CanonicalizationError(
                "run_salt must be at least 16 characters; got "
                f"{len(self.run_salt)}. Set RUN_SALT in .env — a salt committed to "
                "config/ can be brute-forced against a list of candidate accounts."
            )
        if len(self.batch_id) != ACCOUNT_KEY_PREFIX_LEN:
            raise CanonicalizationError(
                f"batch_id must be {ACCOUNT_KEY_PREFIX_LEN} hex characters, got "
                f"{len(self.batch_id)}"
            )
        if self.run_id and not is_ulid(self.run_id):
            raise CanonicalizationError(
                f"run_id {self.run_id!r} is not a 26-character ULID (DEV-003)"
            )


def new_run_identity(run_salt: str, batch_id: str) -> RunIdentity:
    """Build the run identity for one ingest.

    ``batch_id`` is a parameter rather than a ``uuid4()`` here, and that is the
    substance of DEV-012: a random batch id embedded in the canonical columns makes
    two runs of the same corpus produce different bytes, so
    ``make verify-determinism`` could never pass and the "rerun and diff the
    checksums" gate would be unimplementable. The caller therefore passes a
    content-derived id (see ``batch_id_for_rows``), and the only random value in a
    run is ``run_id``, which lives in the manifest sidecar instead of the data.
    """
    return RunIdentity(run_salt=run_salt, batch_id=batch_id, run_id=new_ulid())


def hmac_sha256_hex(message: str, salt: str) -> str:
    """Salted SHA-256 as lowercase hex, via the stdlib ``hmac`` module."""
    return hmac.new(salt.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def account_key(raw_name: str, identity: RunIdentity) -> str:
    """Hash an account name into a stable, non-reversible 12-hex-character key.

    HMAC-SHA256 keyed by the per-run salt over ``"<salt>|<raw_name>"``, truncated to
    48 bits. The exact scheme, including the message layout, is pinned by an
    independent oracle in ``tests/unit/test_p1b_ibm_aml.py`` rather than by this
    function's own output: a digest nobody can compute by hand should not be
    defined by the code that computes it (00 B).

    Truncation is safe here because the input space is account names, not
    adversarial collisions: 2^48 sits far above the 9,073,900 distinct names in the
    PaySim corpus, and the zero-collision fact is asserted per run by
    ``test_account_key_unique_per_run`` rather than assumed. The salt is
    per-deployment on purpose — an unsalted SHA-256 of ``C1231006815`` is trivially
    reversed by hashing a list of candidate names, which is the failure 03 D exists
    to prevent.
    """
    return hmac_sha256_hex(f"{identity.run_salt}|{raw_name}", identity.run_salt)[
        :ACCOUNT_KEY_PREFIX_LEN
    ]


def display_account_key(key: str) -> str:
    """Render a stored key in the operator-facing form ``ACC-7F2A19``.

    Display is a function of the stored value, never a second stored value: the
    Parquet and the warehouse carry lowercase hex, and uppercasing happens only on
    the way to a screen. Two spellings in two places is how a join quietly returns
    zero rows.
    """
    return f"ACC-{key.upper()}"


def node_key(source_dataset: str, key: str) -> str:
    """The graph node identity for an account within one corpus.

    This exists because of DEV-011. Two corpora sharing one deployment salt hash the
    same raw account name to the same 12-hex key, and the graph stage builds nodes
    from a key column alone — so a PaySim ``C1231006815`` and an IBM-AML account that
    happened to carry the same name would merge into one node and inherit each
    other's degree, counterparties and community. Merging two corpora into one node
    is not a subtle error, it fabricates network structure that neither corpus has.

    ``account_key`` itself is deliberately *not* changed to include the corpus: its
    scheme is externally pinned (see its docstring) and the canonical column list is
    frozen at canonical v1. Namespacing therefore happens at the node, which is the
    layer where the merge would actually occur, and
    ``test_no_cross_source_node_merge`` pins it.
    """
    if not source_dataset:
        raise CanonicalizationError("node_key requires the source_dataset namespace")
    return f"{source_dataset}#{key}"


def step_to_timestamp(
    step: int,
    epoch_utc: datetime,
    step_hours: int,
    offset_us: int,
) -> datetime:
    """Expand PaySim's synthetic day counter into a real UTC instant.

    ``step`` is a day index, not a timestamp, so the conversion needs a fixed epoch.
    Every transaction inside a step shares the same second, which would make
    intra-step ordering arbitrary; the caller supplies a deterministic per-transaction
    microsecond offset derived from the transaction id, so ordering is stable across
    runs without pretending the corpus knows it. The synthetic window is documented
    as synthetic in ``data/DATASET_CARD.md``.
    """
    base = epoch_utc + timedelta(hours=step_hours * step)
    return base + timedelta(microseconds=offset_us)


def intra_step_offset_us(txn_id: str, salt: str, modulus_us: int) -> int:
    """Deterministic microsecond offset within a step, from the transaction id.

    Seed-independent by design (03 C): the same transaction always lands at the same
    instant, so two runs produce byte-identical outputs and a feature computed at a
    cutoff cannot shift between runs. This is the scalar reference the vectorised
    adapter path is checked against by ``test_step_expansion_stable``.
    """
    digest = hmac_sha256_hex(f"{salt}|{txn_id}", salt)
    return int(digest[:16], 16) % modulus_us


# --- vectorised primitives ------------------------------------------------


def minor_units_expr(column: str, *, alias: str) -> pl.Expr:
    """Integer minor units for a column of ``DECIMAL_2DP_PATTERN``-shaped strings.

    Pure Polars, over all rows at once. The arithmetic is on the *digits*: whole
    times one hundred plus the fractional digits, left-padded. Doing it this way is
    what keeps the conversion exact, and doing it in expressions is what keeps it
    fast enough to run over six million rows.

    The obvious implementation is the wrong one: ``float("9839.64") * 100`` is
    983963.9999999999, so it loses a cent on some rows, and a cent error that only
    appears on some rows surfaces months later in a reconciliation rather than today.

    Callers MUST mask on ``DECIMAL_2DP_PATTERN`` first. Feeding a three-place or
    scientific string here raises a Polars cast error rather than the quarantine
    record the contract wants, which is why ``minor_units_from_decimal_text`` exists
    beside it for the distinct-value path.
    """
    parts = pl.col(column).str.split(".")
    whole = parts.list.get(0, null_on_oob=True)
    frac = parts.list.get(1, null_on_oob=True)
    cents = (
        pl.when(frac.is_null())
        .then(pl.lit(0, dtype=pl.Int64))
        .when(frac.str.len_chars() == 1)
        .then(frac.cast(pl.Int64) * 10)
        .otherwise(frac.cast(pl.Int64))
    )
    return (whole.cast(pl.Int64) * 100 + cents).cast(pl.Int64).alias(alias)


def minor_units_from_decimal_text(text: str) -> int:
    """Integer minor units for one decimal string, exact, with banker's rounding.

    ``Decimal`` on the string form, never ``float``: binary floating point cannot hold
    0.10, and the balances in both corpora arrive as Float64 that must be converted
    without the cent-level drift that multiplying by 100 would introduce.

    Values with at most two places convert with no rounding at all, which is the case
    for every row of the PaySim corpus measured this session. Anything finer is
    quantised with ROUND_HALF_EVEN, the rounding mode that keeps a long column of
    ".5" values from drifting upward the way plain half-up does — a systematic bias
    in a money column is worse than a random one, because it never cancels out.
    """
    cleaned = text.strip()
    if not cleaned:
        raise CanonicalizationError("empty amount")
    if cleaned.startswith("-"):
        raise CanonicalizationError(f"negative amount rejected: {text!r}")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise CanonicalizationError(f"amount is not a plain decimal: {text!r}") from exc
    if not value.is_finite():
        raise CanonicalizationError(f"amount is not finite: {text!r}")
    scaled = value.scaleb(2)
    # ``to_integral_value`` rather than ``quantize``: quantize raises InvalidOperation
    # once the result exceeds the context precision, which would turn a large but
    # perfectly representable amount into a crash. to_integral_value is exact and
    # leaves an already-integral value untouched, so the two-decimal case — every row
    # of PaySim measured this session — rounds nothing and asserts nothing either.
    return int(scaled.to_integral_value(rounding=ROUND_HALF_EVEN))


def amount_minor_to_text(minor: int) -> str:
    """Render integer minor units back to a decimal string, for audit output.

    Strings, not floats, on the way out too: a packet that shows a suspect's amounts
    must show exactly the integers that were summed.
    """
    sign = "-" if minor < 0 else ""
    whole, frac = divmod(abs(minor), 100)
    return f"{sign}{whole}.{frac:02d}"


# Backwards-compatible name used by the IBM-AML adapter and the P0 tests.
parse_amount_minor = minor_units_from_decimal_text


def local_hour_expr(column: str, zone: str) -> pl.Expr:
    """Hour of day in the deployment timezone, from a UTC instant.

    Derived exactly once, at this boundary, into its own column (03 C). A rule that
    needs human hours reads ``local_hour`` and never recomputes from
    ``event_ts_utc``; recomputation in two places is how one of them ends up wrong.
    """
    return pl.col(column).dt.convert_time_zone(zone).dt.hour().cast(pl.Int8)


def local_date_expr(column: str, zone: str) -> pl.Expr:
    """Calendar date in the deployment timezone, from a UTC instant."""
    return pl.col(column).dt.convert_time_zone(zone).dt.date()


def resolve_local_timezone(deployment_tz: ZoneInfo) -> str:
    """Validate the deployment zone and return the name the expressions use.

    Fails here rather than inside ``convert_time_zone`` during a six-million-row
    collect, because Polars raises a ComputeError from the middle of a query that
    points at nothing until the traceback is read, and the wasted batch is the only
    feedback. The zone is a run-level constant from ``config/pipeline.yaml``; it is
    resolved once and passed to the expression builders instead of being held in a
    module global, so two runs with different zones cannot leak into each other.
    """
    name = deployment_tz.key
    try:
        ZoneInfo(name)
    except Exception as exc:
        raise CanonicalizationError(
            f"deployment_timezone {name!r} is not resolvable by the host tz database: {exc}"
        ) from exc
    return name


# --- parallel identity derivation ----------------------------------------


def hash_workers(hint: int | None = None) -> int:
    """How many processes the identity hashing may use.

    A performance knob only, and deliberately not a config key: it cannot change a
    byte of output, which is what ``test_ingest_deterministic`` proves by running the
    same fixture at one worker and at four. A tunable that can move a number belongs
    in ``config/``; one that cannot would only add a file to keep in sync.
    """
    if hint is not None:
        return max(1, min(hint, 16))
    env = os.environ.get("OXBOW_HASH_WORKERS", "")
    if env.isdigit():
        return max(1, min(int(env), 16))
    return max(1, min((os.cpu_count() or 1), 8))


def _account_key_chunk(salt: str, names: list[str]) -> list[str]:
    """Worker body: HMAC-salt keys for a contiguous slice of distinct names.

    Module-level and taking plain arguments rather than a tuple because it is handed to
    a ``ProcessPoolExecutor`` through :func:`functools.partial`: a lambda is not
    picklable, and the pool call used to fail with a ``PicklingError`` that the
    serial-fallback branch swallowed — so the "parallel" path quietly ran on one core
    and the full-corpus timing looked twice as bad as the arithmetic predicted. The
    fallback stays (a restricted host is a real case), but it is no longer the normal
    route, and ``test_ingest_deterministic`` compares one worker against four to prove
    the pool is both used and equivalent.
    """
    prefix = f"{salt}|"
    return [hmac_sha256_hex(prefix + name, salt)[:ACCOUNT_KEY_PREFIX_LEN] for name in names]


def _txn_identity_chunk(
    namespace: str, offset_salt: str, modulus_us: int, keys: list[str]
) -> list[tuple[str, int]]:
    """Worker body: ``(txn_id, intra_step_offset_us)`` for a slice of row keys.

    Both digests are computed in one pass over the row because both are SHA-256 of
    short strings: doing them together halves the Python work relative to two
    independent maps, and the second depends on the first only through the id itself,
    which is known before the offset is taken.

    The id is keyed by the corpus namespace rather than by the run salt. That is
    deliberate: ``txn_id`` is not personal data, it is a transaction identifier that
    must survive a salt rotation, so that re-running an ingest with a fresh salt changes
    every account key (the erasure property 03 L wants) without re-labelling six million
    transactions.
    """
    out: list[tuple[str, int]] = []
    for key in keys:
        txn_id = f"{namespace}:{hmac_sha256_hex(key, namespace)[:16]}"
        offset = int(hmac_sha256_hex(f"{offset_salt}|{txn_id}", offset_salt)[:16], 16) % modulus_us
        out.append((txn_id, offset))
    return out


def map_in_chunks(
    function: Callable[[list[str]], list[object]],
    values: Sequence[str],
    *,
    pool: ProcessPoolExecutor | None = None,
) -> list[object]:
    """Apply a picklable chunk function over ``values``, preserving order.

    Chunks are contiguous and results are reassembled in chunk order, so the output is
    byte-identical to the serial loop regardless of worker count or where a chunk
    boundary falls. On Windows the pool spawns interpreters and pickles the strings,
    which costs about a second of start-up and buys a 3-4x speedup on the
    nine-million-name case; below two chunks' worth of work the overhead is not worth
    it and the serial path runs.

    ``pool`` lets the caller pass one warm executor for a whole run. Cold start is
    roughly a second per pool, so sixty-four batches each building their own would
    spend more on spawning than the hashing saves.

    A pool failure (restricted environment, nested pool) degrades to the serial path
    rather than failing the run: the output is identical either way, so refusing to
    ingest because a performance optimisation was unavailable would trade a real
    guarantee for a theoretical one.
    """
    count = len(values)
    if count == 0:
        return []
    workers = hash_workers()
    if workers == 1 or count < _HASH_CHUNK_MIN * 2:
        return list(function(list(values)))
    size = -(-count // workers)
    chunks = [list(values[i : i + size]) for i in range(0, count, size)]
    try:
        if pool is not None:
            results = list(pool.map(function, chunks))
        else:
            with ProcessPoolExecutor(max_workers=len(chunks)) as executor:
                results = list(executor.map(function, chunks))
    except Exception:
        return list(function(list(values)))
    return [item for chunk in results for item in chunk]


def account_keys_for_names(
    names: Sequence[str], run_salt: str, *, pool: ProcessPoolExecutor | None = None
) -> list[str]:
    """12-hex account keys for a sequence of distinct raw names, order preserved."""
    results = map_in_chunks(partial(_account_key_chunk, run_salt), names, pool=pool)
    return [str(item) for item in results]


def txn_ids_and_offsets(
    row_keys: Sequence[str],
    *,
    namespace: str,
    offset_salt: str,
    modulus_us: int,
    pool: ProcessPoolExecutor | None = None,
) -> tuple[list[str], list[int]]:
    """``(txn_id, intra_step_offset_us)`` for a sequence of row-content keys.

    The id part is a truncated HMAC-SHA256 over the row's own content, prefixed by the
    corpus namespace, so it is stable across runs and across chunk boundaries: the same
    PaySim row yields the same ``txn_id`` whether it lands in batch 1 or batch 40
    (C4/DEV-004). Sixteen hex characters are used rather than the 12 an account key
    needs because six million ids drawn from a 48-bit space would be expected to
    collide a handful of times, and a colliding ``txn_id`` is a lost transaction.
    """
    pairs = map_in_chunks(
        partial(_txn_identity_chunk, namespace, offset_salt, modulus_us),
        row_keys,
        pool=pool,
    )
    as_tuples = [item for item in pairs if isinstance(item, tuple)]
    return (
        [str(item[0]) for item in as_tuples],
        [int(item[1]) for item in as_tuples],
    )


def batch_id_for_rows(source_dataset: str, ordinal: int, row_keys: Iterable[str]) -> str:
    """A deterministic 12-hex batch id for one batch of raw rows.

    Content-derived so a rerun over the same bytes yields the same id, which is the
    precondition for ``make verify-determinism`` (DEV-012), and salted with the
    ``source_dataset`` plus the batch ordinal so two batches that happen to contain
    identical rows still get distinct ids. The full 32 bytes of the digest are
    reduced to 12 hex characters, matching the width 03 B declares for a batch id.
    """
    digest = hashlib.sha256()
    digest.update(f"{source_dataset}|{ordinal}".encode())
    for key in row_keys:
        digest.update(key.encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()[:ACCOUNT_KEY_PREFIX_LEN]


def utc_timestamp_series(values_us: Sequence[int], name: str) -> pl.Series:
    """Microseconds-since-epoch integers as a ``Datetime('us', UTC)`` series.

    Integers are what the step-expansion arithmetic produces, and building the column
    from integers rather than from six million Python ``datetime`` objects is most of
    the difference between a vectorised build and a loop. Polars interprets an Int64
    cast to ``Datetime`` as an epoch offset in the target unit, so no Python-side
    conversion happens at all.
    """
    return pl.Series(name, list(values_us), dtype=pl.Int64).cast(UTC_MICROS)


def canonical_dtypes_subset(columns: Sequence[str]) -> dict[str, PolarsDtype]:
    """The declared dtype for each of ``columns``, from the one source of truth."""
    return {name: CANONICAL_DTYPES[name] for name in columns}


__all__ = [
    "ACCOUNT_KEY_PREFIX_LEN",
    "CANONICAL_COLUMNS",
    "CANONICAL_DTYPES",
    "DECIMAL_2DP_PATTERN",
    "PAYSIM_CURRENCY",
    "PERSISTED_CANONICAL_COLUMNS",
    "SCIENTIFIC_DECIMAL_PATTERN",
    "UTC_MICROS",
    "CanonicalizationError",
    "RunIdentity",
    "account_key",
    "account_keys_for_names",
    "amount_minor_to_text",
    "batch_id_for_rows",
    "canonical_dtypes_subset",
    "display_account_key",
    "hash_workers",
    "hmac_sha256_hex",
    "intra_step_offset_us",
    "local_date_expr",
    "local_hour_expr",
    "map_in_chunks",
    "minor_units_expr",
    "minor_units_from_decimal_text",
    "new_run_identity",
    "node_key",
    "parse_amount_minor",
    "resolve_local_timezone",
    "step_to_timestamp",
    "txn_ids_and_offsets",
    "utc_timestamp_series",
]
