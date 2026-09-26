#!/usr/bin/env python
"""Fail the build if money is typed as a float inside the pipeline package.

01 B hard constraint: money is integer minor units. ``amount_minor: int64``
everywhere, Pandera rejects floats, and a lint rule bans float money inside the
pipeline package. This is that lint rule, and it runs pre-commit so the coupling
is stopped at introduction rather than caught in CI three commits later.

03 A rule 2 is why this is strict: never let an unknown become a zero. A float
money column does not crash, it drifts. ``0.1 + 0.2`` summed over six million
rows is a reconciliation failure that stays invisible until someone totals a
column, by which point the number is quoted in a packet.

Exemptions are explicit and narrow, because an exemption list that grows quietly
is the same defect wearing a hat:

  * ``recovery.rate`` and its sensitivity band are genuine ratios, not money.
    They are config values, and the config test asserts the type separately.
  * Probabilities (p_calibrated, anomaly_norm) are floats by definition and are
    never multiplied into a minor-unit amount without an explicit cast.
  * Files under ``config/`` are YAML and are validated by the config test suite.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PACKAGE_ROOT = Path("packages/pipeline/oxbow")

# Identifiers whose values are genuinely real-valued. Each entry states why, so
# a reader can challenge it rather than assume the author knew.
FLOAT_ALLOWED: dict[str, str] = {
    # Calibrated probabilities and normalised scores: in [0, 1] by construction.
    "p_calibrated": "calibrated probability, not money",
    "p_scorecard": "calibrated probability, not money",
    "p_gbm": "calibrated probability, not money",
    "anomaly_norm": "percentile-rank anomaly score, not money",
    "probability": "calibrated probability, not money",
    # Recovery rate: a ratio. Never a money amount.
    "recovery_rate": "assumption ratio, not money",
    "value_share": "a share of a total, not money",
    # Statistical quantities.
    "reliability": "curve coordinate, not money",
    "woe": "weight of evidence, log ratio",
    "iv": "information value",
    "shap": "SHAP contribution in probability space",
}

# Name fragments that mark a value as money. Matched against the assigned name.
MONEY_NAME_FRAGMENTS = (
    "amount",
    "minor",
    "balance",
    "exposure",
    "cost",
    "value_avoided",
    "loss",
    "benefit",
    "ev_",
    "price",
    "fee",
    "revenue",
    "profit",
)

ANNOTATION_FLOAT = {"float", "np.float64", "numpy.float64", "np.float32", "numpy.float32"}


def _is_float_annotation(node: ast.expr) -> bool:
    """True when an annotation resolves to a float type."""
    if isinstance(node, ast.Name):
        return node.id == "float"
    if isinstance(node, ast.Attribute):
        # `np.float64` and `numpy.float32` both render as Attribute(value=Name),
        # so the base must be narrowed before it is interpolated. Assuming a Name
        # here is what makes mypy reject the whole checker.
        base = node.value.id if isinstance(node.value, ast.Name) else ""
        return f"{base}.{node.attr}" in {"np.float64", "np.float32"} or node.attr in {
            "float64",
            "float32",
        }
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value in ANNOTATION_FLOAT
    return False


def _mentions_money(name: str) -> bool:
    lowered = name.lower()
    return any(fragment in lowered for fragment in MONEY_NAME_FRAGMENTS)


def _exempt(name: str) -> str | None:
    """Return the exemption reason for ``name``, or None if it is not exempt."""
    return FLOAT_ALLOWED.get(name)


def scan(path: Path) -> list[str]:
    """Return one message per float-money violation in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    problems: list[str] = []

    for node in ast.walk(tree):
        # Annotated assignments: `amount_minor: float = ...`
        if (
            isinstance(node, ast.AnnAssign)
            and node.annotation is not None
            and isinstance(node.target, ast.Name)
            and _is_float_annotation(node.annotation)
        ):
            target = node.target.id
            if _mentions_money(target) and _exempt(target) is None:
                problems.append(
                    f"{path}:{node.lineno}: {target} is annotated as a float but names "
                    "money. Use int64 minor units (01 B)."
                )

        # Function parameters and returns annotated as float on a money name.
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            args = node.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                if (
                    arg.annotation is not None
                    and _is_float_annotation(arg.annotation)
                    and _mentions_money(arg.arg)
                    and _exempt(arg.arg) is None
                ):
                    problems.append(
                        f"{path}:{arg.lineno}: parameter {arg.arg} is annotated as a "
                        "float but names money. Use int64 minor units (01 B)."
                    )
            if (
                node.returns is not None
                and _is_float_annotation(node.returns)
                and _mentions_money(node.name)
                and _exempt(node.name) is None
            ):
                problems.append(f"{path}:{node.lineno}: {node.name} returns float but names money.")

    return problems


def main() -> int:
    """Scan the pipeline package and exit non-zero on any violation."""
    if not PACKAGE_ROOT.is_dir():
        print(f"no-float-money: {PACKAGE_ROOT} not found; run from the repo root")
        return 2

    all_problems: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        all_problems.extend(scan(path))

    if all_problems:
        print("no-float-money: float money is a defect, not a style choice (01 B)")
        for problem in all_problems:
            print(f"  {problem}")
        return 1

    print(f"no-float-money: OK ({len(list(PACKAGE_ROOT.rglob('*.py')))} files scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
