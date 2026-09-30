"""The four ``/api/validation`` fields that a stored row can back — and the two it will not fake.

``apps/web`` used to decode six names this route had never sent: ``pr_curve``, ``brier``,
``calibration_floor``, ``shap_waterfall``, ``time``, ``policy_id``. The client's decoders are
gone; this is the server side of that contract, and it is tested against the rule the whole read
model is built on — **the API never publishes a statistic the pipeline did not publish** (plan 02
§B seam 5). So each of the four served names is asserted to be *the stored row, reshaped and not
combined*, and each of the two absent ones is asserted to be *absent from the response model*,
which is a stronger statement than serving a null or an empty list: a field that is not declared
cannot be rendered as a result by a client that does not know what it would have meant.

The route is called directly with a fake source that records every read, because the point of
this test is which rows the answer came from, and a live warehouse on this host has none of them
landed yet.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
for _extra in (REPO_ROOT / "apps", REPO_ROOT / "apps" / "api"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from api.routers.validation import (  # noqa: E402
    _brier_measurement,
    _calibration_floor,
    _policy_identity,
    _run_time,
    validation,
)
from api.schemas.validation import ValidationBundle  # noqa: E402

RUN_ID = "01VALIDATIONBUNDLE0000000000"
UNCALIBRATED_NOTE = (
    "14 validation positives is below calibration.min_positives_for_calibration=50; an observed "
    "rate measured on that few rows is noise, and Module C multiplies it by money"
)


def _fold(index: int, *, brier: float, corpus: str = "real-corpus") -> dict[str, Any]:
    return {
        "fold_index": index,
        "corpus": corpus,
        "train_start": datetime(2014, 1, 1).date(),
        "train_end": datetime(2014, 6, 30).date(),
        "embargo_days": 30,
        "embargo_end": datetime(2014, 7, 30).date(),
        "test_start": datetime(2014, 7, 31).date(),
        "test_end": datetime(2014, 12, 31).date(),
        "n_train": 30_722,
        "n_test": 23_032,
        "pr_auc": 0.00650211,
        "auroc": 0.61015557,
        "brier": brier,
        "precision_at_budget": None,
        "recall_at_budget": 0.0,
        "precision_undefined": True,
        "alerts": 0,
        "captured_value_minor": 0,
        "cost_minor": 0,
        "net_benefit_minor": 0,
        "max_drawdown_minor": 0,
        "var95_minor": 658_016_522,
        "es975_minor": 761_949_953,
        "mc_runs": 10_000,
        "mc_seed": 1337,
        "currency": "UGX",
        "entity_disjoint": False,
        "test_fold_touched_at": None,
    }


class FakeSource:
    """The one read primitive the route uses: ``select`` by table name, filtered in Python."""

    name = "test-fake"

    def __init__(self, tables: Mapping[str, Sequence[dict[str, Any]]]) -> None:
        self.tables = {table: [dict(row) for row in rows] for table, rows in tables.items()}
        self.reads: list[tuple[str, dict[str, Any]]] = []

    def select(
        self,
        table: str,
        *,
        where: Mapping[str, Any] | None = None,
        columns: Sequence[str] | None = None,
        order: str | None = None,
        descending: bool = False,
        limit: int | None = None,
        offset: int = 0,
        allow_missing: bool = False,
        with_count: bool = True,
    ) -> tuple[list[dict[str, Any]], int | None]:
        del allow_missing
        self.reads.append((table, dict(where or {})))
        rows = [
            row
            for row in self.tables.get(table, [])
            if all(str(row.get(key)) == str(value) for key, value in (where or {}).items())
        ]
        if order is not None:
            rows.sort(key=lambda row: row.get(order), reverse=descending)
        total = len(rows) if with_count else None
        window = rows[offset:] if limit is None else rows[offset : offset + limit]
        if columns:
            window = [{key: row[key] for key in columns if key in row} for row in window]
        return window, total


class FakeReadModel:
    def __init__(self, tables: Mapping[str, Sequence[dict[str, Any]]]) -> None:
        self.source = FakeSource(tables)

    @property
    def money_decimals(self) -> int:
        return 2

    def resolve_run(self, run_id: str | None, *, state: str | None = None) -> dict[str, Any]:
        del run_id, state
        return self.source.tables["run"][0]


class FakeContainer:
    """Only what ``validation`` and ``build_meta`` actually touch."""

    def __init__(self, tables: Mapping[str, Sequence[dict[str, Any]]]) -> None:
        self.read_model = FakeReadModel(tables)

    class _Economics:
        class _Path:
            name = "economics.yaml"

        source_path = _Path()

    economics = _Economics()

    def components(self) -> list[Any]:
        return []

    def component(self, name: str) -> Any:
        raise AssertionError(name)


def _tables(**extra: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    tables: dict[str, list[dict[str, Any]]] = {
        "run": [
            {
                "run_id": RUN_ID,
                "state": "complete",
                "created_at": datetime(2026, 9, 27, 17, 45, 23, tzinfo=UTC),
                "finished_at": datetime(2026, 9, 27, 19, 29, 19, tzinfo=UTC),
                "model_version": "oxbow/0.1.0",
                "provenance": "pipeline",
            }
        ],
        "backtest_fold": [_fold(0, brier=0.00123847), _fold(4, brier=0.00040969)],
        "ablation_row": [],
        "validation_metric": [],
        "fairness_row": [],
        "perturbation_row": [],
        "confusion_cell": [],
        "curve_point": [],
        "score": [],
        "stage_event": [],
        "policy_allocation": [],
    }
    tables.update({key: list(value) for key, value in extra.items()})
    # Every row of a run-scoped table carries its run, because that is what the source filters
    # on: a fixture without it would silently match no `where={"run_id": …}` probe.
    for name, rows in tables.items():
        if name != "run":
            for row in rows:
                row.setdefault("run_id", RUN_ID)
    return tables


def _body(tables: Mapping[str, Sequence[dict[str, Any]]]) -> dict[str, Any]:
    result = validation(run_id=None, container=FakeContainer(tables), principal=None)  # type: ignore[arg-type]
    return result["data"].model_dump(mode="json")


# ---------------------------------------------------------------------------
# brier: the distribution the folds carry, never a mean of it
# ---------------------------------------------------------------------------


def test_brier_is_the_per_fold_distribution_and_no_aggregate() -> None:
    body = _body(_tables())

    brier = body["brier"]
    assert brier is not None
    assert [item["brier"] for item in brier["per_fold"]] == [0.00123847, 0.00040969]
    assert [item["fold_index"] for item in brier["per_fold"]] == [0, 4]
    assert {item["corpus"] for item in brier["per_fold"]} == {"real-corpus"}
    assert brier["aggregation"].startswith("none")
    assert "mean" in brier["aggregation"], "the field has to say in words that none was taken"
    # The values are the fold rows' own, and no float anywhere in the field is a combination.
    assert all(
        item["brier"] == fold["brier"]
        for item, fold in zip(brier["per_fold"], _tables()["backtest_fold"], strict=True)
    )


def test_brier_helper_adds_no_new_read() -> None:
    """``brier`` is built from the fold rows the route already fetched — no second query."""
    tables = _tables()
    container = FakeContainer(tables)

    _brier_measurement(container.read_model.source.tables["backtest_fold"])

    assert container.read_model.source.reads == []


# ---------------------------------------------------------------------------
# calibration_floor: the scorer's refusal, echoed
# ---------------------------------------------------------------------------


def test_calibration_floor_echoes_the_stored_refusal_and_names_its_source() -> None:
    tables = _tables(
        score=[
            {
                "account_key": "ACC-1",
                "calibration_kind": "uncalibrated",
                "calibration_note": UNCALIBRATED_NOTE,
            }
        ]
    )

    floor = _calibration_floor(FakeReadModel(tables), RUN_ID)

    assert floor is not None
    assert floor.kind == "uncalibrated"
    assert floor.refused is True
    assert floor.note == UNCALIBRATED_NOTE, "the producer's sentence travels unchanged"
    assert "min_positives_for_calibration=50" in (floor.note or "")
    assert floor.floor_source == "config/model.yaml#/calibration/min_positives_for_calibration"


def test_a_calibrated_run_is_not_reported_as_refused() -> None:
    tables = _tables(
        score=[
            {
                "account_key": "ACC-1",
                "calibration_kind": "calibrated_band",
                "calibration_note": None,
            }
        ]
    )

    floor = _calibration_floor(FakeReadModel(tables), RUN_ID)

    assert floor is not None
    assert floor.kind == "calibrated_band"
    assert floor.refused is False
    assert floor.note is None


def test_no_stored_score_row_is_no_floor_claim_rather_than_a_default() -> None:
    floor = _calibration_floor(FakeReadModel(_tables()), RUN_ID)

    assert floor is None, "no row, no statement"


def test_a_run_whose_rows_disagree_about_calibration_publishes_neither_label() -> None:
    tables = _tables(
        score=[
            {
                "account_key": "ACC-1",
                "calibration_kind": "uncalibrated",
                "calibration_note": UNCALIBRATED_NOTE,
            },
            {
                "account_key": "ACC-2",
                "calibration_kind": "calibrated_band",
                "calibration_note": None,
            },
        ]
    )

    assert _calibration_floor(FakeReadModel(tables), RUN_ID) is None, (
        "picking the first row of two contradictory stored labels is the invention this rule "
        "forbids; the run has to agree before the page can say anything"
    )


# ---------------------------------------------------------------------------
# time: two stamps and the stage ledger, with no computed duration
# ---------------------------------------------------------------------------


def test_time_carries_the_recorded_stamps_and_stage_elapsed() -> None:
    tables = _tables(
        stage_event=[
            {
                "id": 41,
                "stage": "score",
                "status": "complete",
                "rows": 43_720,
                "elapsed_ms": 812_004,
                "emitted_at": datetime(2026, 9, 27, 18, 0, 0, tzinfo=UTC),
            }
        ]
    )

    run_time = _run_time(FakeReadModel(tables), tables["run"][0])

    assert run_time is not None
    assert run_time.run_id == RUN_ID
    assert run_time.state == "complete"
    assert run_time.created_at == datetime(2026, 9, 27, 17, 45, 23, tzinfo=UTC)
    assert run_time.finished_at is not None
    assert [(s.stage, s.elapsed_ms, s.rows) for s in run_time.stages] == [
        ("score", 812_004, 43_720)
    ]
    assert run_time.stages_note is None


def test_a_run_with_no_readable_stage_rows_says_so_instead_of_showing_an_empty_chart() -> None:
    model = FakeReadModel(_tables())

    run_time = _run_time(model, model.source.tables["run"][0])

    assert run_time.stages == []
    assert run_time.stages_note is not None
    assert "stage_events.jsonl" in run_time.stages_note
    assert not hasattr(
        run_time, "duration_ms"
    ), "no total is published: subtracting the two stamps would price in time no stage recorded"


# ---------------------------------------------------------------------------
# policy_id: an identifier off a row, or null with its reason
# ---------------------------------------------------------------------------


def test_policy_id_is_read_off_the_run_stored_allocations() -> None:
    tables = _tables(
        policy_allocation=[
            {"policy_id": "ev_cpsat", "rank": 1, "account_key": "ACC-1"},
            {"policy_id": "ev_cpsat", "rank": 2, "account_key": "ACC-2"},
        ]
    )

    policy_id, note = _policy_identity(FakeReadModel(tables), RUN_ID)

    assert policy_id == "ev_cpsat"
    assert note is None


def test_a_run_with_no_allocations_returns_null_and_its_reason() -> None:
    policy_id, note = _policy_identity(FakeReadModel(_tables()), RUN_ID)

    assert policy_id is None
    assert note is not None
    assert "policy_allocation" in note


def test_the_served_bundle_carries_the_four_and_only_the_four() -> None:
    body = _body(
        _tables(
            score=[
                {
                    "account_key": "ACC-1",
                    "calibration_kind": "uncalibrated",
                    "calibration_note": UNCALIBRATED_NOTE,
                }
            ],
            policy_allocation=[{"policy_id": "ev_cpsat", "rank": 1, "account_key": "ACC-1"}],
            stage_event=[
                {
                    "id": 42,
                    "stage": "warehouse",
                    "status": "complete",
                    "rows": 7,
                    "elapsed_ms": 51_000,
                    "emitted_at": datetime(2026, 9, 27, 19, 29, 19, tzinfo=UTC),
                }
            ],
        )
    )

    assert body["policy_id"] == "ev_cpsat"
    assert body["policy_id_note"] is None
    assert body["calibration_floor"]["refused"] is True
    assert body["time"]["stages"][0]["stage"] == "warehouse"
    assert body["brier"]["per_fold"]

    declared = set(ValidationBundle.model_fields)
    assert {"brier", "calibration_floor", "time", "policy_id"} <= declared
    # The two that no row backs are absent from the contract, not nulled into it.
    assert "pr_curve" not in declared
    assert "shap_waterfall" not in declared


def test_pr_curve_is_forwarded_if_and_only_if_a_curve_point_row_exists() -> None:
    """The route already serves any family it is handed; nothing in the pipeline writes one.

    Asserting both halves keeps the fix honest: if a future landing writes ``curve_point``, the
    page fills without a code change, and until then the field is empty rather than invented.
    """
    without = _body(_tables())
    assert without["curves"] == []

    with_curves = _body(
        _tables(
            curve_point=[
                {
                    "family": "pr_curve",
                    "point_index": 0,
                    "x": 0.1,
                    "y": 0.9,
                    "n": 100,
                    "label": None,
                    "operating_point": False,
                },
                {
                    "family": "pr_curve",
                    "point_index": 1,
                    "x": 0.2,
                    "y": 0.8,
                    "n": 200,
                    "label": None,
                    "operating_point": True,
                },
            ]
        )
    )
    families = [series["family"] for series in with_curves["curves"]]
    assert families == ["pr_curve"]
    series = with_curves["curves"][0]
    assert (series["x_label"], series["y_label"]) == ("recall", "precision")
    assert [point["x"] for point in series["points"]] == [0.1, 0.2]
    assert series["operating_threshold"] == 0.2
