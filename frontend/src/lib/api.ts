/* Typed client for the EventAlpha FastAPI backend (same-origin /api via the Vite proxy). */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

export type DataMode = "demo" | "live";
export type Provenance = "observed" | "external_estimate" | "model_forecast";
export type Scenario = "bear" | "base" | "bull";

export class ApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { ...init, headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) } });
  } catch {
    throw new ApiError(0, "network", "Cannot reach the EventAlpha API. Is the backend running on port 8000?");
  }
  const text = await res.text();
  const body = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const err = body?.error ?? {};
    throw new ApiError(res.status, err.code ?? "error", err.message ?? `Request failed (${res.status})`);
  }
  return body as T;
}

// ---------------------------------------------------------------- types
export interface LatestPrice {
  price: number;
  trade_date: string;
  change_pct: number | null;
  volume: number | null;
  provenance: Provenance;
  source: string | null;
  ingested_at: string | null;
  stale_sessions: number;
}

export interface Health {
  status: string;
  database: string;
  data_mode: DataMode;
  providers: { massive_configured: boolean; openai_configured: boolean; sec_user_agent_has_contact: boolean };
}

export interface IndexQuote {
  symbol: string;
  name: string;
  value: number | null;
  change_pct: number | null;
  as_of_date: string | null;
  kind: "index" | "etf_proxy";
  proxy_for: string | null;
  source: string;
  unavailable_reason: string | null;
}

export interface MarketOverview {
  data_mode: DataMode;
  indices: IndexQuote[];
  unavailable_reason?: string;
  coverage_notes: string[];
}

export interface DiscoveryItem {
  rank: number;
  ticker: string;
  name: string | null;
  sector: string | null;
  latest_price: LatestPrice | null;
  score: number;
  score_band: string;
  factors: { factors: Record<string, number | null>; contributions: Record<string, number>; weights: Record<string, number> };
  outlook: "bullish" | "neutral" | "bearish";
  risk_rating: "Low" | "Medium" | "High";
  horizon_class: "short_term" | "long_term";
  potential_return: number | null;
  target_price: number | null;
  bear_target: number | null;
  bull_target: number | null;
  target_date: string | null;
  catalyst: { title: string | null; event_id: number | null; event_date: string | null };
  analysis_at: string | null;
  ai_kind: string | null;
}

export interface Job {
  id: string;
  job_type: string;
  status: "queued" | "running" | "succeeded" | "failed";
  progress: string | null;
  result: Record<string, unknown> | null;
  error: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface Discovery {
  data_mode: DataMode;
  items: DiscoveryItem[];
  run: null | { id: number; started_at: string; finished_at: string; universe_size: number; candidates_evaluated: number; notes: Record<string, unknown> };
  running_job: Job | null;
  ranking_explanation?: string;
}

export interface StockOverview {
  data_mode: DataMode;
  ticker: string;
  name: string | null;
  exchange: string | null;
  cik: string | null;
  sector: string | null;
  industry: string | null;
  latest_price: LatestPrice | null;
  market_cap: { value: number | null; provenance: Provenance; basis: string };
  high_52w: number | null;
  low_52w: number | null;
  avg_dollar_volume_20d: number | null;
  volatility_1y: number | null;
  return_1y: number | null;
  pe_ttm: number | null;
  pe_note: string | null;
  latest_analysis: AnalysisSummary | null;
}

export interface AnalysisSummary {
  id: number;
  created_at: string;
  ai_kind: "openai" | "rule_based" | "demo";
  ai_model: string | null;
  observed_price: number;
  observed_price_date: string;
  outlook: string | null;
  ai_outlook: string | null;
  risk_rating: string | null;
  base_target_12m: number | null;
  bear_target_12m: number | null;
  bull_target_12m: number | null;
  base_return_12m: number | null;
}

export interface Claim {
  text: string;
  claim_type: "verified_fact" | "management_guidance" | "analyst_estimate" | "model_assumption" | "speculative";
  source_ids: string[];
  warning?: string;
}

export interface Thesis {
  summary: string;
  outlook: string;
  short_term_outlook: string;
  long_term_outlook: string;
  data_gaps: string[];
  [k: string]: unknown;
}

export interface Diff {
  changes: { field: string; label: string; previous: unknown; current: unknown }[];
  bullish_points: { added: string[]; removed: string[] };
  bearish_points: { added: string[]; removed: string[] };
}

export interface AnalysisDetail extends AnalysisSummary {
  data_mode: DataMode;
  ticker: string;
  company: string | null;
  thesis: Thesis;
  snapshot: Record<string, unknown>;
  warnings: string[];
  prompt_version: string | null;
  usage: Record<string, unknown> | null;
  cost_usd: number | null;
  sources: { label: string; type: string; title: string | null; url: string; publisher: string | null; published_at: string | null }[];
  previous: AnalysisSummary | null;
  diff: Diff | null;
  disclaimer: string;
}

export interface ScenarioResult {
  scenario: Scenario;
  revenue: number | null;
  revenue_growth: number | null;
  operating_margin: number | null;
  net_margin: number | null;
  eps: number | null;
  free_cash_flow: number | null;
  pe_multiple: number | null;
  target_price_pe: number | null;
  target_price_dcf: number | null;
  target_price_ev_sales: number | null;
  target_price: number | null;
  potential_return: number | null;
  explanation: string;
  details: Record<string, unknown>;
}

export interface Assumption {
  name: string;
  value: unknown;
  basis: string;
  kind: "observed" | "derived" | "model_assumption" | "ai_adjustment";
}

export interface ForecastRecord {
  id: number;
  analysis_id: number;
  created_at: string;
  horizon_months: number;
  target_date: string;
  observed_price: number;
  observed_price_date: string;
  financial_data_version: string | null;
  ai_model: string | null;
  forecast_model_version: string;
  scenarios: Record<Scenario, ScenarioResult>;
  valuation_methods?: Record<string, { applicable?: boolean; note?: string | null } | unknown>;
  assumptions?: { items: Assumption[]; warnings: string[]; volatility_range: null | { low_1sigma: number; high_1sigma: number; sigma_annual: number; note: string } };
  sensitivity?: {
    pe_growth?: { rows_growth: number[]; cols_pe: number[]; target_prices: (number | null)[][]; note: string };
    dcf_wacc_terminal?: { rows_wacc: number[]; cols_terminal_growth: number[]; per_share_values: (number | null)[][] };
  };
}

export interface FinPoint {
  period_end: string;
  value: number;
  fiscal_year: number | null;
  fiscal_period: string | null;
  derived: boolean;
  accession?: string;
  filed_date?: string;
}

export interface Financials {
  data_mode: DataMode;
  currency: string;
  source: string;
  source_url: string | null;
  financial_data_version: string | null;
  latest_filed: string | null;
  ttm_period_end: string | null;
  ttm: Record<string, number | null>;
  ttm_prior: Record<string, number | null>;
  ratios: Record<string, number | null>;
  balance_sheet: Record<string, { value: number; as_of: string; accession: string; filed_date: string }>;
  quarterly: Record<string, FinPoint[]>;
  annual: Record<string, FinPoint[]>;
  warnings: string[];
  notes: string[];
}

export interface EventItem {
  id: number;
  title: string;
  summary: string | null;
  event_type: string;
  status: "confirmed" | "unconfirmed" | "scheduled" | "estimated";
  impact: "positive" | "negative" | "uncertain";
  impact_explanation: string | null;
  materiality: number | null;
  source_name: string | null;
  url: string | null;
  published_at: string | null;
  last_published_at: string | null;
  event_date: string | null;
  article_count: number;
  relationship_confidence: number | null;
  ai_analysis: Record<string, unknown> | null;
  articles: { title: string; url: string; source_name: string | null; published_at: string | null; is_rumor: boolean }[];
  companies?: { ticker: string; name: string | null; confidence: number; method: string; impact: string }[];
}

export interface WatchItem {
  ticker: string;
  name: string | null;
  sector: string | null;
  added_at: string;
  latest_price: LatestPrice | null;
  latest_analysis: AnalysisSummary | null;
  previous_analysis: AnalysisSummary | null;
  diff: Diff | null;
  forecast_history_12m: { created_at: string; observed_price: number; bear?: number; base?: number; bull?: number }[];
  upcoming: { title: string; event_date: string; status: string; event_type: string }[];
}

export interface BacktestItem {
  ticker?: string;
  as_of?: string;
  created_at?: string;
  target_date: string;
  horizon_months: number;
  predicted_base_return: number | null;
  predicted_bear_return: number | null;
  predicted_bull_return: number | null;
  realized_return: number;
  benchmark_return: number | null;
  excess_vs_benchmark: number | null;
}

export interface BacktestSummary {
  count: number;
  direction_hit_rate?: number | null;
  within_bear_bull_range?: number;
  mean_abs_error_base?: number | null;
  mean_realized_return?: number;
  mean_benchmark_return?: number;
  mean_excess_vs_benchmark?: number | null;
  note?: string;
}

// ---------------------------------------------------------------- hooks
export const useHealth = () => useQuery({ queryKey: ["health"], queryFn: () => api<Health>("/api/health"), refetchInterval: 30_000 });
export const useMarket = () => useQuery({ queryKey: ["market"], queryFn: () => api<MarketOverview>("/api/market/overview") });
export const useDiscovery = (params = "") =>
  useQuery({
    queryKey: ["discovery", params],
    queryFn: () => api<Discovery>(`/api/discovery${params}`),
    refetchInterval: (q) => (q.state.data?.running_job ? 4000 : false),
  });
export const useStock = (t: string) => useQuery({ queryKey: ["stock", t], queryFn: () => api<StockOverview>(`/api/stocks/${t}`), retry: false });
export const usePrices = (t: string, range: string) =>
  useQuery({
    queryKey: ["prices", t, range],
    queryFn: () => api<{ bars: { date: string; close: number; volume: number | null }[]; benchmark: { ticker: string; bars: { date: string; close: number }[] } | null; adjusted: string }>(`/api/stocks/${t}/prices?range=${range}`),
  });
export const useFinancials = (t: string) => useQuery({ queryKey: ["fin", t], queryFn: () => api<Financials>(`/api/stocks/${t}/financials`), retry: false });
export const useStockNews = (t: string) =>
  useQuery({ queryKey: ["news", t], queryFn: () => api<{ recent: EventItem[]; upcoming: EventItem[] }>(`/api/stocks/${t}/news?days=60`) });
export const useAnalysis = (t: string) => useQuery({ queryKey: ["analysis", t], queryFn: () => api<AnalysisDetail>(`/api/stocks/${t}/analysis`), retry: false });
export const useForecast = (t: string) =>
  useQuery({ queryKey: ["forecast", t], queryFn: () => api<{ forecasts: ForecastRecord[]; interpretation: string }>(`/api/stocks/${t}/forecast`), retry: false });
export const useForecastHistory = (t: string, horizon = 12) =>
  useQuery({ queryKey: ["fhist", t, horizon], queryFn: () => api<{ forecasts: ForecastRecord[] }>(`/api/stocks/${t}/forecast-history?horizon=${horizon}`), retry: false });
export const useEvents = (qs: string) =>
  useQuery({ queryKey: ["events", qs], queryFn: () => api<{ events: EventItem[]; event_types: string[]; notes: string[] }>(`/api/events?${qs}`) });
export const useWatchlist = () => useQuery({ queryKey: ["watchlist"], queryFn: () => api<{ items: WatchItem[] }>("/api/watchlist") });
export const useSettings = () => useQuery({ queryKey: ["settings"], queryFn: () => api<Record<string, any>>("/api/settings") }); // eslint-disable-line @typescript-eslint/no-explicit-any
export const useJob = (id: string | null) =>
  useQuery({
    queryKey: ["job", id],
    queryFn: () => api<Job>(`/api/jobs/${id}`),
    enabled: !!id,
    refetchInterval: (q) => (q.state.data && ["succeeded", "failed"].includes(q.state.data.status) ? false : 1500),
  });
export const useRecentForecasts = (horizon: number) =>
  useQuery({ queryKey: ["recentForecasts", horizon], queryFn: () => api<{ forecasts: (ForecastRecord & { ticker: string; name: string })[] }>(`/api/forecasts?horizon=${horizon}`) });
export const useBacktest = (horizon?: number) =>
  useQuery({ queryKey: ["backtest", horizon], queryFn: () => api<{ items: BacktestItem[]; unresolved: unknown[]; summary: BacktestSummary }>(`/api/backtest${horizon ? `?horizon=${horizon}` : ""}`) });
export const useReplay = (t: string, horizon: number) =>
  useQuery({
    queryKey: ["replay", t, horizon],
    queryFn: () => api<{ items: BacktestItem[]; summary: BacktestSummary; method?: string; error?: string }>(`/api/backtest/replay/${t}?horizon=${horizon}`),
    enabled: !!t,
  });

export type AnalyzeResponse = { status: "cached"; analysis_id: number; ticker: string; message: string } | { status: string; job_id: string; ticker: string };

export function useAnalyze() {
  return useMutation({
    mutationFn: ({ ticker, force = false, use_ai = true }: { ticker: string; force?: boolean; use_ai?: boolean }) =>
      api<AnalyzeResponse>(`/api/stocks/${encodeURIComponent(ticker)}/analyze`, { method: "POST", body: JSON.stringify({ force, use_ai }) }),
  });
}

export function useInvalidateAll() {
  const qc = useQueryClient();
  return () => qc.invalidateQueries();
}
