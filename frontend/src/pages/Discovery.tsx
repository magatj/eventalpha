import { useQueryClient } from "@tanstack/react-query";
import { Download, Info, Loader2, Play } from "lucide-react";
import { useMemo, useState } from "react";

import { DemoBadge, EmptyState, ErrorState, Kpi, LoadingRows, PageHeader, ValuationStatusBadge } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { OpportunityTable } from "@/components/OpportunityTable";
import { SelectionAudit, SelectionSummary } from "@/components/SelectionAudit";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/input";
import { api, type DiscoveryItem, type DiscoveryOverview, useDiscovery, useDiscoveryOverview } from "@/lib/api";
import { dateTime, label } from "@/lib/format";

const SECTORS = ["Technology", "Communication Services", "Consumer Discretionary", "Consumer Staples", "Health Care", "Financials", "Industrials", "Energy", "Utilities", "Materials", "Real Estate"];
const SIZES = ["mega", "large", "mid", "small", "micro"];
const PATHWAYS = ["financial_leader", "valuation_opportunity", "new_catalyst", "underexplored", "baseline_leader", "refill"];

function n(v: unknown): string {
  return typeof v === "number" ? v.toLocaleString() : "—";
}

const CODE_LABEL: Record<string, string> = {
  insufficient_financial_data: "Insufficient financial data",
  unknown_market_cap: "Unknown market cap",
  below_min_market_cap: "Below minimum market cap",
  insufficient_liquidity: "Insufficient liquidity",
  stale_price: "Stale price",
  insufficient_price_history: "Short price history",
  invalid_identity: "Invalid company identity",
  foreign_issuer: "Foreign issuer (other provider needed)",
  implausible_financial_data: "Implausible financial data",
  unsupported_valuation: "Unsupported valuation",
};

function CoverageCard({ st }: { st: Record<string, unknown> }) {
  const c = st.coverage_metrics as Record<string, unknown> | undefined;
  if (!c) return null;
  const codes = (c.exclusion_codes ?? {}) as Record<string, number>;
  const vs = (st.valuation_statuses ?? {}) as Record<string, number>;
  const srcs = (c.market_cap_sources ?? {}) as Record<string, number>;
  return (
    <Card className="mb-4">
      <CardHeader>
        <CardTitle>Market-wide data coverage</CardTitle>
        <CardDescription>
          Counts of securities with each input actually available (unknown values are counted as unknown, never as zero). A stock can fail several eligibility checks.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4 lg:grid-cols-8">
          <Kpi label="Discovered" value={n(c.securities_discovered)} />
          <Kpi label="Valid market cap" value={n(c.valid_market_cap)} sub={Object.entries(srcs).map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`).join(" · ")} />
          <Kpi label="Revenue data" value={n(c.with_revenue)} />
          <Kpi label="Net income data" value={n(c.with_net_income)} />
          <Kpi label="Usable cash flow" value={n(c.with_cash_flow)} />
          <Kpi label="Valid valuation method" value={n(c.valid_valuation_method)} />
          <Kpi label="Eligible" value={n(c.eligible)} />
          <Kpi label="Need other provider" value={n(c.requiring_alternative_provider)} sub="foreign issuers / ADRs" />
        </div>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
          <span className="text-subtle">Exclusion reasons (all that apply):</span>
          {Object.entries(codes).map(([k, v]) => (
            <span key={k}>
              {CODE_LABEL[k] ?? k} <strong className="text-foreground">{v.toLocaleString()}</strong>
            </span>
          ))}
        </div>
        {Object.keys(vs).length > 0 && (
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
            <span className="text-subtle">Valuation status of the analyzed candidates:</span>
            {Object.entries(vs).map(([k, v]) => (
              <span key={k} className="inline-flex items-center gap-1">
                {k === "not_analyzed" ? <Badge>Not analyzed</Badge> : <ValuationStatusBadge status={k} />} {v}
              </span>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function MarketOverview({ o }: { o: DiscoveryOverview }) {
  const st = (o.last_run?.stats ?? {}) as Record<string, unknown>;
  const dynamic = (o.last_run?.universe_mode ?? o.universe_mode) === "dynamic";
  const calls = o.last_run?.api_calls?.requests ?? {};
  return (
    <Card className="mb-4">
      <CardHeader>
        <CardTitle>Market Discovery Overview</CardTitle>
        <CardDescription>
          {dynamic
            ? "Real counts from the master universe and the latest discovery run (whole US-listed market, NYSE / Nasdaq / NYSE American)."
            : "Fixed-universe mode (universe.txt). Set DISCOVERY_UNIVERSE_MODE=dynamic for market-wide discovery."}
          {o.master_universe.last_refreshed_at && ` Universe refreshed ${dateTime(o.master_universe.last_refreshed_at)}.`}
        </CardDescription>
      </CardHeader>
      <CardContent className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <Kpi label="US securities discovered" value={n(o.master_universe.total)} sub={`${n(o.master_universe.active)} actively listed`} />
        <Kpi label="Eligible stocks" value={n(st.eligible)} sub={`of ${n(st.screened ?? st.universe)} screened`} />
        <Kpi label="Valid financials" value={n(st.valid_financials)} sub={`${n(st.needs_alt_fundamentals)} need other fundamentals`} />
        <Kpi label="Recent news coverage" value={n(st.recent_news_coverage)} sub="stocks with events in 14 days" />
        <Kpi label="Newly discovered" value={n(st.newly_discovered)} sub="first time in the candidate pool" />
        <Kpi label="Candidates selected" value={n(st.selected)} sub={`${n(st.excluded_after_analysis ?? st.excluded)} excluded after analysis`} />
        <Kpi label="Quantitative analyses" value={n(st.candidates_analyzed ?? st.selected)} />
        <Kpi label="AI analyses" value={n(st.ai_completed)} sub="top 10 only" />
        <Kpi label="Top opportunities" value={n(st.published ?? st.ranked)} sub="published (minimum score applies)" />
        <Kpi
          label="API calls (last run)"
          value={Object.values(calls).reduce((a, b) => a + b, 0).toLocaleString()}
          sub={Object.entries(calls).map(([k, v]) => `${k} ${v}`).join(" · ") || (typeof st.processing_seconds === "number" ? `${Math.round(st.processing_seconds as number)}s` : "")}
        />
      </CardContent>
    </Card>
  );
}

type Filters = { sector: string; size: string; minStrength: string; minGrowth: string; minValuation: string; risk: string; catalyst: string; horizon: string; pathway: string };
const EMPTY: Filters = { sector: "", size: "", minStrength: "", minGrowth: "", minValuation: "", risk: "", catalyst: "", horizon: "", pathway: "" };

function applyFilters(items: DiscoveryItem[], f: Filters): DiscoveryItem[] {
  return items.filter(
    (i) =>
      (!f.sector || i.sector === f.sector) &&
      (!f.size || i.size === f.size) &&
      (!f.minStrength || (i.financial_strength ?? -1) >= Number(f.minStrength)) &&
      (!f.minGrowth || (i.potential_return ?? -9) >= Number(f.minGrowth)) &&
      (!f.minValuation || (i.valuation ?? -1) >= Number(f.minValuation)) &&
      (!f.risk || i.risk_rating === f.risk) &&
      (!f.catalyst || i.catalyst_category === f.catalyst) &&
      (!f.horizon || i.horizon_class === f.horizon) &&
      (!f.pathway || i.selection_pathway === f.pathway || (i.qualifying_pathways ?? []).includes(f.pathway)),
  );
}

export default function DiscoveryPage() {
  const [sort, setSort] = useState("score");
  const [f, setF] = useState<Filters>(EMPTY);
  const { data, isLoading, error, refetch } = useDiscovery(`?sort=${sort}`);
  const overview = useDiscoveryOverview();
  const qc = useQueryClient();
  const [starting, setStarting] = useState(false);
  const run = async () => {
    setStarting(true);
    try {
      await api("/api/discovery/run", { method: "POST" });
      await qc.invalidateQueries({ queryKey: ["discovery"] });
    } finally {
      setStarting(false);
    }
  };
  const running = data?.running_job;
  const items = useMemo(() => applyFilters(data?.items ?? [], f), [data, f]);
  const categories = useMemo(() => [...new Set((data?.items ?? []).map((i) => i.catalyst_category).filter(Boolean))] as string[], [data]);
  const set = (k: keyof Filters) => (e: React.ChangeEvent<HTMLSelectElement>) => setF({ ...f, [k]: e.target.value });
  const active = Object.values(f).some(Boolean);
  const notes = data?.run?.notes as { skipped?: { ticker: string; reason: string }[] } | undefined;
  return (
    <div>
      <PageHeader
        title="Stock Discovery"
        description="Market-wide funnel: listed securities → eligibility screening → financial/valuation/catalyst selection → deep quantitative analysis → AI for the top 10 → up to 20 published opportunities."
        actions={
          <>
            <DemoBadge mode={data?.data_mode} />
            {data?.run && (
              <Button variant="outline" asChild>
                <a href={`/api/discovery/audit.csv?run_id=${data.run.id}`} download>
                  <Download /> Audit CSV
                </a>
              </Button>
            )}
            <Button onClick={run} disabled={!!running || starting}>
              {running || starting ? <Loader2 className="animate-spin" /> : <Play />}
              {running ? "Running…" : "Run discovery"}
            </Button>
          </>
        }
      />
      {running && (
        <Card className="mb-4 border-accent/40 p-3 text-sm">
          <Loader2 className="mr-2 inline size-4 animate-spin text-accent" />
          {running.status === "queued" ? "Queued — waiting for the worker" : running.progress ?? "Running"}
        </Card>
      )}
      {overview.data && <MarketOverview o={overview.data} />}
      {overview.data?.last_run?.stats && <CoverageCard st={overview.data.last_run.stats as Record<string, unknown>} />}
      {data?.run?.stats && data.run.stats.universe != null && !("screened" in (data.run.stats as object)) && (
        <div className="mb-4">
          <SelectionSummary stats={data.run.stats} />
        </div>
      )}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Select value={sort} onChange={(e) => setSort(e.target.value)} aria-label="Sort by">
          <option value="score">Sort: Score</option>
          <option value="growth">Sort: Potential growth</option>
          <option value="risk">Sort: Lowest risk</option>
          <option value="event_date">Sort: Event date</option>
          <option value="forecast_date">Sort: Forecast date</option>
        </Select>
        <Select value={f.sector} onChange={set("sector")} aria-label="Sector">
          <option value="">All sectors</option>
          {SECTORS.map((s) => <option key={s}>{s}</option>)}
        </Select>
        <Select value={f.size} onChange={set("size")} aria-label="Company size">
          <option value="">Any size</option>
          {SIZES.map((s) => <option key={s} value={s}>{label(s)} cap</option>)}
        </Select>
        <Select value={f.minStrength} onChange={set("minStrength")} aria-label="Financial strength">
          <option value="">Any financial strength</option>
          <option value="0.5">Strength ≥ 0.5</option>
          <option value="0.7">Strength ≥ 0.7</option>
          <option value="0.85">Strength ≥ 0.85</option>
        </Select>
        <Select value={f.minGrowth} onChange={set("minGrowth")} aria-label="Growth potential">
          <option value="">Any growth potential</option>
          <option value="0">Base return ≥ 0%</option>
          <option value="0.1">Base return ≥ 10%</option>
          <option value="0.25">Base return ≥ 25%</option>
        </Select>
        <Select value={f.minValuation} onChange={set("minValuation")} aria-label="Valuation">
          <option value="">Any valuation</option>
          <option value="0.4">Valuation factor ≥ 0.4</option>
          <option value="0.6">Valuation factor ≥ 0.6</option>
        </Select>
        <Select value={f.risk} onChange={set("risk")} aria-label="Risk">
          <option value="">Any risk</option>
          <option>Low</option>
          <option>Medium</option>
          <option>High</option>
        </Select>
        <Select value={f.catalyst} onChange={set("catalyst")} aria-label="Catalyst category">
          <option value="">Any catalyst</option>
          {categories.map((c) => <option key={c} value={c}>{label(c)}</option>)}
        </Select>
        <Select value={f.horizon} onChange={set("horizon")} aria-label="Horizon">
          <option value="">Any horizon</option>
          <option value="short_term">Short-term</option>
          <option value="long_term">Long-term</option>
        </Select>
        <Select value={f.pathway} onChange={set("pathway")} aria-label="Discovery pathway">
          <option value="">Any pathway</option>
          {PATHWAYS.map((p) => <option key={p} value={p}>{label(p)}</option>)}
        </Select>
        {active && (
          <Button size="sm" variant="ghost" onClick={() => setF(EMPTY)}>
            Clear filters
          </Button>
        )}
        {data?.run && (
          <span className="ml-auto text-xs text-subtle">
            Run #{data.run.id} · {dateTime(data.run.finished_at)} · {data.run.candidates_evaluated} analyzed of {data.run.universe_size} screened
          </span>
        )}
      </div>
      <Card>
        <CardContent className="pt-4">
          {isLoading ? (
            <LoadingRows rows={10} />
          ) : error ? (
            <ErrorState error={error} onRetry={refetch} />
          ) : items.length ? (
            <OpportunityTable items={items} />
          ) : (
            <EmptyState title={data?.run ? "No results match these filters" : "No discovery run yet"}>
              {!data?.run && "Click “Run discovery”. The first market-wide run collects ~3 months of prices for every listed stock and is rate-limited on the free Massive tier, so it can take over an hour."}
            </EmptyState>
          )}
        </CardContent>
      </Card>
      {data?.ranking_explanation && (
        <p className="mt-3 flex gap-1.5 text-xs text-muted">
          <Info className="mt-0.5 size-3.5 shrink-0" /> {data.ranking_explanation}
        </p>
      )}
      <SelectionAudit />
      {notes?.skipped && notes.skipped.length > 0 && (
        <details className="mt-3 text-xs text-muted">
          <summary className="cursor-pointer">{notes.skipped.length} candidate(s) skipped</summary>
          <ul className="mt-2 list-disc pl-5">
            {notes.skipped.map((s) => (
              <li key={s.ticker}>
                <strong>{s.ticker}</strong>: {s.reason}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
