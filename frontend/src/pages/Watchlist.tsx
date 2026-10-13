import { useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Loader2, Plus, RefreshCw, Trash2 } from "lucide-react";
import { Fragment, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { TargetHistoryChart } from "@/components/charts";
import { DemoBadge, EmptyState, ErrorState, LoadingRows, OutlookBadge, PageHeader, PriceTag, ReturnText, RiskBadge } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { api, ApiError, type WatchItem, useAnalyze, useHealth, useJob, useWatchlist } from "@/lib/api";
import { day, money, relative } from "@/lib/format";
import { DiffCard } from "@/pages/StockDetail";

function RefreshButton({ ticker }: { ticker: string }) {
  const analyze = useAnalyze();
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useJob(jobId);
  const qc = useQueryClient();
  const busy = analyze.isPending || (!!jobId && (!job.data || ["queued", "running"].includes(job.data.status)));
  useEffect(() => {
    if (job.data?.status === "succeeded") {
      setJobId(null);
      qc.invalidateQueries({ queryKey: ["watchlist"] });
    }
  }, [job.data?.status, qc]);
  return (
    <Button
      size="icon"
      variant="ghost"
      title={job.data?.status === "failed" ? `Failed: ${job.data.error}` : "Refresh analysis"}
      disabled={busy}
      onClick={() => analyze.mutate({ ticker, force: true }, { onSuccess: (r) => "job_id" in r && setJobId(r.job_id) })}
    >
      {busy ? <Loader2 className="animate-spin" /> : <RefreshCw className={job.data?.status === "failed" ? "text-down" : ""} />}
    </Button>
  );
}

function Row({ w, onRemove }: { w: WatchItem; onRemove: () => void }) {
  const [open, setOpen] = useState(false);
  const a = w.latest_analysis;
  return (
    <Fragment>
      <TR>
        <TD>
          <button onClick={() => setOpen(!open)} aria-label="Toggle details" className="text-subtle">
            {open ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
          </button>
        </TD>
        <TD>
          <Link to={`/stocks/${w.ticker}`} className="font-semibold hover:text-accent">
            {w.ticker}
          </Link>
          <div className="max-w-40 truncate text-[11px] text-muted">{w.name}</div>
        </TD>
        <TD>
          <PriceTag p={w.latest_price} />
        </TD>
        <TD>
          {a ? (
            <>
              <div className="num">{money(a.base_target_12m)}</div>
              <ReturnText value={a.base_return_12m} className="text-xs" />
            </>
          ) : (
            <span className="text-xs text-subtle">Analysis pending</span>
          )}
        </TD>
        <TD>{a && <OutlookBadge outlook={a.outlook} />}</TD>
        <TD>{a && <RiskBadge risk={a.risk_rating} />}</TD>
        <TD className="text-xs">
          {w.diff ? (
            w.diff.changes.length ? (
              <span className="text-warn">{w.diff.changes.length} change(s)</span>
            ) : (
              <span className="text-subtle">unchanged</span>
            )
          ) : (
            <span className="text-subtle">first analysis</span>
          )}
        </TD>
        <TD className="max-w-56 whitespace-normal text-xs">
          {w.upcoming[0] ? (
            <>
              {w.upcoming[0].title} <span className="text-subtle">({day(w.upcoming[0].event_date)}, {w.upcoming[0].status})</span>
            </>
          ) : (
            <span className="text-subtle">—</span>
          )}
        </TD>
        <TD className="text-xs text-muted">{a ? relative(a.created_at) : "—"}</TD>
        <TD>
          <div className="flex">
            <RefreshButton ticker={w.ticker} />
            <Button size="icon" variant="ghost" onClick={onRemove} title="Remove from watchlist">
              <Trash2 />
            </Button>
          </div>
        </TD>
      </TR>
      {open && (
        <TR className="hover:bg-transparent">
          <TD colSpan={10} className="whitespace-normal bg-surface-2/30">
            <div className="grid gap-4 py-2 lg:grid-cols-2">
              <div>
                <div className="mb-1 text-xs text-muted">12-month forecast history ({w.forecast_history_12m.length} forecasts)</div>
                {w.forecast_history_12m.length > 0 ? <TargetHistoryChart rows={w.forecast_history_12m} /> : <p className="text-sm text-subtle">None yet.</p>}
              </div>
              <div className="flex flex-col gap-3">
                {w.diff && <DiffCard diff={w.diff} prevAt={w.previous_analysis?.created_at} />}
                {w.upcoming.length > 0 && (
                  <div className="text-xs">
                    <div className="mb-1 text-muted">Upcoming catalysts</div>
                    <ul className="flex flex-col gap-1">
                      {w.upcoming.map((u) => (
                        <li key={u.title}>
                          {day(u.event_date)} — {u.title} <span className="text-subtle">({u.status})</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            </div>
          </TD>
        </TR>
      )}
    </Fragment>
  );
}

export default function WatchlistPage() {
  const { data, isLoading, error, refetch } = useWatchlist();
  const { data: health } = useHealth();
  const qc = useQueryClient();
  const [t, setT] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const add = async () => {
    setErr(null);
    try {
      await api("/api/watchlist", { method: "POST", body: JSON.stringify({ ticker: t }) });
      setT("");
      qc.invalidateQueries({ queryKey: ["watchlist"] });
      setTimeout(() => qc.invalidateQueries({ queryKey: ["watchlist"] }), 6000);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    }
  };
  const remove = async (ticker: string) => {
    await api(`/api/watchlist/${ticker}`, { method: "DELETE" });
    qc.invalidateQueries({ queryKey: ["watchlist"] });
  };
  return (
    <div>
      <PageHeader
        title="Watchlist"
        description="Track prices, forecast history, upcoming catalysts and how each thesis changes between analyses."
        actions={
          <>
            <DemoBadge mode={health?.data_mode} />
            <form
              className="flex gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                if (t.trim()) add();
              }}
            >
              <Input value={t} onChange={(e) => setT(e.target.value.toUpperCase())} placeholder="Add ticker" className="w-32" aria-label="Ticker to add" />
              <Button type="submit">
                <Plus /> Add
              </Button>
            </form>
          </>
        }
      />
      {err && <p className="mb-3 text-sm text-down">{err}</p>}
      <Card>
        <CardContent className="pt-4">
          {isLoading ? (
            <LoadingRows />
          ) : error ? (
            <ErrorState error={error} onRetry={refetch} />
          ) : data?.items.length ? (
            <Table>
              <THead>
                <TR>
                  <TH />
                  <TH>Ticker</TH>
                  <TH>Last price</TH>
                  <TH>Base 12m target</TH>
                  <TH>Outlook</TH>
                  <TH>Risk</TH>
                  <TH>vs previous</TH>
                  <TH>Next catalyst</TH>
                  <TH>Analyzed</TH>
                  <TH />
                </TR>
              </THead>
              <TBody>
                {data.items.map((w) => (
                  <Row key={w.ticker} w={w} onRemove={() => remove(w.ticker)} />
                ))}
              </TBody>
            </Table>
          ) : (
            <EmptyState title="Your watchlist is empty">Add a ticker above — it is analyzed automatically if it has no analysis yet.</EmptyState>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
