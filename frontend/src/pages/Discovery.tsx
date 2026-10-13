import { useQueryClient } from "@tanstack/react-query";
import { Info, Loader2, Play } from "lucide-react";
import { useState } from "react";

import { DemoBadge, EmptyState, ErrorState, LoadingRows, PageHeader } from "@/components/common";
import { OpportunityTable } from "@/components/OpportunityTable";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Select } from "@/components/ui/input";
import { api, useDiscovery } from "@/lib/api";
import { dateTime } from "@/lib/format";

const SECTORS = ["Technology", "Communication Services", "Consumer Discretionary", "Consumer Staples", "Health Care", "Financials", "Industrials", "Energy", "Utilities", "Materials", "Real Estate"];

export default function DiscoveryPage() {
  const [sort, setSort] = useState("score");
  const [sector, setSector] = useState("");
  const [risk, setRisk] = useState("");
  const [horizon, setHorizon] = useState("");
  const qs = new URLSearchParams({ sort, ...(sector && { sector }), ...(risk && { risk }), ...(horizon && { horizon }) }).toString();
  const { data, isLoading, error, refetch } = useDiscovery(`?${qs}`);
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
  const notes = data?.run?.notes as { skipped?: { ticker: string; reason: string }[] } | undefined;
  return (
    <div>
      <PageHeader
        title="Stock Discovery"
        description="Candidates come from recent and upcoming events, are screened for liquidity and data quality, valued by the quantitative engine, and ranked by an explainable score."
        actions={
          <>
            <DemoBadge mode={data?.data_mode} />
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
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Select value={sort} onChange={(e) => setSort(e.target.value)} aria-label="Sort by">
          <option value="score">Sort: Score</option>
          <option value="growth">Sort: Potential growth</option>
          <option value="risk">Sort: Lowest risk</option>
          <option value="event_date">Sort: Event date</option>
          <option value="forecast_date">Sort: Forecast date</option>
        </Select>
        <Select value={sector} onChange={(e) => setSector(e.target.value)} aria-label="Sector">
          <option value="">All sectors</option>
          {SECTORS.map((s) => (
            <option key={s}>{s}</option>
          ))}
        </Select>
        <Select value={risk} onChange={(e) => setRisk(e.target.value)} aria-label="Risk">
          <option value="">Any risk</option>
          <option>Low</option>
          <option>Medium</option>
          <option>High</option>
        </Select>
        <Select value={horizon} onChange={(e) => setHorizon(e.target.value)} aria-label="Horizon">
          <option value="">Any horizon</option>
          <option value="short_term">Short-term</option>
          <option value="long_term">Long-term</option>
        </Select>
        {data?.run && (
          <span className="ml-auto text-xs text-subtle">
            Run #{data.run.id} · {dateTime(data.run.finished_at)} · {data.run.candidates_evaluated} evaluated of {data.run.universe_size} monitored
          </span>
        )}
      </div>
      <Card>
        <CardContent className="pt-4">
          {isLoading ? (
            <LoadingRows rows={10} />
          ) : error ? (
            <ErrorState error={error} onRetry={refetch} />
          ) : data?.items.length ? (
            <OpportunityTable items={data.items} />
          ) : (
            <EmptyState title={data?.run ? "No results match these filters" : "No discovery run yet"}>
              {!data?.run && "Click “Run discovery”. Live mode with the free Massive tier is rate-limited to 5 requests/minute, so a full run can take a while."}
            </EmptyState>
          )}
        </CardContent>
      </Card>
      {data?.ranking_explanation && (
        <p className="mt-3 flex gap-1.5 text-xs text-muted">
          <Info className="mt-0.5 size-3.5 shrink-0" /> {data.ranking_explanation}
        </p>
      )}
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
