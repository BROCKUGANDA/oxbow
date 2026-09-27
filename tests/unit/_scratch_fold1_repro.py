"""Scratch repro (NOT a pytest test; the leading underscore keeps it out of collection).

The smallest faithful reproduction of the failure that landed fold 0 and killed folds 1-4 of
run 01M3FZ2GC3J71AYT1QEKPWEDKJ with ``LightGBMError: bad allocation``: the real fold-1
training frame out of the landed corpus, the real 75-feature list, the real dtypes, the real
hyperparameters ``config/model.yaml`` names -- and one variable: how the three declared
categorical columns are coded on their way into ``lightgbm.Dataset``.

    prefix  : what the code did until now -- P4a's FNV-1a encoder applied to ALREADY-CODED
              integer columns, producing 2.4e9-magnitude values, then named to
              ``categorical_feature`` as category indices.
    postfix : what the code does now -- the features layer's own dense integer codes
              (0..23, 0..5, 0..2498) named to ``categorical_feature``.

Both branches see the same rows, the same features, the same params. RSS and free physical
memory are printed at every step, so the allocation is attributed to the coding and not to
the frame, the fold count, ``max_bin``, ``num_leaves`` or ``n_estimators``.

    uv run python -u tests/unit/_scratch_fold1_repro.py prefix --fold 1
    uv run python -u tests/unit/_scratch_fold1_repro.py postfix --fold 1
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import gc
import sys
import time
from pathlib import Path

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parents[2]
CORPUS = REPO / "out/score/01M3FZ2GC3J71AYT1QEKPWEDKJ/backtest_corpus.parquet"
ECON_COLS = ("exposure_minor", "amount_minor", "review_minutes", "review_cost_minor")


class PMC(ctypes.Structure):
    _fields_ = [  # noqa: RUF012 -- ctypes requires a plain class attribute
        ("cb", ctypes.wintypes.DWORD),
        ("PageFaultCount", ctypes.wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("a", ctypes.c_size_t),
        ("b", ctypes.c_size_t),
        ("c", ctypes.c_size_t),
        ("d", ctypes.c_size_t),
        ("e", ctypes.c_size_t),
        ("f", ctypes.c_size_t),
    ]


def _ws(peak: bool = False) -> float:
    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    ok = ctypes.windll.psapi.GetProcessMemoryInfo(
        ctypes.c_void_p(k32.GetCurrentProcess()), ctypes.byref(pmc), ctypes.wintypes.DWORD(pmc.cb)
    )
    return (
        float("nan")
        if not ok
        else ((pmc.PeakWorkingSetSize if peak else pmc.WorkingSetSize) / (1024 * 1024))
    )


def free_gb() -> float:
    class STATUSEX(ctypes.Structure):
        _fields_ = [  # noqa: RUF012 -- ctypes requires a plain class attribute
            ("dwLength", ctypes.wintypes.DWORD),
            ("dwMemoryLoad", ctypes.wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    stat = STATUSEX()
    stat.dwLength = ctypes.sizeof(STATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
    return stat.ullAvailPhys / 1024**3


def mark(label: str) -> None:
    print(
        f"[repro] {label} | rss {_ws():.0f} MB peak {_ws(peak=True):.0f} MB free {free_gb():.2f} GB",
        flush=True,
    )


def fold_slices(fold_index: int) -> tuple[pl.DataFrame, pl.DataFrame]:
    from oxbow.backtest.fold_provider import SplitsFoldProvider
    from oxbow.backtest.splits import build_walk_forward
    from oxbow.features.compute import registry_from_repo

    frame = pl.read_parquet(CORPUS)
    frame = frame.drop([c for c in ECON_COLS if c in frame.columns]).sort(
        ["as_of_ts", "account_key"]
    )
    registry = registry_from_repo(str(REPO))
    timeline = pl.concat(
        [
            frame.select(
                pl.col("as_of_ts").alias("event_ts_utc"),
                pl.col("account_key").alias("entity"),
            ),
            pl.DataFrame(
                {
                    "event_ts_utc": pl.Series([frame.get_column("as_of_ts").max()]).dt.offset_by(
                        "1us"
                    ),
                    "entity": [str(frame.get_column("account_key")[0])],
                }
            ),
        ]
    )
    plan = build_walk_forward(timeline, registry=registry, config_dir=REPO / "config")
    provider = SplitsFoldProvider(plan, as_of_column="as_of_ts")
    for fold, harness_fold in zip(plan.folds, provider.folds(frame), strict=True):
        if fold.index != fold_index:
            continue
        return (
            frame.filter(pl.Series(harness_fold.train_mask)),
            frame.filter(pl.Series(harness_fold.validation_mask)),
        )
    raise SystemExit(f"no fold {fold_index} in the plan")


def matrix(
    frame: pl.DataFrame, features: list[str], categorical: tuple[str, ...], coding: str
) -> np.ndarray:
    """The model input matrix, coded the way the branch names says it was."""
    if coding == "postfix":
        from oxbow.models.inputs import feature_matrix

        return feature_matrix(frame, tuple(features), categorical)
    from oxbow.scoring.frame import _stable_category_codes

    cat = set(categorical)
    columns: list[np.ndarray] = []
    for name in features:
        column = frame.get_column(name)
        if name in cat:
            # THE OLD LINE, verbatim: models/inputs.feature_matrix did exactly this for every
            # column named in scorecard.yaml's binning.categorical_features, integer or not.
            columns.append(_stable_category_codes(column).astype(np.float64))
        else:
            casted = column.cast(pl.Float64, strict=False) if column.dtype != pl.Float64 else column
            columns.append(casted.fill_null(np.nan).to_numpy(allow_copy=True).astype(np.float64))
    return np.ascontiguousarray(np.column_stack(columns), dtype=np.float64)


def main() -> int:
    import lightgbm as lgb

    from oxbow.models.config import load_model_config
    from oxbow.models.gbm import MIN_POSITIVES_FOR_FIT
    from oxbow.scoring.config import load_feature_registry, load_scorecard_config

    ap = argparse.ArgumentParser()
    ap.add_argument("coding", choices=("prefix", "postfix"))
    ap.add_argument("--fold", type=int, default=1)
    args = ap.parse_args()

    mark(f"start coding={args.coding} fold={args.fold}")
    registry = load_feature_registry(REPO)
    features = list(registry.names)
    declared = tuple(load_scorecard_config(REPO).binning.categorical_features)
    model_cfg = load_model_config(REPO)
    train, valid = fold_slices(args.fold)
    mark(f"fold {args.fold} slices train={train.height} valid={valid.height}")

    x_train = matrix(train, features, declared, args.coding)
    x_valid = matrix(valid, features, declared, args.coding)
    y_train = train.get_column("label_is_fraud").cast(pl.Int32).to_numpy()
    y_valid = valid.get_column("label_is_fraud").cast(pl.Int32).to_numpy()
    cat_for_lgbm = (
        list(categorical_for_lightgbm_names(x_train, features, declared))
        if args.coding == "postfix"
        else [name for name in features if name in set(declared)]
    )
    print(
        f"[repro] x_train {x_train.shape} {x_train.nbytes/1024**2:.1f} MB "
        f"categorical named to lgbm={cat_for_lgbm} "
        f"cat-column max code={x_train[:, [features.index(n) for n in declared]].max(axis=0)}",
        flush=True,
    )
    mark("matrices built")
    if int(y_train.sum()) < MIN_POSITIVES_FOR_FIT or int(y_valid.sum()) == 0:
        raise SystemExit(
            f"fold {args.fold} has no fit population ({int(y_train.sum())}/{int(y_valid.sum())})"
        )

    params = dict(model_cfg.gbm.lgb_params)
    params["seed"] = model_cfg.seed
    params["data_random_seed"] = model_cfg.seed
    params["bagging_seed"] = model_cfg.seed
    params["feature_fraction_seed"] = model_cfg.seed
    params["thread_seed"] = model_cfg.seed
    params["scale_pos_weight"] = float((len(y_train) - y_train.sum()) / max(1, int(y_train.sum())))
    print(f"[repro] params={params}", flush=True)

    ds = lgb.Dataset(
        x_train,
        label=y_train,
        feature_name=features,
        categorical_feature=cat_for_lgbm if cat_for_lgbm else "auto",
        free_raw_data=False,
    )
    dv = lgb.Dataset(
        x_valid,
        label=y_valid,
        reference=ds,
        feature_name=features,
        categorical_feature=cat_for_lgbm if cat_for_lgbm else "auto",
        free_raw_data=False,
    )
    gc.collect()
    t0 = time.perf_counter()
    try:
        booster = lgb.train(
            params,
            ds,
            num_boost_round=model_cfg.gbm.n_estimators,
            valid_sets=[dv],
            valid_names=["validation"],
            callbacks=[lgb.early_stopping(model_cfg.gbm.early_stopping_rounds, verbose=False)],
        )
    except Exception as exc:
        print(
            f"[repro] {args.coding} fold {args.fold}: {type(exc).__name__}: {exc} "
            f"after {time.perf_counter()-t0:.1f}s",
            flush=True,
        )
        mark("FAILED")
        return 1
    print(
        f"[repro] {args.coding} fold {args.fold}: OK in {time.perf_counter()-t0:.1f}s "
        f"best_iteration={booster.best_iteration} "
        f"pr_auc={booster.best_score['validation']['average_precision']:.6f} "
        f"split_features={int(np.count_nonzero(booster.feature_importance('split')))}",
        flush=True,
    )
    mark("fitted")
    return 0


def categorical_for_lightgbm_names(
    matrix: np.ndarray, features: list[str], declared: tuple[str, ...]
) -> tuple[str, ...]:
    """The declared categoricals whose codes are admissible LightGBM category indices."""
    from oxbow.models.inputs import MAX_CATEGORY_INDEX

    out = []
    for name in declared:
        if name not in features:
            continue
        column = matrix[:, features.index(name)]
        finite = column[np.isfinite(column)]
        if finite.size == 0:
            out.append(name)
            continue
        if float(finite.min()) >= 0.0 and float(finite.max()) <= MAX_CATEGORY_INDEX:
            out.append(name)
    return tuple(out)


if __name__ == "__main__":
    sys.exit(main())
