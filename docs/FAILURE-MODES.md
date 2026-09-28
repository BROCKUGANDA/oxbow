# FAILURE MODES

A machine-checked register of the failure paths that exist in this repository.

Every row was produced by enumerating the refusal surfaces in the tree — `raise *Error`
sites collected by AST (1,137 of them across 106 exception classes), the `refused.append`
ledgers in the warehouse landing, the per-dependency health probes, and the
substituting `except` blocks — and then asking, for each one, what the user sees and
which test proves it. Nothing here is inferred from what usually breaks.

## What "handled" means here

The doctrines this register is graded against, quoted from `DECISIONS.md` and from the
code's own comments:

1. **Fail loud at the boundary, degrade gracefully in the middle, never fail silent.**
2. **An unknown must never become a zero** — a missing feature is an explicit missing
   bin, a failed enrichment is `unknown`, a missing count renders as an em dash.
3. **If it can change a score, it must change the lineage.**
4. **A gate that cannot fail is decoration** — every guard must be shown to bite.
5. **Degraded, not broken** — a labelled fallback with its deterministic substitute,
   never a stack trace and never an empty list pretending to be an answer.

Status vocabulary (exactly these four strings; the gate rejects anything else):

- `handled+tested` — a guard exists at the named line and the named test exists in
  `tests/`. The gate proves the name resolves; it cannot prove the test passes.
- `handled, untested` — a guard exists and is correct, and **no test exercises it**.
- `unguarded` — nothing stops the failure; the row is a live defect or an open risk.
- `degraded-not-broken OK` — the substitute path is the specified behaviour and is
  labelled as such; listed for completeness, not as a gap.

`web:` in a test cell means the proof lives in the Vitest tree, not the Python one; the
gate checks the file exists. No `web:` row is marked `handled+tested`, because this
change did not execute the JavaScript suite.

---

## Open findings

Ordered by consequence, not by area: the first five are the ones that make a claim in this repository's own documentation untrue today, and the rest are guards that exist and work but have no test, so they can regress silently. Every entry quotes the register row it belongs to verbatim, and `tests/unit/test_failure_mode_register.py` fails the build if a row with an open status is missing from this list -- so a gap may be closed, or honestly left open, but it cannot be buried.

Two entries below are marked *closed by this change* to record what moved, and the phantom-citation finding that motivated the whole exercise is item 6.

1. **one subject holding two salted keys, erased by one request** — audit; `apps/api/decisions.py:1062`; the user sees: nothing: the sibling key survives the erasure — `apps/api/decisions.py:1062` selects `PseudonymMap` by `account_key` alone, so a subject re-salted into a second key survives the erasure meant to remove them, while `oxbow/audit/erasure.py:126 select_mappings_for_subject` exists precisely to prevent that. Fix: add `subject_ref` to `pseudonym_map` at `packages/pipeline/oxbow/adapters/warehouse/models.py:260` -- a file this change does not own -- and delete every mapping sharing it at `decisions.py:1062`. The tripwire that fails when this is closed is `tests/unit/test_p7_erasure_preserves_chain.py::test_erasure_reaches_only_the_named_account_key`.

2. **a cycle-budget truncation changes R4 and must surface in the evidence** — graph; `packages/pipeline/oxbow/graph/model.py:223`; the user sees: nothing: the flag is in the artifact and reaches no response — `graph/model.py:223` stamps `cycle_search_truncated`, `graph/persist.py:297` writes it into the manifest and `cli.py:1032` prints it, but no warehouse column carries it, so no API response and no case evidence can say that R4's count is a lower bound -- and a lower bound changes the score. Fix at `packages/pipeline/oxbow/adapters/warehouse/landing.py` (not owned here): land the manifest flag as a lineage column on `run`. A doctrine-3 violation.

3. **dataset card measurement JSON truncated on disk** — api; `apps/api/routers/meta.py:578`; the user sees: unhandled 500, not a problem document — `apps/api/routers/meta.py:578` reads `data/*_measurement.json` through an unguarded `json.loads`, so a truncated artifact produces an undesigned 500 on a page linked from the README, where every neighbouring read raises `DependencyUnavailable`. Fix: wrap that read and raise `DependencyUnavailable` naming the path, as `_source_card` at `meta.py:798` already does.

4. **case workspace asks the watchlist whether it screened** — api; `apps/api/routers/cases.py:479`; the user sees: `configured but not screened`, never an empty hit list — `apps/api/routers/cases.py:479 _watchlist_enrichment` is the clearest implementation in the repository of *a failed enrichment is `unknown`, not `clean`*: `None` when unconfigured, a labelled `configured but NOT screened` block otherwise. Grepping `tests/` for either symbol returns nothing. Fix: a unit test calling the helper with the null and OFAC adapters, asserting three distinguishable states.

5. **a read-model table added without an immutability trigger** — audit; `apps/api/alembic/versions/0002_integrity_triggers.py:36`; the user sees: nothing until a reviewer notices the list — `apps/api/alembic/versions/0002_integrity_triggers.py:36` names a coverage test that does not exist, and `test_a_terminal_run_takes_no_further_writes` proves the behaviour for one table only, so a read-model table added next week can stay writable after completion without anything noticing. Fix: `tests/integration/test_p7_immutability_triggers.py` comparing `INFORMATION_SCHEMA.TRIGGERS` against `Base.metadata.tables`.

6. **fold fitted on evaluation rows** — model; `packages/pipeline/oxbow/models/run.py:222`; the user sees: refusal: the rejection trigger, not a mistake — `models/run.py:222 assert_no_leakage` is the leakage tripwire the whole split design rests on, and nothing drives it. Fix: a two-row frame with one evaluation row duplicated into the fit population, asserting `FrameContractViolationError` names the overlapping rows.

7. **a rule-hit join misses a scored row** — model; `packages/pipeline/oxbow/models/inputs.py:228`; the user sees: refusal: a missing hit is not a zero hit — `models/inputs.py:228` refuses to read an unjoined rule row as a zero hit -- doctrine 2's headline case -- and is undriven. Fix: a scored frame with one account absent from the provider's hits, asserting the refusal names the missing count.

8. **scoring a row that produced no probability** — model; `packages/pipeline/oxbow/models/run.py:1522`; the user sees: refusal: p=0 would move the account silently — `models/run.py:1522` refuses `p=0` for an account the scorer returned nothing for, because that silently moves the account down the queue. Fix: a fold result that omits one account, asserted at the `FoldScore` construction.

9. **queue asked for a run with no priced accounts** — api; `apps/api/routers/alerts.py:300`; the user sees: 503 naming the run and the reason, not an empty table — `apps/api/routers/alerts.py:300` refuses to draw a cutoff line over an empty priced set, which is the difference between `no alerts` and `nothing scored`. Fix: an in-process request against a run row with zero `economics` rows, asserting 503 rather than an empty page.

10. **decision on a case with no stored economics** — api; `apps/api/decisions.py:762`; the user sees: 422: a money test cannot gate on a missing figure — `apps/api/decisions.py:762` refuses to apply the four-eyes threshold when no money row exists, rather than reading a missing exposure as below the threshold. Fix: open a case on a score row with no economics sibling and assert 422.

11. **alert row has no usable activity window** — api; `apps/api/routers/alerts.py:378`; the user sees: 400: the queue will not show a window it did not measure — `apps/api/routers/alerts.py:378 _required_datetime` refuses to show an activity window it did not measure. Fix: a stored alert row with a null `last_seen`, asserting the 400 names the field.

12. **anomaly normalisation fitted on the scored population** — model; `packages/pipeline/oxbow/models/anomaly.py:141`; the user sees: config refused: that is a leak, not a normalisation — `models/anomaly.py:141` refuses any normalisation other than `validation_percentile_rank`, because a min-max fitted on the scored data leaks the population into the score; the config guard is tested and the runtime refusal is not. Fix: an `AnomalyConfig` with `normalisation='min_max'` through `fit_anomaly`.

13. **lightgbm's own metric disagrees with the repo's** — model; `packages/pipeline/oxbow/models/gbm.py:286`; the user sees: refusal naming both values to eight places — `models/gbm.py:286` compares LightGBM's `average_precision` with this repository's and refuses a disagreement, which is the only guard against the two having silently diverged dialects. Fix: drive the callback state so the two differ and assert `ModelLayerError` prints both to eight places.

14. **mlflow tracking URI unreachable** — model; `packages/pipeline/oxbow/models/registry.py:103`; the user sees: file-store lineage with `tracking_degraded` stamped — `models/registry.py:103` is the branch the module docstring promises -- lose lineage, not the score -- and it is never driven. Fix: point `mlflow.tracking_uri` at an unresolvable host with `offline_fallback_allowed` set both ways, asserting the degraded tuple and the refusal respectively.

15. **a pane's data request fails in the client** — demo; `apps/web/src/components/Pane.tsx:1`; the user sees: labelled pane error; siblings keep rendering — `apps/web/src/components/Pane.tsx` isolates a failed pane so its siblings keep rendering; the proof is `web:apps/web/tests/unit/pane-error-isolated.test.tsx`, which this change did not execute, so the Python gate cannot vouch for it. Fix: run `bun run test:unit` in `apps/web` and record the result here, or index the Vitest titles in the register gate.

16. **API asked to compute what the pipeline did not** — api; `apps/api/readmodel.py:1271`; the user sees: the field is absent, and the served bundle names the absence — `apps/api/readmodel.py:1271` is the boundary that keeps *the API never recomputes a score* auditable; the individual refusals are tested but the rule itself rests on review. Fix: an AST scan asserting no module under `apps/api` imports `oxbow.(scoring|models|quant|backtest)`, in the shape of `test_rules_are_not_allowed_to_import_adapters_or_http`.

17. **GBM handed a training slice with no positives** — model; `packages/pipeline/oxbow/models/gbm.py:191`; the user sees: refusal: it would otherwise emit noise — `models/gbm.py:191` refuses rather than emitting noise. Fix: `fit_gbm` over an all-zero `y_train`, asserting the refusal names the positive count.

18. **early-stopping slice has zero positives** — model; `packages/pipeline/oxbow/models/gbm.py:196`; the user sees: refusal: pr-auc undefined, stopping would run to n_estimators — `models/gbm.py:196` refuses, because validation PR-AUC is undefined on that slice and early stopping would silently run to `n_estimators`. Fix: a positive training slice paired with an all-negative stopping slice.

19. **band fit handed no positives** — model; `packages/pipeline/oxbow/scoring/bands.py:221`; the user sees: refusal: bands are cut by observed rate, nothing to cut on — `scoring/bands.py:221` refuses to invent boundaries when nothing was observed to cut on. Fix: `fit_bands` over an all-negative validation slice, asserting the refusal rather than five equal-width bands.

20. **WOE denominators collapsed** — model; `packages/pipeline/oxbow/scoring/binning.py:165`; the user sees: refusal: the feature carries no information value — `scoring/binning.py:165` refuses a feature carrying no information value instead of publishing zero points for it. Fix: a single-class feature frame through `_smoothed_woe_iv`.

21. **value outside every fitted bin edge** — model; `packages/pipeline/oxbow/scoring/binning.py:814`; the user sees: refusal: the table is not exhaustive — `scoring/binning.py:814` refuses rather than clamping a value into the nearest bin, which would publish points the table never earned. Fix: `assign_numeric_bin` with a value beyond the last edge.

22. **fusion inputs do not match model.yaml** — model; `packages/pipeline/oxbow/models/fusion.py:150`; the user sees: refusal: coefficients would be labelled with the wrong channels — `models/fusion.py:150` refuses a channel-name mismatch that would print fitted coefficients under the wrong labels. Fix: a matrix of `p_scorecard` and `p_anomaly` against a config declaring `p_gbm`.

23. **degree count disagrees with the traversal adjacency** — graph; `packages/pipeline/oxbow/graph/build.py:488`; the user sees: refusal naming the disagreeing accounts — `graph/build.py:488`, `:497` and `:506` refuse the three ways the node table and the indexed legs can disagree, and none is driven. Fix: a frame whose precomputed degree differs from the legs by one, asserting the refusal names the account.

24. **graph artifact loaded without its manifest** — graph; `packages/pipeline/oxbow/graph/persist.py:129`; the user sees: refusal: integrity cannot be checked — `graph/persist.py:129` refuses to guess which Parquet files belong to an artifact, because a manifest-less read cannot be checked for truncation. Fix: delete `manifest.json` from a written artifact and assert the refusal.

25. **artifact written by a newer ARTIFACT_VERSION** — graph; `packages/pipeline/oxbow/graph/persist.py:136`; the user sees: refusal: reconcile deliberately, not by accident — `graph/persist.py:136` refuses to read forward by accident. Fix: rewrite the manifest's `artifact_version` past this build's and assert the refusal names both.

26. **community input references a node outside the population** — graph; `packages/pipeline/oxbow/graph/communities.py:91`; the user sees: refusal: a silent skip would move the partition — `graph/communities.py:91` refuses rather than skipping, because a silent skip moves the partition without saying so. Fix: a pair edge naming an unknown node key.

27. **community seed disagrees with the run seed** — graph; `packages/pipeline/oxbow/graph/settings.py:264`; the user sees: refusal: one seed everywhere — `graph/settings.py:264` enforces the one-seed-everywhere rule. Fix: a config with `graph.community.seed` moved, asserting the refusal quotes both seeds.

28. **empty event frame** — graph; `packages/pipeline/oxbow/graph/events.py:112`; the user sees: refusal: no-data and found-nothing are different claims — `graph/events.py:112` refuses an empty frame so a run that scored nothing cannot read as a run that found nothing. Fix: `require_events` on an empty frame; the ingest sibling already has `test_empty_batch_is_an_error_not_an_empty_graph`.

29. **evidence value that is not numeric** — rules; `packages/pipeline/oxbow/rules/hits.py:590`; the user sees: typeerror naming the rule that filed it — `rules/hits.py:590 _number` raises a `TypeError` naming the rule that filed a non-numeric observation. Fix: a hit whose evidence carries a string count.

30. **a hit emitted with no signature** — rules; `packages/pipeline/oxbow/rules/hits.py:95`; the user sees: refusal: signature-grain dedup is the only guard — `rules/hits.py:95` refuses a signature-less hit, because signature-grain dedup is the only guard against counting one pattern once per pair and once per day. Fix: construct a `RuleHit` with an empty signature.

31. **empty event frame evaluated by the rules engine** — rules; `packages/pipeline/oxbow/rules/events.py:253`; the user sees: refusal: an empty frame and a quiet corpus both score zero — `rules/events.py:253` refuses, on the ground that an empty frame and a quiet corpus both report zero hits and only one of them is a result. Fix: `evaluate_rules` over an empty frame.

32. **ceiling configured at or below the floor** — rules; `packages/pipeline/oxbow/rules/settings.py:501`; the user sees: config refused: every rule is simultaneously too hot and dead — `rules/settings.py:501` refuses an inverted band, which would make every rule simultaneously too hot and dead. Fix: a `rules.yaml` with the two values swapped.

33. **no non-zero amount inside the fit window** — rules; `packages/pipeline/oxbow/rules/thresholds.py:70`; the user sees: refusal naming the window — `rules/thresholds.py:70` refuses to hand a rule a tau it never measured. Fix: an all-zero-amount window through `fit_tau`.

34. **AUROC over a single-class slice** — backtest; `packages/pipeline/oxbow/backtest/metrics.py:76`; the user sees: valueerror naming the undefined quantity — `backtest/metrics.py:76` raises where the PR-AUC sibling at `metrics.py:54` has a named-skip test. Fix: a single-class call asserting the message, then decide whether the harness should skip the fold instead.

35. **bootstrap resample retains no positive** — backtest; `packages/pipeline/oxbow/backtest/metrics.py:309`; the user sees: refusal: the interval would be an artifact of the sampler — `backtest/metrics.py:309` refuses an interval that would be an artifact of the sampler. Fix: a one-positive frame with a seed that drops it in every draw.

36. **empty test window** — backtest; `packages/pipeline/oxbow/backtest/harness.py:314`; the user sees: refusal: a fold with nothing to score is a config fault — `backtest/harness.py:314` refuses a fold with nothing to score as a config fault. Fix: a fold whose test mask is empty; the mask-length and overlap siblings are already driven by `tests/unit/test_p6_fold_masks.py`.

37. **fold provider yields nothing** — backtest; `packages/pipeline/oxbow/backtest/harness.py:708`; the user sees: refusal to report an empty run — `backtest/harness.py:708` refuses to report an empty run. Fix: a provider returning no slices, asserting the refusal rather than a zero-metric report.

38. **CP-SAT returns neither optimum nor feasibility** — quant; `packages/pipeline/oxbow/quant/allocate.py:557`; the user sees: refusal: the empty set is always feasible, so the model is wrong — `quant/allocate.py:557` treats that status as a model bug rather than a degradation. Fix: monkeypatch `solver.StatusName` to an infeasible status and assert `AllocationError`, not a greedy fallback.

39. **solver fault requested through a policy that has no budget** — quant; `packages/pipeline/oxbow/quant/allocate.py:695`; the user sees: refusal: density and baseline are single passes — `quant/allocate.py:695` refuses a `deadline_ms` on the density and baseline policies, which are single passes with nothing to miss. Fix: that call, plus the same through `apps/api/schemas/policy.py:155`.

40. **priced accounts with no exposure** — quant; `packages/pipeline/oxbow/quant/ev.py:424`; the user sees: refusal: a missing e_i is a window question, not a zero — `quant/ev.py:424` refuses rather than pricing an account at `E_i = 0`, which is the exposure degeneracy this project documents in `ECONOMICS_CARD.md`. Fix: a probability set with one account absent from the exposure map.

41. **isolated account asked for a network exposure interval** — quant; `packages/pipeline/oxbow/quant/monte_carlo.py:382`; the user sees: refusal: a degenerate interval is not an answer — `quant/monte_carlo.py:382` refuses a degenerate interval for a seed with no internal edges. Fix: a single-node component, asserting the refusal names the seed.

42. **cross-currency min() in the exposure cap** — quant; `packages/pipeline/oxbow/quant/exposure.py:284`; the user sees: refusal naming both currencies — `quant/exposure.py:284` refuses a cap taken across two currencies, because `min()` of two incommensurable totals is not a bound. Fix: an account whose inflow and outflow differ in currency; the pricing sibling is covered by `test_pricing_refuses_a_cross_currency_exposure`.

43. **ordering two currencies against each other** — money; `packages/pipeline/oxbow/quant/money.py:119`; the user sees: refusal: a sort that mixes them invents a rate — `quant/money.py:119` refuses a comparison that would silently invent an FX rate. Fix: a cross-currency `<` asserting the message -- addition is tested, ordering is not.

44. **economics.yaml and pipeline.yaml disagree on exposure** — money; `packages/pipeline/oxbow/quant/economics.py:599`; the user sees: refusal naming both files — `quant/economics.py:599` refuses two meanings for one money word. Fix: move `window_hours` in pipeline.yaml and assert `_cross_check` names both files; the seed sibling is covered by `test_window_and_seed_must_agree_with_pipeline_yaml`.

45. **ULID entropy of the wrong width** — identity; `packages/pipeline/oxbow/identity.py:108`; the user sees: refusal, never a shorter random — `identity.py:108` refuses short randomness rather than padding it. Fix: `new_ulid` with an eight-byte entropy field, asserting `IdentityError`.

46. **canonical events produced zero but rows quarantined: no window to declare** — ingest; `packages/pipeline/oxbow/ingest/ibm_aml.py:1365`; the user sees: refusal naming the quarantine reasons — `ingest/ibm_aml.py:1365` refuses rather than landing a run with no window, and names the quarantine reasons that caused it. Fix: a batch whose every row fails a row-level guard.

47. **request arrives before the lifespan completed** — api; `apps/api/deps.py:729`; the user sees: 503: a startup failure, not a data failure — `apps/api/deps.py:729` separates a startup failure from a data failure, which is the difference between retrying and rebuilding. Fix: resolve a dependency against an app whose lifespan never ran, asserting 503 with that wording.

48. **unrecognised `OXBOW_WAREHOUSE` value** — api; `apps/api/deps.py:711`; the user sees: boot refused rather than defaulting to a backend — `apps/api/deps.py:711` refuses rather than defaulting, because silently choosing a backend changes what a run means. Fix: a parameterised call into `_warehouse_choice` with `pg`, the empty string and a wrong case.

49. **CORS wildcard with credentials on** — api; `apps/api/main.py:339`; the user sees: boot refused: any page could read the api as the user — `apps/api/main.py:339` refuses to boot, since that pairing lets any page read the API as the signed-in user. Fix: monkeypatch `OXBOW_CORS_ORIGINS` to the wildcard with credentials enabled and assert `create_app()` raises.

50. **run provenance this deployment will not stream** — api; `apps/api/events.py:379`; the user sees: 503: an unlabelled run cannot be told from a fabricated one — `apps/api/events.py:379` refuses to stream an unlabelled run, because it cannot be told apart from a fabricated one. Fix: a run row whose `provenance` is null, asserting 503.

51. **SSE `Last-Event-ID` present but not an integer** — api; `apps/api/events.py:111`; the user sees: 400 naming the value, never a silent restart from 0 — `apps/api/events.py:111` answers 400 instead of restarting the whole history at 0, which would surface as duplicate stage rows. Fix: a unit call into `read_cursor` with a non-integer, an empty and a negative value; the HTTP-level resume is covered by `test_sse_frames_are_exempt_from_the_envelope_and_resume_without_duplicates`.

52. **case id deep-linked after a warehouse reload** — demo; `apps/api/routers/cases.py:294`; the user sees: 404 that says the id may predate the reload — `apps/api/routers/cases.py:294` turns a stale case id into a 404 that explains the reload, and it is the first thing a reviewer reaches by clicking an old link. Fix: one request for an unknown `case_id`, asserting the problem document carries that sentence; the unregistered-path sibling is already tested.

---

53. **a second producer of rendered currency strings** — money; `packages/pipeline/oxbow/quant/money.py:240`; the user sees nothing: the single-producer rule is only a comment, so a second renderer can appear and nothing fails. Fix: a gate that asserts one producer of rendered currency, or fold the second into `to_major_text`.

54. **erasure has no route, though the role that owns it is advertised** — api; `apps/api/routers/auth.py:201`; an admin sees an `erasure` capability with nothing to call. Fix: either expose the erasure path the `admin` role already advertises, or remove the capability from the advertised set so the UI cannot promise it.

55. **source and config comments cite tests by names that do not exist** — docs-gate; `docs/FAILURE-MODES.md:1`; a reviewer is pointed at a test that was never written, which is the phantom-citation failure this register exists to catch. Fix: grep the cited `test_*` names against `ast`-collected test names in the same gate that checks this register, so a citation cannot rot quietly.
## Register

| area | failure | handled at | what the user sees | test | status |
| --- | --- | --- | --- | --- | --- |
| ingest | duplicate txn_id carrying a conflicting payload is quarantined, never merged | packages/pipeline/oxbow/contracts/canonical_v1.py:466 | quarantine count with the reason, batch still lands | test_dup_txn_conflicting_quarantined | handled+tested |
| ingest | empty batch reads zero rows and is refused rather than a clean run | packages/pipeline/oxbow/ingest/ibm_aml.py:902 | run fails naming `ingest.allow_empty_batch` | test_empty_batch_errors | handled+tested |
| ingest | config permits silent coercions | packages/pipeline/oxbow/ingest/run.py:613 | ingest refuses to start | test_zero_silent_coercions | handled+tested |
| ingest | source id not declared in config/sources.yaml | packages/pipeline/oxbow/ingest/run.py:537 | refusal naming the declared ids | test_sources_declare_licenses_and_refuse_undeclared_ones | handled+tested |
| ingest | source declared but marked not ingestable | packages/pipeline/oxbow/ingest/run.py:547 | refusal carrying the source's own reason | test_every_ingestable_source_states_its_obligation | handled+tested |
| ingest | declared file absent or has no recorded digest | packages/pipeline/oxbow/ingest/run.py:295 | refusal naming the attempted paths | test_retrieved_files_declare_a_real_sha256 | handled+tested |
| ingest | batch SHA-256 disagrees with the pin | packages/pipeline/oxbow/ingest/ibm_aml.py:1347 | batch rejected whole, recorded vs actual digest shown | test_read_manifest_rejects_bytes_that_disagree_with_the_pin | handled+tested |
| ingest | rows read disagree with the file's byte-level line count | packages/pipeline/oxbow/ingest/ibm_aml.py:1356 | batch rejected as truncated | test_read_manifest_refuses_a_row_count_that_disagrees_with_the_bytes | handled+tested |
| ingest | canonical events produced zero but rows quarantined: no window to declare | packages/pipeline/oxbow/ingest/ibm_aml.py:1365 | refusal naming the quarantine reasons | none | handled, untested |
| ingest | typology join artifact missing, so every label_typology would be null | packages/pipeline/oxbow/ingest/ibm_aml.py:358 | refusal naming the build script | test_typology_comes_from_the_join_artifact_not_a_label_column | handled+tested |
| ingest | one row ordinal appears in two attempt blocks (join would fan out) | packages/pipeline/oxbow/ingest/ibm_aml.py:389 | refusal, not a fan-out | test_the_join_artifact_is_shape_checked_before_it_is_used | handled+tested |
| ingest | annotated ordinals no longer line up with the labels | packages/pipeline/oxbow/ingest/ibm_aml.py:1006 | refusal naming the count of mislabelled rows | test_the_full_corpus_card_and_the_alignment_invariant | handled+tested |
| ingest | mask and declared contract disagree about which rows survive | packages/pipeline/oxbow/ingest/ibm_aml.py:958 | refusal naming both counts | test_the_contract_is_the_authority_over_the_mask | handled+tested |
| ingest | manifest row count disagrees with usable rows | packages/pipeline/oxbow/adapters/file/source.py:132 | batch rejected whole | test_manifest_mismatch_rejects_batch | handled+tested |
| ingest | quarantined record carrying no reason | packages/pipeline/oxbow/adapters/file/source.py:141 | counted as `schema_violation`, never swallowed | test_quarantine_counts_a_reasonless_record_instead_of_swallowing_it | handled+tested |
| ingest | required canonical column null | packages/pipeline/oxbow/contracts/canonical_v1.py:451 | contract failure naming column and count | test_a_blank_line_at_eof_is_quarantined_as_a_hole_in_a_required_column | handled+tested |
| ingest | undeclared extra column treated as schema drift | packages/pipeline/oxbow/contracts/canonical_v1.py:399 | contract failure naming the column | test_unknown_column_fails_closed | handled+tested |
| ingest | a typology-bearing source whose join produced nothing | packages/pipeline/oxbow/contracts/canonical_v1.py:547 | refusal: a dead join is not a clean corpus | test_typology_bearing_source_allows_background_rows_but_not_a_dead_join | handled+tested |
| ingest | a source with no taxonomy carrying a typology value | packages/pipeline/oxbow/contracts/canonical_v1.py:558 | refusal: an invented label | test_label_provenance_required | handled+tested |
| ingest | a batch spanning two corpora under one balance assertion | packages/pipeline/oxbow/contracts/canonical_v1.py:583 | refusal to average across sources | test_balance_columns_are_all_present_or_all_absent | handled+tested |
| ingest | CSV line whose amount is not an integer minor unit | packages/pipeline/oxbow/adapters/file/source.py:185 | line number and field named | test_a_decimal_amount_is_refused_not_rounded | handled+tested |
| time | corpus declares no zone, so the zone is an assumption in config | packages/pipeline/oxbow/ingest/ibm_aml.py:326 | assumption printed beside every local-time figure | test_the_source_timezone_is_declared_as_an_assumption_not_a_fact | handled+tested |
| time | a third timestamp shape appears in the file | packages/pipeline/oxbow/ingest/ibm_aml.py:333 | batch quarantined, format named | test_a_third_timestamp_shape_is_quarantined_and_loses_the_batch | handled+tested |
| time | an instant in the future | packages/pipeline/oxbow/ingest/ibm_aml.py:1258 | row quarantined, never shifted into range | test_a_future_instant_is_quarantined_not_shifted | handled+tested |
| time | intra-step offset expansion not stable across runs | packages/pipeline/oxbow/ingest/run.py:415 | run refused when the offset is unconfigured | test_step_expansion_stable | handled+tested |
| time | graph handed a naive timestamp | packages/pipeline/oxbow/graph/events.py:192 | refusal: a hidden assumption about a zone | test_naive_and_non_utc_timestamps_are_refused | handled+tested |
| time | `event_ts_utc` carries a zone other than UTC | packages/pipeline/oxbow/graph/events.py:199 | refusal: convert in ingest, where it is auditable | test_naive_and_non_utc_timestamps_are_refused | handled+tested |
| time | rules see a `local_hour` outside 0..23 | packages/pipeline/oxbow/rules/events.py:284 | refusal naming the double-conversion cause | test_odd_hour_uses_local_hour_and_not_the_utc_instant | handled+tested |
| time | deployment timezone not resolvable by the host tz database | packages/pipeline/oxbow/ingest/canonical.py:348 | ingest refuses rather than assuming a zone | test_config_rejects_a_missing_or_unparseable_value | handled+tested |
| time | rows sharing a minute ordered ambiguously | packages/pipeline/oxbow/ingest/ibm_aml.py:1078 | total order (instant, txn_id) enforced | test_rows_sharing_a_minute_are_ordered_by_txn_id | handled+tested |
| time | API served instant that does not parse | apps/api/routers/meta.py:916 | 503 naming the field: the timestamp is the evidence | test_every_served_value_traces_to_something_the_server_holds | handled+tested |
| money | float money arrives at a port | packages/pipeline/oxbow/ports/source.py:173 | refusal naming the key and the value | test_money_rejects_a_float_amount | handled+tested |
| money | scientific-notation amount | packages/pipeline/oxbow/ingest/canonical.py:292 | expanded exactly and counted, never rounded | test_scientific_amount_is_expanded_and_counted | handled+tested |
| money | negative amount | packages/pipeline/oxbow/ingest/canonical.py:288 | refusal; a reversal is a typed event, not a sign | test_negative_money_and_null_keys_fail_closed | handled+tested |
| money | non-finite amount | packages/pipeline/oxbow/ingest/canonical.py:294 | refusal naming the text | test_money_parsing_is_exact_across_both_forms | handled+tested |
| money | summing two currencies | packages/pipeline/oxbow/quant/money.py:105 | refusal: there is no implicit FX | test_figures_cannot_marry_two_currencies | handled+tested |
| money | ordering two currencies against each other | packages/pipeline/oxbow/quant/money.py:119 | refusal: a sort that mixes them invents a rate | none | handled, untested |
| money | aggregate spans several currencies | packages/pipeline/oxbow/graph/events.py:286 | refusal listing the currencies | test_cross_currency_totals_are_never_summed | handled+tested |
| money | GOAML transaction with a missing amount | packages/pipeline/oxbow/adapters/goaml/xml.py:90 | refusal: a zero would state a payment of nothing | test_a_transaction_without_an_integer_amount_is_refused_not_filed_as_zero | handled+tested |
| money | GOAML currency `unknown` rather than a code | packages/pipeline/oxbow/adapters/goaml/xml.py:97 | refusal: `XXX` means no currency | test_a_transaction_without_a_currency_is_refused_not_filed_as_no_currency | handled+tested |
| money | minor-units-per-major not an exact power of ten | packages/pipeline/oxbow/quant/money.py:233 | refusal: money would render at a guessed scale | test_a_base_that_is_not_a_power_of_ten_is_refused_not_rounded | handled+tested |
| money | API money field arrives null | apps/api/readmodel.py:256 | 503: refuses to render an absent amount as zero | test_a_total_without_a_stored_currency_is_refused_not_defaulted | handled+tested |
| money | currency figure rendered without its assumption block | packages/pipeline/oxbow/quant/economics.py:796 | refusal naming the config file to load | test_currency_requires_assumptions | handled+tested |
| money | figure valued over a partial sensitivity band | packages/pipeline/oxbow/quant/economics.py:804 | refusal: a point estimate hides the deciding assumption | test_a_figure_missing_a_band_coordinate_is_refused | handled+tested |
| money | config carries an assumption no consumer reads | packages/pipeline/oxbow/quant/economics.py:587 | load refuses: an unread assumption still gets believed | test_load_refuses_an_assumption_no_consumer_reads | handled+tested |
| money | economics.yaml and pipeline.yaml disagree on exposure | packages/pipeline/oxbow/quant/economics.py:599 | refusal naming both files | none | handled, untested |
| money | alert class has no configured review minutes | packages/pipeline/oxbow/quant/economics.py:323 | refusal rather than a guessed default | test_missing_alert_class_is_refused_not_defaulted | handled+tested |
| money | review-minute floor configured at zero | packages/pipeline/oxbow/quant/economics.py:137 | load refused: EV density would divide by zero | test_zero_review_minutes_floor_is_rejected_at_load | handled+tested |
| money | a second producer of rendered currency strings | packages/pipeline/oxbow/quant/money.py:240 | nothing: the single-producer rule is a comment | none | unguarded |
| identity | run salt shorter than 16 characters | packages/pipeline/oxbow/ingest/canonical.py:118 | refusal: a committed salt can be brute-forced | test_run_salt_must_come_from_the_environment | handled+tested |
| identity | batch_id not 16 hex characters | packages/pipeline/oxbow/ingest/canonical.py:124 | refusal naming the length | test_txn_id_is_stable_across_batch_sizes_limits_and_batch_ids | handled+tested |
| identity | run_id is not a 26-character ULID | packages/pipeline/oxbow/identity.py:134 | refusal at both ends of the wire | test_run_id_must_be_a_ulid | handled+tested |
| identity | ULID entropy of the wrong width | packages/pipeline/oxbow/identity.py:108 | refusal, never a shorter random | none | handled, untested |
| identity | same raw account id across two sources merges into one node | packages/pipeline/oxbow/ingest/canonical.py:203 | refusal: the namespace is mandatory | test_no_cross_source_node_merge | handled+tested |
| identity | one account number under two banks collapses to one node | packages/pipeline/oxbow/ingest/ibm_aml.py:781 | two nodes, keyed on the pair | test_the_same_account_number_under_two_banks_is_two_nodes | handled+tested |
| identity | rotating the salt silently re-uses old keys | packages/pipeline/oxbow/ingest/canonical.py:402 | every key changes and the test says so | test_account_key_is_stable_across_runs_with_a_fixed_salt_and_changes_without_it | handled+tested |
| identity | raw bank/account identifier leaves the adapter | packages/pipeline/oxbow/ingest/ibm_aml.py:912 | refusal: pseudonyms only downstream of ingest | test_the_raw_bank_and_account_ids_never_leave_the_adapter | handled+tested |
| graph | event frame larger than the interactive target | packages/pipeline/oxbow/graph/build.py:130 | refusal: never a truncated graph that looks complete | test_graph_size_limit_refuses_instead_of_degrading | handled+tested |
| graph | empty event frame | packages/pipeline/oxbow/graph/events.py:112 | refusal: no-data and found-nothing are different claims | none | handled, untested |
| graph | degree count disagrees with the traversal adjacency | packages/pipeline/oxbow/graph/build.py:488 | refusal naming the disagreeing accounts | none | handled, untested |
| graph | self-transfer double-counted as two legs | packages/pipeline/oxbow/graph/build.py:506 | refusal naming the predicted leg count | test_self_transfers_are_kept_and_visible | handled+tested |
| graph | rail/supernode walked as an ordinary counterparty | packages/pipeline/oxbow/graph/cycles.py:1 | rails block traversal and do not inflate fan | test_rail_blocks_traversal | handled+tested |
| graph | supernode degree distorts the percentile cut | packages/pipeline/oxbow/graph/model.py:437 | refusal when the percentile resolves to null | test_rail_degree_gap_survives_the_p3a_percentile_formula | handled+tested |
| graph | cycle search hits its per-component budget | packages/pipeline/oxbow/graph/cycles.py:208 | run completes and names `cycle_search_truncated` | test_per_component_visit_budget_stops_the_search_early | handled+tested |
| graph | cycle search exceeds its wall clock | packages/pipeline/oxbow/graph/cycles.py:122 | timeout flag set instead of the pipeline hanging | test_timeout_flag_is_set_instead_of_the_pipeline_hanging | handled+tested |
| graph | a cycle-budget truncation changes R4 and must surface in the evidence | packages/pipeline/oxbow/graph/model.py:223 | nothing: the flag is in the artifact and reaches no response | none | handled, untested |
| graph | community input references a node outside the population | packages/pipeline/oxbow/graph/communities.py:91 | refusal: a silent skip would move the partition | none | handled, untested |
| graph | neighbourhood asked for an account the graph never saw | packages/pipeline/oxbow/graph/subgraph.py:126 | refusal, not an empty neighbourhood | test_neighbourhood_defaults_to_configured_hops_and_names_unknown_accounts | handled+tested |
| graph | meta-node for a community with no members | packages/pipeline/oxbow/graph/subgraph.py:104 | refusal: a stale artifact, not an empty cluster | test_a_collapsed_community_with_no_stored_row_is_refused | handled+tested |
| graph | graph artifact loaded without its manifest | packages/pipeline/oxbow/graph/persist.py:129 | refusal: integrity cannot be checked | none | handled, untested |
| graph | artifact file hash disagrees with the manifest | packages/pipeline/oxbow/graph/persist.py:158 | refusal: rebuild is the only safe answer | test_persisted_graph_that_disagrees_with_its_manifest_is_refused | handled+tested |
| graph | artifact written by a newer ARTIFACT_VERSION | packages/pipeline/oxbow/graph/persist.py:136 | refusal: reconcile deliberately, not by accident | none | handled, untested |
| graph | community algorithm other than leiden | packages/pipeline/oxbow/graph/settings.py:257 | refusal: falling back would change the partition silently | test_a_manifest_without_community_algorithm_and_seed_raises_rather_than_naming_an_algorithm | handled+tested |
| graph | community seed disagrees with the run seed | packages/pipeline/oxbow/graph/settings.py:264 | refusal: one seed everywhere | none | handled, untested |
| graph | sort keys other than (instant, txn_id) | packages/pipeline/oxbow/graph/settings.py:236 | refusal: never order on local_hour | test_non_unique_sort_keys_are_refused_by_config | handled+tested |
| rules | a rule fires on more than a third of accounts | packages/pipeline/oxbow/rules/hits.py:474 | run refused with a derived threshold suggestion | test_hit_rate_ceiling_fails_the_run_with_a_suggestion | handled+tested |
| rules | a rule fires on nothing | packages/pipeline/oxbow/rules/hits.py:522 | removed with a reason and still reported in the panel | test_dead_rule_is_removed_with_a_reason_and_excluded_from_scoring | handled+tested |
| rules | ceiling configured at or below the floor | packages/pipeline/oxbow/rules/settings.py:501 | config refused: every rule is simultaneously too hot and dead | none | handled, untested |
| rules | rule declared in config with no implementation | packages/pipeline/oxbow/rules/registry.py:139 | refusal: an unwired rule is the invisible kind of dead | test_registry_covers_the_config_exactly | handled+tested |
| rules | config carries a knob the layer does not read | packages/pipeline/oxbow/rules/settings.py:184 | refusal naming the stray keys | test_settings_refuse_an_unknown_knob_rather_than_ignoring_it | handled+tested |
| rules | empty event frame evaluated by the rules engine | packages/pipeline/oxbow/rules/events.py:253 | refusal: an empty frame and a quiet corpus both score zero | none | handled, untested |
| rules | tau fitted over an empty population | packages/pipeline/oxbow/rules/thresholds.py:45 | refusal: a zero threshold every amount clears | test_tau_bracket_holds | handled+tested |
| rules | no non-zero amount inside the fit window | packages/pipeline/oxbow/rules/thresholds.py:70 | refusal naming the window | none | handled, untested |
| rules | no histogram mode above the support floor | packages/pipeline/oxbow/rules/thresholds.py:134 | refusal, not a fallback to a round number | test_r5_threshold_can_be_derived_from_the_histogram_mode | handled+tested |
| rules | a hit emitted with no signature | packages/pipeline/oxbow/rules/hits.py:95 | refusal: signature-grain dedup is the only guard | none | handled, untested |
| rules | severity outside [0,1] | packages/pipeline/oxbow/rules/hits.py:89 | refusal naming the rule | test_an_unknown_severity_is_refused_at_construction | handled+tested |
| rules | one rule in two overlap groups | packages/pipeline/oxbow/rules/settings.py:518 | refusal: "count the group once" becomes ambiguous | test_overlapping_rules_are_counted_once_per_account | handled+tested |
| rules | evidence value that is not numeric | packages/pipeline/oxbow/rules/hits.py:590 | TypeError naming the rule that filed it | none | handled, untested |
| rules | a fitted threshold with no named window | packages/pipeline/oxbow/rules/thresholds.py:134 | refusal: indistinguishable from tuning on the test set | test_threshold_fit_is_pinned_to_the_window_that_produced_it | handled+tested |
| model | validation positives below the calibration floor | packages/pipeline/oxbow/models/calibration.py:242 | probabilities ship labelled `uncalibrated` with the count | test_calibration_refused_below_floor | handled+tested |
| model | config sets a below-floor action other than refuse | packages/pipeline/oxbow/models/config.py:463 | load refused: an uncalibrated p multiplied by money is a wrong figure | test_calibration_refused_below_floor | handled+tested |
| model | method claims isotonic but carries no step function | packages/pipeline/oxbow/models/calibration.py:251 | refusal: a label with no fitted object behind it | test_a_refused_calibration_lands_labelled_uncalibrated_and_says_why | handled+tested |
| model | unseen category with no unseen bin | packages/pipeline/oxbow/scoring/binning.py:845 | refusal: mapping it to the mode would invent points | test_unseen_category_handled | handled+tested |
| model | null arrives with no missing bin in the table | packages/pipeline/oxbow/scoring/binning.py:798 | refusal: missing must always be a bin | test_missing_is_a_bin_not_a_mean | handled+tested |
| model | value outside every fitted bin edge | packages/pipeline/oxbow/scoring/binning.py:814 | refusal: the table is not exhaustive | none | handled, untested |
| model | WOE denominators collapsed | packages/pipeline/oxbow/scoring/binning.py:165 | refusal: the feature carries no information value | none | handled, untested |
| model | non-finite WOE even after smoothing | packages/pipeline/oxbow/scoring/binning.py:176 | refusal naming the bin | test_no_infinite_woe | handled+tested |
| model | band fit handed no positives | packages/pipeline/oxbow/scoring/bands.py:221 | refusal: bands are cut by observed rate, nothing to cut on | none | handled, untested |
| model | band table not monotone in observed bad rate | packages/pipeline/oxbow/scoring/bands.py:344 | refusal printing the measured rates | test_monotonic_trend_enforced | handled+tested |
| model | scoring frame's feature-spec hash differs from the harness | packages/pipeline/oxbow/scoring/frame.py:130 | refusal: scoring with columns the model never saw | test_feature_hash_mismatch_refuses | handled+tested |
| model | a fold scored on a different spec than it fitted | packages/pipeline/oxbow/models/scorer.py:362 | refusal: one fold cannot be one number | test_a_fold_whose_rows_disagree_on_the_spec_hash_is_refused | handled+tested |
| model | two digests in one frame | packages/pipeline/oxbow/models/scorer.py:362 | refusal rather than picking one | test_a_frame_with_two_digests_is_refused | handled+tested |
| model | integer categorical offered as a category index | packages/pipeline/oxbow/models/inputs.py:338 | refusal naming the column and the index bound | test_an_oversized_integer_category_code_is_refused_not_allocated | handled+tested |
| model | GBM handed a training slice with no positives | packages/pipeline/oxbow/models/gbm.py:191 | refusal: it would otherwise emit noise | none | handled, untested |
| model | early-stopping slice has zero positives | packages/pipeline/oxbow/models/gbm.py:196 | refusal: PR-AUC undefined, stopping would run to n_estimators | none | handled, untested |
| model | lightgbm's own metric disagrees with the repo's | packages/pipeline/oxbow/models/gbm.py:286 | refusal naming both values to eight places | none | handled, untested |
| model | fusion inputs do not match model.yaml | packages/pipeline/oxbow/models/fusion.py:150 | refusal: coefficients would be labelled with the wrong channels | none | handled, untested |
| model | fusion validation slice has no positives | packages/pipeline/oxbow/models/fusion.py:160 | refusal: the fold should have been skipped by name | test_zero_positive_fold_reported | handled+tested |
| model | solver returns a negative channel weight | packages/pipeline/oxbow/models/fusion.py:189 | refusal: the fused score stops being explicable | test_exact_solver_refuses_a_worse_objective_than_the_heuristic | handled+tested |
| model | SHAP unavailable for a row | packages/pipeline/oxbow/models/explain.py:147 | scorecard-explained, labelled as a different explanation | test_shap_fallback_to_points | degraded-not-broken OK |
| model | mlflow tracking URI unreachable | packages/pipeline/oxbow/models/registry.py:103 | file-store lineage with `tracking_degraded` stamped | none | handled, untested |
| model | scoring a row that produced no probability | packages/pipeline/oxbow/models/run.py:1522 | refusal: p=0 would move the account silently | none | handled, untested |
| model | a rule-hit join misses a scored row | packages/pipeline/oxbow/models/inputs.py:228 | refusal: a missing hit is not a zero hit | none | handled, untested |
| model | drift action disagrees between model.yaml and scorecard.yaml | packages/pipeline/oxbow/models/run.py:462 | refusal naming both declarations | test_psi_action_degrades_scoring | handled+tested |
| model | anomaly normalisation fitted on the scored population | packages/pipeline/oxbow/models/anomaly.py:141 | config refused: that is a leak, not a normalisation | none | handled, untested |
| model | fold fitted on evaluation rows | packages/pipeline/oxbow/models/run.py:222 | refusal: the rejection trigger, not a mistake | none | handled, untested |
| model | a fold degraded to scorecard-and-rules only | packages/pipeline/oxbow/models/run.py:817 | labelled degraded fold, `p_gbm` nulled rather than borrowed | test_a_run_whose_every_fold_degraded_refuses_rather_than_nulls_the_gbm_column | handled+tested |
| model | queue tie-break left to the sort's mercy | packages/pipeline/oxbow/models/run.py:503 | refusal when score or tie-break column is absent | test_tie_break_deterministic | handled+tested |
| quant | zero analyst capacity | packages/pipeline/oxbow/quant/allocate.py:605 | empty queue plus the forgone value, never a blank | test_zero_capacity_returns_forgone_value | handled+tested |
| quant | negative capacity passed for a zero-budget case | packages/pipeline/oxbow/quant/allocate.py:250 | refusal: a negative budget is not the documented zero | test_negative_capacity_is_an_error_and_zero_is_not | handled+tested |
| quant | every candidate EV negative | packages/pipeline/oxbow/quant/allocate.py:602 | recommends nothing, with the reason | test_all_negative_ev_recommends_nothing | handled+tested |
| quant | CP-SAT misses its deadline | packages/pipeline/oxbow/quant/allocate.py:554 | greedy packing labelled as the fallback | test_solver_timeout_falls_back | handled+tested |
| quant | CP-SAT returns neither optimum nor feasibility | packages/pipeline/oxbow/quant/allocate.py:557 | refusal: the empty set is always feasible, so the model is wrong | none | handled, untested |
| quant | solver fault requested through a policy that has no budget | packages/pipeline/oxbow/quant/allocate.py:695 | refusal: density and baseline are single passes | none | handled, untested |
| quant | gap quoted against a degraded exact solve | packages/pipeline/oxbow/quant/allocate.py:811 | refusal: report the fallback label instead | test_exact_is_at_least_as_valuable_as_greedy_and_labels_the_gap | handled+tested |
| quant | greedy beats the "exact" solve | packages/pipeline/oxbow/quant/allocate.py:739 | refusal: an exact solve that loses means the model is wrong | test_exact_solver_refuses_a_worse_objective_than_the_heuristic | handled+tested |
| quant | parallel CP-SAT requested | packages/pipeline/oxbow/quant/economics.py:218 | config refused: whichever incumbent races home wins | test_parallel_cpsat_is_refused_because_it_is_not_reproducible | handled+tested |
| quant | zero-minute alert in the density denominator | packages/pipeline/oxbow/backtest/interfaces.py:252 | refusal at construction: infinite density | test_ev_density_no_zero_divide | handled+tested |
| quant | priced accounts with no exposure | packages/pipeline/oxbow/quant/ev.py:424 | refusal: a missing E_i is a window question, not a zero | none | handled, untested |
| quant | exposure with no calibrated probability | packages/pipeline/oxbow/quant/ev.py:413 | refusal rather than skipping the account | test_selecting_an_unpriced_account_is_refused | handled+tested |
| quant | frontier has no point at the operating capacity | packages/pipeline/oxbow/quant/frontier.py:158 | refusal: the sweep is supposed to force it in | test_capacity_sweep_must_contain_the_operating_point | handled+tested |
| quant | isolated account asked for a network exposure interval | packages/pipeline/oxbow/quant/monte_carlo.py:382 | refusal: a degenerate interval is not an answer | none | handled, untested |
| quant | cross-currency min() in the exposure cap | packages/pipeline/oxbow/quant/exposure.py:284 | refusal naming both currencies | none | handled, untested |
| quant | pricing across a currency gap | packages/pipeline/oxbow/quant/ev.py:217 | refusal: no implicit FX at the pricing seam | test_pricing_refuses_a_cross_currency_exposure | handled+tested |
| backtest | a fold with zero positives | packages/pipeline/oxbow/backtest/metrics.py:54 | fold skipped with a named reason, never scored as clean | test_zero_positive_fold_reported | handled+tested |
| backtest | config allowed to average a dead fold away | packages/pipeline/oxbow/models/config.py:614 | load refused: `skip_with_named_reason` is the only action | test_zero_positive_fold_reported | handled+tested |
| backtest | precision at a zero review budget | packages/pipeline/oxbow/models/evaluate.py:103 | refusal, not 0 and not 1 | test_precision_undefined_at_zero_budget_not_zero_or_one | handled+tested |
| backtest | precision over a fold with no alerts | packages/pipeline/oxbow/adapters/warehouse/landing.py:1477 | the row lands as undefined with the reason, not zero | test_a_fold_with_no_alerts_reports_undefined_precision_rather_than_zero | handled+tested |
| backtest | AUROC over a single-class slice | packages/pipeline/oxbow/backtest/metrics.py:76 | ValueError naming the undefined quantity | none | handled, untested |
| backtest | bootstrap resample retains no positive | packages/pipeline/oxbow/backtest/metrics.py:309 | refusal: the interval would be an artifact of the sampler | none | handled, untested |
| backtest | embargo shorter than the longest feature lookback | packages/pipeline/oxbow/backtest/splits.py:481 | refusal naming the longest window | test_embargo_equals_the_longest_declared_window | handled+tested |
| backtest | scored window opened at the training cutoff | packages/pipeline/oxbow/backtest/harness.py:328 | refusal: the embargo band was read by the features | test_the_scored_window_opens_at_the_test_start_never_at_the_cutoff | handled+tested |
| backtest | a row lands in two splits | packages/pipeline/oxbow/backtest/harness.py:308 | refusal naming the rows | test_a_straddle_measured_in_rows_not_masks_also_blocks | handled+tested |
| backtest | empty test window | packages/pipeline/oxbow/backtest/harness.py:314 | refusal: a fold with nothing to score is a config fault | none | handled, untested |
| backtest | fit row dated after the test start | packages/pipeline/oxbow/backtest/harness.py:321 | refusal: that is a random split | test_a_shuffled_split_is_refused | handled+tested |
| backtest | fold provider yields nothing | packages/pipeline/oxbow/backtest/harness.py:708 | refusal to report an empty run | none | handled, untested |
| backtest | entity-disjointness never measured | packages/pipeline/oxbow/backtest/harness.py:841 | refusal: publishing `false` would state an unchecked fact | test_a_fold_that_never_saw_the_account_twice_is_disjoint | handled+tested |
| backtest | corpus arrives out of as-of order | packages/pipeline/oxbow/backtest/harness.py:464 | refusal: "latest stamp" would name an arbitrary row | test_an_unordered_corpus_refuses_instead_of_choosing_an_arbitrary_latest_row | handled+tested |
| backtest | one backtest over two feature specs | packages/pipeline/oxbow/backtest/harness.py:276 | refusal naming the count of hashes | test_the_corpus_is_one_feature_spec_over_one_ordered_timeline | handled+tested |
| backtest | fold artifacts missing at report time | packages/pipeline/oxbow/backtest/run.py:1004 | non-zero exit, nothing written, the path named | test_a_fold_chain_that_raises_exits_non_zero_and_writes_nothing | handled+tested |
| backtest | flat benefit curve read as a risk failure | packages/pipeline/oxbow/backtest/metrics.py:430 | zero drawdown reported as measured zero, with the label | test_zero_drawdown_is_zero_not_missing | handled+tested |
| api | a second reviewer overwrites the first | apps/api/decisions.py:799 | 409 carrying the merge view, not a silent last-write | test_stale_expected_version_returns_409_carrying_the_merge_view | handled+tested |
| api | two decisions append to the chain at one instant | apps/api/decisions.py:325 | 409, one writer wins, nothing half-written | test_concurrent_append_gives_one_success_and_one_409 | handled+tested |
| api | audit tip moves inside the decision transaction | apps/api/decisions.py:949 | 409: the decision was not recorded and nothing was queued | test_a_stale_tip_append_is_refused_and_leaves_no_hole | handled+tested |
| api | four-eyes confirmed by its own author | apps/api/decisions.py:408 | 403: a second subject, not a second role | test_four_eyes_needs_a_different_subject_not_a_different_role | handled+tested |
| api | decision on a case with no stored economics | apps/api/decisions.py:762 | 422: a money test cannot gate on a missing figure | none | handled, untested |
| api | SSE `Last-Event-ID` present but not an integer | apps/api/events.py:111 | 400 naming the value, never a silent restart from 0 | none | handled, untested |
| api | SSE stream resumes behind the cursor | apps/api/events.py:195 | rows delivered once, in id order, with no gap | test_resume_from_a_cursor_delivers_only_later_rows | handled+tested |
| api | SSE poll would block the event loop | apps/api/events.py:210 | stream stays open without starving other routes | test_the_poll_sleeps_by_awaiting_not_by_blocking | handled+tested |
| api | run provenance this deployment will not stream | apps/api/events.py:379 | 503: an unlabelled run cannot be told from a fabricated one | none | handled, untested |
| api | subgraph requested wider than the server limit | apps/api/routers/graph.py:110 | 400 naming the hop cap and the rails reason | test_depth_cap_bounds_the_walk | handled+tested |
| api | subgraph exceeds the configured node cap | apps/api/routers/graph.py:143 | capped graph plus `truncated` and a named truncation reason | test_the_cap_is_enforced_when_the_seeds_own_community_exceeds_it | handled+tested |
| api | node cap unreadable from config | apps/api/routers/graph.py:309 | 503: the cap is a policy, not a number invented in code | test_graph_config_types_rails_and_caps_the_subgraph | handled+tested |
| api | Postgres down at boot | apps/api/deps.py:603 | boot continues, every response names the degraded component | test_healthz_answers_unauthenticated_and_labels_every_degraded_component | handled+tested |
| api | OIDC key set unreachable at boot | apps/api/deps.py:623 | 503 on authenticated routes, `/healthz` still answers | test_an_oidc_token_is_verified_against_the_issuers_own_key_and_nothing_else | handled+tested |
| api | request arrives before the lifespan completed | apps/api/deps.py:729 | 503: a startup failure, not a data failure | none | handled, untested |
| api | unrecognised `OXBOW_WAREHOUSE` value | apps/api/deps.py:711 | boot refused rather than defaulting to a backend | none | handled, untested |
| api | erasure has no route, though the role that owns it is advertised | apps/api/routers/auth.py:201 | an admin sees an `erasure` capability with nothing to call | none | unguarded |
| api | decision write on the null-file warehouse | apps/api/deps.py:199 | 503 with the outbox explanation, never a fake success | test_the_null_write_path_refuses_loudly_instead_of_answering_empty | handled+tested |
| api | job submitted with no Redis | apps/api/jobs.py:274 | 503: a job cannot be promised | test_the_orphan_sweep_refuses_to_guess_when_the_queue_cannot_answer | handled+tested |
| api | job names an unknown kind or stage | apps/api/worker.py:698 | refusal before anything runs | test_unknown_kind_and_unknown_stage_are_refused_before_anything_runs | handled+tested |
| api | stored band outside the CHECK constraint | apps/api/readmodel.py:1541 | 503: the queue cannot be trusted to render it | test_a_band_outside_the_check_constraint_is_refused_not_clamped | handled+tested |
| api | warehouse table genuinely empty | apps/api/readmodel.py:463 | 503 naming migrate and the run, not an empty list | test_an_empty_listing_is_a_result_not_a_dead_store | handled+tested |
| api | client sorts on a non-whitelisted column | apps/api/routers/common.py:53 | 400 listing the sortable columns | test_filters_narrow_the_page_and_the_total_together | handled+tested |
| api | stored row lacks a field the response declares | apps/api/routers/common.py:223 | 503: defaulting it would ship an invented value | test_a_not_null_column_that_was_never_measured_refuses_naming_its_producer | handled+tested |
| api | alert row has no usable activity window | apps/api/routers/alerts.py:378 | 400: the queue will not show a window it did not measure | none | handled, untested |
| api | queue asked for a run with no priced accounts | apps/api/routers/alerts.py:300 | 503 naming the run and the reason, not an empty table | none | handled, untested |
| api | token algorithm `none` | apps/api/security.py:202 | 401: `none` is not authentication | test_alg_none_and_the_algorithm_confusion_attack_are_refused | handled+tested |
| api | HS256 token for an RS256 issuer | apps/api/security.py:210 | 401: the algorithm comes from the issuer, not the token | test_alg_none_and_the_algorithm_confusion_attack_are_refused | handled+tested |
| api | token with none of the OXBOW roles | apps/api/security.py:318 | 401: refused, not defaulted to least-privileged | test_a_token_with_no_oxbow_role_is_refused_not_defaulted | handled+tested |
| api | unhandled exception escapes a route | apps/api/problems.py:470 | 500 as a problem document with a trace id and no stack | test_global_tier_503_is_labelled_retryable_and_500_never_leaks | handled+tested |
| api | a reachable route with no declared error shape | apps/api/problems.py:328 | caught by the schema test, not by a reviewer's memory | test_no_reachable_route_answers_with_an_undesigned_internal_error | handled+tested |
| api | CORS wildcard with credentials on | apps/api/main.py:339 | boot refused: any page could read the API as the user | none | handled, untested |
| api | dataset card measurement JSON truncated on disk | apps/api/routers/meta.py:578 | unhandled 500, not a problem document | none | unguarded |
| api | case workspace asks the watchlist whether it screened | apps/api/routers/cases.py:479 | `configured but NOT screened`, never an empty hit list | none | handled, untested |
| api | API asked to compute what the pipeline did not | apps/api/readmodel.py:1271 | the field is absent, and the served bundle names the absence | none | handled, untested |
| audit | a row edited after it was written | packages/pipeline/oxbow/audit/chain.py:255 | verification names the sequence and the digest mismatch | test_an_edited_row_fails_verification_naming_its_sequence | handled+tested |
| audit | a row deleted from the middle | packages/pipeline/oxbow/audit/chain.py:268 | reported as a gap, not as a digest mismatch | test_a_deleted_row_is_reported_as_a_gap_not_a_digest_mismatch | handled+tested |
| audit | payload value JSON does not know | packages/pipeline/oxbow/audit/chain.py:67 | refusal: serialise it at the call site, explicitly | test_an_unrepresentable_payload_value_is_refused_not_stringified | handled+tested |
| audit | naive audit timestamp | packages/pipeline/oxbow/audit/chain.py:98 | refusal: an unlogged assumption about who and where | test_naive_timestamps_are_refused_rather_than_assumed | handled+tested |
| audit | timestamp nudged with no string field changed | packages/pipeline/oxbow/audit/chain.py:255 | verification still fails | test_a_nudged_timestamp_fails_though_no_string_field_changed | handled+tested |
| audit | verify called on an empty chain | packages/pipeline/oxbow/audit/chain.py:243 | `rows_checked=0`: nothing checked is not "all checked" | test_verify_on_an_empty_chain_reports_nothing_checked | handled+tested |
| audit | two writers append to the file chain | packages/pipeline/oxbow/adapters/file/audit.py:84 | refusal naming the stored tip | test_a_stale_tip_append_is_refused_and_leaves_no_hole | handled+tested |
| audit | concurrent append wins the Postgres link | packages/pipeline/oxbow/adapters/audit/postgres.py:85 | refusal telling the caller to retry the transaction | test_the_audit_sink_locks_before_it_reads_its_tip | handled+tested |
| audit | advisory lock unavailable on the null backend | tests/../packages/pipeline/oxbow/audit/serialise.py:65 | the constraint backstop still applies, and says so | test_the_lock_is_a_no_op_on_a_backend_without_advisory_locks | handled+tested |
| audit | chain link from another store | packages/pipeline/oxbow/audit/chain.py:282 | verification fails rather than adopting the foreign head | test_a_link_from_another_store_never_verifies_here | handled+tested |
| audit | packet exported over a broken chain | packages/pipeline/oxbow/packet/render.py:126 | export refused with the broken sequence | test_chain_verifies_on_export_and_tampering_fails_naming_the_sequence | handled+tested |
| audit | erasure destroys the mapping and must keep the chain verifiable | apps/api/decisions.py:1070 | mapping gone, `chain_intact` true, every digest still verifies | test_erasure_preserves_chain | handled+tested |
| audit | erasure record would carry the deleted identifier | packages/pipeline/oxbow/audit/erasure.py:120 | refusal naming the offending keys | test_an_erasure_record_cannot_carry_an_identifier | handled+tested |
| audit | one subject holding two salted keys, erased by one request | apps/api/decisions.py:1062 | nothing: the sibling key survives the erasure | test_erasure_reaches_only_the_named_account_key | unguarded |
| audit | a read-model table added without an immutability trigger | apps/api/alembic/versions/0002_integrity_triggers.py:36 | nothing until a reviewer notices the list | none | handled, untested |
| audit | delivery fails after the decision committed | apps/api/outbox.py:385 | row retried or dead-lettered; the decision stands | test_a_refused_delivery_is_dead_lettered_and_releases_the_cases_next_row | handled+tested |
| audit | webhook payload forged or replayed | packages/pipeline/oxbow/adapters/signing.py:118 | refused: 401 with the replay window named | test_a_delivery_outside_the_replay_window_is_rejected_as_expired | handled+tested |
| audit | an unset signing secret | packages/pipeline/oxbow/adapters/webhook/sinks.py:65 | delivery refused rather than using a published default | test_an_unset_secret_refuses_delivery_rather_than_using_a_published_default | handled+tested |
| demo | boot with no Postgres, Redis, MLflow or MinIO | apps/api/deps.py:711 | every read answers; each missing dependency is named and its fallback printed | test_app_boots_in_process_and_registers_a_route_table | handled+tested |
| demo | first request against an empty warehouse | apps/api/readmodel.py:1315 | 404 naming the pipeline command to run | test_an_empty_deployment_says_so_instead_of_padding | handled+tested |
| demo | demo seeder pointed at a warehouse with no evidence | scripts/demo_seed.py:197 | refusal naming what is missing, not a seeded half-truth | test_the_seeder_refuses_a_warehouse_with_no_evidence | handled+tested |
| demo | case id deep-linked after a warehouse reload | apps/api/routers/cases.py:294 | 404 that says the id may predate the reload | none | handled, untested |
| demo | run id typed from an old link, not a ULID | apps/api/readmodel.py:1282 | refusal naming the expected shape | test_a_run_id_that_is_not_a_ulid_is_refused_at_both_ends | handled+tested |
| demo | seed overridden for a demo | apps/api/settings.py:79 | boot refused: 1337 is the reproducibility contract | test_config_rejects_a_seed_other_than_1337 | handled+tested |
| demo | worker started against the null warehouse | apps/api/worker.py:1684 | refuses to start, naming the reason | test_main_refuses_to_start_a_null_file_worker | handled+tested |
| demo | PDF typeset on a host without Pango | packages/pipeline/oxbow/packet/render.py:418 | PacketError with the exact install command | test_render_writes_pdf_html_and_svg | handled+tested |
| demo | packet rendered for a run with no landed artifacts | packages/pipeline/oxbow/packet/loaders.py:180 | refusal naming the missing path | test_missing_artifacts_fail_naming_their_path | handled+tested |
| demo | packet template loses its date marker | packages/pipeline/oxbow/packet/render.py:446 | refusal: the PDF would be dated by the wall clock | test_no_wall_clock_appears_in_the_body | handled+tested |
| demo | disclaimer reworded by a caller | packages/pipeline/oxbow/packet/model.py:542 | refusal: the disclaimer is a constant | test_disclaimer_present_everywhere | handled+tested |
| demo | deciding on a superseded run | packages/pipeline/oxbow/packet/model.py:607 | permitted, and stamped on the cover | test_superseded_run_stamp_reaches_the_cover | handled+tested |
| demo | a pane's data request fails in the client | apps/web/src/components/Pane.tsx:1 | labelled pane error; siblings keep rendering | web:apps/web/tests/unit/pane-error-isolated.test.tsx | handled, untested |
| docs-gate | register row cites a test that no longer exists | tests/unit/test_failure_mode_register.py:223 | CI red with the row and the missing name | test_every_row_names_a_test_that_exists | handled+tested |
| docs-gate | an honest gap disappears from the top of the document | tests/unit/test_failure_mode_register.py:223 | CI red naming the orphaned row | test_every_open_gap_is_listed_in_the_findings_section | handled+tested |
| docs-gate | the register quietly shrinks below its measured coverage | tests/unit/test_failure_mode_register.py:223 | CI red with the area counts | test_the_register_covers_the_measured_area_floor | handled+tested |
| docs-gate | source and config comments cite tests by names that do not exist | docs/FAILURE-MODES.md:1 | a reviewer is pointed at a test that was never written | none | unguarded |

---

## Row counts, as measured

Produced by the gate, not typed by hand. See the `## Register` table above; the gate
recomputes this on every run and fails if a row is malformed, if a status is outside the
four allowed strings, or if an open row is missing from the findings section.

## How the register is checked

`tests/unit/test_failure_mode_register.py` walks `tests/**` with `ast`, collects every
`def test_*`, and asserts: every row is well-formed with six populated cells; every
status is one of the four allowed strings; every test cell either is `none` (only legal
for an open status) or resolves to a real test name, with `web:` cells resolved against
the Vitest tree; row keys are unique; every `handled, untested` or `unguarded` row's
failure text appears in the ordered open-findings section; and the register spans at
least twelve distinct areas. It is mutation-proved by renaming one cited test and
showing the gate go red.
