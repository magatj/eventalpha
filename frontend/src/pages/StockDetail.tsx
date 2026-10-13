import { useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ExternalLink, Loader2, RefreshCw, Star } from "lucide-react";
import React, { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { FinBarChart, PriceChart, ScenarioChart, TargetHistoryChart } from "@/components/charts";
import {
  DemoBadge,
  Disclaimer,
  EmptyState,
  ErrorState,
  EventStatusBadge,
  ImpactBadge,
  Kpi,
  LoadingRows,
  OutlookBadge,
  PriceTag,
  ProvenanceBadge,
  ReturnText,
  RiskBadge,
} from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  api,
  ApiError,
  type Claim,
  type EventItem,
  type ForecastRecord,
  useAnalysis,
  useAnalyze,
  useFinancials,
  useForecast,
  useForecastHistory,
  useJob,
  usePrices,
  useStock,
  useStockNews,
} from "@/lib/api";
import { compact, dateTime, day, isNum, label, money, num, pct, relative } from "@/lib/format";
import { cn } from "@/lib/utils";

export default function StockDetailPage() {
  const { ticker = "" } = useParams();
  const t = ticker.toUpperCase();
  const stock = useStock(t);
  const qc = useQueryClient();
  const analyze = useAnalyze();
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useJob(jobId);
  const [watchMsg, setWatchMsg] = useState<string | null>(null);

  useEffect(() => {
    if (job.data?.status === "succeeded") {
      qc.invalidateQueries();
      setJobId(null);
    }
  }, [job.data?.status, qc]);

  const refresh = () =>
    analyze.mutate({ ticker: t, force: true }, { onSuccess: (r) => ("job_id" in r ? setJobId(r.job_id) : qc.invalidateQueries()) });
  const addWatch = async () => {
    const r = await api<{ added: boolean; message?: string }>("/api/watchlist", { method: "POST", body: JSON.stringify({ ticker: t }) });
    setWatchMsg(r.added ? "Added to watchlist" : r.message ?? "Already on watchlist");
    qc.invalidateQueries({ queryKey: ["watchlist"] });
  };

  if (stock.isLoading)
    return (
      <div className="flex flex-col gap-4">
        <Skeleton className="h-20" />
        <Skeleton className="h-80" />
      </div>
    );
  if (stock.error) {
    const notAnalyzed = stock.error instanceof ApiError && stock.error.status === 404;
    return notAnalyzed ? (
      <EmptyState title={`${t} hasn't been analyzed in this data mode yet`}>
        <Button className="mt-3" asChild>
          <Link to={`/analyze?ticker=${t}&run=1`}>Analyze {t}</Link>
        </Button>
      </EmptyState>
    ) : (
      <ErrorState error={stock.error} onRetry={stock.refetch} />
    );
  }
  const s = stock.data!;
  const running = jobId && job.data && ["queued", "running"].includes(job.data.status);

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-2xl font-semibold tracking-tight">{s.ticker}</h1>
            <span className="text-muted">{s.name}</span>
            <DemoBadge mode={s.data_mode} />
          </div>
          <div className="mt-1 flex flex-wrap gap-1.5 text-xs text-subtle">
            {s.exchange && <Badge>{s.exchange}</Badge>}
            {s.sector && <Badge>{s.sector}</Badge>}
            {s.industry && <Badge>{s.industry}</Badge>}
            {s.cik && <Badge>CIK {s.cik}</Badge>}
          </div>
        </div>
        <div className="flex items-start gap-4">
          <PriceTag p={s.latest_price} size="lg" />
          <div className="flex flex-col gap-2">
            <Button size="sm" onClick={refresh} disabled={!!running || analyze.isPending}>
              {running || analyze.isPending ? <Loader2 className="animate-spin" /> : <RefreshCw />}
              {running ? job.data?.progress ?? "Queued…" : "Refresh analysis"}
            </Button>
            <Button size="sm" variant="secondary" onClick={addWatch}>
              <Star /> {watchMsg ?? "Add to watchlist"}
            </Button>
          </div>
        </div>
      </div>
      {job.data?.status === "failed" && <ErrorState error={new Error(job.data.error ?? "failed")} title="Refresh failed" />}
      {s.latest_analysis && (
        <p className="-mt-2 text-xs text-subtle">
          Last analysis {relative(s.latest_analysis.created_at)} ({dateTime(s.latest_analysis.created_at)}) ·{" "}
          {s.latest_analysis.ai_kind === "openai" ? `AI: ${s.latest_analysis.ai_model}` : s.latest_analysis.ai_kind === "demo" ? "demo rule-based" : "rule-based (no AI)"}
        </p>
      )}

      <Tabs defaultValue="overview">
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="financials">Financials</TabsTrigger>
          <TabsTrigger value="news">News & Catalysts</TabsTrigger>
          <TabsTrigger value="thesis">AI Thesis</TabsTrigger>
          <TabsTrigger value="forecast">Forecast</TabsTrigger>
        </TabsList>
        <TabsContent value="overview">
          <Overview t={t} />
        </TabsContent>
        <TabsContent value="financials">
          <FinancialsTab t={t} />
        </TabsContent>
        <TabsContent value="news">
          <NewsTab t={t} />
        </TabsContent>
        <TabsContent value="thesis">
          <ThesisTab t={t} />
        </TabsContent>
        <TabsContent value="forecast">
          <ForecastTab t={t} />
        </TabsContent>
      </Tabs>
      <Disclaimer />
    </div>
  );
}

// ------------------------------------------------------------------------------- overview
function Overview({ t }: { t: string }) {
  const s = useStock(t).data!;
  const [range, setRange] = useState("1y");
  const [indexed, setIndexed] = useState(false);
  const prices = usePrices(t, range);
  const a = s.latest_analysis;
  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 lg:grid-cols-7">
        <Kpi label="Market cap" value={compact(s.market_cap.value)} sub={s.market_cap.basis} />
        <Kpi label="52w high" value={money(s.high_52w)} />
        <Kpi label="52w low" value={money(s.low_52w)} />
        <Kpi label="Volume" value={compact(s.latest_price?.volume, "")} sub={s.latest_price ? day(s.latest_price.trade_date) : undefined} />
        <Kpi label="P/E (TTM)" value={isNum(s.pe_ttm) ? `${num(s.pe_ttm, 1)}x` : "—"} sub={s.pe_note ?? undefined} />
        <Kpi label="Volatility 1y" value={pct(s.volatility_1y, 0, false)} />
        <Kpi label="Return 1y" value={<ReturnText value={s.return_1y} />} />
      </div>
      <Card>
        <CardHeader className="flex-row flex-wrap items-center justify-between gap-2">
          <div>
            <CardTitle>Price history</CardTitle>
            <CardDescription>
              Daily closes, split-adjusted. <ProvenanceBadge kind="observed" />
            </CardDescription>
          </div>
          <div className="flex flex-wrap items-center gap-1">
            {["3m", "6m", "1y", "2y"].map((r) => (
              <Button key={r} size="sm" variant={range === r ? "secondary" : "ghost"} onClick={() => setRange(r)}>
                {r.toUpperCase()}
              </Button>
            ))}
            <Button size="sm" variant={indexed ? "secondary" : "ghost"} onClick={() => setIndexed(!indexed)} title="Compare % change against the S&P 500 (SPY)">
              vs S&P 500
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {prices.isLoading ? <Skeleton className="h-72" /> : prices.data?.bars.length ? (
            <PriceChart bars={prices.data.bars} benchmark={prices.data.benchmark} ticker={t} indexed={indexed} />
          ) : (
            <EmptyState title="No price history stored" />
          )}
        </CardContent>
      </Card>
      {a && (
        <Card>
          <CardHeader>
            <CardTitle>Latest analysis snapshot</CardTitle>
          </CardHeader>
          <CardContent className="grid grid-cols-2 gap-3 md:grid-cols-5">
            <Kpi label="Outlook (quant)" value={<OutlookBadge outlook={a.outlook} />} sub={a.ai_outlook ? `AI view: ${a.ai_outlook}` : undefined} />
            <Kpi label="Risk" value={<RiskBadge risk={a.risk_rating} />} />
            <Kpi label="Bear 12m" value={money(a.bear_target_12m)} badge={<ProvenanceBadge kind="model_forecast" />} />
            <Kpi label="Base 12m" value={money(a.base_target_12m)} sub={<ReturnText value={a.base_return_12m} />} />
            <Kpi label="Bull 12m" value={money(a.bull_target_12m)} />
          </CardContent>
        </Card>
      )}
    </div>
  );
}

// ------------------------------------------------------------------------------- financials
function FinancialsTab({ t }: { t: string }) {
  const { data: f, isLoading, error, refetch } = useFinancials(t);
  if (isLoading) return <LoadingRows rows={8} />;
  if (error) return <ErrorState error={error} onRetry={refetch} title="Financial statements unavailable" />;
  if (!f) return null;
  const ttm = f.ttm;
  const r = f.ratios;
  const fcfQ = f.quarterly.free_cash_flow ?? [];
  return (
    <div className="flex flex-col gap-4">
      <Card className="p-3 text-xs text-muted">
        <div className="flex flex-wrap items-center gap-2">
          <ProvenanceBadge kind="observed" />
          Source: {f.source}
          {f.source_url && (
            <a className="inline-flex items-center gap-1 text-accent" href={f.source_url} target="_blank" rel="noreferrer">
              CompanyFacts JSON <ExternalLink className="size-3" />
            </a>
          )}
          · Reporting currency {f.currency} · TTM through {day(f.ttm_period_end)} · latest filing {day(f.latest_filed)}
        </div>
        <div className="mt-1 break-all text-subtle">Data version: {f.financial_data_version}</div>
      </Card>
      {f.warnings.length > 0 && <Warnings items={f.warnings} />}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <Kpi label="Revenue (TTM)" value={compact(ttm.revenue)} sub={<>YoY <ReturnText value={r.revenue_growth_yoy} /></>} />
        <Kpi label="Revenue CAGR 3y" value={pct(r.revenue_cagr_3y)} />
        <Kpi label="EPS (TTM, diluted)" value={money(ttm.eps)} sub={<>YoY <ReturnText value={r.eps_growth_yoy} /></>} />
        <Kpi label="Operating margin" value={pct(r.operating_margin, 1, false)} />
        <Kpi label="Net margin" value={pct(r.net_margin, 1, false)} />
        <Kpi label="Free cash flow (TTM)" value={compact(ttm.free_cash_flow)} sub={`FCF margin ${pct(r.fcf_margin, 1, false)}`} />
        <Kpi label="Cash & ST investments" value={compact(ttm.cash_and_investments)} />
        <Kpi label="Total debt" value={compact(ttm.total_debt)} />
        <Kpi label="Gross margin" value={pct(r.gross_margin, 1, false)} />
        <Kpi label="Diluted shares" value={compact(ttm.diluted_shares, "")} />
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <ChartCard title="Quarterly revenue" points={f.quarterly.revenue} />
        <ChartCard title="Quarterly net income" points={f.quarterly.net_income} />
        <ChartCard title="Quarterly diluted EPS" points={f.quarterly.eps_diluted} format="eps" />
        <ChartCard title="Quarterly free cash flow" points={fcfQ} note="OCF − capex; lighter bars are derived from year-to-date filings." />
      </div>
      <Card>
        <CardHeader>
          <CardTitle>Annual history</CardTitle>
        </CardHeader>
        <CardContent>
          <AnnualTable f={f} />
        </CardContent>
      </Card>
      {f.notes.map((n) => (
        <p key={n} className="text-[11px] text-subtle">
          {n}
        </p>
      ))}
    </div>
  );
}

function ChartCard({ title, points, format, note }: { title: string; points?: import("@/lib/api").FinPoint[]; format?: "money" | "eps"; note?: string }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        {note && <CardDescription>{note}</CardDescription>}
      </CardHeader>
      <CardContent>{points?.length ? <FinBarChart points={points} name={title} valueFormat={format} /> : <p className="py-8 text-center text-sm text-subtle">Not reported</p>}</CardContent>
    </Card>
  );
}

function AnnualTable({ f }: { f: import("@/lib/api").Financials }) {
  const years = (f.annual.revenue ?? []).map((p) => p.period_end);
  const rows: [string, string, "money" | "eps" | "shares"][] = [
    ["Revenue", "revenue", "money"],
    ["Gross profit", "gross_profit", "money"],
    ["Operating income", "operating_income", "money"],
    ["Net income", "net_income", "money"],
    ["Diluted EPS", "eps_diluted", "eps"],
    ["Operating cash flow", "operating_cash_flow", "money"],
    ["Capital expenditures", "capex", "money"],
    ["Free cash flow", "free_cash_flow", "money"],
  ];
  if (!years.length) return <p className="text-sm text-subtle">No annual data.</p>;
  return (
    <Table>
      <THead>
        <TR>
          <TH>Fiscal year</TH>
          {years.map((y) => (
            <TH key={y} className="text-right">
              {(f.annual.revenue.find((p) => p.period_end === y)?.fiscal_year ?? y.slice(0, 4))}
              <div className="font-normal normal-case text-subtle">ends {day(y)}</div>
            </TH>
          ))}
        </TR>
      </THead>
      <TBody>
        {rows.map(([lbl, key, kind]) => (
          <TR key={key}>
            <TD className="text-muted">{lbl}</TD>
            {years.map((y) => {
              const v = f.annual[key]?.find((p) => p.period_end === y)?.value;
              return (
                <TD key={y} className="text-right">
                  {kind === "eps" ? money(v) : compact(v)}
                </TD>
              );
            })}
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

function Warnings({ items }: { items: string[] }) {
  return (
    <Card className="border-warn/40 bg-warn/5 p-3">
      <ul className="flex flex-col gap-1 text-xs text-warn">
        {items.map((w) => (
          <li key={w} className="flex gap-1.5">
            <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
            {w}
          </li>
        ))}
      </ul>
    </Card>
  );
}

// ------------------------------------------------------------------------------- news
export function EventCard({ e }: { e: EventItem }) {
  return (
    <div className="rounded-lg border border-border bg-surface-2/40 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="text-sm font-medium">{e.url ? <a href={e.url} target="_blank" rel="noreferrer" className="hover:text-accent">{e.title}</a> : e.title}</div>
        <div className="flex flex-wrap gap-1">
          <Badge>{label(e.event_type)}</Badge>
          <EventStatusBadge status={e.status} />
          <ImpactBadge impact={e.impact} />
        </div>
      </div>
      {e.summary && <p className="mt-1 line-clamp-3 text-xs text-muted">{e.summary}</p>}
      <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-subtle">
        <span>{e.source_name}</span>
        {e.published_at && <span>Published {dateTime(e.published_at)}</span>}
        {e.event_date && <span className="text-foreground">Event date {day(e.event_date)}</span>}
        {e.article_count > 1 && <span>{e.article_count} articles merged</span>}
        {isNum(e.relationship_confidence) && <span>Link confidence {pct(e.relationship_confidence, 0, false)}</span>}
        {isNum(e.materiality) && <span>Materiality {num(e.materiality, 2)}</span>}
      </div>
      {e.impact_explanation && <p className="mt-1 text-[11px] text-subtle">{e.impact_explanation}</p>}
      {e.ai_analysis && <AIEventBlock a={e.ai_analysis} />}
      {e.articles.length > 1 && (
        <details className="mt-2 text-xs">
          <summary className="cursor-pointer text-subtle">Sources ({e.articles.length})</summary>
          <ul className="mt-1 flex flex-col gap-1">
            {e.articles.map((a) => (
              <li key={a.url}>
                <a href={a.url} target="_blank" rel="noreferrer" className="text-accent">
                  {a.source_name ?? "source"}
                </a>{" "}
                · {dateTime(a.published_at)} {a.is_rumor && <Badge variant="warn">rumor language</Badge>}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function AIEventBlock({ a }: { a: Record<string, unknown> }) {
  const impacts = (a.affected_companies as { ticker: string; relationship: string; operational_impact: string; direction: string; time_frame: string; relationship_confidence: string }[]) ?? [];
  return (
    <div className="mt-2 rounded-md border border-accent/30 bg-accent/5 p-2 text-xs">
      <div className="mb-1 flex items-center gap-1.5">
        <ProvenanceBadge kind="ai" /> <span className="text-muted">{String(a.factual_summary ?? "")}</span>
      </div>
      {impacts.map((c) => (
        <div key={c.ticker} className="text-muted">
          <strong className="text-foreground">{c.ticker}</strong> ({c.direction}, {label(c.time_frame)}, {c.relationship_confidence} confidence): {c.relationship} — {c.operational_impact}
        </div>
      ))}
    </div>
  );
}

function NewsTab({ t }: { t: string }) {
  const { data, isLoading, error, refetch } = useStockNews(t);
  if (isLoading) return <LoadingRows rows={6} />;
  if (error) return <ErrorState error={error} onRetry={refetch} />;
  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <div className="flex flex-col gap-3 lg:col-span-2">
        <h3 className="text-sm font-semibold">Recent events (60 days)</h3>
        {data?.recent.length ? data.recent.map((e) => <EventCard key={e.id} e={e} />) : <EmptyState title="No recent company events" />}
      </div>
      <div className="flex flex-col gap-3">
        <h3 className="text-sm font-semibold">Upcoming catalysts</h3>
        {data?.upcoming.length ? data.upcoming.map((e) => <EventCard key={e.id} e={e} />) : <EmptyState title="No scheduled events found" />}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------------------- thesis
const CLAIM_LABEL: Record<Claim["claim_type"], string> = {
  verified_fact: "Fact",
  management_guidance: "Guidance",
  analyst_estimate: "Analyst est.",
  model_assumption: "Assumption",
  speculative: "Speculative",
};

function ClaimList({ title, claims, tone }: { title: string; claims?: Claim[]; tone?: "up" | "down" }) {
  if (!claims?.length) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle className={cn(tone === "up" && "text-up", tone === "down" && "text-down")}>{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <ul className="flex flex-col gap-2">
          {claims.map((c, i) => (
            <li key={i} className="text-sm">
              <span>{c.text}</span>{" "}
              <Badge variant={c.claim_type === "speculative" ? "warn" : c.claim_type === "verified_fact" ? "default" : "accent"}>{CLAIM_LABEL[c.claim_type]}</Badge>
              {c.source_ids.map((sid) => (
                <a key={sid} href={`#src-${sid}`} className="ml-1 text-[11px] text-accent">
                  [{sid}]
                </a>
              ))}
              {c.warning && <div className="text-[11px] text-warn">{c.warning}</div>}
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

function ThesisTab({ t }: { t: string }) {
  const { data: a, isLoading, error, refetch } = useAnalysis(t);
  if (isLoading) return <LoadingRows rows={8} />;
  if (error) return <ErrorState error={error} onRetry={refetch} />;
  if (!a) return null;
  const th = a.thesis as Record<string, unknown> & { summary: string };
  const C = (k: string) => th[k] as Claim[] | undefined;
  return (
    <div className="flex flex-col gap-4">
      <Card className="p-4">
        <div className="flex flex-wrap items-center gap-2">
          {a.ai_kind === "openai" ? <ProvenanceBadge kind="ai" /> : <Badge variant={a.ai_kind === "demo" ? "demo" : "warn"}>{a.ai_kind === "demo" ? "Demo rule-based summary" : "Rule-based summary (no AI)"}</Badge>}
          {a.ai_model && <Badge>{a.ai_model}</Badge>}
          {a.prompt_version && <Badge>prompt {a.prompt_version}</Badge>}
          {isNum(a.cost_usd) && <Badge>AI cost ${a.cost_usd.toFixed(4)}</Badge>}
          <OutlookBadge outlook={String(th.outlook ?? "")} />
          <span className="text-xs text-subtle">{dateTime(a.created_at)}</span>
        </div>
        <p className="mt-3 text-sm leading-relaxed">{th.summary}</p>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <div className="rounded-md bg-surface-2 p-3 text-sm">
            <div className="mb-1 text-[11px] uppercase tracking-wide text-subtle">Short-term outlook</div>
            {String(th.short_term_outlook ?? "—")}
          </div>
          <div className="rounded-md bg-surface-2 p-3 text-sm">
            <div className="mb-1 text-[11px] uppercase tracking-wide text-subtle">Long-term outlook</div>
            {String(th.long_term_outlook ?? "—")}
          </div>
        </div>
        <p className="mt-3 text-[11px] text-subtle">The language model never sets price targets; all targets come from the quantitative engine.</p>
      </Card>
      {a.warnings.length > 0 && <Warnings items={a.warnings} />}
      <div className="grid gap-4 md:grid-cols-2">
        <ClaimList title="Bullish arguments" claims={C("bullish_arguments")} tone="up" />
        <ClaimList title="Bearish arguments" claims={C("bearish_arguments")} tone="down" />
        <ClaimList title="Factual events" claims={C("factual_events")} />
        <ClaimList title="Expected catalysts" claims={C("expected_catalysts")} />
        <ClaimList title="Business opportunities" claims={C("business_opportunities")} />
        <ClaimList title="Revenue drivers" claims={C("revenue_drivers")} />
        <ClaimList title="Margin implications" claims={C("margin_implications")} />
        <ClaimList title="Competitive risks" claims={C("competitive_risks")} />
        <ClaimList title="Valuation risks" claims={C("valuation_risks")} />
        <ClaimList title="Principal risks" claims={C("principal_risks")} />
        <ClaimList title="Contradictory evidence" claims={C("contradictory_evidence")} />
      </div>
      {Array.isArray(th.data_gaps) && th.data_gaps.length > 0 && <Warnings items={th.data_gaps as string[]} />}
      {a.diff && <DiffCard diff={a.diff} prevAt={a.previous?.created_at} />}
      <Card>
        <CardHeader>
          <CardTitle>Sources</CardTitle>
          <CardDescription>Every cited claim links to one of these.</CardDescription>
        </CardHeader>
        <CardContent>
          <ol className="flex flex-col gap-1.5 text-xs">
            {a.sources.map((s) => (
              <li key={s.label} id={`src-${s.label}`} className="flex gap-2">
                <span className="w-8 shrink-0 font-mono text-accent">{s.label}</span>
                <span>
                  <a href={s.url} target="_blank" rel="noreferrer" className="hover:text-accent">
                    {s.title ?? s.url}
                  </a>
                  <span className="text-subtle">
                    {" "}
                    · {s.publisher ?? label(s.type)} {s.published_at && `· ${dateTime(s.published_at)}`}
                  </span>
                </span>
              </li>
            ))}
          </ol>
        </CardContent>
      </Card>
    </div>
  );
}

export function DiffCard({ diff, prevAt }: { diff: import("@/lib/api").Diff; prevAt?: string }) {
  const fmt = (v: unknown) => (typeof v === "number" ? (Math.abs(v) < 5 ? num(v, 3) : num(v, 2)) : v == null ? "—" : String(v));
  return (
    <Card>
      <CardHeader>
        <CardTitle>Changes since previous analysis</CardTitle>
        {prevAt && <CardDescription>Previous: {dateTime(prevAt)}</CardDescription>}
      </CardHeader>
      <CardContent>
        {diff.changes.length === 0 && diff.bullish_points.added.length + diff.bearish_points.added.length === 0 ? (
          <p className="text-sm text-muted">No material changes.</p>
        ) : (
          <div className="flex flex-col gap-2 text-sm">
            {diff.changes.map((c) => (
              <div key={c.field} className="flex flex-wrap gap-2">
                <span className="w-44 text-muted">{c.label}</span>
                <span className="num text-subtle line-through">{fmt(c.previous)}</span>→<span className="num">{fmt(c.current)}</span>
              </div>
            ))}
            {diff.bullish_points.added.map((p) => (
              <div key={p} className="text-up">+ Bullish: {p}</div>
            ))}
            {diff.bearish_points.added.map((p) => (
              <div key={p} className="text-down">+ Bearish: {p}</div>
            ))}
            {[...diff.bullish_points.removed, ...diff.bearish_points.removed].map((p) => (
              <div key={p} className="text-subtle">− Removed: {p}</div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ------------------------------------------------------------------------------- forecast
function ForecastTab({ t }: { t: string }) {
  const { data, isLoading, error, refetch } = useForecast(t);
  const hist = useForecastHistory(t, 12);
  const [h, setH] = useState(12);
  if (isLoading) return <LoadingRows rows={8} />;
  if (error) return <ErrorState error={error} onRetry={refetch} />;
  const f = data?.forecasts.find((x) => x.horizon_months === h);
  if (!f) return <EmptyState title="No forecast" />;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-subtle">Horizon</span>
        {data!.forecasts.map((x) => (
          <Button key={x.horizon_months} size="sm" variant={x.horizon_months === h ? "secondary" : "ghost"} onClick={() => setH(x.horizon_months)}>
            {x.horizon_months}m
          </Button>
        ))}
        <span className="ml-auto text-xs text-subtle">
          Target date <strong className="text-foreground">{day(f.target_date)}</strong> · from {money(f.observed_price)} on {day(f.observed_price_date)}
        </span>
      </div>
      <Card className="border-accent/30 bg-accent/5 p-3 text-xs text-muted">
        <ProvenanceBadge kind="model_forecast" /> {data!.interpretation}
      </Card>
      {f.assumptions?.warnings && f.assumptions.warnings.length > 0 && <Warnings items={f.assumptions.warnings} />}
      <div className="grid gap-4 lg:grid-cols-5">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Bear / base / bull value at {h} months</CardTitle>
            <CardDescription>Bars: blended model target; label: return vs current price.</CardDescription>
          </CardHeader>
          <CardContent>
            <ScenarioChart scenarios={f.scenarios} price={f.observed_price} />
            {f.assumptions?.volatility_range && (
              <p className="mt-2 text-[11px] text-subtle">
                Short-term movement (separate from valuation): 1σ band {money(f.assumptions.volatility_range.low_1sigma)} – {money(f.assumptions.volatility_range.high_1sigma)}.{" "}
                {f.assumptions.volatility_range.note}
              </p>
            )}
          </CardContent>
        </Card>
        <Card className="lg:col-span-3">
          <CardHeader>
            <CardTitle>Scenario detail</CardTitle>
          </CardHeader>
          <CardContent>
            <ScenarioTable f={f} />
          </CardContent>
        </Card>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>Scenario explanations & methods</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-2 text-sm">
          {(["bear", "base", "bull"] as const).map((s) => (
            <p key={s}>
              <strong className={cn(s === "bear" && "text-down", s === "bull" && "text-up")}>{s.toUpperCase()}</strong> — {f.scenarios[s].explanation}
            </p>
          ))}
          <MethodNotes f={f} />
        </CardContent>
      </Card>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Assumptions</CardTitle>
          </CardHeader>
          <CardContent>
            <Table>
              <TBody>
                {f.assumptions?.items.map((a) => (
                  <TR key={a.name}>
                    <TD className="whitespace-normal text-muted">{a.name}</TD>
                    <TD className="whitespace-normal">{fmtAssumption(a.value)}</TD>
                    <TD className="whitespace-normal text-[11px] text-subtle">
                      <Badge variant={a.kind === "ai_adjustment" ? "accent" : a.kind === "observed" ? "default" : "warn"}>{label(a.kind)}</Badge> {a.basis}
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          </CardContent>
        </Card>
        <Sensitivity f={f} />
      </div>
      <Card>
        <CardHeader>
          <CardTitle>12-month forecast history</CardTitle>
          <CardDescription>Each analysis stores a new immutable forecast; earlier ones are never overwritten.</CardDescription>
        </CardHeader>
        <CardContent>
          {hist.data && hist.data.forecasts.length > 0 ? (
            <TargetHistoryChart
              rows={[...hist.data.forecasts].reverse().map((x) => ({
                created_at: x.created_at,
                observed_price: x.observed_price,
                base: x.scenarios.base?.target_price ?? undefined,
                bear: x.scenarios.bear?.target_price ?? undefined,
                bull: x.scenarios.bull?.target_price ?? undefined,
              }))}
            />
          ) : (
            <p className="text-sm text-subtle">Only one forecast so far.</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function fmtAssumption(v: unknown): string {
  if (typeof v === "number") return Math.abs(v) < 1 ? pct(v, 2, false) : num(v, 2);
  if (Array.isArray(v)) return v.map((x) => (typeof x === "number" ? num(x, 1) : String(x))).join(" / ");
  if (v && typeof v === "object") return Object.entries(v).map(([k, x]) => `${label(k)} ${typeof x === "number" ? pct(x, 1) : String(x)}`).join(", ");
  return String(v);
}

function ScenarioTable({ f }: { f: ForecastRecord }) {
  const rows: [string, (s: import("@/lib/api").ScenarioResult) => React.ReactNode][] = [
    ["Revenue", (s) => compact(s.revenue)],
    ["Revenue growth / yr", (s) => pct(s.revenue_growth)],
    ["Operating margin", (s) => pct(s.operating_margin, 1, false)],
    ["Net margin", (s) => pct(s.net_margin, 1, false)],
    ["EPS", (s) => money(s.eps)],
    ["Free cash flow", (s) => compact(s.free_cash_flow)],
    ["P/E multiple", (s) => (isNum(s.pe_multiple) ? `${num(s.pe_multiple, 1)}x` : "—")],
    ["Target · forward P/E", (s) => money(s.target_price_pe)],
    ["Target · DCF", (s) => money(s.target_price_dcf)],
    ["Target · EV/Sales", (s) => money(s.target_price_ev_sales)],
    ["Blended target", (s) => <strong>{money(s.target_price)}</strong>],
    ["Potential return", (s) => <ReturnText value={s.potential_return} />],
  ];
  return (
    <Table>
      <THead>
        <TR>
          <TH />
          <TH className="text-right text-down">Bear</TH>
          <TH className="text-right">Base</TH>
          <TH className="text-right text-up">Bull</TH>
        </TR>
      </THead>
      <TBody>
        {rows.map(([lbl, fn]) => (
          <TR key={lbl}>
            <TD className="text-muted">{lbl}</TD>
            {(["bear", "base", "bull"] as const).map((s) => (
              <TD key={s} className="text-right">
                {fn(f.scenarios[s])}
              </TD>
            ))}
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

function MethodNotes({ f }: { f: ForecastRecord }) {
  const m = (f.valuation_methods ?? {}) as Record<string, { applicable?: boolean; note?: string | null }>;
  const weights = (f.scenarios.base.details?.method_weights ?? {}) as Record<string, number>;
  return (
    <div className="mt-2 flex flex-col gap-1 text-xs text-muted">
      {(["forward_pe", "dcf", "ev_sales"] as const).map((k) => (
        <div key={k}>
          <Badge variant={m[k]?.applicable ? "default" : "warn"}>{k === "forward_pe" ? "Forward P/E" : k === "dcf" ? "DCF" : "EV/Sales"}</Badge>{" "}
          {isNum(weights[k]) ? `weight ${pct(weights[k], 0, false)} in base case` : "not used in base case"}
          {m[k]?.note && ` — ${m[k]?.note}`}
        </div>
      ))}
    </div>
  );
}

function Sensitivity({ f }: { f: ForecastRecord }) {
  const pg = f.sensitivity?.pe_growth;
  const dc = f.sensitivity?.dcf_wacc_terminal;
  const cell = (v: number | null) => (
    <TD className={cn("text-right text-xs", v == null ? "text-subtle" : v >= f.observed_price ? "text-up" : "text-down")}>
      {v == null ? "n/a" : `${money(v, 0)} ${v >= f.observed_price ? "▲" : "▼"}`}
    </TD>
  );
  return (
    <Card>
      <CardHeader>
        <CardTitle>Sensitivity analysis</CardTitle>
        <CardDescription>▲/▼ marks values above/below the current price.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {pg ? (
          <div>
            <div className="mb-1 text-xs text-muted">Forward P/E target: growth (rows) × P/E multiple (columns)</div>
            <Table>
              <THead>
                <TR>
                  <TH>Growth</TH>
                  {pg.cols_pe.map((c) => (
                    <TH key={c} className="text-right">
                      {num(c, 1)}x
                    </TH>
                  ))}
                </TR>
              </THead>
              <TBody>
                {pg.rows_growth.map((g, i) => (
                  <TR key={g}>
                    <TD className="text-xs text-muted">{pct(g)}</TD>
                    {pg.target_prices[i].map((v, j) => (
                      <React.Fragment key={j}>{cell(v)}</React.Fragment>
                    ))}
                  </TR>
                ))}
              </TBody>
            </Table>
          </div>
        ) : (
          <p className="text-sm text-subtle">P/E sensitivity not applicable.</p>
        )}
        {dc && (
          <div>
            <div className="mb-1 text-xs text-muted">DCF value per share: discount rate (rows) × terminal growth (columns)</div>
            <Table>
              <THead>
                <TR>
                  <TH>WACC</TH>
                  {dc.cols_terminal_growth.map((c) => (
                    <TH key={c} className="text-right">
                      {pct(c, 1, false)}
                    </TH>
                  ))}
                </TR>
              </THead>
              <TBody>
                {dc.rows_wacc.map((w, i) => (
                  <TR key={w}>
                    <TD className="text-xs text-muted">{pct(w, 1, false)}</TD>
                    {dc.per_share_values[i].map((v, j) => (
                      <React.Fragment key={j}>{cell(v)}</React.Fragment>
                    ))}
                  </TR>
                ))}
              </TBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
