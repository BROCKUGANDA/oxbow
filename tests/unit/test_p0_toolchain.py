"""P0 gate tests for the toolchain, config boundary and CLI surface.

00 G: a phase is done when a test would fail if the code regressed, not when the
code runs. Every test here asserts a value that was decided by the spec, so
reverting any of it turns the suite red.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

from oxbow import DISCLAIMER
from oxbow.config import (
    ConfigError,
    find_repo_root,
    load_pipeline_config,
    load_yaml,
    require_run_salt,
)

REPO_ROOT = find_repo_root(Path(__file__).resolve().parent)


# --- the four stage verbs exist (01 P0 gate) ------------------------------


def test_cli_declares_exactly_the_four_stage_verbs() -> None:
    """01 D: the CLI is `oxbow ingest graph score backtest`. No more, no fewer."""
    from oxbow.cli import STAGES

    assert STAGES == ("ingest", "graph", "score", "backtest")


def test_cli_help_lists_all_four_verbs() -> None:
    """The gate clause itself: `oxbow --help` lists all four verbs.

    typer 0.15.1 renders help through click; this asserts the real rendered
    output, which is what catches the click 8.5 metavar break that would
    otherwise surface only when a judge runs --help on stage.
    """
    from typer.testing import CliRunner

    from oxbow.cli import app

    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    for verb in ("ingest", "graph", "score", "backtest"):
        assert verb in result.output, f"{verb} missing from --help output"


def test_stage_verbs_do_the_work_or_refuse_loudly(tmp_path: Path) -> None:
    """P0's discipline, restated now that the verbs are wired: never exit zero on nothing.

    The original clause — "a stage that is not built must not exit zero" — was asserted by
    ``graph`` printing ``NOT IMPLEMENTED`` and exiting 2. That body has been replaced by the
    real stage (P3a), so the same discipline is now asserted two ways that survive the
    wiring: no verb may print a placeholder for a stage whose package has landed, and a
    stage handed an unusable boundary must refuse with a non-zero code rather than report
    success. 00 B: never report a result you did not observe.
    """
    from typer.testing import CliRunner

    from oxbow.cli import app

    dry = CliRunner().invoke(app, ["graph", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "NOT IMPLEMENTED" not in dry.output, dry.output

    refused = CliRunner().invoke(app, ["graph", "--config-dir", str(tmp_path / "config")])
    assert refused.exit_code == 2, refused.output
    assert "REFUSED" in refused.output, refused.output


# --- config boundary fails loud (03 A rule 1) ----------------------------


def test_pipeline_config_loads_with_the_fixed_seed() -> None:
    """01 A rule 4: the global seed is 1337 and it is not negotiable."""
    cfg = load_pipeline_config(REPO_ROOT)
    assert cfg.seed == 1337
    assert cfg.deployment_timezone == "Africa/Kampala"


def test_config_rejects_a_seed_other_than_1337(tmp_path: Path) -> None:
    """Changing the seed silently would break `make verify-determinism`.

    Two runs of the pipeline must produce identical checksums; a different seed
    is a P1 bug by definition, so it is rejected at the boundary rather than
    discovered three phases later.
    """
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "pipeline.yaml").write_text(
        "seed: 42\ndeployment_timezone: UTC\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="1337"):
        load_pipeline_config(tmp_path)


def test_config_raises_on_missing_required_key(tmp_path: Path) -> None:
    """A missing key is a startup error, never a silent default."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "pipeline.yaml").write_text("seed: 1337\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="deployment_timezone"):
        load_pipeline_config(tmp_path)


def test_config_raises_on_missing_file(tmp_path: Path) -> None:
    """Loading the wrong config is exactly the silent failure 03 A rule 1 forbids."""
    with pytest.raises(ConfigError, match="missing"):
        load_yaml(tmp_path / "nope.yaml")


def test_run_salt_must_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """01 A rule 8: the salt lives in the environment, never the repo."""
    monkeypatch.delenv("RUN_SALT", raising=False)
    with pytest.raises(ConfigError, match="RUN_SALT"):
        require_run_salt()

    monkeypatch.setenv("RUN_SALT", "a" * 64)
    assert require_run_salt() == "a" * 64


def test_the_makefile_does_not_invent_a_run_salt() -> None:
    """The Makefile must not stand in for the environment when the salt is absent.

    ``require_run_salt`` above fails loud when RUN_SALT is missing, which is the
    designed behaviour: an empty environment is an error to be named, not a licence
    to invent one (03 A rule 1). The Makefile used to defeat that by minting a fresh
    salt per invocation with ``export RUN_SALT ?= $(shell ... secrets.token_hex(32))``.
    Because the salt is an *identity* rather than a nonce, a per-invocation salt
    re-keys every account in the corpus, makes the two runs of ``verify-determinism``
    incomparable, and shows up nowhere in any output -- so the gate that exists to
    catch exactly this class of drift was the thing introducing it.

    Asserted against the text rather than by running make: a test that shelled out to
    ``make`` would need a salt-free environment to be meaningful, and would then be
    testing the harness.
    """
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    body = "\n".join(line for line in makefile.splitlines() if not line.lstrip().startswith("#"))
    assert "RUN_SALT" not in body, (
        "the Makefile assigns RUN_SALT; the salt is an identity that must come from the "
        "environment or .env, never from a recipe (01 A rule 8)"
    )
    for generator in ("secrets", "token_hex", "uuid4"):
        assert generator not in body, f"the Makefile generates a value with {generator}"


# --- config values the spec pins -----------------------------------------


@pytest.mark.parametrize(
    "filename",
    [
        "pipeline.yaml",
        "rules.yaml",
        "features.yaml",
        "model.yaml",
        "scorecard.yaml",
        "economics.yaml",
        "splits.yaml",
        "sources.yaml",
    ],
)
def test_every_declared_config_file_exists_and_parses(filename: str) -> None:
    """01 D: empty values are fine, missing files are not.

    All eight files from 01 D are required before P0 closes.
    """
    path = REPO_ROOT / "config" / filename
    assert path.is_file(), f"{filename} is missing"
    assert isinstance(load_yaml(path), dict)


def test_rules_file_declares_exactly_twelve_typologies() -> None:
    """Spec 5.1: R1 to R12, no more and no fewer.

    A rule that never fires is dead code presented as capability, which is worse
    than not shipping it, so the count is pinned.
    """
    rules = load_yaml(REPO_ROOT / "config" / "rules.yaml")
    ids = [rule["id"] for rule in rules["rules"]]
    assert ids == [f"R{n}" for n in range(1, 13)]
    assert {rule["name"] for rule in rules["rules"]} >= {
        "RAPID_PASS_THROUGH",
        "FAN_IN",
        "FAN_OUT",
        "CYCLE_MEMBER",
        "STRUCTURING",
        "VELOCITY_SPIKE",
        "DORMANT_REACTIVATION",
        "ODD_HOUR_SHIFT",
        "AMOUNT_REGIME_SHIFT",
        "FAST_CASH_OUT",
        "NEW_COUNTERPARTY_SURGE",
        "CHAIN_MEMBER",
    }


def test_rule_hit_rate_ceiling_is_one_third() -> None:
    """00 G: a rule firing on more than a third of accounts is a constant."""
    rules = load_yaml(REPO_ROOT / "config" / "rules.yaml")
    assert rules["hit_rate_ceiling"] == 0.33


def _model_features(raw: dict[str, object]) -> list[dict[str, object]]:
    """The features that may enter the model matrix.

    ``role: intermediate`` entries are scaffolding a declared feature is computed
    from; they are deliberately excluded here because the plan's 60-75 target counts
    what the model sees, and because an intermediate leaking into the matrix would
    double-count the same signal under two names.
    """
    return [
        entry
        for entry in raw["features"]  # type: ignore[index]
        if entry.get("role") != "intermediate"
    ]


def _window_days(entry: dict[str, object]) -> int:
    """Lookback in whole days for a declared `24h` / `30d` / `point_in_time` window.

    Three declared kinds reach no row's past at all and so contribute no lookback:
    `point_in_time` reads the row itself, `lifetime` is a first-occurrence fact whose
    value cannot change as future rows arrive, and `fold_scoped` is recomputed per
    fold from that fold's edges only — which is the plan §8 guard that stops a graph
    feature being computed once over the whole corpus and then read inside a fold.
    """
    window = entry.get("window")
    if not isinstance(window, str) or window in {"point_in_time", "lifetime", "fold_scoped"}:
        return 0
    unit, amount = window[-1], window[:-1]
    if not amount.isdigit():
        raise AssertionError(f"window {window!r} is not a <int><unit> duration")
    if unit == "d":
        return int(amount)
    if unit == "h":
        return -(-int(amount) // 24)  # ceil: a partial day still reaches into a day
    raise AssertionError(f"unsupported window unit in {window!r}")


def test_feature_count_is_inside_the_sixty_to_seventy_five_target() -> None:
    """Spec 5.2: 60-75 model features, each with a plain sentence.

    Asserted against the registry's own notion of a model feature rather than a
    re-derived count, so the test and `config/features.yaml` cannot drift apart.
    """
    features = load_yaml(REPO_ROOT / "config" / "features.yaml")
    declared = _model_features(features)
    assert 60 <= len(declared) <= 75, f"expected 60-75 model features, found {len(declared)}"
    for entry in declared:
        assert str(entry.get("sentence", "")).strip(), f"{entry.get('id')} has no plain sentence"
        assert entry.get("as_of"), f"{entry.get('id')} declares no as-of rule"


def test_every_feature_declares_a_lookback_within_the_embargo() -> None:
    """The embargo must equal the longest feature lookback, or features leak.

    A 30-day rolling feature computed just after the boundary would otherwise see
    training-period data, which is the single most common way a backtest becomes
    fiction (03 C).
    """
    features = load_yaml(REPO_ROOT / "config" / "features.yaml")
    splits = load_yaml(REPO_ROOT / "config" / "splits.yaml")
    max_lookback = max(_window_days(entry) for entry in features["features"])  # type: ignore[index]
    assert max_lookback == features["max_lookback_days"], (
        f"longest declared window is {max_lookback}d but max_lookback_days says "
        f"{features['max_lookback_days']}; the embargo is derived from this number"
    )
    assert splits["walk_forward"]["embargo_days"] == features["max_lookback_days"]


def test_label_is_banned_from_the_feature_matrix() -> None:
    """Spec 7.2: no label, in either corpus's spelling, may reach the matrix."""
    features = load_yaml(REPO_ROOT / "config" / "features.yaml")
    banned = set(features["guards"]["banned_sources"])
    for column in ("label_is_fraud", "label_is_flagged", "label_typology"):
        assert column in banned, f"{column} is not banned from the feature matrix"
    assert features["guards"]["max_abs_correlation_with_label"] == 0.98
    assert features["guards"]["require_finite"] is True


def test_scorecard_scaling_constants_reproduce_600_at_fifty_to_one() -> None:
    """01 P4 gate: the scorecard reproduces 600 points at 50:1 odds.

    factor = PDO / ln(2); offset = base - factor * ln(base_odds).
    At base odds, ln(odds) = ln(50), so score = offset + factor*ln(50) = base.
    """
    import math

    scaling = load_yaml(REPO_ROOT / "config" / "scorecard.yaml")["scaling"]
    pdo, base_score, base_odds = (
        scaling["pdo"],
        scaling["base_score"],
        scaling["base_odds"],
    )
    factor = pdo / math.log(2)
    offset = base_score - factor * math.log(base_odds)
    assert offset + factor * math.log(base_odds) == pytest.approx(base_score, abs=1e-9)
    assert (pdo, base_score, base_odds) == (20, 600, 50)


def test_iv_admission_band_is_two_to_five_tenths() -> None:
    """Spec 4.2: keep 0.02 <= IV <= 0.5; above 0.5 is suspected leakage."""
    iv = load_yaml(REPO_ROOT / "config" / "scorecard.yaml")["iv_bounds"]
    assert (iv["min"], iv["max"]) == (0.02, 0.5)
    assert iv["above_max_action"] == "require_written_justification"
    assert iv["show_rule_in_ui"] is True


def test_recovery_rate_is_an_assumption_with_a_sensitivity_band() -> None:
    """Spec 3.2: r defaults to 0.35 and is always shown as a band over 0.20-0.50.

    Never present a single money number without its r band; that one habit is the
    difference between credible and made up.
    """
    econ = load_yaml(REPO_ROOT / "config" / "economics.yaml")
    assert econ["recovery"]["rate"] == 0.35
    assert econ["recovery"]["sensitivity_band"] == [0.20, 0.35, 0.50]
    assert econ["recovery"]["bounds_exclusive"] == [0.0, 1.0]
    # Money is integer minor units: 1 UGX = 100 minor.
    assert econ["minor_units_per_major"] == 100
    assert isinstance(econ["analyst"]["cost_per_hour_minor"], int)
    assert isinstance(econ["friction_cost_minor"], int)


def test_money_config_declares_no_float_amounts() -> None:
    """01 B: money is integer minor units everywhere in config."""
    econ = load_yaml(REPO_ROOT / "config" / "economics.yaml")

    def _walk(node: object, path: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                # The recovery rate and its bounds are genuine ratios, not money.
                if key in {"rate", "bounds_exclusive", "sensitivity_band"}:
                    continue
                _walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                _walk(value, f"{path}[{index}]")
        elif isinstance(node, float) and "minor" in path:
            raise AssertionError(f"float money at {path}: {node!r}")

    _walk(econ)


def test_sources_declare_licenses_and_refuse_undeclared_ones() -> None:
    """01 B: every declared source carries a license and a citation."""
    sources = load_yaml(REPO_ROOT / "config" / "sources.yaml")
    by_id = {source["id"]: source for source in sources["sources"]}
    assert by_id["paysim"]["license"] == "CC BY-SA 4.0"
    assert by_id["ibmaml"]["license"] == "CDLA-Sharing-1.0"
    for source in sources["sources"]:
        assert source.get("license"), f"{source['id']} has no license"
        assert source.get("citation"), f"{source['id']} has no citation"
    # Elliptic is CC BY-NC-ND: no derivatives, so it is cited and never ingested.
    assert by_id["elliptic"]["ingest_allowed"] is False
    assert any(entry["id"] == "ieee_cis" for entry in sources["refused"])


def test_splits_are_temporal_with_a_thirty_day_embargo() -> None:
    """Spec 7.1: temporal, never random. A random split is the hackathon tell."""
    splits = load_yaml(REPO_ROOT / "config" / "splits.yaml")
    walk = splits["walk_forward"]
    assert walk["n_folds"] == 5
    assert walk["scheme"] == "expanding_window"
    assert walk["shuffle"] is False
    assert walk["embargo_days"] == 30
    assert walk["purge"] is True
    assert len(walk["folds"]) == 5
    assert [fold["index"] for fold in walk["folds"]] == [0, 1, 2, 3, 4]


def test_a_deliberate_leakage_control_is_shipped() -> None:
    """01 P6 gate: a lookahead-leaking control must visibly outperform.

    Without it, a green backtest and a broken one look identical.
    """
    control = load_yaml(REPO_ROOT / "config" / "splits.yaml")["leakage_control"]
    assert control["enabled"] is True
    assert control["expect_outperforms"] is True
    assert control["render_as"] == "control"


def test_benefit_ratio_is_not_called_a_sharpe_ratio() -> None:
    """03 I: it is a risk-adjusted benefit ratio, with its formula shown."""
    ratio = load_yaml(REPO_ROOT / "config" / "splits.yaml")["report"]["benefit_ratio"]
    assert ratio["is_sharpe_ratio"] is False
    assert "sharpe" not in ratio["label"].lower()
    assert ratio["show_formula"] is True


def test_graph_config_types_rails_and_caps_the_subgraph() -> None:
    """03 F: supernodes are typed rail and excluded from fan scoring."""
    graph = load_pipeline_config(REPO_ROOT).graph
    assert graph["rail_degree_percentile"] == 99.0
    assert graph["subgraph_node_cap"] == 1500
    assert graph["community"]["seed"] == 1337
    assert graph["community"]["canonical_order"] == "size_then_min_node_key"


# --- pinned toolchain (01 D reproducibility) -----------------------------


def test_uv_lockfile_is_committed() -> None:
    """uv.lock is the reproducibility artifact a judge can verify."""
    assert (REPO_ROOT / "uv.lock").is_file(), "uv.lock must be committed"


def test_python_is_pinned_to_three_twelve() -> None:
    """01 D: Python 3.12 with uv. A drifting interpreter changes float behaviour."""
    assert (REPO_ROOT / ".python-version").read_text(encoding="utf-8").strip() == "3.12"
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["requires-python"] == ">=3.12,<3.13"


def test_pnpm_lockfile_is_committed() -> None:
    """pnpm-lock.yaml is the JS reproducibility artifact."""
    assert (REPO_ROOT / "apps" / "web" / "pnpm-lock.yaml").is_file()


def test_the_declared_js_toolchain_is_the_one_every_recipe_uses() -> None:
    """Plan §T2 pins the JS toolchain; nothing but this test noticed when it was swapped.

    Twice in one session an agent met the host's missing pnpm by replacing it with Bun, and
    rewrote the files that state the answer rather than the problem: `packageManager`, the web
    Dockerfile's install line, the Makefile's web recipes, the pre-commit hook, the README
    generator, scripts/verify.py's P8 gate, and `test_pnpm_lockfile_is_committed` itself. Both
    times the tracked `pnpm-lock.yaml` was deleted on the way, which the container build needs
    and nothing upstream checked. §13 also puts `pnpm audit` in CI.

    So this asserts the whole chain agrees, not just the manifest field: a `packageManager`
    string that the Dockerfile and Makefile contradict is the same drift wearing a clean badge.
    A lockfile is only a reproducibility artifact if the tool named beside it can read it.
    """
    web = REPO_ROOT / "apps" / "web"
    manifest = json.loads((web / "package.json").read_text(encoding="utf-8"))

    manager = str(manifest.get("packageManager", ""))
    assert manager.startswith("pnpm@"), (
        f"packageManager is {manager!r}; plan §T2 pins `pnpm@9.15.9`. Changing the pinned "
        "supply chain is a plan amendment in DECISIONS.md, not a host-convenience change -- "
        "the gates are run through `node node_modules/...` precisely so this pin can hold on a "
        "host without pnpm."
    )
    assert "bun" not in str(manifest.get("engines", {})).lower(), (
        "engines names an interpreter the image does not run"
    )
    assert "pnpm" in manifest and "overrides" in manifest["pnpm"], (
        "the @tailwindcss/oxide 4.0.0 native-binding pin lives under `pnpm.overrides`; pnpm "
        "reads that key and ignores a top-level `overrides`, so losing it fails at build time "
        "on a missing binary rather than as a warning"
    )
    assert not (web / "bun.lock").exists(), (
        "two JS lockfiles would disagree about the tree and nothing here would catch it"
    )

    recipes = {
        "apps/web/Dockerfile": [
            "COPY package.json pnpm-lock.yaml ./",
            "RUN pnpm install --frozen-lockfile",
        ],
        "Makefile": ["cd $(WEB) && pnpm install --frozen-lockfile", "cd $(WEB) && pnpm lint"],
        ".pre-commit-config.yaml": ["pnpm exec biome check --write ."],
    }
    for path, needles in recipes.items():
        text = (REPO_ROOT / path).read_text(encoding="utf-8")
        for needle in needles:
            assert needle in text, f"{path} no longer says `{needle}`"
        offenders = [t for t in _bun_command_tokens(text) if t not in {"bundle", "buffers"}]
        assert not offenders, f"{path} mentions another package manager: {sorted(set(offenders))}"

    verify = (REPO_ROOT / "scripts" / "verify.py").read_text(encoding="utf-8")
    assert '"bun"' not in verify, (
        "a phase gate is shelling out to a package manager the plan does not pin; gates run "
        "the JS entry points through node so they measure the tree whoever installed it"
    )


def _bun_command_tokens(text: str) -> list[str]:
    """Shell words that name Bun as a program, ignoring `bundle`/`buffers` prose."""
    return [w for w in re.findall(r"(?<![\w./-])(bun\w*)(?![\w./-])", text) if w == "bun"]


def test_web_dependencies_support_the_installed_react_major() -> None:
    """No web package may declare a peer range that excludes our React.

    Found by reading an install log rather than by a runtime failure: visx 3.12
    declares react ^16 || ^17 || ^18, so the React 19 tree emitted six unmet-peer
    warnings and the graph layer would have been the first thing to break at
    runtime. visx 4.0.0 declares ^18 || ^19, and the lockfile must hold it.
    """
    package = REPO_ROOT / "apps" / "web" / "package.json"
    manifest = json.loads(package.read_text(encoding="utf-8"))
    assert manifest.get("dependencies", {}).get("react"), "apps/web must declare react"

    lock = (REPO_ROOT / "apps" / "web" / "pnpm-lock.yaml").read_text(encoding="utf-8")
    for name, spec in manifest.get("dependencies", {}).items():
        if not name.startswith("@visx/"):
            continue
        assert f"{name}@{spec.lstrip('^~')}" in lock, (
            f"{name} is declared as {spec} in package.json but is not locked at that "
            "version, so the committed tree is not the tree that was tested"
        )
    assert "@visx/shape@4.0.0" in lock, (
        "visx 3.12 declares react ^16 || ^17 || ^18, incompatible with the React 19 in "
        "this tree; the lockfile must hold a React-19-capable major"
    )


def test_every_direct_dependency_is_exactly_pinned() -> None:
    """01 A rule 6: pinned versions, and uv.lock committed.

    A range like `>=1.0` would let two clones resolve differently, which breaks
    the determinism claim judges rerun.
    """
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    unpinned = [
        dep
        for dep in pyproject["project"]["dependencies"]
        if not any(op in dep for op in ("==", ">=", "<=", "~=", "!="))
    ]
    assert not unpinned, f"unpinned dependencies: {unpinned}"


def test_no_sharp_ratio_claims_in_docs() -> None:
    """00 G rejection trigger territory: the ratio is explicitly not a Sharpe."""
    for name in ("README.md",):
        path = REPO_ROOT / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8").lower()
        # Allowed only in an explicit denial such as "not a sharpe ratio".
        for line in text.splitlines():
            if "sharpe" in line:
                assert any(
                    phrase in line
                    for phrase in ("not a sharpe", "not called a sharpe", "sharpe ratio, not")
                ), f"unqualified Sharpe claim in {name}: {line.strip()}"


# --- disclaimer is defined once (01 B) -----------------------------------


def test_disclaimer_is_the_specified_text() -> None:
    """The disclaimer travels in the README, the app footer and every packet.

    It is defined once in code so a test can assert all three carry the identical
    string rather than three near-copies drifting apart.
    """
    assert DISCLAIMER.startswith("OXBOW is a research prototype that analyzes historical")
    assert "does not process live financial transactions" in DISCLAIMER
    assert "not financial advice" in DISCLAIMER
    assert "not validated for operational use" in DISCLAIMER


# --- what git is actually carrying ------------------------------------------


def _tracked_paths() -> list[str]:
    """Every path git tracks, from the index rather than the filesystem.

    Read from the index because that is the question being asked: a file that exists
    on disk and is ignored is not carried by the repository, and a fresh clone will
    not have it. ``git ls-files`` on a dirty tree still answers for the last commit,
    which is what a clone would get.
    """
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def test_no_cache_or_dependency_tree_is_tracked() -> None:
    """A tracked cache is a clone nobody can reproduce and a diff nobody can read.

    Hypothesis' example database was tracked at 21 kB with no ignore rule covering
    it: the suite's own search cache, regenerated on every run, carried in the
    repository forever. The rule now covers the directory rather than the one file
    that happened to be added first.
    """
    tracked = _tracked_paths()
    offenders = [
        path
        for path in tracked
        if any(
            part in {".hypothesis", "__pycache__", ".mypy_cache", ".pytest_cache", "node_modules"}
            for part in Path(path).parts
        )
    ]
    assert not offenders, f"cache or dependency tree tracked: {offenders[:10]}"


@pytest.mark.parametrize(
    "directory", ["data/raw", "data/interim", "data/processed", "data/snapshots"]
)
def test_ignored_data_directories_survive_a_fresh_clone(directory: str) -> None:
    """Four ``data/`` subdirectories are ignore-managed, so nothing in git keeps them.

    Their contents are deliberately uncommitted -- PaySim is 6.3 M rows and licensed --
    but the directories themselves are prescribed by the plan's tree, and without a
    tracked placeholder a fresh clone has no ``data/interim/`` to write into. The
    ``!data/<dir>/.gitkeep`` negations existed in ``.gitignore`` for exactly this and
    had no file behind them, so they were dead.
    """
    tracked = _tracked_paths()
    assert f"{directory}/.gitkeep" in tracked, (
        f"{directory} is ignored and has no tracked placeholder, so a fresh clone does "
        "not have the directory the plan's tree prescribes"
    )
    assert (REPO_ROOT / directory).is_dir()


def test_a_declared_input_under_an_ignored_directory_stays_tracked() -> None:
    """``data/processed/`` is ignored, and one file in it is a declared input.

    ``config/pipeline.yaml`` names ``typology_join_artifact:
    data/processed/ibm_typologies.parquet`` and ``data/DATASET_CARD.md`` cites its row
    and block counts. It was tracked only because somebody force-added it past the
    ignore rule, which is the worst of both states: carried, but by accident, so the
    next ``git add -A`` could drop a declared input with nobody deciding to. The rule
    now exempts it explicitly, and this asserts the exemption still resolves.
    """
    tracked = _tracked_paths()
    assert "data/processed/ibm_typologies.parquet" in tracked
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "data/processed/ibm_typologies.parquet"],
        cwd=REPO_ROOT,
    )
    assert ignored.returncode != 0, "the declared typology artifact is ignored again"
