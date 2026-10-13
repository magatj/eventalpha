import { useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Trash2, XCircle } from "lucide-react";
import { useState } from "react";

import { ErrorState, Kpi, LoadingRows, PageHeader } from "@/components/common";
import { ModeToggle } from "@/components/Layout";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TBody, TD, TR } from "@/components/ui/table";
import { api, useSettings } from "@/lib/api";
import { label, money, pct } from "@/lib/format";

const Ok = ({ ok }: { ok: boolean }) =>
  ok ? <CheckCircle2 className="inline size-4 text-up" aria-label="configured" /> : <XCircle className="inline size-4 text-down" aria-label="not configured" />;

export default function SettingsPage() {
  const { data: s, isLoading, error, refetch } = useSettings();
  const qc = useQueryClient();
  const [resetMsg, setResetMsg] = useState<string | null>(null);
  if (isLoading) return <LoadingRows rows={8} />;
  if (error) return <ErrorState error={error} onRetry={refetch} />;
  const p = s!.providers;
  const b = s!.ai_budget;
  const reset = async () => {
    if (!confirm("Delete all DEMO-mode data (companies, forecasts, events)? Live data is not touched.")) return;
    const r = await api<{ companies_removed: number }>("/api/demo/reset", { method: "POST" });
    setResetMsg(`Demo data reset (${r.companies_removed} demo companies removed).`);
    qc.invalidateQueries();
  };
  return (
    <div className="flex flex-col gap-5">
      <PageHeader title="Settings" description="Secrets are configured in the backend .env file and are never sent to the browser." />
      <Card>
        <CardHeader>
          <CardTitle>Data mode</CardTitle>
          <CardDescription>Demo uses labeled synthetic data and makes no external calls. Live uses Massive, SEC EDGAR, RSS and (optionally) OpenAI. The two never mix.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-4">
          <ModeToggle />
          <span className="text-xs text-subtle">Startup default: {s!.default_data_mode}</span>
          <Button variant="destructive" size="sm" onClick={reset} className="ml-auto">
            <Trash2 /> Reset demo data
          </Button>
          {resetMsg && <span className="w-full text-xs text-up">{resetMsg}</span>}
        </CardContent>
      </Card>
      <div className="grid gap-5 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Providers</CardTitle>
          </CardHeader>
          <CardContent>
            <Table>
              <TBody>
                <TR>
                  <TD>{p.market_data.name}</TD>
                  <TD>
                    <Ok ok={p.market_data.configured} /> {p.market_data.configured ? "API key set" : "MASSIVE_API_KEY missing"}
                  </TD>
                  <TD className="text-xs text-subtle">{p.market_data.requests_per_minute}/min</TD>
                </TR>
                <TR>
                  <TD>SEC EDGAR</TD>
                  <TD>
                    <Ok ok={p.sec_edgar.has_contact} /> <span className="text-xs">{p.sec_edgar.user_agent}</span>
                  </TD>
                  <TD className="text-xs text-subtle">{p.sec_edgar.requests_per_second}/s</TD>
                </TR>
                <TR>
                  <TD>OpenAI</TD>
                  <TD>
                    <Ok ok={p.openai.configured} /> {p.openai.model}
                  </TD>
                  <TD className="text-xs text-subtle">{p.openai.configured ? "" : "rule-based fallback"}</TD>
                </TR>
              </TBody>
            </Table>
            <div className="mt-3 text-xs text-muted">
              RSS feeds:
              <ul className="mt-1 list-disc pl-5 text-subtle">
                {(p.rss_feeds as string[]).map((f) => (
                  <li key={f} className="break-all">
                    {f}
                  </li>
                ))}
              </ul>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>AI spending</CardTitle>
            <CardDescription>Hard monthly cap; cached results are reused for {b.cache_ttl_hours}h.</CardDescription>
          </CardHeader>
          <CardContent className="grid grid-cols-3 gap-3">
            <Kpi label="Budget" value={money(b.monthly_budget_usd)} />
            <Kpi label="Spent (month)" value={money(b.spent_this_month_usd, 4)} />
            <Kpi label="Remaining" value={money(b.remaining_usd, 2)} />
            <p className="col-span-3 text-[11px] text-subtle">
              Pricing used for estimates: ${b.pricing_per_million.input}/M input, ${b.pricing_per_million.output}/M output tokens (set in .env; verify against current OpenAI pricing).
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Forecasting assumptions</CardTitle>
          </CardHeader>
          <CardContent className="text-sm">
            <Table>
              <TBody>
                {Object.entries(s!.forecasting).map(([k, v]) => (
                  <TR key={k}>
                    <TD className="text-muted">{label(k)}</TD>
                    <TD>{Array.isArray(v) ? v.join(", ") : typeof v === "number" && v < 1 ? pct(v, 2, false) : String(v)}</TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Discovery ranking</CardTitle>
            <CardDescription>Weights are stored with every discovery run.</CardDescription>
          </CardHeader>
          <CardContent>
            <Table>
              <TBody>
                {Object.entries(s!.discovery.weights as Record<string, number>).map(([k, v]) => (
                  <TR key={k}>
                    <TD className="text-muted">{label(k)}</TD>
                    <TD className={v < 0 ? "text-down" : ""}>{v > 0 ? "+" : ""}{v}</TD>
                  </TR>
                ))}
                <TR>
                  <TD className="text-muted">Top N / max candidates / AI top N</TD>
                  <TD>
                    {s!.discovery.top_n} / {s!.discovery.max_candidates} / {s!.discovery.ai_top_n}
                  </TD>
                </TR>
              </TBody>
            </Table>
          </CardContent>
        </Card>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>Scheduled jobs</CardTitle>
          <CardDescription>
            Runs in the separate scheduler service ({s!.scheduler.timezone}; NYSE holidays and early closes respected). {s!.scheduler.enabled ? "Enabled." : "Disabled."}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TBody>
              {(s!.scheduler.jobs as { id: string; schedule: string }[]).map((j) => (
                <TR key={j.id}>
                  <TD className="text-muted">{label(j.id)}</TD>
                  <TD>{j.schedule}</TD>
                </TR>
              ))}
            </TBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
