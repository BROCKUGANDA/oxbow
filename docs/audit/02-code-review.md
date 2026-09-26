# OXBOW — Code-Level Defect, Security and Hardening Audit

**Date:** 2026-09-26
**Tree:** `C:\Users\HP\Desktop\OXBOW`
**Authority order applied:** 00 Constitution > 01 Build Prompt > 03 Edge Cases > 02 Integration > Spec v2.1
**Authority doc read:** `C:\Users\HP\Downloads\2026-09-26_021500-oxbow-master-build-plan.md` (580 lines; §1, §3, §5–§21 read in full)
**Mutations made by this audit:** none. One file created: this report.

---

## 0. Two premises in the brief that the measured tree contradicts

These are stated first because every count below depends on them.

### 0.1 The tree is not at `2e8b4b2`; it is two commits ahead

```
$ git -C C:\Users\HP\Desktop\OXBOW log --oneline -5
1dc2499 docs(state): record the measured position and the next five moves in order
0e8a5b8 style: close the format leg of make lint and 36 rule errors, verified by a full run
2e8b4b2 feat(p4,p6,p9): the model, scoring, backtest and generated-document layers as one tree
92e43f8 feat(p8): the web app's design primitives, screens and the fifth empty state
91d7a0e feat(p7): the API surface, its integration suite, and a synthetic watchlist fixture
```

`0e8a5b8` is a style commit whose own message says it closed **36** rule errors. The working tree is clean and nothing is uncommitted. Every lint number in §1 is measured against `1dc2499`, not `2e8b4b2`.

### 0.2 `ruff check .` reports 45 errors, not 85

```
$ uv run ruff check . --output-format=concise
... (45 lines) ...
Found 45 errors.
```

The 85 figure belongs to the pre-`0e8a5b8` tree. Lint debt is real but roughly half what the brief states. **The severity of the remaining 45 is not lower than the 85, though** — the residue is concentrated in the diagnostic rules (`F822`, `F841`, `B`, `ARG`) precisely because the style rules were the ones that got fixed. See §1.2.

---

## A. Lint debt, itemised

### A.1 Full statistics (`uv run ruff check . --statistics`)

```
15  ARG001  unused-function-argument
 7  RUF002  ambiguous-unicode-character-docstring
 6  UP038   non-pep604-isinstance
 2  RET504  unnecessary-assign
 2  E402    module-import-not-at-top-of-file
 2  F401    unused-import
 1  B017    assert-raises-exception
 1  B904    raise-without-from-inside-except
 1  C401    unnecessary-generator-set
 1  SIM102  collapsible-if
 1  SIM103  needless-bool
 1  N811    constant-imported-as-non-constant
 1  F822    undefined-export
 1  F841    unused-variable
 1  RUF001  ambiguous-unicode-character-string
 1  RUF005  collection-literal-concatenation
 1  RUF007  zip-instead-of-pairwise
```

### A.2 The diagnostic classes, separated from the style classes

The brief asks specifically about `F821`, `F841`, `B`/`S`, `ARG001`. Findings:

#### `F821` (undefined name) — **ZERO. The premise does not hold.**

No `F821` exists anywhere in the tree. `ruff --statistics` lists no `F821` and the concise output contains none. There are therefore **no latent `NameError`s of the "used but never imported" kind** to itemise. This is a genuine clean result, not a gap in my search — I ran both `--statistics` and `--output-format=concise` and grepped the concise output for `F82`.

What *does* exist is the adjacent rule:

| # | Location | Rule | What it is | Runtime effect |
| --- | --- | --- | --- | --- |
| A-1 | `packages/pipeline/oxbow/features/build.py:993` | `F822` | `"build_feature_table"` is listed in `__all__` but **is not defined in that module**. It is defined in `packages/pipeline/oxbow/features/compute.py:108`. | `from oxbow.features.build import *` raises `AttributeError: module 'oxbow.features.build' has no attribute 'build_feature_table'`. Nothing in the tree uses `import *`, so this is latent, not live. But `__all__` is a public contract: any doc generator, `dir()`-driven tool, or future `import *` breaks. **MEDIUM.** |

Fix (one line, committable):

```python
# packages/pipeline/oxbow/features/build.py:993 — delete the line
```
…then re-export deliberately from `compute.py` if the name is meant to be public.

#### `F841` (unused variable) — 1 occurrence, and it is a real refactor scar

| # | Location | Evidence | Assessment |
| --- | --- | --- | --- |
| A-2 | `packages/pipeline/oxbow/packet/render.py:199` | `economics = case.economics` | The function `assumption_keys()` (line 191) builds the assumption-key list from `case.bundle.economics.monte_carlo` (line 214) and `case.assumption_block.source_path` (line 221) and **never reads `economics`**. This is a dead binding left from a refactor, not a style nit. It is **not** a wrong-number defect: the returned string is correct. **LOW**, but it is the exact shape the brief predicted ("a forgotten assignment mid-refactor") and it is worth deleting because it misleads a reader into thinking the economics block is read here. |

Full `render.py:191-221` verified; `economics` appears once in the function body.

#### `B` (flake8-bugbear) — 2 occurrences, both diagnostic

| # | Location | Rule | Assessment |
| --- | --- | --- | --- |
| A-3 | `apps/api/security.py:252` | `B904` | `_peek_issuer()` does `except (TokenError, json.JSONDecodeError): raise TokenError(f"token payload is unreadable")` with no `from exc`. The `JSONDecodeError` is therefore implicitly chained (`__context__`) and will be rendered by `format_exc_info`. Combined with finding **C-4** (redaction runs *before* `format_exc_info`), an attacker-supplied token body can put arbitrary bytes into a log line unredacted. **MEDIUM**, and it is an amplifier for C-4, not a duplicate of it. |
| A-4 | `tests/contracts/test_p1b_batch_boundary.py:222` | `B017` | `pytest.raises(Exception)`. This assertion cannot distinguish "the contract correctly refused the batch" from "the code raised `KeyError` because a dict key is missing". A batch-boundary gate that passes on an unrelated crash is 00 §B's decoration failure. **MEDIUM** (test-quality, not product). |

#### `S` (flake8-bandit) — **ZERO. No `S` rules fire at all.**

Not configured in `pyproject.toml`'s ruff `select`, and nothing in the tree trips it. Recorded as a **coverage** gap rather than a clean result: `S105`/`S106` (hardcoded password) and `S608` (SQL injection) are exactly the checks this audit had to perform by hand (§C.4, §C.6). Recommend adding `S` to the ruff `select` list — it is a pinned-dependency-free addition.

#### `ARG001` (unused function argument) — 15 occurrences, in exactly two groups

**Group 1 — 14 occurrences, all one uniform dispatch signature. NOT a defect.**

`packages/pipeline/oxbow/features/kinds.py` lines 663, 697, 736, 767, 781, 805, 811, 820, 829, 852, 954, 995, 1045, 1050 — every one is the `context: KernelContext` parameter of a `_kind_*(work: pl.DataFrame, entry: FeatureSpec, context: KernelContext) -> pl.Series` kernel.

Verified by reading `kinds.py:662-693`: the kernels are dispatched through a common signature, so interface conformance requires the parameter whether or not a given kernel consumes it. **This is not "a swallowed input."** `LOW` / no action beyond a `# noqa: ARG001` with the reason, so the 14 stops burying the real ones.

The one thing worth checking here — whether a kernel that *should* read label/salt material out of `context` silently ignores it — is covered by the leakage suite, which `STATE.md` records as "proven to bite rather than to pass vacuously" (`tests/test_leakage.py:460` `test_a_label_derived_feature_is_refused_at_load`, `tests/unit/test_p2_features.py:628`). I did not re-run it (constraint) and mark the deeper question **UNVERIFIED**.

**Group 2 — 1 occurrence, and it is a real swallowed input.**

| # | Location | Evidence | Assessment |
| --- | --- | --- | --- |
| A-5 | `packages/pipeline/oxbow/packet/subgraph_image.py:225` | `render_subgraph_svg(view, *, per_major: int, band_letter: str, title: str)` | `per_major` is accepted and never read. Verified by grepping the whole file: money is rendered by a *separate* function, `value_basis_lines(view, *, per_major)` at line 196, which the caller must invoke. The SVG body (lines 233-272) draws only stroke widths from `_edge_width(edge.total_value_minor)` (line 113), never an amount. So the parameter is misplaced, not lost — but a reader of `render_subgraph_svg`'s signature reasonably believes amounts are rendered inside it. **LOW** (misleading signature; no wrong number). |

#### The remaining 26 — genuinely stylistic, listed for completeness, no defect claimed

`RUF002` ×7 (`packages/pipeline/oxbow/eval.py:2307`; `features/registry.py:386`; `tests/unit/p9_fixtures.py:173,175,176`) and `RUF001` ×1 (`eval.py:2307`) are en-dash/minus-sign/multiplication-sign in docstrings and strings. `UP038` ×6, `RET504` ×2 (`eval.py:1830`, `features/kinds.py:896`), `E402` ×2 (`scripts/verify_audit.py:35,36`), `F401` ×2 (`packages/pipeline/oxbow/packet/__init__.py:76,77` — `compose_packet` and `format_instant` imported but not in `__all__`; these are deliberate re-exports missing their `__all__` entry, same shape as A-1), `C401` ×1 (`packet/loaders.py:357`), `SIM102` ×1 (`features/registry.py:662`), `SIM103` ×1 (`apps/api/routers/graph.py:241` — I read `_in_window` at lines 236-243; the two guard clauses plus `return True` are correct and clearer than the collapsed form ruff suggests; **no action**), `N811` ×1 (`apps/api/routers/auth.py:171` `LOCAL_ISSUER` imported as `_local`, an intentional alias), `RUF005` ×1 (`tests/integration/test_p7_api.py:1468`), `RUF007` ×1 (`tests/unit/test_p3b_golden_matrix.py:131`).

---

## B. The §19 anti-rubbish checklist, each item run as a real grep

### B.1 §19.1 — `TODO|FIXME|XXX|HACK|placeholder|lorem` → **24 hits. The plan demands zero.**

```
$ git grep -n -E "TODO|FIXME|XXX|HACK|placeholder|lorem" -- '*.py' '*.ts' '*.tsx'
```

Every hit, classified. "Legitimate prose" means the word appears in a comment/docstring explaining a policy; "real" means it is in shipped behaviour, a user-visible string, or a data default.

| # | file:line | Text | Verdict |
| --- | --- | --- | --- |
| B-1.1 | `packages/pipeline/oxbow/adapters/goaml/xml.py:77` | `currency = str(txn.get("currency", "XXX"))` | **REAL DEFECT.** `XXX` is the ISO 4217 *no-currency* code, used here as a **silent default** in a regulatory-filing writer. A transaction whose currency is absent becomes a filing line marked "no currency" rather than a refusal. 01 §B (plan line 235) requires "currency is part of every amount; aggregations group by currency or fail, no implicit FX," and 03 §A rule 2 is "never let an unknown become a zero" — the same rule for an unknown currency. **MEDIUM.** |
| B-1.2 | `packages/pipeline/oxbow/features/registry.py:211` | `"placeholder",` | Legitimate **data**: a blocklist of forbidden feature-spec words. Correct to keep. |
| B-1.3 | `packages/pipeline/oxbow/features/registry.py:212` | `"lorem ipsum",` | Legitimate **data**, same blocklist. |
| B-1.4 | `apps/web/src/app/dev/states/page.tsx:153` | `<Shimmer width="100%" height={14} label="Sweeping placeholder" />` | **REAL, low.** A user-visible `aria`/label string in the `/dev/states` gallery contains the banned word. It is describing the shimmer sweep and is not a stub, but §19.1 says zero hits in merged code. **LOW.** |
| B-1.5 | `apps/web/src/design/primitives/Shimmer.tsx:27` | `/** Set false to render a plain placeholder block with no sweep at all. */` | Legitimate prose (a documented prop). |
| B-1.6 | `apps/web/src/design/primitives/Skeleton.tsx:89` | `indistinguishable placeholders with no identity of their own: they hold` | Legitimate prose explaining the skeleton design rationale. |
| B-1.7 | `apps/web/src/lib/api/hooks.ts:69` | `placeholderData: options.keepPreviousData === false ? undefined : keepPreviousData,` | **False positive** — TanStack Query's `placeholderData` option. Not the word. |
| B-1.8 | `apps/api/jobs.py:110` | `# queue's own id, and minting a placeholder would let two concurrent` | Legitimate prose explaining why an id is *not* minted. |
| B-1.9 | `packages/pipeline/oxbow/contracts/canonical_v1.py:164` | `# 03 B: the account key is 12 hex characters, displayed uppercased as ACC-XXXXXX.` | Legitimate prose — the `X`es are a display format, matched by the bare `XXX` alternative in the grep. |
| B-1.10 | `scripts/download_data.py:48` | `# A placeholder is what P0 committed, because a hash cannot be known before the` | Legitimate prose. |
| B-1.11 | `scripts/download_data.py:290` | `# The placeholder means "not yet pinned", not "mismatch". The` | Legitimate prose. |
| B-1.12 | `tests/unit/test_anti_rubbish.py:3` | `The standing requirement is: no stubs, no placeholders, no TODO, no lorem, no` | Legitimate — the guard test's own docstring. |
| B-1.13 | `tests/unit/test_anti_rubbish.py:95` | `# A URL fragment or an anchor named `#placeholder` is markup, not an` | Legitimate prose. |
| B-1.14 | `tests/unit/test_anti_rubbish.py:112` | `# A completed phase replaces its verb's placeholder body; ...` | Legitimate prose. |
| B-1.15 | `tests/unit/test_p0_design_system.py:233` | `def test_no_todo_fixme_or_placeholder_in_merged_code() -> None:` | The guard test's own name. |
| B-1.16 | `tests/unit/test_p0_design_system.py:234` | `"""00 B: no TODO, FIXME, lorem or placeholder survives in merged code.` | The guard's docstring. |
| B-1.17 | `tests/unit/test_p0_design_system.py:237` | `comment is not a false positive on the word placeholder.` | Legitimate prose. |
| B-1.18 | `tests/unit/test_p0_design_system.py:284` | `def test_typology_glyph_is_not_an_empty_placeholder(name: str) -> None:` | The guard's own name. |
| B-1.19 | `tests/unit/test_p0_toolchain.py:61` | `wiring: no verb may print a placeholder for a stage whose package has landed, and a` | Legitimate prose. |
| B-1.20 | `tests/unit/test_p1a_sources.py:136` | `"placeholder: the pin is only real once the hash is recorded"` | Legitimate **data** — a sentinel hash string. |
| B-1.21 | `tests/unit/test_p1a_sources.py:241` | `def test_verify_only_treats_the_placeholder_as_unpinned_not_mismatched() -> None:` | Test name. |
| B-1.22 | `tests/unit/test_p1a_sources.py:244` | `The download path skips the check when the pin is a placeholder, so` | Legitimate prose. |
| B-1.23 | `tests/unit/test_p1a_sources.py:254` | `"verify-only must skip the comparison while the pin is a placeholder; "` | Legitimate prose (assertion message). |

**Tally: 1 real defect (B-1.1), 1 real-but-cosmetic (B-1.4), 1 false positive (B-1.7), 21 legitimate prose/data/test-name.**

**The gate that should have caught this does not, and cannot.** This is the more serious half of B.1:

`tests/unit/test_p0_design_system.py:233-245` is the §19.1 enforcement point, and it fails on two counts.

```python
def test_no_todo_fixme_or_placeholder_in_merged_code() -> None:
    """00 B: no TODO, FIXME, lorem or placeholder survives in merged code.
    ...
    """
    pattern = re.compile(r"\b(TODO|FIXME|XXX|HACK|lorem ipsum)\b", re.IGNORECASE)
    offenders = [
        str(path.relative_to(DESIGN))
        for path in TEXT_FILES
        if pattern.search(path.read_text(encoding="utf-8", errors="ignore"))
    ]
```

1. **Scope.** `TEXT_FILES` is derived from `DESIGN` (line 29: `DESIGN = WEB_ROOT / "src" / "design"`). The test scans `apps/web/src/design/` and nothing else — not `packages/`, not `apps/api/`, not `apps/web/src/app/`, not `scripts/`, not `config/`. Of the 24 hits above, **exactly zero are inside its scope.** The guard is green and the checklist it claims to enforce has 24 violations in the tree.
2. **Pattern.** The regex drops `placeholder` and bare `lorem` entirely (it keeps only `lorem ipsum`). The plan's pattern (§19.1, plan line 518) is `TODO|FIXME|XXX|HACK|placeholder|lorem`. So even inside the design tree, `B-1.5` and `B-1.6` would pass.

`tests/unit/test_anti_rubbish.py:8-11` states the division of labour explicitly ("§19.1's unfinished-work markers are **already** enforced by `tests/unit/test_p0_design_system.py`") and then adds only the `any` ban (line 103) and the CLI-wiring check (line 115). So §19.1 has **no repo-wide enforcement at all**, and the file that claims it does is scoped to 3% of the tree with a 60%-narrower pattern.

**Assessment: HIGH.** Not because of the 24 hits (21 of which are legitimate prose), but because the control the plan names as a merge gate does not exist, and a docstring asserts that it does.

**Fix.** Extend `test_anti_rubbish.py` (which already has the correct repo-walking machinery in `iter_source`/`offenders`, lines 69-100) with a §19.1 case that scans `("packages", "apps", "scripts", "config")` with the plan's full pattern and an explicit, *enumerated* allowlist — the 21 legitimate hits above, each with a one-line reason. An enumerated allowlist is the only form that stays honest: a blanket "ignore comments" is what let B-1.4 (a user-visible string) through the prose defence.

### B.2 §19.2 — `: any|<any>|as any` in `apps/web/src` → **ZERO. CLEAN.**

```
$ git grep -n -E ": any|<any>|as any" -- 'apps/web/src'
(no output)
```

The real guard is `tests/unit/test_anti_rubbish.py:41`, whose pattern `r":\s*any\b|\bas\s+any\b|<\s*any\s*[>,]"` is **broader** than §19.2 (it also catches `any` as a generic argument), and `test_no_any_in_frontend_source` (line 103) scans `apps/web/src` with the correct scope. **This is the one §19 item that is genuinely enforced.** No finding.

### B.3 §19.3 / 01 §B — float money

#### Is `scripts/no_float_money.py` wired into `make lint`? **NO.**

`Makefile:133-148`:

```make
lint: lint-python lint-web contracts ## ruff, mypy, biome, import-linter

lint-python:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy packages/pipeline apps/api scripts
```

The rule is wired into **pre-commit** only:

```
.pre-commit-config.yaml:54:      - id: no-float-money
.pre-commit-config.yaml:55:        name: no float money in the pipeline package
.pre-commit-config.yaml:56:        entry: uv run python scripts/no_float_money.py
```

Plan T5 (plan line 151) requires "a **float-money lint rule** banning float money types inside the pipeline package (01 §B, **enforced not assumed**)". Pre-commit satisfies "enforced" in the happy path, but: (a) it is bypassed with `--no-verify` and on any commit that does not touch a pipeline file (the hook's `files:` filter — I did not read the filter, **UNVERIFIED**); (b) `make lint`, `make verify` (Makefile:176) and `make test` (Makefile:154) all pass without ever running it; (c) `.github/workflows/` is **empty** (§C.7), so there is no CI backstop either. Net: the plan's headline money constraint is enforced in exactly one place, and that place is a local hook. **MEDIUM.**

**Fix (config, one line):** add `@$(PY) scripts/no_float_money.py` as a second line of the `lint-python` recipe.

#### Gaps in the rule itself

`scripts/no_float_money.py` is 165 lines, an AST walk. Read in full. Four real gaps:

1. **Annotation-only.** The scan (lines 106-137) inspects exactly three node types: `ast.AnnAssign` with a float annotation on a money-named target, function parameters with a float annotation on a money-named arg, and a function whose *own name* is money-named returning float. It therefore catches **zero** of the actual float-money failure modes: `float(amount_minor)`, `round(x * 0.35)`, `amount_minor / 100.0`, `value = a * 1.1`. The docstring (lines 8-11) frames this as catching "a float money column," which is the annotation case, so the rule is honest about its scope — but a reader of `DECISIONS.md:53` ("Enforced by `scripts/no_float_money.py`, an AST walk, not a code review note") would reasonably assume more. **MEDIUM.**
2. **Pipeline package only.** `PACKAGE_ROOT = Path("packages/pipeline/oxbow")` (line 30). `apps/api` and `apps/web` are outside. Money crosses into both: `apps/api/routers/dashboard.py:230` rounds a delta, and the web app has its own money formatter. **MEDIUM.**
3. **`MONEY_NAME_FRAGMENTS` is a substring match on the *name* (line 47-53).** A money value in a variable named `total`, `x`, `delta` or `figure` is invisible. The rule cannot see `total = exposure * recovery_rate`. **MEDIUM** (inherent to a naming rule; worth stating as a known limit in `DECISIONS.md` rather than pretending otherwise).
4. **Exemptions are name-keyed, not reasoned.** `FLOAT_ALLOWED` (lines 34-48) maps 11 identifiers to justifications. None of the 11 collide with `MONEY_NAME_FRAGMENTS` today, so the exemption list is currently inert — but it is keyed by exact name, so a new float money variable called `value_share_minor` would be flagged while `value_share` would not. Low risk; noted for completeness.

**What is actually right, and should be said:** the *runtime* counterpart `assert_no_float_money(row)` at `packages/pipeline/oxbow/ports/source.py:164` is called on the ingest path from all three adapters (`adapters/null/source.py:63`, `adapters/file/source.py:158`, `adapters/s3/source.py:115`) and from `ingest/ibm_aml.py:1258`. So float money is caught at the boundary by a check that is in the data path, not in a hook. That is the strong control; the lint rule is the weak one.

#### `/ 100` and `* 100` on money outside a labelled render path

Every hit classified. Full grep output is 130 lines; the money-relevant ones:

| Location | Verdict |
| --- | --- |
| `packages/pipeline/oxbow/ingest/canonical.py:267` — `return (whole.cast(pl.Int64) * 100 + cents).cast(pl.Int64)` | **Correct and the right way.** Integer string→minor-unit parse. The docstring at line 248 and `ingest/ibm_aml.py:44` both explain that `float("9839.64") * 100` is `983963.9999999999` and that this avoids it. This is the plan's rule honoured. |
| `scripts/measure_ibm_cycles.py:158` — `return int((Decimal(text.strip()) * 100).to_integral_value(rounding=ROUND_HALF_EVEN))` | **Correct** — `Decimal`, integer result, explicit rounding mode. |
| `apps/web/src/lib/format/money.ts:20` — "The integer long-division below is deliberate. `minor / 100` in floating point is" | **Correct** — the render path, and it uses integer long-division rather than float division. `MoneyFigure.tsx:13` states "It divides by 100 here and only here." Exactly the labelled render path §19.3/01 §B requires. |
| `apps/web/src/fixtures/meta.fixture.ts:17,21` — `minor_units_per_major: 100,` | Test fixture. Allowed by §19.3. |
| **`apps/web/src/app/(dash)/network/page.tsx:311-312`** | **VIOLATION.** See B.4. |
| `packages/pipeline/oxbow/ingest/run.py:203`, `stage_events.py:272,276,319`, `quant/allocate.py:852`, `models/tuning.py:53,77,81` | `round(elapsed, 3)` / elapsed-ms conversions. Not money. |
| The remaining ~110 `round(` hits | All are metric serialisation (`round(brier, 8)`, `round(psi, 6)`, `round(woe, 8)`), latency, or ratio. **None is money.** Verified by inspection of the `round(` grep: the money-adjacent field names (`amount`, `exposure_minor`, `benefit`, `loss`, `ev`) are rendered through `to_major_text` / `MoneyFigure`, not through `round`. |

**Verdict: the money path is clean. The one violation is client-side, in the explorer, and it is a §19.3 hardcoded-number violation as much as a float-money one.**

#### `round(` on money

`apps/api/routers/dashboard.py:230` — `delta=None if value is None or base is None else round(value - base, 6)`. A delta on a dashboard KPI. `value`/`base` here are rates/ratios from the validation payload, not minor units. **No finding.**

#### `numeric` / `decimal` money columns

```
$ git grep -n -E 'numeric|decimal' -- 'apps/api' 'packages/pipeline/oxbow/contracts' 'config'
```
No money column is typed `numeric` or `decimal` anywhere. C5 (plan line 71) resolved in favour of `amount_minor bigint` and required "no `numeric` for money". `DECISIONS.md:48-53` records it and the enforcement. **CLEAN.**

### B.4 §19.3 — hardcoded numbers in `.tsx` that are not scale tokens, test fixtures, or documented constants

Plan §19.3: "No number literal in a `.tsx` that is not a scale token, a test fixture, or a documented constant from config. Every visible figure traces to a config file, a database row, or a computed artifact — **and you can name the file it came from.**"

**The prior audit's five findings, verified and extended.**

| # | Location | Literal | Is it a tunable that belongs in config / `tokens.css`? |
| --- | --- | --- | --- |
| B-4.1 | `apps/web/src/app/(dash)/scorecard/page.tsx:37` | `const IV_BAR_MAX = 0.65;` | **YES — and it can drift from a value that is already in config.** `config/scorecard.yaml:81-82` holds `iv_bounds: {min: 0.02, max: 0.5}`, read by `scoring/config.py:434-435` and enforced in `scoring/selection.py:149,156,163,167`. The bar's full-scale is hardcoded at `0.65` and can never be re-derived. Concretely: if an operator raises `iv_bounds.max` to `0.8` (a legitimate config-only change, since the ceiling exists to force a *written justification*, not to cap the bar), features in `[0.65, 0.80]` render as a **full-width bar identical to a feature at 0.65** — `Math.min(100, ...)` clips silently. The plan's §10 also makes the IV ceiling *visible on the page* because "self-imposed constraints read as rigor"; a bar scaled to a number the page never shows is the opposite. **MEDIUM.** Fix: read `iv_bounds.max` from the scorecard API payload (which already carries it — `scoring/model.py:190-191` emits `iv_min`/`iv_max`) and scale the bar to that, with `IV_BAR_MAX` deleted. |
| B-4.2 | `apps/web/src/app/(dash)/scorecard/page.tsx:377` | `` `oklch(0.78 ${(0.04 + share * 0.14).toFixed(3)} ${from === to ? 195 : 40})` `` | **YES, and this one is worse than a drift risk — it is a theme bug.** The rating-migration heatmap hardcodes `L = 0.78`, which is the **dark-theme** band lightness (`tokens.css:55-59`). The cell text colour is `var(--color-ink-inverse)`, which `tokens.css:181` re-points to `oklch(0.98 0.004 90)` (near-white) under `[data-theme='paper']` (line 165). So in paper mode the heatmap renders **near-white text on an L=0.78 background** — a contrast ratio of roughly 1.3:1, far below the 4.5:1 floor, and below the "zero critical violations" bar in the P8 gate (plan line 397). It is also the only colour ramp in `apps/web/src/app/` not expressed as tokens, which is why it missed the token-sync hook (`.pre-commit-config.yaml:95-100` only guards `tokens.css`→`tokens.ts` and the sprite). **MEDIUM** — I could not verify whether paper mode is reachable from a control in the built app (**UNVERIFIED**), which is why this is not rated HIGH; the defect exists the moment it is. Fix: derive both fills from tokens (`var(--color-band-c)` / a `--color-heat-{lo,hi}` pair added to `tokens.css` for both themes) and take the text colour from the same token as the fill. |
| B-4.3 | `apps/web/src/app/(dash)/alerts/page.tsx:57` | `const PAGE_SIZE = 100;` | **YES.** Used at line 136 as `limit: PAGE_SIZE` — a **server request parameter**, not a display value. It has no comment (the comment at line 55, "THE row height", covers `ROW_HEIGHT` on line 56, not this). If the API enforces a `limit` ceiling the client silently gets a clamped page and the virtualiser's `count` disagrees with the scrollbar. Nothing in `apps/web/src` or `apps/api` ties the two together. **MEDIUM.** Fix: return the server's effective page size in the list response envelope and use it, or move `PAGE_SIZE` to `config/pipeline.yaml` and expose it on `/api/meta/runtime` (the hook `useRuntime` is already imported at line 50). |
| B-4.4 | `apps/web/src/app/(dash)/network/canvas.tsx:312` | `numIter: built.length > 800 ? 1_500 : 3_000,` | **YES, and it is a magic threshold rather than a tunable.** The `800` node count at which the solver halves its iteration budget is nowhere in config and carries no name. It is surrounded by six more unnamed fcose constants on lines 307-318 (`nodeRepulsion: 6_500`, `idealEdgeLength: 40`, `edgeElasticity: 0.2`, `gravity: 90`, `gravityRange: 3`, `nodeSeparation: 60`, `padding: 24`). The P8 gate requires "the network explorer holds **60fps with 1,500 nodes**" (plan line 397) — so `800` and the iteration budget are exactly the numbers that gate is measured against, and they are the numbers nobody can change without reading the JSX. **MEDIUM.** Fix: a `LAYOUT` block in `design/tokens.ts` (which is generated, so a named `layout:` section in `tokens.css` is the source) or a `graph_layout` block in `config/pipeline.yaml`. |
| B-4.5 | `apps/web/src/app/global-error.tsx:24,25,32,42,57,58,59,67` | 8 OKLCH triples | **NO — this one is correct as written, and the prior finding should be withdrawn.** The file's own header comment (lines 6-10) states the reasoning: "it deliberately imports nothing from the design system: if the failure is in the shell, a surface built from the shell is the thing that has just failed. The values are the token literals inlined as text, which is the one place in the app where repeating a colour is the correct engineering choice." I checked each triple against `tokens.css`: `0.17 0.012 250` = `--color-canvas` (line 28), `0.96 0.006 250` = `--color-ink` (line 47), `0.74 0.01 250` = `--color-ink-muted` (line 48), `0.86 0.01 250 / 0.16` = `--color-hairline-strong` (line 43), `0.56 0.012 250` = `--color-ink-faint` (line 49). **All eight are correct and current.** The trade-off is real and correctly reasoned. The one residual risk is drift with no detector — the values are inlined text, so the token-sync hook cannot see them. **LOW, informational:** add a test asserting these eight match `tokens.css` rather than moving them. |

**Extended — two the prior audit did not name:**

| # | Location | Literal | Assessment |
| --- | --- | --- | --- |
| B-4.6 | `apps/web/src/app/(dash)/network/page.tsx:311-312` | `100_000_000` and the float path `(Number(event.target.value) / 100) * 100_000_000` | **The worst of the set, because it is a money scale and it is not a render path.** Line 311 is the slider's *value* and line 312 builds the **`min_minor` API query parameter**. `100_000_000` minor units is 1,000,000.00 major — a money ceiling with no name, no config key, and no comment. The arithmetic is float (`/` then `*` on a `Number`) and only `Math.round`s at the end, which is the one thing 01 §B and plan line 235 forbid outside a render ("round only at render"). The integer long-division discipline that `lib/format/money.ts:20` applies to *display* is absent on the *input* side. It does not currently produce a wrong `min_minor` (the round is consistent and monotonic), so this is not a wrong-number defect — it is §19.3 plus a money-representation inconsistency. **MEDIUM.** Fix: name the ceiling (`const MINOR_CEILING = 100_000_000` sourced from `config/pipeline.yaml graph.max_edge_amount_minor`) and derive the slider position with integer arithmetic as `lib/format/money.ts` already does. |
| B-4.7 | `apps/web/src/app/(dash)/network/page.tsx:47`, `canvas.tsx:52` | `minHeight: 420`; `VISIBLE_OUTLINE = 25` | `minHeight` is geometry, and `CANVAS_HEIGHT` (network/page.tsx:57) is already a single named constant shared by both skeletons — this file handles geometry correctly, which is why `420` standing alone next to it is inconsistent rather than dangerous. `VISIBLE_OUTLINE = 25` is a **named** constant with a five-line rationale comment (canvas.tsx:49-51) explaining the accessibility choice. Both are **acceptable** under §19.3's "documented constant" carve-out. **No finding.** |

**Also verified clean:** every `* 100` in `apps/web/src` that is *not* money is a percentage render — `(rate * 100).toFixed(1)` for observed rate, population share, bad rate, severity, PSI, swept-fraction — all correct, all fed from an API field, none a hardcoded figure. The `100%` occurrences are CSS widths and gradient stops. `provenance.tsx:244` `Math.round(Math.min(Math.max(share,0),1) * 100)` is a clamped percentage. **No fabricated UI number was found**, which is the §18 rejection trigger "a number rendered in the UI that no API response contains" — **not tripped.**

---

## C. Security audit (plan §13 line 391, 02 §F)

### C.1 PII boundary — does any raw account identifier escape ingest?

**YES. One committed artifact contains 20 raw PaySim account identifiers in the clear.**

```
$ git ls-files data/graph_measurement.json
data/graph_measurement.json

$ git grep -coE "\bC[0-9]{6,}\b" -- 'data' 'docs' '*.md' 'config'
data/graph_measurement.json:20
```

Sampled hits, `data/graph_measurement.json:38,42,46,50,54,…`: `"account": "C1677795071"`, `"C1999539787"`, `"C1462946854"`, … 20 of them. This is the **top-20 degrees measurement** that plan §4 step 3 and the day-3 gate (plan line 205, "degree distribution and top-20 degrees printed") require.

Against the requirement — 02 §F (plan line 391): "**PII boundary — raw ids never leave ingest**, everything downstream including logs, traces and packets uses `account_key`" — this is a direct violation. The file is **tracked in git**, so it is in history; deleting the file does not remediate it without a history rewrite.

It is also self-incriminating in a way that makes the fix obvious: `apps/api/observability.py:44` classifies `^C\d{6,}$` as a raw identifier that must be redacted. The repo's own PII control would flag the repo's own committed artifact.

**Rated HIGH, not CRITICAL, and the reasoning is explicit:** the underlying data is public, synthetic, Kaggle-distributed PaySim, and account ids in it are not personal data about a person. The severity comes from (a) breaching an absolute, explicitly-stated plan boundary, and (b) being unremediable without rewriting history. The fix is a rewrite of the top-20 to use `account_key`, or to publish the degrees without the identifier column.

**Other surfaces checked, all clean:**

| Surface | Result |
| --- | --- |
| `out/` tree | `git ls-files out` → **empty**. Nothing tracked. Clean. |
| `obs/` tree | **Does not exist** in the tree. Nothing to check. |
| API responses | `tests/integration/test_p7_api.py:1534` `test_only_pseudonymous_account_keys_reach_a_response` exists and asserts it. Covered. |
| Packets | `packet/` renders `account_key`; `tests/unit/test_p9_packet.py` covers the packet. `graph.py:89-94` writes `IdentifierTypeCode: "AccountKey"` into the GOAML filing — correct, the key not the raw id. |
| Error messages | `apps/api/problems.py` — not read line-by-line in this pass. `redact_value` is shared with the response scrubber by design (observability.py:104-108 docstring), which is the right structure. **Partially UNVERIFIED.** |
| Logs | See C.2 and C.3 — the filter exists and is wired, but is untested and has an ordering hole. |

### C.2 Does the redaction filter fire — and is there a test that asserts it?

**The filter is real and correctly wired. There is no test. The docstring says there is.**

`apps/api/main.py:89` calls `configure_logging(json_output=_json_logs_enabled())`, and `observability.py:185-206` installs the chain:

```python
shared: list[Callable[..., Any]] = [
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    add_run_and_trace,
    redaction_processor,
]
structlog.configure(
    processors=[
        *shared,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,          # <-- runs AFTER redaction
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer(),
    ],
    ...
)
```

`redact_value` (line 106) is a genuinely careful piece of work: it redacts on field-name match, then on three corpus-specific value shapes, then on the config-declared pattern — and the config-declared pattern is `^(?!ACC-)[A-Za-z0-9_\.]{6,}$`, which the docstring (lines 10-13) correctly identifies as matching `escalate`, `complete` and `Africa/Kampala`. The fix (line 128: require a digit and forbid a space) is right. The `SAFE_KEYS` allowlist (line 63) correctly exempts `account_key`, `run_id`, `txn_id`. This is above-average defensive logging.

**The problem is the evidence, and it is a plan requirement, not a preference.**

Plan §13 line 391 (02 §F) requires: "a redaction filter dropping any field matching the raw-identifier pattern, **and a test asserting the filter fires**."

```
$ git grep -rn "redact_value\|redaction_processor\|REDACTED" -- 'tests'
(no output)

$ git grep -rn "observability" -- 'tests'
(no output)

$ git ls-files tests/unit/test_p7_logging.py
(no output — the file does not exist)
```

And `apps/api/observability.py:21-22` asserts the opposite:

> "The tests in ``tests/unit/test_p7_logging.py`` assert the filter fires on a raw id and that an ``ACC-`` key survives it."

**That is a false claim about a test suite that does not exist.** A security control whose only description of its own verification is wrong is the 00 §B failure mode verbatim: the reader is told the gate bites, and it does not exist. **HIGH.**

**Fix (test):** create `tests/unit/test_p7_logging.py` with the two assertions the docstring already promises — `redact_value("account_id", "C1234567") == ("[redacted:field-name]", "field-name")` and `redact_value("account_key", "ACC-7F2A19") == ("ACC-7F2A19", None)` — plus one through `redaction_processor` on a whole event dict, and one for the config-pattern digit rule. Four cases, all hand-computable, per 03's "cause → handling → named test".

### C.3 Does the filter actually hold under an exception? **NO — ordering hole.**

`format_exc_info` is registered **after** `redaction_processor` (observability.py:189-194). `redaction_processor` (line 136) iterates `event_dict.items()`; at that moment `exc_info` is a bool or an exception tuple — not a `Mapping` — so the `elif isinstance(value, Mapping)` branch at line 149 does not fire and the value passes through untouched. `format_exc_info` then renders it into a fully-formatted `exception` string containing the traceback text. **The traceback is never offered to the redactor.**

Consequence: any exception whose message embeds an account id writes that id to the log verbatim. Concretely, `apps/api/security.py:252` (finding A-3) raises `TokenError("token payload is unreadable")` from inside an `except` on a *caller-supplied JWT payload*; and the B904-missing `from exc` means the underlying `JSONDecodeError` — which quotes the offending bytes — is chained and rendered by the very processor that runs too late. The plan's boundary is "raw ids never leave ingest, **including logs**". This is the one path where they do. **HIGH.**

**Fix (code, two lines):** move `redaction_processor` to be the **last** processor before the renderer, i.e. after `format_exc_info`:

```python
structlog.configure(
    processors=[*shared_without_redaction, StackInfoRenderer(), format_exc_info,
                redaction_processor, JSONRenderer() if json_output else ConsoleRenderer()],
    ...
)
```
…where `shared_without_redaction` is `shared` minus its last element. `redact_value` works on the rendered `exception` string via the value-shape patterns, so this closes the hole without new logic.

### C.4 `RUN_SALT` — never in the repo, never in an artifact, missing/short salt fails loudly

Three separate questions, three different answers.

#### (a) Is it in the repo? **No — but a hardcoded fallback salt is, in a script.**

`.env` is correctly excluded: `git check-ignore -v .env` → `.gitignore:6:.env`, and `git ls-files --error-unmatch .env` → *"did not match any file(s) known to git"*. **The live `.env` on disk holds a real 64-hex-char salt and is untracked. Correct.**

But:

```
packages/pipeline/oxbow/adapters/../ingest/../..  — no
scripts/measure_ibm_cycles.py:166:    RUN_SALT from the environment instead (01 §A rule 8).
scripts/measure_ibm_cycles.py:170:    return os.environ.get("RUN_SALT", "oxbow-ibm-cycle-measurement-v1")
```

**A static, published, guessable salt literal is committed to the repository**, used as the fallback for the IBM cycle measurement. Any artifact that script produced is keyed by a salt the whole world can read, which makes every `account_key` in it trivially reversible to a raw IBM `Account####` id by anyone with the corpus. That is the exact re-identification path the salt exists to close, and it is defeated by a one-line default. 01 §A rule 8 / plan line 194: "RUN_SALT per run, in the environment, **never in the repo**." **HIGH.**

**Fix (code, one line):** delete the default and let it raise —
```python
return os.environ.get("RUN_SALT") or (_ for _ in ()).throw(SystemExit("RUN_SALT must be set; no default salt exists (01 §A rule 8)"))
```
…or simply `os.environ["RUN_SALT"]`, which raises `KeyError` and fails loudly. Then re-run the measurement under a real salt and re-key any artifact it wrote.

#### (b) `Makefile:31` regenerates the salt on every invocation — **CRITICAL, see §E.1.**

#### (c) Does a missing or short salt fail loudly? **Partially.**

- **Missing** — yes, twice, and correctly. `config.py:148-161` `require_run_salt()` raises `ConfigError` on empty; `config.py:188-212` `resolve_run_salt()` raises when neither env nor `.env` has it, with the right message ("An empty environment is therefore an error to be named, never a licence to invent"). `docker-compose.yml:185,202` uses `${RUN_SALT:?RUN_SALT is required and must come from the environment, never the repo}`. Tested at `tests/unit/test_p0_toolchain.py:117-123`. **Good.**
- **Short** — **NO.** There is no length or entropy check anywhere. `require_run_salt` and `resolve_run_salt` accept any non-empty string, including `"x"`. The only length assertion in the codebase is *inside* the account-key derivation:
  ```
  packages/pipeline/oxbow/ingest/canonical.py:119:                f"{len(self.run_salt)}. Set RUN_SALT in .env — a salt committed to "
  ```
  …which fires *after* the key has been computed, and only reports the length. So a one-character salt is accepted and every account is keyed by it. `tests/unit/test_p0_toolchain.py:123` sets `"a" * 64` — the test uses a *good* salt, so it never exercises the bad case. **MEDIUM.** Fix: in both resolvers, reject `len(salt) < 32` with a `ConfigError` naming the requirement, and add a negative test with `"a"`.

### C.5 HMAC signing — constant-time, 300 s window, raw bytes

**All three confirmed, and this is the cleanest security surface in the repo.**

`packages/pipeline/oxbow/adapters/signing.py` (141 lines, read in full):

| Requirement (02 §E / plan line 384) | Evidence | Verdict |
| --- | --- | --- |
| HMAC-SHA256 | `signing.py:63` — `hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()` | ✅ |
| Over `t + "." + raw_body` | `signing.py:62` — `signed = timestamp.encode("utf-8") + b"." + raw_body` | ✅ |
| **Over raw bytes** | `raw_body: bytes` throughout; docstring lines 9-11 explains that re-serialising JSON "would change key order or number formatting and invalidate a correct signature". `sign_body(raw_body: bytes, ...)` and `verify_signature(..., raw_body: bytes, ...)` both take bytes, never a parsed object. | ✅ **Exactly right**, and the reason is stated. |
| **Constant-time compare** | `signing.py:123` — `if not hmac.compare_digest(expected, provided):` | ✅ |
| **300 s replay window** | `signing.py:38` — `REPLAY_WINDOW_SECONDS: Final = 300`; enforced at `signing.py:117` — `if abs(current - sent_at) > window_seconds: raise SignatureExpiredError(...)` | ✅ Both directions (`abs`), so future-dated timestamps are also refused. |
| Timestamp inside the signed material | `signing.py:7-8` — "a captured request cannot be replayed later with a fresh `t` without the secret" | ✅ |
| One implementation, both sides | Module docstring lines 15-23: `apps/api/echo.py` calls `verify_signature` directly rather than re-deriving, because "the copies drifted, and a drifted signature check is a check that passes for the wrong reason." | ✅ Excellent — this is the right call and it is documented. |
| Typed failure taxonomy | `MalformedSignatureError` / `SignatureExpiredError` / `SignatureMismatchError` under a `SignatureError` base, with the ordering rationale at lines 103-108. | ✅ |

Tests exist and are behavioural: `tests/unit/test_webhook_signing.py:156` (malformed header), `tests/integration/test_p7_api.py:2197` (stale timestamp), `:2213` (wrong signature), `:2233` (signature valid for a different body — the key negative test).

**One robustness defect.** `hmac.compare_digest(expected, provided)` at line 123 operates on two **`str`**. CPython's `compare_digest` accepts `str` only if both are ASCII-only; a non-ASCII digest in the header raises `TypeError: comparing strings with non-ASCII characters is not supported`. So a request with `X-OXBOW-Signature: t=1700000000,v1=café` produces an **unhandled `TypeError` → HTTP 500**, not the `SignatureMismatchError` the module's own taxonomy provides for exactly this input class, and not the 400 the docstring (lines 105-106) says a malformed header should be. Trivial to trigger, trivially fixed. **MEDIUM.**

**Fix (code, 2 lines):**
```python
try:
    matched = hmac.compare_digest(expected, provided)
except TypeError:
    raise SignatureMismatchError("digest is not ASCII hex") from None
if not matched:
    raise SignatureMismatchError("digest does not match the body under this secret")
```

### C.6 Secrets scan

Full-repo scan across `*.md`, `*.yaml`, `*.yml`, `*.ipynb`, `*.json`, `*.toml`, `*.py`, `*.ts`, `*.tsx` for `api[_-]?key`, `secret`, `passw`, `token`, `bearer`, `postgres://`, `postgresql://`, `amqp://`, `redis://`, and PEM private-key headers.

**Result: zero real secrets. Every hit is a placeholder, a doc reference, or a local-only default.**

| Location | Text | Verdict |
| --- | --- | --- |
| `.env.example:39,45,52,58,68` | `change_me_local` | Placeholder. Correct — the committed file is meant to hold no secrets. |
| `.env` (untracked) | `RUN_SALT=a3004e73…`, `POSTGRES_PASSWORD=oxbow_local_only`, `KEYCLOAK_ADMIN_PASSWORD=admin_local_only`, `WEBHOOK_SIGNING_SECRET=oxbow_local_secret` | **Real values, untracked, gitignored.** Not a repo leak. See C.4 for the *static-salt* problem, which is separate. |
| `docker-compose.yml:23,77,95,137,161,183,200,201` | `${POSTGRES_PASSWORD:-oxbow_local_only}` etc. | Local-dev defaults with env override. `docker-compose.yml:183` is a connection string **with credentials in it** — but built from env vars with a localhost-only fallback, and it is compose-internal, never committed elsewhere. Acceptable for a local compose path. |
| `docker-compose.yml:3` | `# 02 F supply chain: containers built from PINNED DIGESTS, secrets from the` | **The comment is false. See C.7.** |
| `.pre-commit-config.yaml:3,10` | secret-scanning hook description | Legitimate. See C.7. |
| `pnpm-lock.yaml:316-3748` | `@csstools/css-tokenizer`, `js-tokens@4.0.0` | Package names. False positives. |
| `apps/api/alembic.ini:10,11,15,52` | `%(here)s`, `%%(rev)s_%%(slug)s`, `%(levelname)-5.5s` | Config interpolation syntax. False positives. |

**No real secret is committed. No connection string with a real credential is committed.**

**One structural finding:** `docker-compose.yml:3` claims PINNED DIGESTS. It is a comment asserting a security property that the file does not have. `DECISIONS.md` and `LIMITATIONS.md` should not carry a claim the compose file contradicts — a judge reading line 3 and then line 19 finds two different stories. **Folded into C.7.**

### C.7 Docker — digest-pinned or tag-pinned? **TAG-PINNED. All six. Zero digests.**

Plan T4 (line 149): "**Pinned image digests** (02 §F supply chain)." Plan §13 line 391 (02 §F): "containers from pinned digests."

```
$ Select-String -Path docker-compose.yml -Pattern "image:|@sha256"
19:    image: postgres:16.4-alpine
52:    image: redis:7.4-alpine
70:    image: quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z
89:    image: minio/mc:RELEASE.2024-08-13T05-33-17Z
103:    image: ghcr.io/mlflow/mlflow:v2.19.0
129:    image: quay.io/keycloak/keycloak:26.0
```

**Six images, six tags, zero `@sha256` occurrences in the file.** Every tag is mutable: `postgres:16.4-alpine` and `redis:7.4-alpine` float patch releases; `ghcr.io/mlflow/mlflow:v2.19.0` is a semver tag that a registry can re-push. Nothing in the repo records a digest, so the build is not reproducible even in principle, and there is no record of what bytes were ever verified. This is the one 02 §F supply-chain requirement that is **flatly unmet**, and `docker-compose.yml:3` actively asserts the opposite. **HIGH.**

**Fix (config):** for each of the six, resolve the digest once (`docker buildx imagetools inspect <image>:<tag>`) and pin `image: <repo>@sha256:<64-hex>`, keeping the tag as a trailing comment. Add a pre-commit hook that greps for `image: ` lines without `@sha256:`.

### C.8 SQL injection surface in `apps/api`

**No injection surface found. Every statement is either a SQLAlchemy Core/ORM expression or a literal `text()` with no interpolation.**

Enumerated (`text(`, `execute(`, `f"SELECT`, `+ sql`, `% (`, `concat` across `apps/api`):

- **ORM/Core expressions** — the overwhelming majority. `decisions.py:149,168,671,709,746,779,797,944,957,975,1015`; `outbox.py:184,410,415`; `policy_engine.py:561,566,574,626`; `readmodel.py:283,285,306,479,480,573,605`; `routers/alerts.py:205`; `jobs.py:152,258`. All `select(...)`/`insert(...)`/`update(...)` with bound parameters. **Clean.**
- **`text()` with static SQL only** — `deps.py:283` `text("SELECT 1")` and `deps.py:286-289` `text("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")`. Neither interpolates anything; both are constant strings. **Clean.**
- **Alembic migrations** — `0001_*.py` uses `sa.text()` exclusively for `server_default=` (`"now()"`, `"true"`, `"'queued'"`), which is DDL and takes no runtime input. `0002_integrity_triggers.py:342,350` uses f-strings: `op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_run_mutable ON {table}")` and `op.execute(f"DROP FUNCTION IF EXISTS {function}()")`, where `table` iterates `RUN_SCOPED_TABLES` and `function` a literal tuple, both module constants. **No user input reaches either.** Not an injection surface; flagged only because string-formatted SQL is the shape that *becomes* one. **No finding.**

Two secondary observations, both LOW and both positive-by-accident rather than by design:

1. `deps.py:296-297` — `detail=f"Postgres at {make_url(self.settings.sqlalchemy_url).host!r} refused: {str(exc.orig)[:200]}"` puts a **driver exception message into an API response body**. `exc.orig` from psycopg can contain the failing statement fragment. It is truncated to 200 chars, but it is unfiltered by `redact_value`. This is an information-disclosure surface of the "error detail leaks internals" kind, and plan §18 explicitly rejects "catching a broad exception and returning an empty list" for the analogous reason. **LOW-MEDIUM.** Fix: log `exc.orig` through `redact_value` and return a generic detail plus the `run_id` (which the problem model already carries).
2. No query timeouts or statement-count limits are visible on the read paths in `readmodel.py`. For a research prototype against a local Postgres this is acceptable. **No finding.**

### C.9 P0 T6 — GitHub Actions: **`.github/workflows/` IS EMPTY**

```
$ Get-ChildItem -Recurse .github
C:\Users\HP\Desktop\OXBOW\.github\workflows
```

The directory exists and contains **no files**. Plan T6 (line 153) requires: "lint, typecheck, unit tests, 50k-row pipeline smoke, one-fold backtest smoke, image build, `pip-audit`, `pnpm audit`, plus `schemathesis` and `import-linter` contracts when those land." Plan P0's gate evidence (line 167) requires "the CI run URL".

Meanwhile `STATE.md:11` records **P0 as DONE** with proof "`verify.py --phase P0` 2/2 · `9582638`" — the proof being a local script, not a CI run.

This matters beyond the checklist. Three of the findings in this report are exactly what CI would have caught: C.4(a) the salt, §B.1 the missing §19.1 gate, and E.1 the `make test` failure. With no CI, the only gates are local hooks a developer can bypass and a `make` target nobody is required to run. **HIGH.**

---

## D. Edge cases — the named-test table (03 Edge Cases)

Method: for each of the 35 names in the brief, `git grep -n "def <name>\b" -- tests` for an exact-name function definition; failing that, a repo-wide grep for the string to distinguish *prose-only* (named in a docstring/comment/fixture) from *wholly absent*. Where a name is absent but the behaviour is clearly tested under a different name, that is called out separately, because the plan's rule is about the **behaviour** being gated, not the spelling.

**Legend.** `PRESENT` = a real test function with that exact name. `RENAMED` = behaviour implemented **and** tested under a different name (this is a naming deviation, not a gap). `PROSE-ONLY` = the name appears only in a docstring/comment/fixture; **no test function exists**. `ABSENT` = no mention anywhere.

| # | Named test | Status | Evidence / what it would take |
| --- | --- | --- | --- |
| D-1 | `test_dup_txn_conflicting_quarantined` | **PRESENT** | `tests/contracts/test_p1b_canonical_ingest.py:167` |
| D-2 | `test_missing_required_column` | **PRESENT** | `tests/contracts/test_p1b_canonical_ingest.py:218` |
| D-3 | `test_zero_silent_coercions` | **PRESENT** | `tests/contracts/test_p1b_canonical_ingest.py:246` |
| D-4 | `test_label_provenance_required` | **PRESENT** | `tests/contracts/test_p1b_canonical_ingest.py:428` |
| D-5 | `test_empty_batch_errors` | **PRESENT** | `tests/contracts/test_p1b_canonical_ingest.py:561` |
| D-6 | `test_future_timestamp_quarantined` | **PRESENT** | `tests/contracts/test_p1b_canonical_ingest.py:646` |
| D-7 | `test_salt_not_in_artifacts` | **ABSENT** | Zero mentions repo-wide. No test asserts the pseudonym map or the salt is excluded from exports. Nearest: `tests/integration/test_p7_api.py:1534` `test_only_pseudonymous_account_keys_reach_a_response` covers the **API response**, not artifacts; and `tests/unit/test_p1b_ibm_aml.py:887` covers key stability under a fixed salt. **The export-exclusion property is untested.** Plan line 194 names this test explicitly. |
| D-8 | `test_embargo_blocks_leakage` | **PRESENT** | `tests/unit/test_p2_splits.py:244` |
| D-9 | `test_cycle_search_respects_budget` | **RENAMED** | Behaviour is gated: `tests/unit/test_p3a_cycles.py:417,437,466` all assert `cycle_search_truncated is True`, and `tests/unit/test_p3b_rules.py:611-614` asserts `unbounded is False` / `bounded is True`. The behaviour is real and tested; the plan's name is not used. |
| D-10 | `test_overlapping_rules_counted_once` | **PROSE-ONLY** | Named in `tests/golden/build_fixture.py:206` (a comment marking a planted overlap group) and `tests/golden/expected.yaml:153` (an expectation string). **No test function asserts the score counts the group once.** The fixture plants the case; nothing asserts the counting. |
| D-11 | `test_periodic_cycle_downweighted` | **PROSE-ONLY** | Named only in `tests/golden/expected.yaml:198` — "suppressed (test_periodic_cycle_downweighted)". The expectation *text* exists in a hand-checked fixture, which is 03-conformant, but no test function reads it. **Between PROSE-ONLY and gated**: the golden matrix (`tests/unit/test_p3b_golden_matrix.py`) does compare against `expected.yaml`, so the downweighting is probably asserted *indirectly* through the matrix. **UNVERIFIED** — I did not run the suite. |
| D-12 | `test_no_dead_rules` | **RENAMED** | Behaviour is gated: `tests/unit/test_p3b_rules.py:1204` `test_dead_rule_is_removed_with_a_reason_and_excluded_from_scoring` and `:1218` `test_dead_rule_removal_can_be_turned_off_by_config`. Real behaviour, different name. |
| D-13 | `test_separation_detected` | **PROSE-ONLY** | Named in `packages/pipeline/oxbow/scoring/model.py`, `scoring/scale.py`, `scoring/errors.py`. The **behaviour is implemented** (`scoring/model.py:110` emits `separation_auc_by_feature`, and `scoring/selection.py` refuses on it), but `git grep -n "def test_.*separation" -- tests apps` returns **nothing**. An implemented, named, load-bearing guard with zero tests. |
| D-14 | `test_missing_is_a_bin_not_a_mean` | **PROSE-ONLY** | Named in `packages/pipeline/oxbow/scoring/binning.py`. `scoring/binning.py:144` implements `structural_zero_share` and `missing_share` as separate bins. No test function. |
| D-15 | `test_unseen_category_handled` | **PROSE-ONLY** | Named in `packages/pipeline/oxbow/scoring/binning.py`. The `__unseen__` bin is implemented (`scoring/binning.py` special-bin handling; `scoring/model.py:517` indexes `fit.bin_woe[feature][labels[feature]]`). No test function. |
| D-16 | `test_no_infinite_woe` | **PROSE-ONLY** | Named in `scoring/binning.py` and `scoring/errors.py`. Laplace smoothing implemented (`config/scorecard.yaml:53` `laplace_alpha: 0.5`; min-bin-count merge in `scoring/binning.py`). No test function. |
| D-17 | `test_calibration_refused_below_floor` | **PROSE-ONLY** | Named in `packages/pipeline/oxbow/models/calibration.py`. The floor and the refusal are implemented. No test function. |
| D-18 | `test_shap_fallback_to_points` | **PROSE-ONLY** | Named in `packages/pipeline/oxbow/models/explain.py`. The fallback is implemented and its *output* is asserted once indirectly: `tests/unit/test_p9_packet.py:669` `assert "scorecard-explained" in html`. So the label is checked; the fallback *decision* is not. |
| D-19 | `test_erasure_preserves_chain` | **PROSE-ONLY** | Named in `packages/pipeline/oxbow/audit/erasure.py`. The module exists — right-to-erasure is a plan-mandated capability (plan line 391) and 02 §F calls it out by name. `git grep -n "def test_.*erasure" -- tests` returns **nothing**. **The entire erasure capability has zero tests.** |
| D-20 | `test_label_not_sharpe` | **RENAMED** | Behaviour gated: `tests/unit/test_p0_toolchain.py:364` `test_benefit_ratio_is_not_called_a_sharpe_ratio`. The name is also carried in `backtest/metrics.py`, `backtest/model_card.py`, `apps/api/schemas/validation.py` and `apps/api/schemas/policy.py` — the labelling requirement is honoured in all four places the plan names (UI, model card, script, formula shown). **This one is genuinely well-covered.** |
| D-21 | `test_zero_capacity_returns_forgone_value` | **PRESENT** | `tests/unit/test_p5_allocate.py:157` |
| D-22 | `test_all_negative_ev_recommends_nothing` | **PRESENT** | `tests/unit/test_p5_allocate.py:181` |
| D-23 | `test_solver_timeout_falls_back` | **PRESENT** | `tests/unit/test_p5_allocate.py:248` |
| D-24 | `test_recovery_rate_bounds` | **PRESENT** | `tests/unit/test_p5_economics.py:110` |
| D-25 | `test_currency_requires_assumptions` | **PRESENT** | `tests/unit/test_p5_economics.py:398`. Reinforced by `tests/unit/test_p9_packet.py:448` `test_money_line_refuses_to_exist_without_assumptions` — the plan's "a consumer cannot receive an OXBOW number without also receiving what it depends on" is genuinely enforced. |
| D-26 | `test_var_reproducible` | **PRESENT** | `tests/unit/test_p5_monte_carlo.py:94` |
| D-27 | `test_no_alerts_fold_undefined_not_zero` | **RENAMED** | Behaviour gated: `tests/unit/test_p6_metrics.py:47` `test_precision_undefined_at_zero_budget_not_zero_or_one`, whose comment at line 48 cites the plan name. Asserts `precision is None`, `recall is None`, `count == 0` — exactly the "undefined with the count, never zero or one" requirement. |
| D-28 | `test_stale_response_discarded` | **PROSE-ONLY** | Named in `apps/web/src/lib/api/client.ts` and `apps/web/src/app/dev/states/gallery-rows.tsx`. `git grep -n "def test_" -- tests` for a matching name: none; and there are **no JS/TS test files at all** (see D-34). The request-sequence guard is **implemented in the client and has no test in any language.** |
| D-29 | `test_cls_zero` | **ABSENT** | No Playwright config, no spec files, no test. |
| D-30 | `test_four_empty_states_distinct` | **ABSENT** | As above. `EmptyState` exists at `apps/web/src/design/primitives/EmptyState.tsx` and the four states are built (plan line 420), but nothing asserts they are distinct or that none says "No data". |
| D-31 | `test_pane_error_isolated` | **ABSENT** | As above. `ErrorPane` and `Pane` exist. |
| D-32 | `test_reduced_motion_gallery` | **ABSENT** | As above. `<MotionConfig reducedMotion="user">` and `/dev/states?motion=reduced` are implemented (plan line 424); the guarantee is asserted nowhere. **This is the plan's own phrase: "so the guarantee is tested rather than asserted."** |
| D-33 | `test_greyscale_bands_distinguishable` | **ABSENT** | As above. Band letter + five-segment meter glyph are implemented (`design/tokens.ts:109-113` `bandMeterSegments`; `components/ui/BandBadge.tsx`). Nothing tests greyscale, and the print stylesheet is untested. |
| D-34 | `test_timestamps_show_zone` | **ABSENT** | As above. `components/ui/provenance.tsx` and `lib/format/time.ts` implement zone-aware formatting; `packet/render.py:183` appends `zone.abbrev` explicitly. Untested. |
| D-35 | `test_disclaimer_present_everywhere` | **PRESENT** | `tests/unit/test_p9_packet.py:789`. I independently confirmed the third location by reading it: `apps/web/src/app/global-error.tsx:68-71` carries the disclaimer **verbatim**, including in the tier-4 boundary that re-declares `<html>`. README and app footer not independently re-read in this pass. |

### D.1 The category that matters most: implemented-but-untested = 00 §B's "decoration"

Plan line 478: "**no `TODO`, `FIXME`, `any`, or commented-out block survives in merged code**" and line 225: "*a test that does not fail when a deliberately leaking feature is introduced is not a gate; it is decoration.*"

The worse failure is not a missing behaviour — it is a **present** behaviour with **no test**, because a present behaviour reads as done. Called out separately, as asked:

| Behaviour | Implemented at | Named test | Test exists? |
| --- | --- | --- | --- |
| **Right-to-erasure vs immutable audit** | `packages/pipeline/oxbow/audit/erasure.py` | `test_erasure_preserves_chain` | **NO — zero tests for the module.** Plan line 391 names this capability explicitly. This is the single largest untested surface: it is a legal-compliance claim. |
| **Separation detection** | `scoring/model.py:110`, `scoring/selection.py:149-172` | `test_separation_detected` | **NO** |
| **`__unseen__` category bin** | `scoring/binning.py`, `scoring/model.py:517` | `test_unseen_category_handled` | **NO** |
| **Missing-as-a-bin, not a mean** | `scoring/binning.py:143-144` | `test_missing_is_a_bin_not_a_mean` | **NO** |
| **Laplace smoothing / no infinite WOE** | `scoring/binning.py`; `config/scorecard.yaml:53` | `test_no_infinite_woe` | **NO** |
| **Calibration refused below the floor** | `models/calibration.py` | `test_calibration_refused_below_floor` | **NO** |
| **SHAP → scorecard-points fallback** | `models/explain.py` | `test_shap_fallback_to_points` | **NO** (label only, at `test_p9_packet.py:669`) |
| **Overlap groups counted once** | `rules/` (DEV-015) | `test_overlapping_rules_counted_once` | **NO** (fixture plants it; nothing asserts the count) |
| **Salt excluded from artifacts** | — | `test_salt_not_in_artifacts` | **NO** (and the property may not even be implemented — see E.6) |
| **Stale-response discard** | `apps/web/src/lib/api/client.ts` | `test_stale_response_discarded` | **NO** in any language |

**Ten named gates, from 03 Edge Cases, with the behaviour shipped and no test.** Every one of these is a claim the product makes in its own UI or docs that no command can confirm. Seven of the ten are in the scoring/model layer — the part of the system whose entire argument is "every number is defensible."

### D.2 The web test surface does not exist at all

This is not a per-test gap; it is the whole surface, and it is why D-28 through D-34 are all ABSENT.

```
$ git ls-files "apps/web" | Select-String -Pattern "spec\.|test\.|e2e|playwright|vitest"
(no output)
```

`apps/web/package.json` declares `"test:unit": "vitest"` and `"test:e2e": "playwright test"`. There is **no `vitest.config.*`, no `playwright.config.*`, and not one `*.test.ts` / `*.spec.ts` file** in the repository.

And it is not merely untested — **`make test` is red**:

```
$ cd apps/web; npx vitest run
 RUN  v2.1.8 C:/Users/HP/Desktop/OXBOW/apps/web
 include: **/*.{test,spec}.?(c|m)[jt]s?(x)
No test files found, exiting with code 1
EXITCODE=1
```

`Makefile:160-162`:
```make
test-web:
	cd $(WEB) && pnpm test:unit --run
```
and `Makefile:153-154`:
```make
test: test-python test-web ## pytest + vitest
```

**So `make test` cannot pass.** Plan P0's gate (line 115) is `make bootstrap && make up && make lint && make test` exiting zero. That gate is currently unreachable, and `STATE.md:11` records P0 as **DONE**. `STATE.md:29` is honest about the narrower truth — P8 is "code present, **never rendered** … no test config" — but the P0 row and the plan's gate are not reconciled with it.

This is **CRITICAL** and it is the reason the plan's §18 rejection trigger "Default shadcn look shipped unrestyled" and its P8 gate ("Playwright screenshot suite green", "axe reports zero critical violations", "measured CLS is zero", "`prefers-reduced-motion` is a **tested path, not an assertion**") have no enforcement behind them whatsoever.

---

## E. Hardening pass — prioritised by blast radius

Ordered worst-first. Each: location · one-sentence failure scenario · one-sentence fix · fix type.

### E.1 — CRITICAL · correctness + reproducibility

**`Makefile:31`** — `export RUN_SALT ?= $(shell uv run python -c "import secrets;print(secrets.token_hex(32))")`

*Failure scenario:* every `make ingest` / `make pipeline` invocation generates a **fresh random salt**, `resolve_run_salt` (`config.py:202-204`) prefers the environment over `.env`, so every run re-keys every account in the corpus — two runs of the same command produce different `account_key`s for the same person, and nothing in the output says so.
*Why it is CRITICAL:* `packages/pipeline/oxbow/config.py:195-200` names this exact failure in its own docstring — "a run that silently picks up a different salt re-keys every account in the corpus. Two such runs cannot be joined, `make verify-determinism` cannot compare their bytes, **and neither failure is visible in the output**." The Makefile does precisely what that comment forbids.
*And the gate cannot see it:* `scripts/verify_determinism.py:193-199` resolves the salt **once** and injects it into both child processes (`child_env = dict(os.environ, RUN_SALT=salt, …)`), so the determinism gate compares two runs that were *forced* to share a salt and is structurally incapable of detecting salt drift. `STATE.md:278` records "**67 artifacts byte-identical across two runs**" as proof — that proof is vacuous with respect to this defect. This is 00 §B's decoration failure in its purest form: a green gate that cannot go red.
*Fix:* drop the `$(shell …)` generator and require the salt from outside — `export RUN_SALT` (no default) so an unset salt hits the `ConfigError` at `config.py:208`, plus a determinism sub-check that runs the stage **twice through separate process launches without an injected salt** and diffs the account keys.
*Type:* **config** (Makefile) + **test** (verify_determinism).

### E.2 — CRITICAL · the definition of done is red

**`Makefile:160-162`, `apps/web/package.json` (`test:unit`/`test:e2e`), `.github/`**

*Failure scenario:* `make test` exits 1 with `No test files found`, so the plan's P0 gate cannot pass, and `STATE.md:11` claims P0 **DONE** on the strength of a local script.
*Fix:* add a `vitest.config.ts` and at least one spec file (the cheapest high-value first spec is D-28's request-sequence guard, since it needs no DOM), then reconcile `STATE.md`'s P0 row with the gate's actual result.
*Type:* **test** + **doc**.

### E.3 — HIGH · PII boundary breach in a committed artifact

**`data/graph_measurement.json:38` and 19 more lines**

*Failure scenario:* twenty raw PaySim account identifiers (`C1677795071`, …) are committed to git, so the plan's absolute rule — "raw ids never leave ingest" (02 §F) — is breached by a tracked file, and remediation requires a history rewrite rather than a delete.
*Fix:* republish the top-20 degrees keyed by `account_key` (the degrees themselves are the deliverable; the identifiers are not), then `git filter-repo` the blob out of history.
*Type:* **code** (regenerate) + **config/process** (history rewrite).

### E.4 — HIGH · the PII control has no test and its docstring says it does

**`apps/api/observability.py:21-22`; missing `tests/unit/test_p7_logging.py`**

*Failure scenario:* the redaction filter is the plan's named PII boundary and its only documented verification is a reference to a test file that does not exist, so a regression that stops redacting is invisible — and `observability.py:20-21` tells the next reader it is covered.
*Fix:* write the four assertions the docstring already promises (field-name match, `ACC-` survival, whole-event-dict pass-through, config-pattern digit rule).
*Type:* **test** (+ correct the docstring).

### E.5 — HIGH · redaction is bypassed by exception rendering

**`apps/api/observability.py:189-194`** (processor order)

*Failure scenario:* `format_exc_info` runs after `redaction_processor`, so any exception message or chained traceback containing an account id is written to the log unredacted — the one path by which a raw id reaches a log, which is exactly what the module exists to prevent.
*Fix:* move `redaction_processor` to be the last processor before the renderer so it sees the rendered `exception` string.
*Type:* **code** (2 lines) + **test** (a case that logs `exc_info=True` with an id in the message and asserts the output).

### E.6 — HIGH · a hardcoded salt is committed to the repository

**`scripts/measure_ibm_cycles.py:170`** — `os.environ.get("RUN_SALT", "oxbow-ibm-cycle-measurement-v1")`

*Failure scenario:* any artifact that script produced is keyed by a salt published in the repo, so every `account_key` in it is reversible to a raw IBM `Account####` by anyone holding the corpus — the salt's entire purpose defeated by a default argument.
*Fix:* `os.environ["RUN_SALT"]` (raises `KeyError`, fails loudly), then re-run the measurement under a real salt and re-key what it wrote.
*Type:* **code** (1 line) + **process** (re-run).

### E.7 — HIGH · no container is digest-pinned, and the file claims otherwise

**`docker-compose.yml:19,52,70,89,103,129` (and the false claim at `:3`)**

*Failure scenario:* all six images float on mutable tags, so the build is not reproducible and a re-pushed tag changes the bytes under a verified-looking manifest.
*Fix:* resolve and pin each `@sha256:…`, keep the tag as a comment, and correct line 3.
*Type:* **config**.

### E.8 — HIGH · no CI exists

**`.github/workflows/` (empty)**

*Failure scenario:* every gate in the plan is a local hook or a `make` target, so E.1, E.2 and the missing §19.1 gate (§B.1) have no enforcement point, and P0's required "CI run URL" evidence does not exist.
*Fix:* add the plan's T6 workflow — lint, typecheck, unit, `pip-audit`, `pnpm audit`, `lint-imports`.
*Type:* **config**.

### E.9 — HIGH · a short salt is accepted

**`packages/pipeline/oxbow/config.py:148-161, 188-212`**

*Failure scenario:* `require_run_salt`/`resolve_run_salt` accept any non-empty string, so `RUN_SALT=x` keys every account in the corpus with one character and nothing complains until `canonical.py:119` reports the length after the damage.
*Fix:* reject `len(salt) < 32` in both resolvers with a `ConfigError` naming the requirement, and add the negative test with `"a"` (the existing test at `test_p0_toolchain.py:123` uses `"a" * 64` and so never exercises it).
*Type:* **code** + **test**.

### E.10 — MEDIUM · ten named gates ship untested

See §D.1 for the full table. Highest-value three, in order: **erasure** (`audit/erasure.py`, a legal-compliance claim with zero tests), **separation detection** (`scoring/model.py:110`), **`__unseen__` bin** (`scoring/binning.py`).
*Fix:* one `def test_<plan name>()` per row in §D.1, each with a hand-computed expectation per §19.6.
*Type:* **test**.

### E.11 — MEDIUM · §19.1 has no repo-wide enforcement

**`tests/unit/test_p0_design_system.py:233-245`** (scope) and **`:239`** (pattern)

*Failure scenario:* the merge gate for unfinished-work markers scans `apps/web/src/design/` only and drops `placeholder` and `lorem` from its pattern, so 24 real §19.1 hits live in `packages/`, `apps/api/`, `apps/web/src/app/` and `scripts/` with the guard green.
*Fix:* add a §19.1 case to `test_anti_rubbish.py` using its existing `iter_source`/`offenders` helpers, scanning `("packages","apps","scripts","config")` with the plan's full pattern and an enumerated, justified allowlist of the 21 legitimate hits.
*Type:* **test**.

### E.12 — MEDIUM · the money lint is not in `make lint` and has two coverage gaps

**`Makefile:136-140`; `scripts/no_float_money.py:30, 106-137`**

*Failure scenario:* the plan's headline money constraint runs only as a local pre-commit hook, so `make lint`/`make verify`/`make test` all pass without it, there is no CI, and the rule itself is annotation-only and pipeline-package-only — so `float(amount)`, `round(x * 0.35)`, and any float money in `apps/api` or `apps/web` are all invisible to it.
*Fix:* add `@$(PY) scripts/no_float_money.py` to the `lint-python` recipe, widen `PACKAGE_ROOT` to `("packages/pipeline/oxbow", "apps/api")`, and add `ast.Call` checks for `float()`/`round()` on money-named targets.
*Type:* **config** + **code**.

### E.13 — MEDIUM · `hmac.compare_digest` raises `TypeError` on a non-ASCII digest → HTTP 500

**`packages/pipeline/oxbow/adapters/signing.py:123`**

*Failure scenario:* `X-OXBOW-Signature: …,v1=café` raises an unhandled `TypeError` instead of the module's own `SignatureMismatchError`, turning a malformed-header 400 into a 500 and letting an unauthenticated caller generate error noise at will.
*Fix:* wrap the compare in `try/except TypeError` and raise `SignatureMismatchError` (patch in §C.5).
*Type:* **code** + **test**.

### E.14 — MEDIUM · hardcoded client-side money scale and float money on an input path

**`apps/web/src/app/(dash)/network/page.tsx:311-312`**

*Failure scenario:* the amount filter's ceiling is a bare `100_000_000` and the slider→`min_minor` conversion is float division and multiplication rounded at the end, which is the one thing 01 §B forbids outside a render path, and the ceiling traces to no config file.
*Fix:* source the ceiling from `config/pipeline.yaml graph.max_edge_amount_minor` via `/api/meta/runtime` and derive the slider position with the integer long-division already implemented in `apps/web/src/lib/format/money.ts:20`.
*Type:* **code** + **config**.

### E.15 — MEDIUM · the IV bar scale can silently clip against a configurable ceiling

**`apps/web/src/app/(dash)/scorecard/page.tsx:37, 165`** vs **`config/scorecard.yaml:81-82`**

*Failure scenario:* the bar is scaled to a hardcoded `0.65` while the admission ceiling is configurable and currently `0.5`; raise the ceiling past `0.65` in config and features in the gap render as a full-width bar indistinguishable from a feature at exactly `0.65`, because `Math.min(100, …)` clips without warning.
*Fix:* scale to `iv_bounds.max` from the scorecard payload (already emitted at `scoring/model.py:190-191`) and delete `IV_BAR_MAX`.
*Type:* **code**.

### E.16 — MEDIUM · the rating-migration heatmap is not theme-aware

**`apps/web/src/app/(dash)/scorecard/page.tsx:377`**

*Failure scenario:* the cell fill hardcodes `L = 0.78` (the dark-theme band lightness) while the text uses `var(--color-ink-inverse)`, which paper mode re-points to near-white (`tokens.css:181`), so in paper mode the heatmap renders ~1.3:1 contrast — white on a light cell, failing the P8 "zero critical violations" bar and the plan's colour-alone legibility rules.
*Fix:* add `--color-heat-{lo,hi}` to `tokens.css` for both themes and use `var()` for both fill and text.
*Type:* **code** + **config**.

### E.17 — MEDIUM · `PAGE_SIZE` is a client constant controlling a server parameter

**`apps/web/src/app/(dash)/alerts/page.tsx:57, 136`**

*Failure scenario:* `limit: 100` is sent to the API from a value that exists in no config and is not returned by the server, so any server-side ceiling silently clamps the page and the virtualiser's `count` disagrees with the rendered rows.
*Fix:* return the effective page size in the list envelope (or publish it on `/api/meta/runtime`, whose `useRuntime` hook is already imported at line 50) and drive `limit` from it.
*Type:* **code** + **config**.

### E.18 — MEDIUM · the layout budget the 60 fps gate is measured against is not configurable

**`apps/web/src/app/(dash)/network/canvas.tsx:307-318`** (esp. `:312`)

*Failure scenario:* `numIter: built.length > 800 ? 1_500 : 3_000` and six sibling fcose constants are the numbers the plan's "60fps with 1,500 nodes" gate is measured against, and changing any of them requires editing a JSX literal with no name.
*Fix:* move the block into a named `LAYOUT` section of `design/tokens.css` (so the generated `tokens.ts` carries it) or a `graph_layout` block in `config/pipeline.yaml`.
*Type:* **config**.

### E.19 — MEDIUM · a missing currency becomes ISO "XXX" in a regulatory filing

**`packages/pipeline/oxbow/adapters/goaml/xml.py:77`**

*Failure scenario:* `currency = str(txn.get("currency", "XXX"))` writes the ISO no-currency marker into a GOAML filing instead of refusing, so a transaction with an absent currency is filed as "no currency" rather than quarantined — the 03 §A rule-2 shape ("never let an unknown become a zero") applied to currency.
*Fix:* raise a `ContractError` naming the missing field; the filing writer is enrichment-only, so refusing costs nothing.
*Type:* **code** + **test**.

### E.20 — MEDIUM · a `B017` broad-exception assertion can pass for the wrong reason

**`tests/contracts/test_p1b_batch_boundary.py:222`**

*Failure scenario:* `pytest.raises(Exception)` passes on an unrelated `KeyError`, so the batch-boundary gate can be green while the contract is broken.
*Fix:* assert the specific `ContractError`/`ConfigError` type and match its message.
*Type:* **test**.

### E.21 — LOW · `F822`: a public `__all__` entry that does not exist

**`packages/pipeline/oxbow/features/build.py:993`**

*Failure scenario:* `from oxbow.features.build import *` raises `AttributeError`; `__all__` is a public contract and any `dir()`-driven consumer breaks.
*Fix:* delete the line, or re-export `build_feature_table` from `compute.py` in `build.py`'s namespace.
*Type:* **code**.

### E.22 — LOW · a driver exception message is returned in an API response body

**`apps/api/deps.py:296-297`**

*Failure scenario:* `f"…refused: {str(exc.orig)[:200]}"` puts psycopg's message — which can quote the failing statement — into a `problem+json` detail, unfiltered by `redact_value` and bypassing the log scrubber.
*Fix:* log `exc.orig` through `redact_value` and return a generic detail plus the `run_id` the problem model already carries.
*Type:* **code**.

### E.23 — LOW · `B904` chains a caller-supplied parse error into a log

**`apps/api/security.py:252`**

*Failure scenario:* `raise TokenError("token payload is unreadable")` without `from exc` implicitly chains the `JSONDecodeError`, which quotes attacker-supplied bytes, into a traceback that E.5 shows is never redacted.
*Fix:* `raise TokenError(...) from None` (the error is not informative to the caller) or `from exc` with the detail kept out of the message.
*Type:* **code**.

### E.24 — LOW · dead bindings and parameters

**`packages/pipeline/oxbow/packet/render.py:199`** (`economics` assigned, never read) · **`packages/pipeline/oxbow/packet/subgraph_image.py:225`** (`per_major` accepted, never read; money is rendered by `value_basis_lines` at `:196`) · **`packages/pipeline/oxbow/packet/__init__.py:76,77`** (re-exports missing from `__all__`)

*Failure scenario:* no wrong number results; each is a misleading signature or a refactor scar that costs a reader time. `render.py:199` in particular implies the economics block is read in `assumption_keys` when it is not.
*Fix:* delete `economics = case.economics`; drop `per_major` from `render_subgraph_svg`; add both names to `packet/__init__.py`'s `__all__`.
*Type:* **code**.

### E.25 — LOW · 14 `ARG001` findings mask the real ones

**`packages/pipeline/oxbow/features/kinds.py` (14 sites)**

*Failure scenario:* no defect — this is the uniform `_kind_*(work, entry, context)` dispatch signature, and interface conformance requires the parameter. But 14 identical findings train a reviewer to skip the rule, which is how the one genuine swallowed input (`subgraph_image.py:225`) gets lost.
*Fix:* `# noqa: ARG001` with the one-line reason on the dispatch-signature kernels, so the rule's signal survives.
*Type:* **code**.

### E.26 — LOW · `global-error.tsx` token literals have no drift detector

**`apps/web/src/app/global-error.tsx:24,25,32,42,57,58,59,67`**

*Failure scenario:* the eight inlined OKLCH values are all currently correct (verified against `tokens.css` above), and inlining them is the right engineering call for a boundary above the shell. But they are inlined *text*, so the `design-tokens-in-sync` hook (`.pre-commit-config.yaml:95-100`, which guards `tokens.css`→`tokens.ts` and the sprite) cannot see them, and a token change will drift this file silently.
*Fix:* **do not move them** — add a test asserting these eight match `tokens.css`.
*Type:* **test**.

### E.27 — LOW · the §19.1 word appears in a user-visible string

**`apps/web/src/app/dev/states/page.tsx:153`** — `label="Sweeping placeholder"`

*Failure scenario:* §19.1 demands zero hits; a judge opening `/dev/states` reads the word on screen.
*Fix:* relabel (e.g. `"Sweeping skeleton"`).
*Type:* **code**.

### E.28 — LOW · ruff's `S` (bandit) rules are not enabled

**`pyproject.toml` ruff `select`**

*Failure scenario:* `S105`/`S106` (hardcoded password) and `S608` (SQL injection) are exactly the checks this audit had to perform by hand in §C.6 and §C.8, and they found nothing — but that is a manual result, not an enforced one, and the next contributor gets no signal.
*Fix:* add `"S"` to `select`.
*Type:* **config**.

### E.29 — Observability · what is genuinely good, so it is not re-litigated

Recorded because a hardening pass that only lists defects misrepresents the codebase. Verified by reading, not inferred:

- `adapters/signing.py` — one implementation for both sides, constant-time compare, 300 s window enforced in both directions, signed over raw bytes, with the reason for each stated in the module docstring. The decision to have `apps/api/echo.py` call `verify_signature` rather than re-derive it is correct and documented.
- `config.py:188-212` `resolve_run_salt` — fails loudly with a message that explains *why* inventing a salt is forbidden. This is the right instinct; E.1 is a Makefile bug, not a bug here.
- `lib/format/money.ts:20` — integer long-division for money rendering, with the float-division failure named. Exactly right, and `MoneyFigure.tsx:13` states the "divides by 100 here and only here" boundary explicitly.
- `lib/colour.ts` — exists *because* Cytoscape's parser silently rejected `oklch()` and the graph had been rendering with no band information at all, and the header documents the browser-verified failure. A colour-space conversion with an explicit gamut clamp, written out rather than pulled in, because 01 §A rule 6 forbids unapproved dependencies. This is the best-documented engineering decision in the repo.
- `global-error.tsx:6-10` — the reasoning for inlining tokens above the shell is correct and stated.
- `observability.py:10-13, 128-133` — recognising that the configured pattern would match `escalate` and `Africa/Kampala`, and narrowing it with a digit requirement, is a real adversarial review of one's own config.
- `tests/unit/test_p0_design_system.py:262-280` — glyphs asserted for the 24px grid, 1.75px stroke, square caps and `round` joins, per-file. A hand-drawn set is checked for actually being hand-drawn.
- `STATE.md` — honest. It records P8 as "code present, **never rendered**", P4 as "code present, **not verified by the orchestrator**", P6 as "its numbers have not been reproduced from a command here", and lists 8 API tests that cannot run on this host. The gap is that the phase table's P0 row says DONE while `make test` is red (E.2) — the honesty is real, the summary row is not reconciled with it.

---

## F. Summary table

| Severity | Count |
| --- | --- |
| **CRITICAL** | **2** |
| **HIGH** | **7** |
| **MEDIUM** | **12** |
| **LOW** | **10** |
| **Total** | **31** |

Plus 5 sections of verified-clean surfaces (§B.2 the `any` ban, §B.3 the money path, §C.5 HMAC signing, §C.6 no committed secrets, §C.8 no SQL injection) and 1 premise correction (§0.2: 45 lint errors, not 85; §A.2: zero `F821`).

**Rejection triggers from plan §18 — none tripped.** No UI number lacks an API source; no LLM sits between features and a score; no 4xx retry loop; no broad-exception-to-empty-list in a router path; no chart added for density; no default shadcn look. The findings above are gate-integrity and boundary problems, not the failure modes §18 enumerates.

**Top three by blast radius:** E.1 (the salt is regenerated per `make`, and the determinism gate is structurally blind to it), E.2 (`make test` is red while P0 is recorded DONE), E.5 (redaction is bypassed by exception rendering).
