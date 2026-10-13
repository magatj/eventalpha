import { CheckCircle2, Circle, Loader2, XCircle } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { DemoBadge, ErrorState, PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useAnalyze, useHealth, useJob } from "@/lib/api";
import { cn } from "@/lib/utils";

const STEPS = [
  "Resolving company identity",
  "Retrieving prices",
  "Retrieving SEC financial statements",
  "Collecting news, filings and catalysts",
  "Interpreting news and financials",
  "Computing forecasts",
];
const TICKER_RE = /^[A-Z]{1,5}([.-][A-Z]{1,2})?$/;

export default function AnalyzePage() {
  const [params, setParams] = useSearchParams();
  const [ticker, setTicker] = useState(params.get("ticker") ?? "");
  const [force, setForce] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [validation, setValidation] = useState<string | null>(null);
  const analyze = useAnalyze();
  const job = useJob(jobId);
  const nav = useNavigate();
  const { data: health } = useHealth();
  const autoRan = useRef(false);

  const submit = (t: string, f = force) => {
    const clean = t.trim().toUpperCase();
    if (!TICKER_RE.test(clean)) {
      setValidation("Enter a valid US ticker symbol (1–5 letters, optional class suffix like BRK.B).");
      return;
    }
    setValidation(null);
    setJobId(null);
    analyze.mutate(
      { ticker: clean, force: f },
      {
        onSuccess: (r) => {
          if (r.status === "cached") nav(`/stocks/${r.ticker}`);
          else if ("job_id" in r) setJobId(r.job_id);
        },
      },
    );
  };

  useEffect(() => {
    if (!autoRan.current && params.get("run") === "1" && params.get("ticker")) {
      autoRan.current = true;
      setParams({ ticker: params.get("ticker")! }, { replace: true });
      submit(params.get("ticker")!);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (job.data?.status === "succeeded") {
      const t = (job.data.result?.ticker as string) ?? ticker.toUpperCase();
      const id = setTimeout(() => nav(`/stocks/${t}`), 600);
      return () => clearTimeout(id);
    }
  }, [job.data, nav, ticker]);

  const progressIdx = job.data?.progress ? STEPS.indexOf(job.data.progress) : -1;
  const busy = analyze.isPending || (job.data ? ["queued", "running"].includes(job.data.status) : !!jobId);

  return (
    <div className="mx-auto max-w-3xl">
      <PageHeader title="Analyze Ticker" description="Run the full research pipeline for any supported US-listed stock." actions={<DemoBadge mode={health?.data_mode} />} />
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Ticker symbol</CardTitle>
          <CardDescription>
            Fresh cached analyses are reused automatically (no repeated AI requests). Tick “new analysis” to force a refresh.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form
            className="flex flex-col gap-3 sm:flex-row"
            onSubmit={(e) => {
              e.preventDefault();
              submit(ticker);
            }}
          >
            <Input
              autoFocus
              value={ticker}
              onChange={(e) => setTicker(e.target.value.toUpperCase())}
              placeholder="e.g. NKE, NVDA, MSFT"
              maxLength={8}
              className="h-12 text-lg font-semibold tracking-wider"
              aria-label="Ticker symbol"
              aria-invalid={!!validation}
            />
            <Button size="lg" className="h-12" type="submit" disabled={busy}>
              {busy ? <Loader2 className="animate-spin" /> : null}
              Analyze Stock
            </Button>
          </form>
          <label className="mt-3 flex items-center gap-2 text-xs text-muted">
            <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} className="accent-[var(--color-accent)]" />
            Run a new analysis even if a fresh cached one exists
          </label>
          {validation && <p className="mt-2 text-sm text-down">{validation}</p>}
        </CardContent>
      </Card>

      {analyze.error && <div className="mt-4"><ErrorState error={analyze.error} title="Could not start analysis" /></div>}

      {jobId && (
        <Card className="mt-4">
          <CardHeader>
            <CardTitle>
              Analyzing {ticker.toUpperCase()} {job.data?.status === "queued" && <span className="text-xs font-normal text-muted">· queued for the worker</span>}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <ol className="flex flex-col gap-2">
              {STEPS.map((s, i) => {
                const failed = job.data?.status === "failed" && i === Math.max(progressIdx, 0);
                const done = job.data?.status === "succeeded" || (progressIdx > i && !failed);
                const active = job.data?.status === "running" && progressIdx === i;
                return (
                  <li key={s} className={cn("flex items-center gap-2 text-sm", done ? "text-foreground" : active ? "text-accent" : "text-subtle")}>
                    {failed ? <XCircle className="size-4 text-down" /> : done ? <CheckCircle2 className="size-4 text-up" /> : active ? <Loader2 className="size-4 animate-spin" /> : <Circle className="size-4" />}
                    {s}
                  </li>
                );
              })}
            </ol>
            {job.data?.status === "failed" && (
              <div className="mt-4">
                <ErrorState error={new Error(job.data.error ?? "Analysis failed")} title="Analysis failed" onRetry={() => submit(ticker, true)} />
              </div>
            )}
            {job.data?.status === "succeeded" && <p className="mt-3 text-sm text-up">Done — opening results…</p>}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
