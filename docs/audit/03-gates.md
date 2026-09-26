# Gate evidence — measured, not reported

Run by the orchestrator on 2026-09-26 against **HEAD `470b09c`**. Every number below is
pasted command output. Where a gate could not pass because it selects zero tests, it is
reported **UNRUNNABLE** rather than FAIL — the distinction is the point of this file.

The tree was being committed to by other agents throughout (`2e8b4b2` → `1dc2499` →
`16cec41` → `470b09c`). Numbers are only valid for the commit named above.

---

## 1. Full test suite — FAIL (8 failures, one file)

```
$ uv run pytest -q --no-header
FAILED tests/integration/test_p7_api.py::test_every_operational_endpoint_answers_200_with_the_documented_shape
FAILED tests/integration/test_p7_api.py::test_money_never_leaves_the_api_as_a_float
FAILED tests/integration/test_p7_api.py::test_only_pseudonymous_account_keys_reach_a_response
FAILED tests/integration/test_p7_api.py::test_four_eyes_needs_a_different_subject_not_a_different_role
FAILED tests/integration/test_p7_api.py::test_stale_expected_version_returns_409_carrying_the_merge_view
FAILED tests/integration/test_p7_api.py::test_concurrent_append_gives_one_success_and_one_409
FAILED tests/integration/test_p7_api.py::test_outbox_ordering_is_per_case_and_never_global
FAILED tests/integration/test_p7_api.py::test_the_outbox_uses_the_same_implementation_the_receiver_verifies_against

8 failed, 790 passed, 1 skipped, 103 warnings in 427.21s (0:07:07)
```

All 8 are in `tests/integration/test_p7_api.py`. The phase's own recorded outstanding
reason is that those tests need PostgreSQL through a Docker engine that went away
mid-session. **UNVERIFIED:** the `docker` CLI is not on PATH in this shell
(`docker : The term 'docker' is not recognized`), so the daemon's state could not be
confirmed here. Earlier in the session the same endpoints returned 500s with genuine
code faults — `9 validation errors for AlertRow`, `'TailRiskLevels' object has no
attribute 'alpha_var'`, 4 errors for `CurveSeries`, 3 for `ScorecardAttributeView`,
`'set' object has no attribute 'append'` — which are code defects rather than
infrastructure. Both can be true; the endpoint faults were observed before the engine
went away.

Everything else in the repository is green: **790 passed**, including the leakage gate
(19 tests, proven to bite), the P2 feature/split/distinct-count set (70), the P3a graph
set (42) and the P3b rules/golden/cycle set (86).

## 2. Lint — FAIL (45 errors)

```
$ uv run ruff check .
Found 45 errors.
```

Down from 85 earlier in the session; a concurrent commit (`0e8a5b8`) closed 36. The
`F821` "undefined name" class that existed earlier is now **zero**; what remains is one
`F822`. Per-category detail is in `02-code-review.md`.

## 3. Architecture contracts — PASS

```
$ uv run python scripts/verify.py --phase P7
[P7]   PASS    architecture contracts: no adapter import outside adapters/
        Scoring and quant layers must not import an HTTP client KEPT |
        Only the CLI composes adapters KEPT
        Contracts: 4 kept, 0 broken.
```

4 import-linter contracts kept, 0 broken, over the tree as it stands.

## 4. Port conformance — **UNRUNNABLE**, reported as FAIL

```
$ uv run python scripts/verify.py --phase P7
[P7] $ uv run pytest -q tests/contracts_adapters
[P7] $ uv run lint-imports
P7   FAIL    port conformance across every adapter
        ... 2 warnings in 0.05s          <-- no test count: zero tests collected
gates run: 2  passed: 1  failed: 1
exit=1
```

`tests/contracts_adapters/` contains **no test module** — only
`fixtures/rs256_token.json`. pytest exits 5 (nothing collected), so the gate named
"port conformance across every adapter" cannot pass as written. This is the plan's
"the part judges interrogate": 7 ports, 7 adapter families, all present, and **nothing
pins them to each other**. `apps/api/routers/cases.py:482` cites
`tests/contracts_adapters/test_watchlist_conformance.py` as its justification, and that
file does not exist.

## 5. Why `make verify` can be green while the above fails

`make verify` runs `python scripts/verify.py` with no `--phase`, which calls
`verify(PHASES)` — it runs the gates of phases **marked done**. P7 is not marked done,
so its unrunnable gate is invisible to it. The commit `470b09c` ("record make verify
green") is therefore true as stated and misleading as a summary: it is green because
the failing phase is skipped, not because the gate passes. Running the phase explicitly
is the only way to see it.

## 6. P4 gate — UNRUNNABLE

`verify.py` selects the P4 tests with `pytest -q tests/unit -k p4`, and **no
`test_p4_*.py` file exists anywhere** in the repository (count: 0). The gate cannot
select a test, so P4's real state is "no tests", not "tests failing". `STATE.md`'s row
("code present, not verified by the orchestrator") understates this.

## 7. Prescribed paths that do not exist

Checked against plan §5 T1. All confirmed missing:

| Path | Consequence |
| --- | --- |
| `PROMPT.md` | T1 requires it committed, verbatim; "missing files are not [fine]" |
| `notebooks/02_features.ipynb` | T1 requires 01/02/03 |
| `notebooks/03_validation.ipynb` | as above |
| `apps/api/worker.py` | `Makefile:122 make worker` and `docker-compose.yml:195` both invoke it; the `full` profile cannot start |
| `scripts/demo_seed.py` | `Makefile:127 make demo` invokes it — the "phantom gate" already recorded as STATE.md item 11 |
| `apps/api/Dockerfile` | referenced by `docker-compose.yml:176` |
| `apps/web/Dockerfile` | referenced by `docker-compose.yml:208` |

## 8. Findings from this pass confirmed by direct inspection

Both were reported by the code-review subagent and were re-checked here rather than
taken on trust.

**CONFIRMED — `Makefile:31` minted a salt per invocation.**
`export RUN_SALT ?= $(shell uv run python -c "import secrets;print(secrets.token_hex(32))")`
`?=` means it applies whenever the environment and `.env` are both empty, so every
`make` call invented a fresh salt. `oxbow.config.resolve_run_salt`'s own docstring names
this exact failure — "a generated stand-in would re-hash every account key and make the
two runs of verify-determinism incomparable" — and `require_run_salt` already fails
loud. Fixed by removing the invention, with `test_the_makefile_does_not_invent_a_run_salt`
added and proven to fail when the line is restored.

**CONFIRMED — 20 raw PaySim account ids are committed.**
`data/graph_measurement.json` contains `C1065307291, C1312671831, C1314302355,
C1462946854, C1530544995, C1677795071, …` (20 unique, matched `C\d{9,}`). This is the
PII boundary the plan draws at ingest: raw ids must not appear downstream of ingest, and
this file is a committed artifact. Unresolved here because the remedy is a history
rewrite, which is a decision for a human, not a cleanup.

## 9. Committed artifacts that should not be

| Path | Size | Problem |
| --- | --- | --- |
| `.hypothesis/unicode_data/15.0.0/charmap.json.gz` | 21,726 B | tracked; no `.gitignore` rule covers `.hypothesis/` |
| `data/processed/ibm_typologies.parquet` | 9,357 B | tracked despite `.gitignore:52` |

Small in bytes, but both are cache/generated output. Total tracked size is only
386 files / 5.6 MB, so nothing large has leaked into git.

Four dead `.gitkeep` negations mean all five `data/` subdirectories — including two
prescribed ones — disappear on a fresh clone. `.gitignore:68` also promises a
`schema.d.ts` that does not exist.
