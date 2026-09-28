"""economics: a Monte Carlo interval that was never run becomes a stored absence, not a dropped row

Revision ID: 0004_mc_interval_optional
Revises: 0003_calibration_kind
Create Date: 2026-09-28

Why this migration exists, in one paragraph. ``economics.mc_p05_minor``, ``mc_p50_minor`` and
``mc_p95_minor`` were NOT NULL. They are the results of a per-account propagation
(``oxbow.quant.monte_carlo.simulate_exposure_interval``) over the fold's landed edge list, and
``oxbow score`` builds its graph in memory for the rules layer and never writes that edge list, so
no scored fold in this repository has ever produced them. The loader therefore could not finish an
``economics`` row at all: 0 of this run's 43,046 priced accounts landed, and ``/api/alerts``
answered ``DependencyUnavailable`` on a run whose scores were fine. Those three columns were the
only thing standing between a priced account and a priced queue.

The two ways out were both fabrications, which is why neither was taken. A zero reports a
distribution concentrated at nothing. The configured ``monte_carlo.runs`` -- 10,000 in
``config/economics.yaml`` -- reports ten thousand draws that were never taken, beside a quantile
nobody sampled; and ``apps/api/schemas/case.py``'s ``MonteCarlo.runs`` is ``Field(ge=1)``, so even
"we drew zero" cannot be rendered on the case page. So the columns stop being required. The interval
is a RESULT, and its absence is a fact about the run rather than a quantity inside it -- the same
distinction 0003 drew for a refused calibration, one table further along.

``mc_runs`` keeps its NOT NULL and changes meaning in the only way that keeps it truthful: it records
the draws THIS ROW ACTUALLY MADE, so 0 means the propagation never ran and a positive number is
always a count of work somebody did. ``ck_economics_mc_interval_pairing`` binds the count to the
trio -- all four present, or the three quantiles null with the count at zero -- so no writer can
store a half-simulated distribution, and the ``COMMENT ON COLUMN`` lines below say as much where
someone inspecting the schema will find them. This is the shape the money boundary already had:
``oxbow.ports.case_sink.EconomicsBlock.monte_carlo`` is optional and ``oxbow.packet.loaders``
reconstructs the ``None``. Only the warehouse table was out of step.

No backfill, deliberately: every existing row carries all three quantiles already, and a revision
that touched them would be inventing a value in order to move one. DEV-031 holds the measurement
that settled the sibling question -- the ``downstream_outflow_24h_minor`` nulls of the same run.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_mc_interval_optional"
down_revision: str | None = "0003_calibration_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The three columns that stop being required. They are one claim about one experiment, so all three
#: are absent together -- which is what the pairing constraint below exists to make unskippable.
_QUANTILES: tuple[str, ...] = ("mc_p05_minor", "mc_p50_minor", "mc_p95_minor")

_PAIRING: str = (
    "(mc_p05_minor IS NOT NULL AND mc_p50_minor IS NOT NULL "
    "AND mc_p95_minor IS NOT NULL AND mc_runs >= 1) "
    "OR (mc_p05_minor IS NULL AND mc_p50_minor IS NULL "
    "AND mc_p95_minor IS NULL AND mc_runs = 0)"
)

#: The rule, stated once, on all three columns: a reader who finds one null has to be able to tell
#: "not sampled" from "sampled and found to be nothing", without opening the writer.
_ABSENT_RULE: str = (
    "the simulated exposure interval is a RESULT, not a term of E_i. null here means "
    "quant/monte_carlo.simulate_exposure_interval never ran for this account against the fold's "
    "landed edge list, which oxbow score builds in memory and never writes; it does NOT mean zero "
    "exposure, because a distribution that is absent is not a distribution concentrated at nothing. "
    "mc_runs = 0 beside it states the draws actually taken, and ck_economics_mc_interval_pairing "
    "forbids one of the three being null while the others hold figures. See DEV-031, and this row's "
    "assumptions JSONB key exposure_interval_basis for the producer the landing named."
)

#: Why `mc_runs` keeps its NOT NULL and what its zero now means. Stated on the column because the
#: number is read on its own: a positive count is a claim about work done, and only the comment says
#: which of the two readings a 0 belongs to.
_DRAWS_RULE: str = (
    "the draws this row ACTUALLY made. 0 means the propagation never ran and the three quantile "
    "columns are null; 1 or more means they are its results, with mc_seed the seed used and "
    "mc_interval the nominal coverage. config/economics.yaml monte_carlo.runs declares the intended "
    "experiment and is never written here as though it had been performed, because that would claim "
    "draws that were not taken (DEV-031, migration 0004)."
)

_PAIRING_RULE: str = (
    "a simulated distribution is either entirely present or entirely absent: the draw count and the "
    "three quantiles are one claim about the run, not four columns that can be half-filled. This is "
    "0003's pairing discipline applied to the Monte Carlo, and it is why making the quantiles "
    "nullable did not make the table unable to tell zero exposure from no exposure."
)


def _literal(text: str) -> str:
    """The text as a Postgres string literal, with its apostrophes doubled.

    Written rather than interpolated, because these comments say "this row's own wording" and
    "0003's pairing discipline": a migration that failed on an apostrophe would be a migration that
    cannot be applied under the one condition it exists to serve.
    """
    return "'" + text.replace("'", "''") + "'"


def upgrade() -> None:
    for name in _QUANTILES:
        op.alter_column("economics", name, existing_type=sa.BigInteger(), nullable=True)

    op.create_check_constraint("ck_economics_mc_interval_pairing", "economics", _PAIRING)

    for name in _QUANTILES:
        op.execute(f"COMMENT ON COLUMN economics.{name} IS {_literal(_ABSENT_RULE)}")
    op.execute(f"COMMENT ON COLUMN economics.mc_runs IS {_literal(_DRAWS_RULE)}")
    op.execute(
        "COMMENT ON CONSTRAINT ck_economics_mc_interval_pairing ON economics IS "
        + _literal(_PAIRING_RULE)
    )


def downgrade() -> None:
    # REFUSES rather than deletes, and says which it does and why. Pre-0004 cannot hold an
    # interval-free row, so the two ways through were to delete every row whose propagation never
    # ran, or to fill its quantiles with something.
    #
    # 0003 deletes, and deletion was right there: the row it loses is one the pre-0003 loader was
    # already rejecting on the same terms, and the surviving rows keep a value the old schema could
    # state. Here the arithmetic is different in kind. Every account this repository has ever priced
    # is interval-free, because nothing in it produces the quantiles -- so the DELETE would empty
    # `economics` for the live run and turn the alert queue from 43,046 priced accounts into none.
    # That is the exact failure this revision clears, arriving because somebody typed `alembic
    # downgrade`. The alternative is worse: defaulting the trio to satisfy NOT NULL is the
    # fabrication 0004 removes, and it would be written by the tool that checks for it.
    #
    # So it refuses, and names the way out, which is to do the propagation for real: land the edge
    # list `oxbow score` never writes, run `simulate_exposure_interval` over it, re-land `economics`
    # with `intervals=`, and this downgrade then has nothing to invent.
    absent = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM economics WHERE mc_p05_minor IS NULL"))
        .scalar_one()
    )
    if absent:
        raise RuntimeError(
            f"downgrade 0004 refuses: {absent:,} `economics` row(s) carry no simulated interval, and "
            "the pre-0004 schema declares mc_p05_minor/mc_p50_minor/mc_p95_minor NOT NULL. Deleting "
            "them would empty the table the alert queue prices from; defaulting them would store a "
            "distribution nobody sampled. Either run "
            "quant/monte_carlo.simulate_exposure_interval over the fold's landed edge list and "
            "re-land economics with intervals=, or drop the runs those rows belong to and downgrade "
            "again. Nothing here chooses for you."
        )

    op.drop_constraint("ck_economics_mc_interval_pairing", "economics", type_="check")
    op.execute("COMMENT ON COLUMN economics.mc_runs IS NULL")
    for name in _QUANTILES:
        op.execute(f"COMMENT ON COLUMN economics.{name} IS NULL")
        op.alter_column("economics", name, existing_type=sa.BigInteger(), nullable=False)
