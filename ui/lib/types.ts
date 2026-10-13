// Shapes mirror the worker's export bundle (worker/worker/cli/ui_cmd.py). Keep both in sync.

export type Status = "ok" | "stale" | "missing";

export interface Tile {
  key: string;
  label: string;
  unit: string;
  price: number | null;
  source: string | null;
  resolution: string | null;
  ts: string | null;
  age_minutes: number | null;
  status: Status;
}

export interface LiveBundle {
  as_of: string;
  stale_after_minutes: number;
  mode: "paper";
  tiles: Tile[];
  open_position: Record<string, unknown> | null;
  last_equity: Record<string, unknown> | null;
}

export interface LeaderboardRow {
  username: string;
  title: string | null;
  active: boolean;
  tier: string;
  label: "lead" | "lag" | "noise" | "insufficient";
  label_reason: string | null;
  lead_lag_minutes: number | null;
  n_events: number;
  lifts_net_bps: Record<string, number | null>;
  ci_95: Record<string, [number, number] | null>;
  q_values: Record<string, number | null>;
  fdr_corrected: boolean;
  pumper_flag: boolean;
  quoted_bias_pct: number | null;
  burst_share: number | null;
  measures: Record<string, number>;
  method_version: string;
}

export interface LeaderboardBundle {
  asof_date: string | null;
  method_version: string;
  family: { name: string; size: number; q_threshold: number; significant: number } | null;
  cost_model: { spread_bps: number; slippage_bps: number; signal_cost_bps: number; round_trip_cost_bps: number } | null;
  channels: LeaderboardRow[];
}

export interface GateCheck {
  name: string;
  passed: boolean;
  detail: string;
  hard_veto: boolean;
}

export interface Verdict {
  action: "BUY" | "WAIT" | "AVOID";
  reason: string;
  checks: GateCheck[];
}

export interface Decision {
  decision_id: number;
  day: string;
  checkpoint: string;
  decision_ts: string;
  split: string;
  state_hash: string;
  model: string | null;
  from_cache: boolean;
  confidence: number | null;
  day_pnl_bps: number | null;
  verdict: Verdict | null;
  answers: unknown;
  questions: unknown;
  state: Record<string, unknown>;
}

export interface DecisionBundle {
  split: string;
  items: Decision[];
}

export interface TimelineItem {
  id: number;
  published_at: string;
  /** minutes since the 08:00 Tehran session open (computed by the worker) */
  t: number;
  username: string;
  title: string | null;
  persian_text: string | null;
  english_text: string | null;
  persian_gloss: string | null;
  kind: string | null;
  direction: string | null;
  intensity: number | null;
  topic: string | null;
  confidence: number | null;
  quoted_asset: string | null;
  quoted_price: number | null;
  is_edited: boolean;
  version: number;
  extraction_status: string;
}

export interface PricePath {
  source: string | null;
  symbol: string | null;
  resolution: string | null;
  points: { t: number; p: number }[];
}

export interface TimelineDay {
  day: string;
  items: TimelineItem[];
  truncated: boolean;
  price_path: PricePath;
}

export interface TimelineIndex {
  days: {
    day: string;
    n_messages: number;
    n_signals: number;
    truncated: boolean;
    price_path: PricePath;
  }[];
}

export interface EquityRow {
  day: string;
  trades: number;
  net_bps: number;
  equity: number;
  drawdown: number;
  unmeasured_marks?: number;
}

export interface BaselineRow {
  name: string;
  n_calls: number;
  coverage: number;
  directional_accuracy: number | null;
  mean_signed_bps_gross: number;
  mean_signed_bps_net: number;
}

export interface CalibrationBundle {
  n: number;
  brier: number | null;
  ece: number | null;
  reliability: { bin_low: number; bin_high: number; n: number; mean_confidence: number; empirical_frequency: number; gap: number }[];
  sweep: { threshold: number; coverage: number; n_trades: number; precision: number | null; hit_rate_up_days: number | null }[];
}

export interface BacktestBundle {
  run_id: number;
  config: Record<string, unknown>;
  summary: Record<string, number>;
  scoring: { channels: LeaderboardRow[]; family: LeaderboardBundle["family"]; days_covered: number; days_missing: number };
  baselines: BaselineRow[];
  calibration: CalibrationBundle;
  equity: EquityRow[];
  n_day_records: number;
}

export interface CoverageBundle {
  rows: {
    source: string; symbol: string; resolution: string; month: string;
    n_bars: number; expected_bars: number; coverage_pct: number; n_gaps: number; gap_minutes: number;
  }[];
  acceptance: { name: string; passed: boolean; detail: string; threshold: string }[];
}

export interface HypothesisRow {
  id: number;
  text: string;
  status: string;
  proposed_by_model: string | null;
  test_method: string | null;
  test_results_json: unknown;
  proposed_at: string;
  tested_at: string | null;
}

export interface MetaBundle {
  generated_at: string;
  generated_by: string;
  demo: boolean;
  banner: string | null;
  split: string;
  split_window: { start: string; discovery_end: string; outofsample_end: string };
  days: { first: string; last: string; n: number };
  method_version: string;
  tev: { timezone: string; open_local: string; flat_local: string };
  has_backtest: boolean;
  stale_after_minutes: number;
  series: { source: string; symbol: string; resolution: string; last_ts: string; n_bars: number }[];
}

export interface AdminBundle {
  settings: Record<string, unknown>;
  run_config: { key: string; value_json: unknown; set_at: string; set_by: string }[];
}

export interface Bundle {
  meta: MetaBundle;
  live: LiveBundle;
  leaderboard: LeaderboardBundle;
  decisions: DecisionBundle;
  timelineIndex: TimelineIndex;
  coverage: CoverageBundle;
  hypotheses: { items: HypothesisRow[] };
  runConfig: AdminBundle;
  backtest: BacktestBundle | null;
}
