/* FIXTURE DATA — developer contract double (see common.fixture.ts).
   Deployment metadata, the dataset card and the validation record.

   MIRRORED FROM THE RESPONSE MODELS, NOT FROM THE SCREEN. These objects are the served
   `DatasetMeta` and `ValidationBundle` (`apps/api/schemas/catalog.py`,
   `apps/api/schemas/validation.py`), field for field. That is the entire reason this file
   exists as a separate double: a fixture shaped by the client's own decoder could agree
   with the client while disagreeing with the server, which is exactly the failure the
   explorer and `/model` were in. So nothing here carries a name the Pydantic model does
   not declare — no `baseline_table`, no bundle-level `pr_curve`, `brier` or
   `calibration_floor`, no `shap_importance`, `drawdown`, `risk_adjusted`, `seeds`,
   `test_touched_at`, `configurations_evaluated` or `degraded_dependencies` at the top
   level. Where the server sends null — a confusion matrix at an unnamed budget, a fold
   whose precision is undefined, a one-point curve's `note` — the double sends null too,
   because a fixture that fills the gap teaches the screen to hide it.

   The dataset card reproduces the measured facts recorded in data/DATASET_CARD.md and
   STATE.md — IBM-AML for the network module, PaySim for volume, the DEV-011 degree
   measurement, the licences with their share-alike obligations — because the screens that
   render them must be legible against the real numbers, not against invented ones. */

import type {
  DatasetCard,
  Money,
  RuntimeMeta,
  StageEventPayload,
  Validation,
  ValidationMetric,
} from '../lib/api/contract';

const RUN_ID = '01J4Z7M2QK9N7V1C4X6E8G0B2D';

export const runtime: RuntimeMeta = {
  run_id: RUN_ID,
  deployment_timezone: 'Africa/Kampala',
  currency: 'UGX',
  minor_units_per_major: 100,
  economics_source: 'config/economics.yaml',
  economics: {
    currency: 1,
    minor_units_per_major: 100,
    'analyst.cost_per_hour_minor': 900_000,
    'analyst.cost_per_minute_minor': 15_000,
    'analyst.hours_per_period': 40,
    'analyst.min_review_minutes': 5,
    'recovery.rate': 0.35,
    friction_cost_minor: 2_500_000,
    'exposure.window_hours': 24,
    'exposure.downstream_hops': 1,
    'capacity.review_minutes_per_period': 12_000,
    'capacity.default_alerts_reviewed': 200,
    'solver.greedy_budget_ms': 200,
    'solver.cpsat_deadline_ms': 5_000,
    'solver.agreement_tolerance_ratio': 0.02,
    'monte_carlo.runs': 10_000,
    'monte_carlo.max_depth': 4,
    'monte_carlo.seed': 1337,
    'four_eyes.threshold_exposure_minor': 50_000_000,
    'tail_risk.var_alpha': 0.95,
    'tail_risk.es_alpha': 0.975,
  },
  dataset: 'IBM-AML HI-Small (Module B) · PaySim (Module A)',
  licence: 'CDLA-Sharing-1.0 · CC BY-SA 4.0',
  model_version: 'lgbm-fusion-4.5.3+isotonic',
  demo_data: false,
};

/* ------------------------------------------------------------- dataset ---- */

function money(minor: number): Money {
  return { minor, currency: 'UGX', decimals: 2 };
}

const utc = (year: number, month: number, day: number, hour = 0, minute = 0): string =>
  new Date(Date.UTC(year, month, day, hour, minute)).toISOString();

export const datasetCard: DatasetCard = {
  sources: [
    {
      source_id: 'ibmaml',
      name: 'IBM Transactions for Anti-Money Laundering (AML), HI-Small',
      role: 'primary for the network module: typology labels and real multi-account structure',
      module: 'Module B',
      source_url: 'https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml',
      retrieval: 'Kaggle dataset download, archived by sha256 rather than by re-fetch',
      license: 'CDLA-Sharing-1.0',
      license_obligation:
        'Share-alike applies to derived data: the canonical event table, the feature tables and any published sample inherit the licence.',
      citation: 'Altman, E. IBM Transactions for Anti-Money Laundering (AML), Kaggle, CDLA-Sharing-1.0.',
      description:
        'Synthetic transaction history generated with SIMFIN. HI-Small_Trans.csv holds the account-level event stream; HI-Small_Patterns.txt annotates the planted laundering attempts.',
      label_caveat:
        'A transaction is positive when it appears in HI-Small_Patterns.txt as a member of an annotated laundering attempt, in one of eight typologies. The annotation is a scenario the generator planted, not a case law enforcement opened, and unlabelled transactions are treated as negative.',
      known_biases: [
        'Median account degree is 10.0 with self-loops excluded and 6.0 with them, and 11.6 % of rows are self-transfers, which is why they are excluded from cycle and fan detection rather than deleted.',
        'Two files disagree on timestamp format (/ versus -), so the reader normalises at ingest and records that it did.',
        'The RANDOM block in the pattern file is a negative control, not a typology.',
      ],
      synthetic_fields: ['all amounts, balances and timestamps: SIMFIN generated them'],
      ingest_allowed: true,
      retrieved_at: utc(2026, 8, 22, 8, 40),
      files: [
        {
          file_name: 'HI-Small_Trans.csv',
          sha256: '9c1f4ba0d7e2385a6b0c4f1d8e97a2c35b6d4f0e1a2c3d4e5f60718293a4b5c6',
          size_bytes: 512_845_312,
          row_count: 647_939,
          verified_at: utc(2026, 8, 22, 9, 5),
        },
        {
          file_name: 'HI-Small_Patterns.txt',
          sha256: '2d5e8f1a3c6b9d0e2f4a7c8b1d3e5f7a9c0b2d4e6f8a0c1e3d5f7a9b1c3d5e7f',
          size_bytes: 214_833,
          // A file the run never counted is served as null, not as zero rows.
          row_count: null,
          verified_at: null,
        },
      ],
    },
    {
      source_id: 'paysim',
      name: 'PaySim (EMSS 2016)',
      role: 'primary for the tabular/volume module: volume and topology, narrow fraud label',
      module: 'Module A',
      source_url: 'https://www.kaggle.com/datasets/ealaxi/paysim1',
      retrieval: 'Kaggle dataset download; raw zips stay out of version control, hashes recorded',
      license: 'CC BY-SA 4.0',
      license_obligation: 'Share-alike on derivatives.',
      citation: 'Lopez-Rojas, Elmir, Axelsson. EMSS 2016. PaySim, Kaggle, CC BY-SA 4.0.',
      description: 'Agent-based mobile-money payment simulator, 6.36 M transactions over 30 days.',
      label_caveat:
        'isFraud covers one narrow behaviour: an agent takes over an account and drains it via TRANSFER then CASH-OUT. isFlaggedFraud is a crude threshold, not ground truth, and fires 16 times in 6.36 M rows.',
      known_biases: [
        'Star-shaped: median account degree 1.0, sender reuse ratio 0.0015, zero surviving time-respecting 3–6 cycles on a 20 k sample (DEV-011).',
        'Balance columns are internally inconsistent with amount; the inconsistency is kept as a feature rather than repaired.',
      ],
      synthetic_fields: ['all step types, amounts and balances: the EMSS agent model generated them'],
      ingest_allowed: true,
      retrieved_at: utc(2026, 8, 20, 14, 12),
      files: [
        {
          file_name: 'PS_2017439255969_translations.csv',
          sha256: '5b7d9f1c3e5a7b9d0f2c4e6a8b0d2f4a6c8e0b2d4f6a8c0e2d4f6a8b0c2d4e6',
          size_bytes: 331_072_256,
          row_count: 6_362_620,
          verified_at: utc(2026, 8, 20, 15, 2),
        },
      ],
    },
  ],
  refused_sources: [
    {
      source_id: 'elembud',
      name: 'Elliptic Bitcoin Fraud Dataset',
      reason: 'Licence forbids redistribution of derived feature tables, and the product publishes its feature tables.',
      status: 'refused-before-ingest',
    },
  ],
  measurements: [
    {
      scope: 'ibmaml',
      name: 'median account degree, self-loops excluded',
      value: 10.0,
      unit: 'edges',
      command: 'uv run python scripts/measure_dataset_facts.py --source ibmaml --stat degree_median',
      measured_at: utc(2026, 8, 23, 11, 40),
    },
    {
      scope: 'ibmaml',
      name: 'share of rows that are self-transfers',
      value: 0.116,
      unit: 'share',
      command: 'uv run python scripts/measure_dataset_facts.py --source ibmaml --stat self_transfer_share',
      measured_at: utc(2026, 8, 23, 11, 41),
    },
    {
      scope: 'paysim',
      name: 'surviving time-respecting 3–6 hop cycles in a 20 k sample',
      value: 0,
      unit: 'cycles',
      command: 'uv run python scripts/measure_dataset_facts.py --source paysim --stat cycle_count --sample 20000',
      measured_at: utc(2026, 8, 23, 11, 58),
    },
    {
      scope: 'paysim',
      name: 'isFlaggedFraud positives',
      value: 16,
      unit: 'rows',
      command: 'uv run python scripts/measure_dataset_facts.py --source paysim --stat flagged_fraud_rows',
      measured_at: utc(2026, 8, 23, 12, 1),
    },
  ],
  // `config/pipeline.yaml`'s `sampling` section, forwarded verbatim by the route.
  sampling: {
    subcorpus_target_rows: 500_000,
    strategy: 'connected-component-preserving sample',
    seed: 1337,
    full_corpus_metrics: 'computed offline and reported separately (plan §2 B3)',
  },
  deidentification: {
    account_key: {
      scheme: 'sha256 of the source account id, truncated to 6 hex characters behind an ACC- prefix',
      salt_source: 'config/pipeline.yaml deidentification.salt_env',
      reversible: false,
    },
    raw_identifier_policy: {
      customer_id: 'dropped at ingest; never written to any table the API reads',
      name: 'not present in either corpus',
    },
    note: 'Account keys are the only identifier the product handles. A key is a join handle, not a name: no route will show an activity window it did not measure.',
  },
  disclaimer:
    'Both corpora are synthetic. Every figure the product reports describes a generator’s idea of laundering, not a measured caseload.',
};

/* ----------------------------------------------------------- validation --- */

/** The limitations the run stores, which `_limitations` echoes as the `note` on each
 *  `limitation:NN` metric row. Declared above `validation` because the double builds both
 *  the metric rows and the echoed list from this one array, exactly as the route builds
 *  the list from the rows — a second copy of the prose would be a second document to
 *  fall out of date. */
const LIMITATIONS: readonly string[] = [
  'I trained and selected on IBM-AML scenario data, so every figure here describes a generator’s idea of laundering rather than a measured caseload.',
  'I priced exposure with a recovery rate I assumed, not observed: 0.35 with a band to 0.20 and 0.50, and every money figure on this site moves when that assumption moves.',
  'I treated unlabelled transactions as negative, so an unannotated scheme shows up in my numbers as a false negative rather than as a miss I never knew about.',
  'I measured per corpus and never averaged the two into one headline, because PaySim’s label and IBM-AML’s label describe different things.',
  'I report PR-AUC of 0.412 on a first honest run; anything near 0.9 in this data would be a leakage report, and I would have stopped and said so.',
  'I have no protected attributes in either corpus, so my fairness section checks proxies — amount decile, account age, community size — and says plainly that it is a proxy check.',
  'I ran the Monte Carlo interval with a fixed seed and a fixed depth of 4, so its spread is a property of that model, not of the real onward flow of funds.',
  'I cannot tell you whether a case should be filed. I rank a queue under a budget and show the arithmetic; the decision, and the written reason for it, are yours.',
];

/** `_limitations` reads `limitation:`-prefixed metric rows and echoes their `note`, so the
 *  double builds the list the same way rather than carrying a second copy of the prose. */
function limitationMetrics(): ValidationMetric[] {
  return LIMITATIONS.map((text, index) =>
    metric(`limitation:${String(index + 1).padStart(2, '0')}`, 1, 'ibmaml', 'flag', text, null),
  );
}

/** One fold, as `FoldRow` declares it. `precision_at_budget` is null with
 *  `precision_undefined: true` and the alert count on the last fold — the undefined case is
 *  the one plan §12 names, and encoding it as 0 or 1 is the fabrication the schema refuses. */
function fold(overrides: Partial<Validation['folds'][number]> & Pick<Validation['folds'][number], 'fold_index'>) {
  const base: Validation['folds'][number] = {
    fold_index: overrides.fold_index,
    corpus: overrides.corpus ?? 'ibmaml',
    train_start: overrides.train_start ?? utc(2026, 2, 1),
    train_end: overrides.train_end ?? utc(2026, 4, 30),
    embargo_days: overrides.embargo_days ?? 30,
    embargo_end: overrides.embargo_end ?? utc(2026, 5, 30),
    test_start: overrides.test_start ?? utc(2026, 6, 1),
    test_end: overrides.test_end ?? utc(2026, 6, 30),
    n_train: overrides.n_train ?? 384_210,
    n_test: overrides.n_test ?? 96_420,
    pr_auc: overrides.pr_auc ?? 0.412,
    auroc: overrides.auroc ?? 0.871,
    brier: overrides.brier ?? 0.0318,
    precision_at_budget: overrides.precision_at_budget ?? 0.635,
    recall_at_budget: overrides.recall_at_budget ?? 0.481,
    precision_undefined: overrides.precision_undefined ?? false,
    precision_note: overrides.precision_note ?? null,
    alerts: overrides.alerts ?? 200,
    captured_value: overrides.captured_value ?? money(312_000_000),
    cost: overrides.cost ?? money(68_000_000),
    net_benefit: overrides.net_benefit ?? money(244_000_000),
    max_drawdown: overrides.max_drawdown ?? money(0),
    zero_drawdown_note: overrides.zero_drawdown_note ?? null,
    var95: overrides.var95 ?? money(41_000_000),
    es975: overrides.es975 ?? money(57_000_000),
    monte_carlo_runs: overrides.monte_carlo_runs ?? 10_000,
    monte_carlo_seed: overrides.monte_carlo_seed ?? 1337,
    entity_disjoint: overrides.entity_disjoint ?? true,
    test_fold_touched_at: overrides.test_fold_touched_at ?? null,
  };
  return base;
}

const metric = (
  name: string,
  value: number,
  corpus: string,
  unit: string | null = null,
  note: string | null = null,
  n: number | null = null,
): ValidationMetric => ({ name, value, unit, corpus, note, n });

export const validation: Validation = {
  run_id: RUN_ID,
  corpora: ['ibmaml', 'paysim'],
  folds: [
    fold({ fold_index: 0, test_start: utc(2026, 6, 1), test_end: utc(2026, 6, 30), pr_auc: 0.398 }),
    fold({
      fold_index: 1,
      train_end: utc(2026, 5, 31),
      test_start: utc(2026, 6, 30),
      test_end: utc(2026, 7, 30),
      pr_auc: 0.405,
    }),
    fold({
      fold_index: 2,
      train_end: utc(2026, 6, 30),
      test_start: utc(2026, 7, 30),
      test_end: utc(2026, 8, 29),
      pr_auc: 0.412,
      // The headline fold: touched once, and the timestamp is the claim.
      test_fold_touched_at: utc(2026, 8, 25, 2, 14),
    }),
    fold({
      fold_index: 3,
      corpus: 'paysim',
      train_end: utc(2026, 7, 31),
      test_start: utc(2026, 8, 29),
      test_end: utc(2026, 9, 28),
      pr_auc: 0.241,
      auroc: 0.774,
      brier: 0.0104,
      precision_at_budget: 0.388,
      recall_at_budget: 0.296,
      captured_value: money(141_000_000),
      net_benefit: money(96_000_000),
    }),
    fold({
      fold_index: 4,
      corpus: 'paysim',
      train_end: utc(2026, 8, 31),
      test_start: utc(2026, 9, 29),
      test_end: utc(2026, 10, 29),
      pr_auc: 0.233,
      auroc: 0.769,
      brier: 0.0106,
      precision_at_budget: null,
      recall_at_budget: null,
      precision_undefined: true,
      precision_note:
        'the policy raised 0 alerts above the cutoff in this fold, so precision is undefined: neither 0 nor 1 would be a measurement',
      alerts: 0,
      // A drawdown of exactly zero is a measurement, and the note is what says so.
      max_drawdown: money(0),
      zero_drawdown_note:
        'zero because the policy never lost money cumulatively across these folds, not because the figure is missing',
    }),
  ],
  ablation: [
    {
      variant: 'rules-only severity sum',
      question: 'Does the ML earn its complexity?',
      corpus: 'ibmaml',
      pr_auc: 0.265,
      net_benefit: money(168_000_000),
      ci_low: 0.241,
      ci_high: 0.29,
      ci_method: 'bootstrap, 2 000 resamples, percentile interval',
      n_resamples: 2_000,
      seed: 1337,
      is_leakage_control: false,
      is_graph_thesis: false,
      is_pricing_thesis: false,
    },
    {
      variant: 'scorecard only (WOE logistic)',
      question: 'Is the transparent model enough?',
      corpus: 'ibmaml',
      pr_auc: 0.318,
      net_benefit: money(214_000_000),
      ci_low: 0.292,
      ci_high: 0.345,
      ci_method: 'bootstrap, 2 000 resamples, percentile interval',
      n_resamples: 2_000,
      seed: 1337,
      is_leakage_control: false,
      is_graph_thesis: false,
      is_pricing_thesis: false,
    },
    {
      variant: 'LightGBM without graph features',
      question: 'How much does boosting add alone?',
      corpus: 'ibmaml',
      pr_auc: 0.352,
      net_benefit: money(248_000_000),
      ci_low: 0.321,
      ci_high: 0.384,
      ci_method: 'bootstrap, 2 000 resamples, percentile interval',
      n_resamples: 2_000,
      seed: 1337,
      is_leakage_control: false,
      is_graph_thesis: false,
      is_pricing_thesis: false,
    },
    {
      variant: 'LightGBM with graph features',
      question: 'How much does the graph add? The thesis in one row.',
      corpus: 'ibmaml',
      pr_auc: 0.412,
      net_benefit: money(312_000_000),
      ci_low: 0.371,
      ci_high: 0.455,
      ci_method: 'bootstrap, 2 000 resamples, percentile interval',
      n_resamples: 2_000,
      seed: 1337,
      is_leakage_control: false,
      is_graph_thesis: true,
      is_pricing_thesis: false,
    },
    {
      variant: 'threshold policy vs EV policy',
      question: 'How much does pricing the queue add, in money?',
      corpus: 'ibmaml',
      pr_auc: 0.412,
      net_benefit: money(312_000_000),
      ci_low: 0.371,
      ci_high: 0.455,
      ci_method: 'bootstrap, 2 000 resamples, percentile interval',
      n_resamples: 2_000,
      seed: 1337,
      is_leakage_control: false,
      is_graph_thesis: false,
      is_pricing_thesis: true,
    },
    {
      variant: 'deliberate lookahead: features computed after the label',
      question: 'Does the harness detect leakage at all?',
      corpus: 'ibmaml',
      // The control must visibly outperform; that is what proves the harness detects
      // leakage rather than the model being lucky.
      pr_auc: 0.988,
      net_benefit: money(910_000_000),
      ci_low: 0.974,
      ci_high: 0.996,
      ci_method: 'bootstrap, 2 000 resamples, percentile interval',
      n_resamples: 2_000,
      seed: 1337,
      is_leakage_control: true,
      is_graph_thesis: false,
      is_pricing_thesis: false,
    },
    {
      variant: 'full system on PaySim corpus',
      question: 'Does any of it transfer across corpora?',
      corpus: 'paysim',
      pr_auc: 0.241,
      net_benefit: money(141_000_000),
      ci_low: 0.212,
      ci_high: 0.272,
      ci_method: 'bootstrap, 2 000 resamples, percentile interval',
      n_resamples: 2_000,
      seed: 1337,
      is_leakage_control: false,
      is_graph_thesis: false,
      is_pricing_thesis: false,
    },
  ],
  curves: [
    {
      family: 'reliability',
      x_label: 'predicted probability',
      y_label: 'observed frequency',
      currency: null,
      operating_threshold: null,
      note: null,
      points: [
        { point_index: 0, x: 0.048, y: 0.052, n: 1_043, label: '0.00–0.10', operating_point: false },
        { point_index: 1, x: 0.146, y: 0.139, n: 398, label: '0.10–0.20', operating_point: false },
        { point_index: 2, x: 0.248, y: 0.261, n: 141, label: '0.20–0.30', operating_point: false },
        { point_index: 3, x: 0.351, y: 0.338, n: 74, label: '0.30–0.40', operating_point: false },
        { point_index: 4, x: 0.452, y: 0.461, n: 41, label: '0.40–0.50', operating_point: false },
        { point_index: 5, x: 0.596, y: 0.604, n: 28, label: '0.50–0.70', operating_point: false },
        { point_index: 6, x: 0.812, y: 0.796, n: 25, label: '0.70–1.00', operating_point: false },
      ],
    },
    {
      family: 'pr_curve',
      x_label: 'recall',
      y_label: 'precision',
      currency: null,
      operating_threshold: 0.481,
      note: null,
      points: prCurvePoints(),
    },
    {
      family: 'shap_global',
      x_label: 'feature',
      y_label: 'mean |SHAP|',
      currency: null,
      operating_threshold: null,
      note: null,
      points: [
        { point_index: 0, x: 0.184, y: 0.184, n: null, label: 'pass_through_ratio_1h', operating_point: false },
        { point_index: 1, x: 0.141, y: 0.141, n: null, label: 'distinct_senders_24h', operating_point: false },
        { point_index: 2, x: 0.122, y: 0.122, n: null, label: 'cycle_retention_score', operating_point: false },
        { point_index: 3, x: 0.096, y: 0.096, n: null, label: 'counterparty_degree', operating_point: false },
        { point_index: 4, x: 0.081, y: 0.081, n: null, label: 'cash_out_share_6h', operating_point: false },
        { point_index: 5, x: 0.064, y: 0.064, n: null, label: 'inactive_days_prior', operating_point: false },
        { point_index: 6, x: 0.041, y: 0.041, n: null, label: 'night_volume_share', operating_point: false },
        { point_index: 7, x: 0.037, y: 0.037, n: null, label: 'leiden_community_size', operating_point: false },
      ],
    },
    {
      family: 'typology_recall',
      x_label: 'typology',
      y_label: 'recall',
      currency: null,
      operating_threshold: null,
      note: null,
      points: [
        { point_index: 0, x: 0.612, y: 0.612, n: 287, label: 'R4', operating_point: false },
        { point_index: 1, x: 0.548, y: 0.548, n: 342, label: 'R3', operating_point: false },
        { point_index: 2, x: 0.517, y: 0.517, n: 318, label: 'R2', operating_point: false },
        { point_index: 3, x: 0.466, y: 0.466, n: 466, label: 'R5', operating_point: false },
        { point_index: 4, x: 0.181, y: 0.181, n: 191, label: 'R1', operating_point: false },
      ],
    },
    {
      // The single-point series plan §14 tests for: the note is served with it, so the
      // page prints an honest sentence rather than drawing a trend through one dot.
      family: 'per_typology_precision',
      x_label: 'typology',
      y_label: 'precision',
      currency: null,
      operating_threshold: null,
      note: 'this series has 1 point(s). A curve cannot be drawn through it, so the page shows this note and the number instead of an absurd chart (plan §14 test_single_point_series)',
      points: [{ point_index: 0, x: 0.612, y: 0.584, n: 12, label: 'R12', operating_point: false }],
    },
  ],
  confusion: {
    cells: [
      { label: 'fraud', prediction: 'alerted', n: 96 },
      { label: 'fraud', prediction: 'not alerted', n: 104 },
      { label: 'not fraud', prediction: 'alerted', n: 55 },
      { label: 'not fraud', prediction: 'not alerted', n: 1_157 },
    ],
    // The run recorded no review_budget metric, so the budget is null: a matrix drawn at
    // an unnamed budget is not a matrix at a budget.
    budget: null,
    basis: 'at the configured review budget, not over the whole ranking (plan §12)',
  },
  fairness: [
    {
      axis: 'amount decile',
      axis_rationale:
        'No protected attribute exists in either corpus, so the check runs along amount decile and says plainly that it is a proxy check.',
      rows: [
        { bucket: '1 (smallest)', fp_rate: 0.071, fn_rate: 0.402, n: 51_508 },
        { bucket: '5', fp_rate: 0.044, fn_rate: 0.451, n: 51_508 },
        { bucket: '10 (largest)', fp_rate: 0.031, fn_rate: null, n: 51_508 },
      ],
    },
    {
      axis: 'account age',
      axis_rationale:
        'Newly opened accounts are the population a mule farm draws from, so age is the proxy with the most reason to differ.',
      rows: [
        { bucket: '< 30 days', fp_rate: 0.096, fn_rate: 0.512, n: 12_044 },
        { bucket: '≥ 720 days', fp_rate: 0.028, fn_rate: 0.288, n: 204_116 },
      ],
    },
    {
      axis: 'community size',
      axis_rationale:
        'Community size is the graph-derived proxy; a large community is where a collapsed meta-node lives.',
      rows: [
        { bucket: '1 – 3', fp_rate: 0.038, fn_rate: null, n: 88_212 },
        { bucket: '≥ 25', fp_rate: 0.062, fn_rate: 0.331, n: 21_884 },
      ],
    },
  ],
  perturbations: [
    {
      kind: 'all amounts +10 %',
      magnitude: 0.1,
      result: 0.981,
      unit: 'Spearman rank correlation of scores',
      note: 'rank order is essentially preserved',
      seed: 1337,
    },
    {
      kind: 'all amounts −10 %',
      magnitude: -0.1,
      result: 0.977,
      unit: 'Spearman rank correlation of scores',
      note: 'the ordering is not an artefact of the amount scale',
      seed: 1337,
    },
    {
      kind: 'drop 10 % of edges',
      magnitude: 0.1,
      result: 0.552,
      unit: 'typology recall',
      note: 'from 0.585; the cycle channel is the sensitive one',
      seed: 2024,
    },
  ],
  // `metrics` is the store the route reads, and the other blocks are DERIVED from it
  // (`_overfitting`, `_label_quality`, `_limitations`), so the double carries the same
  // dependency rather than a second copy of each number.
  metrics: [
    metric('review_budget', 200, 'ibmaml', 'alerts per period', null, null),
    metric('configurations_evaluated', 40, 'ibmaml', 'configurations', null, null),
    metric('test_fold_touched_once', 1, 'ibmaml', 'count', 'the test fold was touched exactly once', null),
    metric('selection_on_validation', 1, 'ibmaml', 'flag', 'selection happened on the validation folds', null),
    metric(
      'risk_adjusted_benefit_ratio',
      1.84,
      'ibmaml',
      'ratio',
      'mean per-period net benefit ÷ its standard deviation; explicitly NOT a Sharpe ratio — there is no risk-free rate and no annualisation here',
      null,
    ),
    metric('seed_stability_count', 5, 'ibmaml', 'seeds', null, null),
    metric('seed_stability_mean_pr_auc', 0.409, 'ibmaml', 'PR-AUC', null, null),
    metric('seed_stability_sd_pr_auc', 0.011, 'ibmaml', 'PR-AUC', null, null),
    metric('label_prevalence', 0.00129, 'paysim', 'share', 'positive rate of the learning label', 6_362_620),
    metric('flagged_fraud_rows', 16, 'paysim', 'rows', 'rows carrying the crude isFlaggedFraud threshold', null),
    metric('typology_recall_R4', 0.612, 'ibmaml', 'recall', null, 287),
    metric('typology_recall_R3', 0.548, 'ibmaml', 'recall', null, 342),
    metric('typology_recall_R2', 0.517, 'ibmaml', 'recall', null, 318),
    metric('typology_recall_R5', 0.466, 'ibmaml', 'recall', null, 466),
    metric('typology_recall_R1', 0.181, 'ibmaml', 'recall', null, 191),
    ...limitationMetrics(),
  ],
  typology_recall: [
    metric('typology_recall_R4', 0.612, 'ibmaml', 'recall', null, 287),
    metric('typology_recall_R3', 0.548, 'ibmaml', 'recall', null, 342),
    metric('typology_recall_R2', 0.517, 'ibmaml', 'recall', null, 318),
    metric('typology_recall_R5', 0.466, 'ibmaml', 'recall', null, 466),
    metric('typology_recall_R1', 0.181, 'ibmaml', 'recall', null, 191),
  ],
  overfitting: {
    configurations_evaluated: 40,
    test_fold_touched_once: 1,
    selection_on_validation: 1,
    caveat:
      'with N configurations tried, the best validation result is optimistically biased by multiple testing; the headline comes from the untouched fold for that reason',
    keys: {
      configurations_evaluated: 'configurations evaluated during selection',
      test_fold_touched_once: 'the test fold was touched exactly once',
      selection_on_validation: 'selection happened on the validation folds',
    },
  },
  label_quality: {
    entries: [
      { name: 'label_prevalence', value: 0.00129, meaning: 'positive rate of the learning label', corpus: 'paysim' },
      {
        name: 'flagged_fraud_rows',
        value: 16,
        meaning: 'rows carrying the crude isFlaggedFraud threshold',
        corpus: 'paysim',
      },
    ],
    note: "PaySim's isFlaggedFraud is not a usable target; isFraud is the only viable label on that corpus (DEV-011). IBM-AML's typology labels come from its pattern file, not its label column (DEV-014).",
  },
  limitations: [...LIMITATIONS],
  assumptions: [
    {
      key: 'config/economics.yaml',
      value: 'economics.yaml',
      source: 'config/economics.yaml',
      note: 'every money figure on this page is a function of these assumptions',
    },
  ],
};

/** The PR sweep, at 21 recall steps. One curve, no corpus axis: `CurveSeries` has no
 *  `corpus` field, which is why `/model` can no longer draw per-corpus PR curves
 *  (apps/web/CONTRACT-GAPS.md). */
function prCurvePoints(): Validation['curves'][number]['points'] {
  const points = [];
  for (let step = 0; step <= 20; step += 1) {
    const recall = step / 20;
    points.push({
      point_index: step,
      x: recall,
      y: Math.round(Math.max(0.02, (1 - recall * 0.92) * 0.62 + 0.02) * 1000) / 1000,
      n: null,
      label: null,
      operating_point: step === 10,
    });
  }
  return points;
}

const LIMITATIONS_TEXT: readonly string[] = LIMITATIONS;
void LIMITATIONS_TEXT;

/* -------------------------------------------------------- stage events ---- */

export function stageEvents(count: number, failed: number | null = null): StageEventPayload[] {
  const stages = ['ingest', 'canonicalise', 'graph', 'features', 'rules', 'score', 'calibrate', 'allocate', 'backtest'];
  const taken = stages.slice(0, Math.min(count, stages.length));
  return taken.map((stage, index) => {
    const isFailed = failed === index;
    const done = index < taken.length - 1;
    return {
      id: `01J4Z7${String(index).padStart(2, '0')}EVT`,
      stage,
      status: isFailed ? 'failed' : done ? 'complete' : 'running',
      rows: done || isFailed ? 12_044 + index * 91_113 : index === taken.length - 1 ? null : 0,
      elapsed_ms: done ? 1_400 + index * 3_900 : isFailed ? 8_200 : 2_150,
      run_id: RUN_ID,
    };
  });
}
