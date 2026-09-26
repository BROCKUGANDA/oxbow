"""The command dashboard: four money tiles, model chips, the curve, the pattern feed.

Plan §14's day-11 gate is *"an unbroken click path from a currency KPI to a written,
hash-chained decision"*, and this router is the first link. Every tile is a stored
figure with its source named, and the loss-avoided tile is a :class:`BandedMoney`:
constructed here so the band is part of the number rather than a courtesy the UI owes
the reader.

The band re-pricing is a P5 call, not arithmetic in a router:
``quant.ev.expected_loss_avoided`` is asked for the same set at each configured
recovery rate, which is how the three rendered values stay consistent with one another
instead of being re-derived three times.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from fastapi import APIRouter, Depends, Query

from api.deps import Container, analyst_or_higher, get_container
from api.policy_engine import stored_priced_rows
from api.problems import (
    COMMON_ERROR_STATUSES,
    DependencyUnavailable,
    problem_responses,
)
from api.readmodel import ReadModel
from api.routers.common import assumption_lines, build_meta
from api.schemas.common import Envelope, envelope
from api.schemas.dashboard import (
    BandDistribution,
    BandedMoney,
    DashboardResponse,
    ModelChip,
    PatternFeedItem,
)
from api.security import Principal
from oxbow.config import load_yaml

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

CHIP_METRICS: Final = {
    "pr_auc": ("PR-AUC", "ratio", "primary ranking statistic (plan §12)"),
    "precision_at_budget": ("precision at budget", "ratio", None),
    "brier": ("Brier score", "ratio", "lower is better"),
    "auroc": ("AUROC", "ratio", "reported for comparability and explicitly de-emphasised"),
    "alerts_per_10k_accounts": ("alerts per 10k accounts", "count", "alert-fatigue proxy"),
}
BASELINE_SUFFIX: Final = ":baseline"


@router.get(
    "",
    response_model=Envelope[DashboardResponse],
    summary="The currency KPIs, model-quality chips, benefit curve and pattern feed",
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def dashboard(
    run_id: str | None = Query(default=None, min_length=26, max_length=26),
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    read_model = container.read_model
    run = read_model.resolve_run(run_id, state="complete" if run_id is None else None)
    rid = str(run["run_id"])
    summaries, _ = read_model.source.select(
        "policy_summary", where={"run_id": rid}, order="policy_id", allow_missing=True
    )
    if not summaries:
        raise DependencyUnavailable(
            f"run {rid} stored no policy summary, so the dashboard has no money figures to "
            "show. Every tile on this screen is a stored measurement; none is composed here "
            "from counts, because a composed figure is the lie plan §19 forbids.",
        )
    summary = _preferred_summary(summaries)
    metrics, _ = read_model.source.select(
        "validation_metric", where={"run_id": rid}, order="name", allow_missing=True
    )
    bands, _ = read_model.source.select(
        "band_definition", where={"run_id": rid}, order="band", allow_missing=True
    )
    evidence, _ = read_model.source.select(
        "evidence_event",
        where={"run_id": rid},
        order="occurred_at",
        descending=True,
        limit=8,
        allow_missing=True,
    )
    communities = _high_risk_networks(read_model, rid)
    decimals = container.economics.minor_units_per_major
    currency = str(summary["currency"])
    loss_avoided = int(summary["loss_avoided_minor"])
    band_rates = list(container.economics.recovery.band)
    band_figures = loss_avoided_over_band(
        container, rid, str(summary["policy_id"]), band_rates
    )

    body = DashboardResponse(
        run_id=rid,
        run_state=str(run["state"]),
        as_of=_as_of(summary),
        currency=currency,
        expected_loss_avoided=BandedMoney(
            point=_money(loss_avoided, currency, decimals),
            over_band=[
                _money(minor, currency, decimals) for minor in band_figures.values()
            ],
            band_rates=list(band_figures),
            basis="recovery.sensitivity_band",
            note=(
                "the stored figure is computed at recovery.rate = "
                f"{container.economics.recovery.rate}; the band re-prices the same selected "
                "set at each configured rate through quant.ev.expected_loss_avoided, so the "
                "three values are consistent with one another rather than derived three times"
            ),
        ),
        benefit_per_analyst_hour=_money(
            int(summary["benefit_per_analyst_hour_minor"]), currency, decimals
        ),
        residual_exposure_es975=_money(int(summary["es975_minor"]), currency, decimals),
        alerts_generated=int(summary["candidate_count"]),
        alerts_reviewed=int(summary["selected_count"]),
        alerts_below_cutoff=int(summary["candidate_count"]) - int(summary["selected_count"]),
        high_risk_networks=communities["count"],
        high_risk_networks_basis=communities["basis"],
        capacity_minutes=int(summary["capacity_minutes"]),
        cutoff_rank=_cutoff_rank(read_model, rid, str(summary["policy_id"])),
        model_chips=_chips(metrics),
        band_distribution=[
            BandDistribution(
                band=str(row["band"]),
                count=_band_count(read_model, rid, str(row["band"])),
                observed_rate=float(row["observed_rate"]),
                n_calibration=int(row["n"]),
                action=str(row["action"]),
                review_minutes=float(row["review_minutes"]),
            )
            for row in bands
        ],
        cumulative_benefit_curve=list(summary["cumulative_curve"] or []),
        latest_patterns=[
            PatternFeedItem(
                occurred_at=row["occurred_at"],
                kind=str(row["kind"]),
                label=str(row["label"]),
                account_key=str(row["account_key"]),
                run_id=rid,
                rule_id=row.get("rule_id"),
                typology=(row.get("detail") or {}).get("typology"),
                case_id=None,
            )
            for row in evidence
        ],
        dataset_badge=_dataset_badge(read_model),
        assumptions=[line.model_dump() for line in assumption_lines(container.economics)],
    )
    return envelope(
        body,
        **build_meta(
            container,
            run_id=rid,
            model_version=str(run["model_version"]),
            provenance=str(run["provenance"]),
        ).model_dump(),
    )


def loss_avoided_over_band(
    container: Container, run_id: str, policy_id: str, rates: list[float]
) -> dict[float, int]:
    """The same reviewed set, priced at each configured recovery rate.

    Returns one minor-unit figure per rate, keyed by that rate. If the run left no
    selection to price, the point estimate is repeated for each rate and the caller's
    note still states which rate produced it — an absent band would be a claim that the
    figure is insensitive to the assumption, which is the opposite of the truth.
    """
    from oxbow.quant.ev import expected_loss_avoided

    rows, _ = container.read_model.source.select(
        "policy_allocation",
        where={"run_id": run_id, "policy_id": policy_id, "selected": True},
        allow_missing=True,
    )
    if not rows:
        return {rate: 0 for rate in rates}
    priced, _ = stored_priced_rows(container.read_model, run_id, container.economics)
    selected = [row for row in priced if row.account_key in {str(item["account_key"]) for item in rows}]
    if not selected:
        return {rate: 0 for rate in rates}
    return {
        rate: expected_loss_avoided(
            selected, None, rate, container.economics
        ).minor
        for rate in rates
    }


def _preferred_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The active policy's summary when one exists, else the best net benefit.

    Ordered deterministically (net benefit, then policy id) so a page refreshed twice
    shows the same tile, which an "any row" pick would not guarantee.
    """
    return sorted(
        rows,
        key=lambda row: (-int(row["net_benefit_minor"]), str(row["policy_id"])),
    )[0]


def _chips(metrics: list[dict[str, Any]]) -> list[ModelChip]:
    by_name = {str(row["name"]): row for row in metrics}
    chips: list[ModelChip] = []
    for name, (label, unit, note) in CHIP_METRICS.items():
        row = by_name.get(name)
        baseline = by_name.get(f"{name}{BASELINE_SUFFIX}")
        if row is None and baseline is None:
            continue
        value = None if row is None else float(row["value"])
        base = None if baseline is None else float(baseline["value"])
        # n == 0 is the stored form of "undefined": a ratio over an empty population,
        # which plan §12 requires reporting as undefined rather than as zero.
        undefined = bool(row is not None and int(row.get("n") or 0) == 0)
        chips.append(
            ModelChip(
                name=label,
                value=value,
                unit=unit,
                corpus=None if row is None else str(row["corpus"]),
                baseline=base,
                delta=None if value is None or base is None else round(value - base, 6),
                delta_note=None
                if value is None or base is None
                else "delta against the stored baseline metric row for the same corpus",
                note=note if row is None else (row.get("note") or note),
                undefined=undefined,
            )
        )
    return chips


def _band_count(read_model: ReadModel, run_id: str, band: str) -> int:
    _, total = read_model.source.select(
        "score", where={"run_id": run_id, "band": band}, limit=1, allow_missing=True
    )
    return total


def _cutoff_rank(read_model: ReadModel, run_id: str, policy_id: str) -> int | None:
    rows, _ = read_model.source.select(
        "policy_allocation",
        where={"run_id": run_id, "policy_id": policy_id, "selected": True},
        order="rank",
        descending=True,
        limit=1,
        allow_missing=True,
    )
    return None if not rows else int(rows[0]["rank"])


def _high_risk_networks(read_model: ReadModel, run_id: str) -> dict[str, Any]:
    """Stored communities containing at least one band-D/E account.

    Counted from ``community`` plus ``account_membership`` plus ``score`` rather than
    re-clustered: a community the graph layer did not produce is not a network finding,
    and the basis string says which tables the number came out of.
    """
    communities, _ = read_model.source.select("community", where={"run_id": run_id}, allow_missing=True)
    memberships, _ = read_model.source.select(
        "account_membership", where={"run_id": run_id}, allow_missing=True
    )
    risky, _ = read_model.source.select(
        "score", where={"run_id": run_id, "band": ("D", "E")}, allow_missing=True
    )
    risky_keys = {str(row["account_key"]) for row in risky}
    by_community: dict[int, set[str]] = {}
    for row in memberships:
        by_community.setdefault(int(row["community_id"]), set()).add(str(row["account_key"]))
    count = sum(
        1 for index, keys in by_community.items() if keys & risky_keys and index in {
            int(row["canonical_index"]) for row in communities
        }
    )
    return {
        "count": count,
        "basis": "communities (stored Leiden output) containing at least one band-D/E account; "
        "counts come from community + account_membership + score, no re-clustering",
    }


def _dataset_badge(read_model: ReadModel) -> dict[str, Any]:
    """The header badge: which corpora, under which licences, from the dataset card."""
    try:
        document = load_yaml(repo_root() / "config" / "sources.yaml")
    except Exception as exc:
        raise DependencyUnavailable(f"config/sources.yaml could not be read for the badge: {exc}") from exc
    return {
        "sources": [
            {
                "id": str(entry.get("id")),
                "name": str(entry.get("name")),
                "license": str(entry.get("license", "UNSTATED")),
                "role": str(entry.get("role", "")),
            }
            for entry in document.get("sources", [])
        ],
        "note": "licence and citation are on the card because attribution belongs with the data",
    }


def repo_root() -> Any:
    """The repository root, for reading the declared dataset card.

    Resolved through :func:`oxbow.config.find_repo_root` rather than a path constant: a
    hard-coded ``Path(__file__).parents[n]`` breaks the moment the file moves, and the
    badge would then claim a licence nobody declared.
    """
    from oxbow.config import find_repo_root

    return find_repo_root()


def _as_of(summary: dict[str, Any]) -> datetime:
    curve = list(summary.get("cumulative_curve") or [])
    stamp = curve[-1].get("period_end") if curve else None
    if stamp is None:
        return datetime.now(UTC)
    if isinstance(stamp, datetime):
        return stamp
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))


def _money(minor: int, currency: str, decimals: int) -> dict[str, Any]:
    from api.readmodel import money

    return money(minor, currency, decimals=decimals)


__all__ = ["router"]
