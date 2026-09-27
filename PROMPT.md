# PROMPT.md — the build prompt this repository was built from

**Status of this file, stated plainly: this is NOT a verbatim copy of document 01.**

Task **T1** of the master build plan requires the repo tree to contain `PROMPT.md`, defined as
*"the verbatim 01 build prompt, committed, per its own aside."* The master plan —
`2026-09-26_021500-oxbow-master-build-plan.md`, mirrored in this repository at
`.hermes/plans/` — is the only one of its five referenced documents that was supplied to this
checkout. Its §1 opens *"what the five documents actually mandate"* and ranks them; four of the
five (00, 01, 02, 03) exist upstream and are not in this working copy, and no copy of them has
surfaced on this machine during nine days of work on it.

So this file records what is recoverable, and says which of it is verbatim. It does not
reconstruct 01 from memory and label the result "verbatim", because a file whose entire purpose is
provenance would be worthless if it invented its own source. The alternative — shipping a plausible
prompt nobody issued — is the fabrication this repository's money rules, hash chain and
`provenance:` fields all exist to refuse.

## What the five documents are, verbatim from the plan's §1 table

| Doc | Rank | Authority over | Load-bearing content |
| --- | --- | --- | --- |
| **00 Implementation Plan & Agent Constitution** | 1 | process, sequence, done-ness | 20 constitution rules, session loop, 3 go/no-go checkpoints, 10-step cut ladder, 9-question self-audit, 5-block report format, `STATE.md`/`DECISIONS.md`/`BACKLOG.md` |
| **01 Master Build Prompt** | 2 | what to build now | Repo tree, pinned stack, 10 phases P0–P9 each with a command-verified gate, hard-constraint table, rejection triggers, per-session opener |
| **03 Edge Cases** | 3 | failure behaviour | ~80 named edge cases, each with cause → handling → **named test**; 3 meta-rules for unknowns |
| **02 Integration Architecture** | 4 | boundary shape | 7 ports + 7 adapter families, 9 internal seams, identity model (4 id types), external-systems catalogue, outbox pattern, contract testing |
| **Spec v2.1** | 5 | formulas, thresholds, screens, stack, copy | EV formula, scorecard scaling, 12 rules R1–R12 with defaults, feature taxonomy, SQL DDL, API surface, 7 screens, design/motion/state-craft system, 12-day plan, 4-min demo script, judge Q&A, disclaimer copy |

## The non-negotiables, verbatim from the same section

> **The non-negotiables that survive every conflict:** seed `1337` everywhere · money is integer
> minor units (`int64`), never float · no LLM between features and a score · historical
> de-identified data only, no live rails, no advice · append-only hash-chained audit · every
> currency figure carries its recovery-rate band and assumption line · risk never encoded by colour
> alone · one phase per session, gate proven by a command whose output gets pasted.

Where each of those is enforced, by name:

| Rule | The thing that makes it hold |
| --- | --- |
| seed `1337` everywhere | `config/pipeline.yaml`, carried on every scored row and into `model_card.json`; `scripts/verify_determinism.py` runs the pipeline twice and diffs artifact digests |
| money is integer minor units, never float | `scripts/no_float_money.py` is an AST gate over 159 files, and `tests/unit/test_p7_money_scale.py` pins the base-to-exponent boundary that rendered figures at 10^100 |
| no LLM between features and a score | no LLM client exists anywhere on the pipeline import path; `lint-imports` contract *"Scoring and quant layers must not import an HTTP client"* is kept |
| historical, de-identified data only | `config/sources.yaml` declares `ingest_allowed: false` for the corpora that must not be read, and the disclaimer is asserted present on every route by `test_disclaimer_present_everywhere` |
| append-only hash-chained audit | `packages/pipeline/oxbow/audit/chain.py`, walked by `scripts/verify_audit.py`; the case packet re-walks the chain and refuses to render on a broken link |
| every figure carries its band and assumption line | `config/economics.yaml` is the single source and is rendered beside the figure, not summarised away |
| risk never by colour alone | `DESIGN.md` bans it and `/dev/states` renders the alternatives |
| one phase per session, output pasted | **deviated from by the owner's standing instruction** to finish everything in one run — recorded in `DECISIONS.md`, not hidden |

## What this repository does not have, because 00–03 are absent

Anything in those documents that the master plan did not restate. The plan's §3 conflict register
quotes the specific clauses that mattered (02 §F roles and four-eyes, 02 §H outbox and webhook
signatures, 03 §K the audit chain, spec §12.8 the state matrix) and every one of those is
implemented with a named test. What cannot be checked is whether a clause in 01 or 03 that the plan
*didn't* quote is unimplemented. If the upstream documents turn up, diff them against
`DECISIONS.md`'s DEV-001…DEV-025 rather than trusting this file.
