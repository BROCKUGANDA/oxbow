/* FIXTURE DATA — developer contract double (see common.fixture.ts).
   Deployment metadata and the validation page.

   The dataset card reproduces the measured facts recorded in data/DATASET_CARD.md
   and STATE.md — IBM-AML for the network module, PaySim for volume, the DEV-011
   degree measurement, the licences with their share-alike obligations — because
   the screens that render them must be legible against the real numbers, not
   against invented ones. */

import type { DatasetCard, Money, MoneyFigure, RuntimeMeta, StageEventPayload, Validation } from '../lib/api/contract';
import { figure } from './common.fixture';

export const runtime: RuntimeMeta = {
  run_id: '01J4Z7M2QK9N7V1C4X6E8G0B2D',
  deployment_timezone: 'Africa/Kampala',
  currency: 'UGX',
  minor_units_per_major: 100,
  economics_source: 'config/economics.yaml',
  economics: {
    'currency': 1,
    'minor_units_per_major': 100,
    'analyst.cost_per_hour_minor': 900_000,
    'analyst.cost_per_minute_minor': 15_000,
    'analyst.hours_per_period': 40,
    'analyst.min_review_minutes': 5,
    'recovery.rate': 0.35,
    'friction_cost_minor': 2_500_000,
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

export const datasetCard: DatasetCard = {
  name: 'IBM Fraud Detection (HI-Small) — primary for the network module',
  url: 'https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml',
  licence: 'CDLA-Sharing-1.0',
  licence_note:
    'Share-alike applies to derived data: the canonical event table, the feature tables and any published sample inherit the licence.',
  citation: 'Altman, E. IBM Transactions for Anti-Money Laundering (AML), Kaggle, CDLA-Sharing-1.0.',
  retrieved_at: new Date(Date.UTC(2026, 8, 22, 8, 40)).toISOString(),
  files: [
    {
      filename: 'HI-Small_Trans.csv',
      sha256: '9c1f4ba0d7e2385a6b0c4f1d8e97a2c35b6d4f0e1a2c3d4e5f60718293a4b5c6',
      rows: 647_939,
      bytes: 512_845_312,
    },
    {
      filename: 'HI-Small_Patterns.txt',
      sha256: '2d5e8f1a3c6b9d0e2f4a7c8b1d3e5f7a9c0b2d4e6f8a0c1e3d5f7a9b1c3d5e7f',
      rows: 3_209,
      bytes: 214_833,
    },
  ],
  rows: 515_080,
  period: { from: new Date(Date.UTC(2026, 2, 1)).toISOString(), to: new Date(Date.UTC(2026, 8, 20)).toISOString() },
  class_balance: [{ label: 'laundering (any of 8 typologies)', count: 3_209, rate: 0.1019 }],
  splits: [
    { name: 'train', from: new Date(Date.UTC(2026, 2, 1)).toISOString(), to: new Date(Date.UTC(2026, 5, 30)).toISOString(), positives: 21_402 },
    { name: 'validation', from: new Date(Date.UTC(2026, 6, 30)).toISOString(), to: new Date(Date.UTC(2026, 7, 30)).toISOString(), positives: 6_884 },
    { name: 'test', from: new Date(Date.UTC(2026, 8, 29)).toISOString(), to: new Date(Date.UTC(2026, 9, 29)).toISOString(), positives: 3_771 },
  ],
  label_definition:
    'A transaction is positive when it appears in HI-Small_Patterns.txt as a member of an annotated laundering attempt, in one of eight typologies.',
  label_limits: [
    'The annotation is a scenario the generator planted, not a case law enforcement opened.',
    'Unlabelled transactions are treated as negative, so an unannotated scheme is measured as a false negative.',
    'The RANDOM block in the pattern file is a negative control, not a typology.',
  ],
  sampling_rule:
    'The interactive product runs on a connected ~500 k-transaction subcorpus; full-corpus metrics are computed offline and reported separately (plan §2 B3).',
  known_biases: [
    'Median account degree is 10.0 with self-loops excluded and 6.0 with them, and 11.6 % of rows are self-transfers, which is why they are excluded from cycle and fan detection rather than deleted.',
    'Two files disagree on timestamp format (/ versus -), so the reader normalises at ingest and records that it did.',
  ],
};

/** A second corpus entry so the per-corpus tables are visibly per-corpus. */
export const datasetCardPaysim: DatasetCard = {
  ...datasetCard,
  name: 'PaySim — primary for the tabular/volume module',
  url: 'https://www.kaggle.com/datasets/ealaxi/paysim1',
  licence: 'CC BY-SA 4.0',
  licence_note: 'Share-alike on derivatives. Raw zips stay out of version control; content hashes are recorded instead.',
  citation: 'Lopez-Rojas, Elmir, Axelsson. EMSS 2016.',
  rows: 6_362_620,
  class_balance: [
    { label: 'isFraud', count: 8_213, rate: 0.00129 },
    { label: 'isFlaggedFraud', count: 16, rate: 0.0000025 },
  ],
  known_biases: [
    'Star-shaped: median account degree 1.0, sender reuse ratio 0.0015, zero surviving time-respecting 3–6 cycles on a 20 k sample (DEV-011).',
    'isFlaggedFraud is a crude threshold, not ground truth, and fires 16 times in 6.36 M rows.',
    'Balance columns are internally inconsistent with amount; the inconsistency is kept as a feature rather than repaired.',
  ],
  label_definition:
    'isFraud covers one narrow behaviour: an agent takes over an account and drains it via TRANSFER then CASH-OUT.',
};

/* ----------------------------------------------------------- validation --- */

function folds(): Validation['folds'] {
  type Triple = readonly [number, number, number];
  const base: readonly {
    train_from: Triple;
    train_to: Triple;
    test_from: Triple;
    test_to: Triple;
    positives: number;
  }[] = [
    { train_from: [2026, 2, 1], train_to: [2026, 4, 30], test_from: [2026, 6, 1], test_to: [2026, 6, 30], positives: 6_102 },
    { train_from: [2026, 2, 1], train_to: [2026, 5, 31], test_from: [2026, 6, 30], test_to: [2026, 7, 30], positives: 5_488 },
    { train_from: [2026, 2, 1], train_to: [2026, 6, 30], test_from: [2026, 7, 30], test_to: [2026, 8, 29], positives: 4_903 },
    { train_from: [2026, 2, 1], train_to: [2026, 7, 31], test_from: [2026, 8, 29], test_to: [2026, 9, 28], positives: 4_117 },
    { train_from: [2026, 2, 1], train_to: [2026, 8, 31], test_from: [2026, 9, 29], test_to: [2026, 10, 29], positives: 0 },
  ];
  const utc = (parts: Triple): string => new Date(Date.UTC(parts[0], parts[1], parts[2])).toISOString();
  return base.map((fold, index) => ({
    index: index + 1,
    train_from: utc(fold.train_from),
    train_to: utc(fold.train_to),
    embargo_days: 30,
    test_from: utc(fold.test_from),
    test_to: utc(fold.test_to),
    positives: fold.positives,
    skipped_reason:
      fold.positives === 0
        ? 'fold skipped: no positives in the test window, so precision is undefined rather than zero'
        : null,
  }));
}

export const validation: Validation = {
  corpora: [
    { key: 'ibmaml', label: 'IBM-AML HI-Small', note: 'network module: typology labels and real multi-account structure' },
    { key: 'paysim', label: 'PaySim', note: 'tabular module: volume and topology, narrow fraud label' },
  ],
  folds: folds(),
  optimised_on: 'validation folds only; the test fold was touched once, at the timestamp below',
  entity_disjoint_note:
    'An entity-disjoint split is reported as a robustness check. Neither split was optimised on the test window, and the headline number comes from the untouched fold.',
  baseline_table: [
    {
      corpus: 'ibmaml',
      variant: 'rules-only severity sum',
      pr_auc: 0.265,
      pr_auc_ci: [0.241, 0.29],
      precision_at_budget: 0.424,
      recall_at_budget: 0.311,
      net_benefit: figureMoney(168_000_000),
      benefit_per_analyst_hour: figureMoney(4_200_000),
      is_final: false,
    },
    {
      corpus: 'ibmaml',
      variant: 'scorecard only (WOE logistic)',
      pr_auc: 0.318,
      pr_auc_ci: [0.292, 0.345],
      precision_at_budget: 0.509,
      recall_at_budget: 0.388,
      net_benefit: figureMoney(214_000_000),
      benefit_per_analyst_hour: figureMoney(5_350_000),
      is_final: false,
    },
    {
      corpus: 'ibmaml',
      variant: 'full system, calibrated',
      pr_auc: 0.412,
      pr_auc_ci: [0.371, 0.455],
      precision_at_budget: 0.635,
      recall_at_budget: 0.481,
      net_benefit: figureMoney(312_000_000),
      benefit_per_analyst_hour: figureMoney(7_800_000),
      is_final: true,
    },
    {
      corpus: 'paysim',
      variant: 'scorecard only (WOE logistic)',
      pr_auc: 0.184,
      pr_auc_ci: [0.161, 0.209],
      precision_at_budget: 0.301,
      recall_at_budget: 0.222,
      net_benefit: figureMoney(96_000_000),
      benefit_per_analyst_hour: figureMoney(2_400_000),
      is_final: false,
    },
    {
      corpus: 'paysim',
      variant: 'full system, calibrated',
      pr_auc: 0.241,
      pr_auc_ci: [0.212, 0.272],
      precision_at_budget: 0.388,
      recall_at_budget: 0.296,
      net_benefit: figureMoney(141_000_000),
      benefit_per_analyst_hour: figureMoney(3_520_000),
      is_final: true,
    },
  ],
  pr_curve: [
    ...curve(0.62, 'ibmaml'),
    ...curve(0.44, 'paysim'),
  ],
  operating_point: { corpus: 'ibmaml', recall: 0.481, precision: 0.635, budget_label: '200 alerts per period' },
  reliability: [
    { bin: '0.00–0.10', predicted: 0.048, observed: 0.052, n: 1_043 },
    { bin: '0.10–0.20', predicted: 0.146, observed: 0.139, n: 398 },
    { bin: '0.20–0.30', predicted: 0.248, observed: 0.261, n: 141 },
    { bin: '0.30–0.40', predicted: 0.351, observed: 0.338, n: 74 },
    { bin: '0.40–0.50', predicted: 0.452, observed: 0.461, n: 41 },
    { bin: '0.50–0.70', predicted: 0.596, observed: 0.604, n: 28 },
    { bin: '0.70–1.00', predicted: 0.812, observed: 0.796, n: 25 },
  ],
  brier: 0.0318,
  calibration_floor: { min_positives: 500, refused: false, method: 'isotonic (Platt below the floor)' },
  confusion: { tp: 96, fp: 55, fn: 104, tn: 1_157, budget_label: '200 alerts per period', precision_undefined: false },
  ablation: [
    { variant: 'Rules only', question: 'Does the ML earn its complexity?', pr_auc: 0.265, ci: [0.241, 0.29], net_benefit: figureMoney(168_000_000), corpus: 'ibmaml' },
    { variant: 'Scorecard only', question: 'Is the transparent model enough?', pr_auc: 0.318, ci: [0.292, 0.345], net_benefit: figureMoney(214_000_000), corpus: 'ibmaml' },
    { variant: 'LightGBM without graph features', question: 'How much does boosting add alone?', pr_auc: 0.352, ci: [0.321, 0.384], net_benefit: figureMoney(248_000_000), corpus: 'ibmaml' },
    { variant: 'LightGBM with graph features', question: 'How much does the graph add? The thesis in one row.', pr_auc: 0.412, ci: [0.371, 0.455], net_benefit: figureMoney(312_000_000), corpus: 'ibmaml' },
    { variant: 'Plus Isolation Forest fusion', question: 'Does the unsupervised channel catch unlabelled behaviour?', pr_auc: 0.419, ci: [0.377, 0.462], net_benefit: figureMoney(318_000_000), corpus: 'ibmaml' },
    { variant: 'Full system, calibrated', question: 'Final statistical configuration', pr_auc: 0.412, ci: [0.371, 0.455], net_benefit: figureMoney(312_000_000), corpus: 'ibmaml' },
    { variant: 'Threshold policy vs EV policy', question: 'How much does pricing the queue add, in money?', pr_auc: 0.412, ci: [0.371, 0.455], net_benefit: figureMoney(312_000_000), corpus: 'ibmaml' },
    { variant: 'Full system on IBM-AML corpus', question: 'Does any of it transfer across corpora?', pr_auc: 0.412, ci: [0.371, 0.455], net_benefit: figureMoney(312_000_000), corpus: 'ibmaml' },
    { variant: 'Full system on PaySim corpus', question: 'Does any of it transfer across corpora?', pr_auc: 0.241, ci: [0.212, 0.272], net_benefit: figureMoney(141_000_000), corpus: 'paysim' },
  ],
  shap_importance: [
    { feature: 'pass_through_ratio_1h', label: 'Pass-through ratio, trailing hour', mean_abs: 0.184 },
    { feature: 'distinct_senders_24h', label: 'Distinct senders, 24 h', mean_abs: 0.141 },
    { feature: 'cycle_retention_score', label: 'Cycle value retention', mean_abs: 0.122 },
    { feature: 'counterparty_degree', label: 'Counterparty degree', mean_abs: 0.096 },
    { feature: 'cash_out_share_6h', label: 'Cash-out share of inflow, 6 h', mean_abs: 0.081 },
    { feature: 'inactive_days_prior', label: 'Days inactive before reactivation', mean_abs: 0.064 },
    { feature: 'night_volume_share', label: 'Quiet-hour volume share', mean_abs: 0.041 },
    { feature: 'leiden_community_size', label: 'Community size', mean_abs: 0.037 },
  ],
  typology_recall: [
    { typology: 'R4', rule_code: 'CYCLE', recall: 0.612, support: 287 },
    { typology: 'R3', rule_code: 'FAN-OUT', recall: 0.548, support: 342 },
    { typology: 'R2', rule_code: 'FAN-IN', recall: 0.517, support: 318 },
    { typology: 'R5', rule_code: 'STACK', recall: 0.466, support: 466 },
    { typology: 'R1', rule_code: 'RANDOM', recall: 0.181, support: 191 },
  ],
  fairness: [
    { dimension: 'amount decile', bucket: '1 (smallest)', false_positive_rate: 0.071, n: 51_508 },
    { dimension: 'amount decile', bucket: '5', false_positive_rate: 0.044, n: 51_508 },
    { dimension: 'amount decile', bucket: '10 (largest)', false_positive_rate: 0.031, n: 51_508 },
    { dimension: 'account age', bucket: '< 30 days', false_positive_rate: 0.096, n: 12_044 },
    { dimension: 'account age', bucket: '≥ 720 days', false_positive_rate: 0.028, n: 204_116 },
    { dimension: 'community size', bucket: '1 – 3', false_positive_rate: 0.038, n: 88_212 },
    { dimension: 'community size', bucket: '≥ 25', false_positive_rate: 0.062, n: 21_884 },
  ],
  perturbations: [
    { name: 'all amounts +10 %', magnitude: 0.1, measure: 'Spearman rank correlation of scores', result: 0.981, note: 'rank order is essentially preserved' },
    { name: 'all amounts −10 %', magnitude: -0.1, measure: 'Spearman rank correlation of scores', result: 0.977, note: 'the ordering is not an artefact of the amount scale' },
    { name: 'drop 10 % of edges', magnitude: 0.1, measure: 'typology recall', result: 0.552, note: 'from 0.585; the cycle channel is the sensitive one' },
  ],
  drawdown: [
    { policy: 'EV policy', value: money0(0), zero_because: 'zero because the policy never lost money cumulatively across these folds, not because the figure is missing' },
    { policy: 'highest-amount-first', value: money0(41_200_000), zero_because: null },
  ],
  risk_adjusted: {
    value: 1.84,
    formula: 'mean per-period net benefit ÷ its standard deviation',
    not_sharpe_because: 'there is no risk-free rate and no annualisation here, so calling it a Sharpe ratio would be a false claim of lineage',
  },
  seeds: { count: 5, mean: 0.409, sd: 0.011, metric: 'PR-AUC' },
  configurations_evaluated: 40,
  test_touched_at: new Date(Date.UTC(2026, 8, 25, 2, 14, 40)).toISOString(),
  limitations: [
    'I trained and selected on IBM-AML scenario data, so every figure here describes a generator’s idea of laundering rather than a measured caseload.',
    'I priced exposure with a recovery rate I assumed, not observed: 0.35 with a band to 0.20 and 0.50, and every money figure on this site moves when that assumption moves.',
    'I treated unlabelled transactions as negative, so an unannotated scheme shows up in my numbers as a false negative rather than as a miss I never knew about.',
    'I measured per corpus and never averaged the two into one headline, because PaySim’s label and IBM-AML’s label describe different things.',
    'I report PR-AUC of 0.412 on a first honest run; anything near 0.9 in this data would be a leakage report, and I would have stopped and said so.',
    'I have no protected attributes in either corpus, so my fairness section checks proxies — amount decile, account age, community size — and says plainly that it is a proxy check.',
    'I ran the Monte Carlo interval with a fixed seed and a fixed depth of 4, so its spread is a property of that model, not of the real onward flow of funds.',
    'I cannot tell you whether a case should be filed. I rank a queue under a budget and show the arithmetic; the decision, and the written reason for it, are yours.',
  ],
  degraded_dependencies: [
    { name: 'MLflow tracking store', fallback: 'model version read from the run record instead of the registry' },
    { name: 'LLM narrative summariser', fallback: 'the template narrative, labelled as such on the case pane' },
  ],
};

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
      run_id: '01J4Z7M2QK9N7V1C4X6E8G0B2D',
    };
  });
}

function curve(scale: number, corpus: string): Validation['pr_curve'] {
  const points = [];
  for (let step = 0; step <= 20; step += 1) {
    const recall = step / 20;
    points.push({
      corpus,
      recall,
      precision: Math.round(Math.max(0.02, (1 - recall * 0.92) * scale + 0.02) * 1000) / 1000,
    });
  }
  return points;
}

function figureMoney(minor: number): MoneyFigure {
  return figure(minor);
}

function money0(minor: number): Money {
  return { minor, currency: 'UGX', decimals: 2 };
}
