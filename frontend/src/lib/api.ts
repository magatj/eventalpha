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
  selection_pathway: string | null;
  score_display?: number;
  market_cap?: number | null;
  size?: "mega" | "large" | "mid" | "small" | "micro" | null;
  financial_strength?: number | null;
  valuation?: number | null;
  data_completeness?: number | null;
  catalyst_category?: string | null;
  catalyst_claim?: string | null;
  catalyst_url?: string | null;
  reasons?: { code: string; text: string; source_url?: string }[];
  qualifying_pathways?: string[];
  ai_status?: string | null;
  valuation_status?: ValuationStatus | null;
  valuation_method?: string | null;
}

export type ValuationStatus = "valid" | "limited_data" | "requires_review" | "unsupported_valuation" | "invalid_inputs";

export interface MultipleBand {
  kind: string;
  p10: number;
  p25: number;
  p50: number;
  p75: number;
  p90: number;
  n: number;
  rejected: number;
  source: string;
  detail: string;
}

export interface MultipleSelection {
  kind: string;
  current: number | null;
  bear: number;
  base_anchor: number;
  bull: number;
  low: number;
  high: number;
  basis: string;
  limited: boolean;
  evidence: { company_history: MultipleBand | null; peers: MultipleBand | null; fundamental_anchor: number | null; sector_reference: number[] | null };
  notes: string[];
}

export interface ValuationScenario {
  revenue: number | null;
  revenue_growth: number | null;
  net_margin: number | null;
  net_income: number | null;
  eps: number | null;
  free_cash_flow: number | null;
  capex: number | null;
  stock_based_comp: number | null;
  shares: number | null;
  pe_multiple: number | null;
  book_value_model: { multiple: number; return_on_equity: number; [k: string]: unknown } | null;
  ffo_model: { multiple: number; ffo_per_share: number } | null;
  price_to_sales_multiple: number | null;
  dcf: { discount_rate: number; terminal_growth: number; fcf_margin: number; fcf_basis: string } | null;
  method_weights: Record<string, number> | null;
  method_targets: Record<string, number> | null;
  target_price: number | null;
  potential_return: number | null;
  withheld_target: number | null;
  target_date: string;
}

export interface ValuationAudit {
  status: ValuationStatus;
  status_label: string;
  model_version: string;
  method: string;
  method_label: string;
  business_model: string;
  rationale: string;
  required_inputs: Record<string, boolean>;
  missing_inputs: string[];
  plan_notes: string[];
  weights: Record<string, number>;
  historical_multiples: Record<string, MultipleBand>;
  peers: { level: string; tickers: string[]; bands: Record<string, MultipleBand>; profitable_margin: number | null } | null;
  multiples: {
    pe: MultipleSelection | null;
    ps: MultipleSelection | null;
    book: { kind: string; measure: string; per_share_today: number; normalized_return: number; return_basis: string; retention: number; retention_basis: string; cost_of_equity: number; justified_multiple: number | null; multiples: MultipleSelection; notes: string[] } | null;
    ffo: { ffo_per_share_today: number; ffo_basis: string; multiples: MultipleSelection; notes: string[] } | null;
  };
  earnings_state: string | null;
  regime: string | null;
  scenarios_12m: Record<"bear" | "base" | "bull", ValuationScenario>;
  guardrails: {
    status: ValuationStatus;
    label: string;
    flags: { code: string; severity: "info" | "warning" | "review" | "invalid"; message: string }[];
    upside_check: { threshold?: number; base_return_12m?: number | null; supported?: boolean; checks?: Record<string, boolean>; expansion?: number | null };
  };
  market_cap: { value: number | null; basis: string; shares: number | null };
  financial_status: string;
  validation: string[];
  split_adjustments: string[];
  financial_coverage: Record<string, boolean>;
  provenance: Record<string, { concept?: string; period_end?: string; fiscal_year?: number; fiscal_period?: string; filed?: string; accession?: string; basis?: string; ignored?: string } | null>;
  data_timestamps: { price_date: string; latest_filing: string | null; ttm_period_end: string | null };
  notes: string[];
}

export interface DiscoveryOverview {
  data_mode: DataMode;
  universe_mode: "dynamic" | "fixed";
  master_universe: { total: number; active: number; by_status: Record<string, number>; last_refreshed_at: string | null };
  last_run: null | {
    id: number;
    finished_at: string;
    universe_mode: string | null;
    universe_version: string | null;
    api_calls: { requests: Record<string, number>; cache_hits: Record<string, number> } | null;
    stats: Record<string, unknown> | null;
  };
}

export interface DiscoveryStats {
  universe: number;
  eligible: number;
  selected: number;
  pathway_slots: Record<string, number>;
  coverage: Record<string, number>;
  selected_coverage: Record<string, number>;
  previously_unanalyzed_selected: number;
  previously_unanalyzed_ranked: number;
  selected_sectors: Record<string, number>;
  ranked_sectors: Record<string, number>;
  selected_first_letter_span: string | null;
  excluded: number;
  ranked: number;
}

export interface CandidateRow {
  ticker: string;
  status: "ranked" | "screened" | "excluded" | "selected" | "not_selected" | "ineligible";
  selected: boolean;
  selection_order: number | null;
  pathway: string | null;
  qualifying_pathways: string[];
  baseline_score: number | null;
  catalyst_status: "detected" | "none_detected" | "unknown";
  catalyst_score: number | null;
  previously_analyzed: boolean;
  last_analyzed_at: string | null;
  data_timestamps: Record<string, string | null>;
  score: number | null;
  exclusion_reason: string | null;
  exclusion_codes?: string[];
  valuation_status?: ValuationStatus | null;
  final_rank: number | null;
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
  run: null | {
    id: number;
    started_at: string;
    finished_at: string;
    universe_size: number;
    candidates_evaluated: number;
    notes: Record<string, unknown>;
    stats: DiscoveryStats | null;
    seed: string | null;
  };
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
  valuation_status?: ValuationStatus | null;
  valuation_method?: string | null;
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
  valuation?: ValuationAudit | null;
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
export const useDiscoveryOverview = () =>
  useQuery({ queryKey: ["discoveryOverview"], queryFn: () => api<DiscoveryOverview>("/api/discovery/overview") });
export const useDiscoveryCandidates = (enabled: boolean, scope: "selected" | "all" = "selected") =>
  useQuery({
    queryKey: ["discoveryCandidates", scope],
    queryFn: () =>
      api<{ run_id: number; seed: string; stats: DiscoveryStats; candidates: CandidateRow[]; total_rows: number }>(
        `/api/discovery/candidates?scope=${scope}&limit=2000`,
      ),
    enabled,
    retry: false,
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
