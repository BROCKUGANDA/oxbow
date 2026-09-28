"""The review queue's tie-break is one order, owned by one function, proved total.

`oxbow/models/run.py:492 ordered_queue` carries the comment "One function owns the
order, so the queue, precision-at-budget and PR-AUC cannot each invent a tie-break and
disagree about which accounts are in the top 200 (``test_tie_break_deterministic``)".
That test did not exist. The claim is not cosmetic: the queue order decides which
accounts a reviewer opens under a fixed capacity, so a tie-break that depends on input
row order, on a stable-sort accident, or on which of two callers sorted first is a
different investigation for the same warehouse.

What is proved here, in the order the doctrine needs it:

1. **Ties break on the key, not on arrival order.** Two accounts with an identical score
   come out in ascending `account_key` order, and reversing the input reverses nothing.
2. **The order is total and reproducible.** The emitted `queue_rank` is `1..n` with no
   gaps or duplicates, and the same frame fed in a shuffled row order yields the same
   `(rank, account_key)` sequence -- the property `make verify-determinism` depends on.
3. **The queue is the same prefix every consumer reads.** The top-k slice of
   `ordered_queue` is the top-k slice of `sort` on the same frame, so precision at a
   budget and the allocator cannot be looking at two different sets of accounts.
4. **A missing column is a refusal, not a silent re-order.** `ordered_queue` raises
   naming which of the two it needed, because defaulting a tie-break to "whatever the
   sort did" is the failure this file exists to prevent.

`ordered_queue` is pure over a Polars frame, so this is a unit test with no fixture
layer between the assertion and the ordering rule.
"""

from __future__ import annotations

import polars as pl
import pytest

from oxbow.models.errors import ModelLayerError
from oxbow.models.run import QUEUE_RANK_COLUMN, ordered_queue

SCORE = "p_fused"
TIE_BREAK = "account_key"


def _frame(rows: list[tuple[str, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {TIE_BREAK: [key for key, _ in rows], SCORE: [score for _, score in rows]},
        schema={TIE_BREAK: pl.String, SCORE: pl.Float64},
    )


# Ten accounts, four of them sharing one score to the last bit: the tie set has to be
# ordered by the key, and the boundary between tied and untied rows must not move.
TIED = [
    ("ACC-0000000000000000000000A01", 0.91),
    ("ACC-0000000000000000000000A02", 0.75),
    ("ACC-0000000000000000000000A03", 0.75),
    ("ACC-0000000000000000000000A04", 0.75),
    ("ACC-0000000000000000000000A05", 0.75),
    ("ACC-0000000000000000000000A06", 0.5),
    ("ACC-0000000000000000000000A07", 0.5),
    ("ACC-0000000000000000000000A08", 0.25),
    ("ACC-0000000000000000000000A09", 0.25),
    ("ACC-0000000000000000000000A10", 0.25),
]


def test_tie_break_deterministic() -> None:
    """Equal scores order on the account key, and no amount of input shuffling moves them.

    This is the test `ordered_queue`'s own comment names. Before it, two callers could
    disagree about the top of the queue purely because of the row order the warehouse
    happened to return, and nothing in the suite would notice.
    """
    forward = ordered_queue(_frame(TIED), SCORE, TIE_BREAK)
    reversed_input = ordered_queue(_frame(list(reversed(TIED))), SCORE, TIE_BREAK)

    assert forward[TIE_BREAK].to_list() == reversed_input[TIE_BREAK].to_list(), (
        "the queue depends on the order rows arrived in, so the same warehouse supports "
        "two different investigations"
    )
    assert forward[SCORE].to_list() == reversed_input[SCORE].to_list()

    # Descending score, and within a score ascending key: the only total order that
    # does not privilege whichever row the sort met first.
    pairs = list(zip(forward[SCORE].to_list(), forward[TIE_BREAK].to_list(), strict=True))
    assert pairs == sorted(pairs, key=lambda item: (-item[0], item[1])), pairs

    tied_block = [key for score, key in pairs if score == 0.75]
    assert (
        tied_block
        == sorted(tied_block)
        == [
            "ACC-0000000000000000000000A02",
            "ACC-0000000000000000000000A03",
            "ACC-0000000000000000000000A04",
            "ACC-0000000000000000000000A05",
        ]
    ), "the tie set itself must be ordered by key, not by the frame's original sequence"

    ranks = forward[QUEUE_RANK_COLUMN].to_list()
    assert ranks == list(
        range(1, len(TIED) + 1)
    ), f"queue_rank is not a gap-free 1..n sequence: {ranks}"
    assert len(set(forward[TIE_BREAK].to_list())) == len(TIED), (
        "one account appears twice in the queue, so two reviewers can be handed the "
        "same row under different ranks"
    )


def test_the_queue_prefix_is_the_slice_every_consumer_reads() -> None:
    """The top-k of the queue is the top-k of the frame under the same rule.

    `ordered_queue` exists so precision-at-budget, the allocator and the case queue read
    one prefix. Asserted against a plain two-key sort of the same frame: if the queue
    ever quietly adopts a third ordering input, the prefixes diverge here rather than in
    a number nobody can reproduce.
    """
    queued = ordered_queue(_frame(TIED), SCORE, TIE_BREAK)
    reference = _frame(TIED).sort([SCORE, TIE_BREAK], descending=[True, False])
    budget = 4
    assert queued.head(budget)[TIE_BREAK].to_list() == reference.head(budget)[TIE_BREAK].to_list()
    assert queued[QUEUE_RANK_COLUMN].to_list() == list(range(1, queued.height + 1))


def test_a_shuffled_input_yields_the_identical_ranking() -> None:
    """Permutation invariance, stated as the permutation the warehouse can produce.

    Interleaving is the realistic shape of a different page order; a reversal is the
    special case of it. Both are checked, because a stable sort passes reversal and
    fails interleave only when the tie-break is a second sort key that got dropped.
    """
    original = _frame(TIED)
    interleaved = _frame([TIED[i] for i in (5, 0, 7, 2, 9, 4, 1, 8, 3, 6)])
    assert interleaved.height == original.height
    expected = ordered_queue(original, SCORE, TIE_BREAK)[TIE_BREAK].to_list()
    assert ordered_queue(interleaved, SCORE, TIE_BREAK)[TIE_BREAK].to_list() == expected


@pytest.mark.parametrize(
    ("score_column", "tie_break", "missing"),
    [
        # (score absent), (tie-break absent), (both absent -- the score is named first)
        ("p_gbm", "account_key", "p_gbm"),
        ("p_fused", "queue_rank", "queue_rank"),
        ("p_scorecard", "p_anomaly", "p_scorecard"),
    ],
)
def test_a_missing_ordering_column_is_a_refusal_not_a_default(
    score_column: str, tie_break: str, missing: str
) -> None:
    """Neither ordering input may be inferred.

    Defaulting a tie-break to "whatever the sort did" is precisely the silent
    non-determinism the function exists to remove, so the refusal names which of the two
    -- score or tie-break -- it could not find.
    """
    frame = _frame(TIED)
    with pytest.raises(ModelLayerError) as raised:
        ordered_queue(frame, score_column, tie_break)
    message = str(raised.value)
    assert missing in message, message
    assert "absent from the frame" in message, message


def test_the_gate_itself_bites() -> None:
    """The ordering rule is shown to be load-bearing, not merely asserted.

    Removing the tie-break key from the sort -- the exact regression this guards -- is
    reproduced here and shown to produce a queue that depends on arrival order, so the
    test above cannot pass for the wrong reason.
    """
    frame = _frame(TIED)
    score_only_forward = frame.sort(SCORE, descending=True)
    score_only_reversed = frame.reverse().sort(SCORE, descending=True)
    assert score_only_forward[TIE_BREAK].to_list() != score_only_reversed[TIE_BREAK].to_list(), (
        "the degenerate single-key sort is already permutation-invariant on this "
        "fixture, so it proves nothing: vary the fixture, do not relax the assertion"
    )
    queued = ordered_queue(frame, SCORE, TIE_BREAK)
    assert queued[TIE_BREAK].to_list() == sorted(
        frame["account_key"].to_list(),
        key=lambda key: (
            -float(frame.filter(pl.col(TIE_BREAK) == key)[SCORE][0]),
            key,
        ),
    ), "ordered_queue disagrees with the hand-computed total order"
