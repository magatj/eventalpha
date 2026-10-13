import { ArrowRight, Compass, Play, Radar } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { DemoBadge, Disclaimer, EmptyState, ErrorState, EventStatusBadge, ImpactBadge, LoadingRows, PageHeader, ProvenanceBadge, ReturnText } from "@/components/common";
import { OpportunityTable } from "@/components/OpportunityTable";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { api, useDiscovery, useEvents, useHealth, useMarket } from "@/lib/api";
import { day, dateTime, num, relative } from "@/lib/format";
import { useQueryClient } from "@tanstack/react-query";

function MarketOverview() {
  const { data, isLoading, error, refetch } = useMarket();
  if (isLoading)
    return (
      <div className="grid gap-3 sm:grid-cols-3">
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} className="h-24" />
        ))}
      </div>
    );
  if (error) return <ErrorState error={error} onRetry={refetch} title="Market overview unavailable" />;
  if (!data?.indices.length)
    return <EmptyState title="Market data unavailable">{data?.unavailable_reason ?? "No index data returned."}</EmptyState>;
  return (
    <div>
      <div className="grid gap-3 sm:grid-cols-3">
        {data.indices.map((q) => (
          <Card key={q.symbol} className="p-4">
            <div className="flex items-start justify-between gap-2">
              <div>
                <div className="text-xs text-muted">{q.proxy_for}</div>
                <div className="text-[11px] text-subtle">{q.kind === "etf_proxy" ? `${q.symbol} ETF proxy` : q.name}</div>
              </div>
              <ProvenanceBadge kind="observed" />
            </div>
            {q.value == null ? (
              <div className="mt-3 text-sm text-warn">Unavailable: {q.unavailable_reason}</div>
            ) : (
              <div className="mt-2 flex items-baseline gap-2">
                <span className="text-2xl font-semibold num">{num(q.value)}</span>
                <ReturnText value={q.change_pct} digits={2} />
              </div>
            )}
            <div className="mt-1 text-[11px] text-subtle">Close {day(q.as_of_date)}</div>
          </Card>
        ))}
      </div>
      {data.coverage_notes.length > 0 && (
        <p className="mt-2 text-[11px] text-subtle">{data.coverage_notes.join(" ")}</p>
      )}
    </div>
  );
}

function AnalyzeBox() {
  const [t, setT] = useState("");
  const nav = useNavigate();
  return (
    <Card className="bg-gradient-to-br from-surface to-surface-3/60">
      <CardHeader>
        <CardTitle className="text-base">Analyze any US-listed stock</CardTitle>
        <CardDescription>Prices, SEC financials, news, AI thesis and bear/base/bull valuation.</CardDescription>
      </CardHeader>
      <CardContent>
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (t.trim()) nav(`/analyze?ticker=${encodeURIComponent(t.trim().toUpperCase())}&run=1`);
          }}
        >
          <Input value={t} onChange={(e) => setT(e.target.value.toUpperCase())} placeholder="NVDA" maxLength={8} className="h-10 text-base font-semibold tracking-wide" aria-label="Ticker" />
          <Button size="lg" type="submit">
            Analyze Stock
          </Button>
        </form>
        <div className="mt-3 flex flex-wrap gap-1.5">
          {["NKE", "NVDA", "MSFT", "AVGO", "TSM", "VRT", "AAPL", "AMZN"].map((x) => (
            <button key={x} onClick={() => nav(`/analyze?ticker=${x}&run=1`)} className="rounded-md border border-border px-2 py-0.5 text-xs text-muted hover:border-accent hover:text-foreground">
              {x}
            </button>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

export default function DashboardPage() {
  const { data: health } = useHealth();
  const disc = useDiscovery("?sort=score");
  const events = useEvents("days=3&limit=8");
  const qc = useQueryClient();
  const runDiscovery = async () => {
    await api("/api/discovery/run", { method: "POST" });
    qc.invalidateQueries({ queryKey: ["discovery"] });
  };
  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Dashboard"
        description="Event-driven opportunities ranked by an explainable, multi-factor score."
        actions={<DemoBadge mode={health?.data_mode} />}
      />
      <MarketOverview />
      <div className="grid gap-6 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <Card>
            <CardHeader className="flex-row items-center justify-between">
              <div>
                <CardTitle>Top AI-discovered opportunities</CardTitle>
                <CardDescription>
                  {disc.data?.run ? `Last discovery run ${relative(disc.data.run.finished_at)} · ${dateTime(disc.data.run.finished_at)}` : "No discovery run yet"}
                </CardDescription>
              </div>
              <Button size="sm" variant="ghost" asChild>
                <Link to="/discovery">
                  All <ArrowRight />
                </Link>
              </Button>
            </CardHeader>
            <CardContent>
              {disc.isLoading ? (
                <LoadingRows rows={6} />
              ) : disc.error ? (
                <ErrorState error={disc.error} onRetry={disc.refetch} />
              ) : disc.data?.items.length ? (
                <OpportunityTable items={disc.data.items.slice(0, 10)} compact />
              ) : (
                <EmptyState title={disc.data?.running_job ? "Discovery is running…" : "No opportunities yet"} icon={<Compass className="size-6 text-subtle" />}>
                  {disc.data?.running_job ? (
                    <span>{disc.data.running_job.progress ?? "Queued"}</span>
                  ) : (
                    <>
                      <p>Run discovery to scan the monitored universe for event-backed opportunities.</p>
                      <Button className="mt-3" size="sm" onClick={runDiscovery}>
                        <Play /> Run discovery
                      </Button>
                    </>
                  )}
                </EmptyState>
              )}
            </CardContent>
          </Card>
        </div>
        <div className="flex flex-col gap-6">
          <AnalyzeBox />
          <Card>
            <CardHeader className="flex-row items-center justify-between">
              <CardTitle>Latest events</CardTitle>
              <Button size="sm" variant="ghost" asChild>
                <Link to="/events">
                  <Radar /> Radar
                </Link>
              </Button>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              {events.isLoading && <LoadingRows rows={4} />}
              {events.data?.events.length === 0 && <p className="text-sm text-muted">No recent events ingested.</p>}
              {events.data?.events.slice(0, 6).map((e) => (
                <div key={e.id} className="border-b border-border/60 pb-2 last:border-0">
                  <div className="line-clamp-2 text-sm">{e.title}</div>
                  <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-subtle">
                    <ImpactBadge impact={e.impact} />
                    <EventStatusBadge status={e.status} />
                    {e.companies?.slice(0, 3).map((c) => (
                      <Link key={c.ticker} to={`/stocks/${c.ticker}`} className="text-accent">
                        {c.ticker}
                      </Link>
                    ))}
                    <span>{e.event_date ? `on ${day(e.event_date)}` : relative(e.last_published_at)}</span>
                  </div>
                </div>
              ))}
            </CardContent>
          </Card>
        </div>
      </div>
      <Disclaimer />
    </div>
  );
}
