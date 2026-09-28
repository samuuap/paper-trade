/**
 * Typed client for the backend.
 *
 * The shapes mirror `backend/app/metrics/queries.py`, which is the single
 * definition of every number on screen. When a field changes there, it changes
 * here — the dashboard deliberately does no arithmetic of its own beyond
 * formatting, so a figure can never disagree with the API that produced it.
 */

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api";

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { Accept: "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!response.ok) {
    throw new ApiError(response.status, `${response.status} on ${path}`);
  }
  return (await response.json()) as T;
}

// ------------------------------------------------------------------- types

export type AgentKind = "LLM" | "BASELINE";

export interface LeaderboardRow {
  agent_id: string;
  slug: string;
  name: string;
  kind: AgentKind;
  runtime: string;
  color: string;
  rank: number;
  equity: number;
  cash: number;
  positions_value: number;
  position_count: number;
  initial_capital: number;
  total_pnl: number;
  total_return_pct: number;
  realized_pnl: number;
  unrealized_pnl: number;
  fees_paid: number;
  sharpe: number | null;
  sortino: number | null;
  calmar: number | null;
  volatility: number | null;
  max_drawdown: number | null;
  current_drawdown: number | null;
  win_rate: number | null;
  profit_factor: number | null;
  alpha: number | null;
  beta: number | null;
  benchmark_return: number | null;
  metrics_day: string | null;
  decisions: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  inference_seconds: number;
  shadow_cost_usd: number;
  energy_cost_usd: number;
  seconds_per_pnl_dollar: number | null;
}

export interface EquityPoint {
  ts: string;
  equity: number;
}

export interface EquitySeries {
  slug: string;
  name: string;
  color: string;
  kind: AgentKind;
  points: EquityPoint[];
}

export interface EquityResponse {
  series: EquitySeries[];
  benchmark: { symbol: string; points: (EquityPoint & { close?: number })[] };
}

export interface Holding {
  symbol: string;
  quantity: number;
  avg_entry_price: number;
  price: number;
  market_value: number;
  unrealized_pnl: number;
  unrealized_pnl_pct: number;
  weight_pct: number;
  opened_at: string;
}

export interface MetricsDay {
  day: string;
  equity_close: number;
  daily_return: number;
  cumulative_return: number;
  sharpe: number | null;
  sortino: number | null;
  max_drawdown: number;
  current_drawdown: number;
  win_rate: number | null;
  profit_factor: number | null;
  trade_count: number;
  turnover: number;
  gross_exposure_pct: number;
  concentration_hhi: number;
  alpha: number | null;
  beta: number | null;
  benchmark_return: number | null;
  decisions: number;
  prompt_tokens: number;
  completion_tokens: number;
  inference_seconds: number;
  shadow_cost_usd: number;
}

export interface AgentDetail {
  agent: {
    id: string;
    slug: string;
    name: string;
    kind: AgentKind;
    runtime: string;
    description: string;
    color: string;
    model: string | null;
    strategy_prompt: string;
    is_active: boolean;
  };
  portfolio: {
    equity: number;
    cash: number;
    positions_value: number;
    initial_capital: number;
    realized_pnl: number;
    unrealized_pnl: number;
    fees_paid: number;
    gross_exposure: number;
    net_exposure: number;
  };
  positions: Holding[];
  history: MetricsDay[];
}

export interface DecisionRow {
  id: string;
  agent: string;
  agent_name: string;
  color: string;
  phase: string;
  kind: string;
  status: string;
  model: string | null;
  market_view: string;
  reasoning: string;
  output: Record<string, unknown>;
  tool_calls: unknown[];
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  inference_ms: number;
  queue_wait_ms: number;
  tokens_per_second: number;
  tool_call_count: number;
  retries: number;
  shadow_cost_usd: number;
  energy_cost_usd: number;
  error: string | null;
  created_at: string;
}

export interface TradeRow {
  id: string;
  ts: string;
  agent: string;
  agent_name: string;
  color: string;
  symbol: string;
  side: "BUY" | "SELL";
  quantity: number;
  price: number;
  reference_price: number;
  slippage_bps: number;
  slippage_cost: number;
  commission: number;
  gross_amount: number;
  realized_pnl: number;
  rationale: string;
  conviction: number | null;
}

export interface RejectedOrder {
  id: string;
  ts: string;
  agent: string;
  symbol: string;
  side: string;
  quantity: number;
  reject_reason: string;
  rationale: string;
  risk_notes: Record<string, unknown>;
}

export interface Economics {
  slug: string;
  name: string;
  kind: AgentKind;
  color: string;
  decisions: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  tokens_per_decision: number;
  inference_seconds: number;
  avg_tokens_per_second: number;
  tool_calls: number;
  retries: number;
  latency_ms: { p50: number | null; p95: number | null; p99: number | null; samples: number };
  status_counts: Record<string, number>;
  shadow_cost_usd: number;
  energy_cost_usd: number;
  shadow_model: string;
  pnl: number;
  seconds_per_pnl_dollar: number | null;
}

export interface OrchestratorStatus {
  running: boolean;
  phase: string;
  now: string;
  uptime_seconds: number;
  ticks: number;
  replay: boolean;
  session: {
    day: string;
    pre_open: string;
    market_open: string;
    market_close: string;
    post_close: string;
    is_early_close: boolean;
  } | null;
  next_transition: { at: string; phase: string };
  tasks: {
    name: string;
    interval_seconds: number;
    phases: string[];
    next_due: string | null;
    last_run: string | null;
    runs: number;
    failures: number;
    last_error: string | null;
  }[];
}

export interface Health {
  status: string;
  env: string;
  orchestrator: OrchestratorStatus;
  data: {
    quote_lag_seconds: number | null;
    universe_size: number;
    screenable_symbols: number;
    latest_bar_ts: string | null;
    symbols_with_bars: number;
    total_bars: number;
  };
  provider: {
    provider: string;
    offline: boolean;
    rate_limit: {
      limit_per_minute: number;
      used_last_minute: number;
      remaining: number;
      total_requests: number;
      total_throttled: number;
      utilisation: number;
    } | null;
  };
  inference: {
    memory: { total_gb: number; available_gb: number; used_pct: number };
    min_free_memory_gb: number;
    inference_process: { pid: number; name: string; rss_gb: number } | null;
    model_resident: boolean;
    model_open: string;
    model_closed: string;
    model_for_phase: string | null;
    queue: {
      depth: number;
      running: string | null;
      waiting: string[];
      completed: number;
      failed: number;
      avg_wait_ms: number;
      max_wait_ms: number;
    };
  };
  decisions: {
    window_hours: number;
    decisions: number;
    failed: number;
    per_hour: number;
    success_rate: number | null;
  };
  jobs: {
    window_hours: number;
    by_job: Record<string, { ok: number; failed: number; running: number }>;
    success_rate: number | null;
  };
  stream: { subscribers: number };
}

export interface JobRow {
  id: string;
  job: string;
  phase: string | null;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  success: boolean | null;
  error: string | null;
  details: Record<string, unknown>;
}

export interface SystemEventRow {
  id: string;
  ts: string;
  level: string;
  category: string;
  event: string;
  message: string;
  payload: Record<string, unknown>;
}
