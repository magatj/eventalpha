import { useState } from "react";
import { Link } from "react-router-dom";

import { BacktestScatter } from "@/components/charts";
import { DemoBadge, EmptyState, ErrorState, Kpi, LoadingRows, PageHeader, ProvenanceBadge, ReturnText } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Select } from "@/components/ui/input";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { type BacktestItem, type BacktestSummary, useBacktest, useHealth, useRecentForecasts, useReplay } from "@/lib/api";
import { dateTime, day, money, pct } from "@/lib/format";

function SummaryKpis({ s }: { s: BacktestSummary }) {
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
      <Kpi label="Evaluated" value={s.count} />
      <Kpi label="Direction hit rate" value={pct(s.direction_hit_rate, 0, false)} sub="sign of base vs realized" />
      <Kpi label="Within bear–bull" value={pct(s.within_bear_bull_range, 0, false)} />
      <Kpi label="Mean abs. error (base)" value={pct(s.mean_abs_error_base, 1, false)} />
      <Kpi label="Mean excess vs S&P 500" value={<ReturnText value={s.mean_excess_vs_benchmark} />} sub={`Realized ${pct(s.mean_realized_return)} vs SPY ${pct(s.mean_benchmark_return)}`} />
    </div>
  );
}

function ResultsTable({ items, dateKey }: { items: BacktestItem[]; dateKey: "as_of" | "created_at" }) {
  return (
    <Table>
      <THead>
        <TR>
          {dateKey === "created_at" && <TH>Ticker</TH>}
          <TH>{dateKey === "as_of" ? "As of" : "Forecast made"}</TH>
          <TH>Target date</TH>
          <TH className="text-right">Bear</TH>
          <TH className="text-right">Base</TH>
          <TH className="text-right">Bull</TH>
          <TH className="text-right">Realized</TH>
          <TH className="text-right">S&P 500</TH>
          <TH className="text-right">Excess</TH>
        </TR>
      </THead>
      <TBody>
        {items.map((i, k) => (
          <TR key={k}>
            {dateKey === "created_at" && <TD className="font-semibold">{i.ticker}</TD>}
            <TD>{dateKey === "as_of" ? day(i.as_of) : dateTime(i.created_at)}</TD>
            <TD>{day(i.target_date)}</TD>
            <TD className="text-right">{pct(i.predicted_bear_return)}</TD>
            <TD className="text-right">{pct(i.predicted_base_return)}</TD>
            <TD className="text-right">{pct(i.predicted_bull_return)}</TD>
            <TD className="text-right"><ReturnText value={i.realized_return} /></TD>
            <TD className="text-right">{pct(i.benchmark_return)}</TD>
            <TD className="text-right"><ReturnText value={i.excess_vs_benchmark} /></TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

export default function ForecastHistoryPage() {
  const { data: health } = useHealth();
  const [horizon, setHorizon] = useState(12);
  const recent = useRecentForecasts(horizon);
  const realized = useBacktest();
  const [tickerInput, setTickerInput] = useState("");
  const [replayTicker, setReplayTicker] = useState("");
  const [replayH, setReplayH] = useState(6);
  const replay = useReplay(replayTicker, replayH);

  return (
    <div className="flex flex-col gap-6">
      <PageHeader title="Forecast History & Backtesting" description="Every forecast is an immutable record. Evaluations use only information available at forecast time." actions={<DemoBadge mode={health?.data_mode} />} />

      <Card>
        <CardHeader className="flex-row flex-wrap items-center justify-between gap-2">
          <div>
            <CardTitle>Recorded forecasts</CardTitle>
            <CardDescription>Most recent first. Stored with price date, financial data version, model and AI identifiers.</CardDescription>
          </div>
          <Select value={horizon} onChange={(e) => setHorizon(Number(e.target.value))} aria-label="Horizon">
            {[3, 6, 12, 24, 36, 60].map((h) => (
              <option key={h} value={h}>
                {h}-month horizon
              </option>
            ))}
          </Select>
        </CardHeader>
        <CardContent>
          {recent.isLoading ? (
            <LoadingRows />
          ) : recent.error ? (
            <ErrorState error={recent.error} />
          ) : recent.data?.forecasts.length ? (
            <div className="max-h-[420px] overflow-y-auto">
              <Table>
                <THead>
                  <TR>
                    <TH>Ticker</TH>
                    <TH>Created</TH>
                    <TH>Observed price</TH>
                    <TH>Target date</TH>
                    <TH className="text-right">Bear</TH>
                    <TH className="text-right">Base</TH>
                    <TH className="text-right">Bull</TH>
                    <TH>Model</TH>
                  </TR>
                </THead>
                <TBody>
                  {recent.data.forecasts.map((f) => (
                    <TR key={f.id}>
                      <TD>
                        <Link to={`/stocks/${f.ticker}`} className="font-semibold hover:text-accent">
                          {f.ticker}
                        </Link>
                      </TD>
                      <TD className="text-xs">{dateTime(f.created_at)}</TD>
                      <TD>
                        {money(f.observed_price)} <span className="text-[11px] text-subtle">{day(f.observed_price_date)}</span>
                      </TD>
                      <TD>{day(f.target_date)}</TD>
                      {(["bear", "base", "bull"] as const).map((s) => (
                        <TD key={s} className="text-right">
                          {money(f.scenarios[s]?.target_price)}
                          <div className="text-[11px]">
                            <ReturnText value={f.scenarios[s]?.potential_return} />
                          </div>
                        </TD>
                      ))}
                      <TD className="text-[11px] text-subtle">
                        {f.forecast_model_version}
                        {f.ai_model && ` · ${f.ai_model}`}
                      </TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
            </div>
          ) : (
            <EmptyState title="No forecasts recorded yet">Analyze a ticker or run discovery.</EmptyState>
          )}
          <div className="mt-2">
            <ProvenanceBadge kind="model_forecast" />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Realized accuracy of stored forecasts</CardTitle>
          <CardDescription>
            Forecasts whose target date has passed, compared with split-adjusted realized returns and the S&P 500 (SPY) over the same window. Forecasts for tickers
            that later lost price coverage are listed as unresolved, not dropped (avoids survivorship bias).
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {realized.isLoading ? (
            <LoadingRows rows={3} />
          ) : realized.data && realized.data.summary.count > 0 ? (
            <>
              <SummaryKpis s={realized.data.summary} />
              <BacktestScatter items={realized.data.items} />
              <ResultsTable items={realized.data.items} dateKey="created_at" />
            </>
          ) : (
            <EmptyState title="No matured forecasts yet">
              Stored forecasts are evaluated automatically once their target dates pass. Use the point-in-time replay below to evaluate the model on history now.
              {realized.data && realized.data.unresolved.length > 0 && ` ${realized.data.unresolved.length} unresolved.`}
            </EmptyState>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Point-in-time replay backtest</CardTitle>
          <CardDescription>
            Re-runs the quantitative engine at past dates using only prices up to that date and SEC facts filed on or before it (no look-ahead), without AI
            adjustments. Requires the ticker to have been analyzed (data stored locally).
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <form
            className="flex flex-wrap gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              setReplayTicker(tickerInput.trim().toUpperCase());
            }}
          >
            <Input value={tickerInput} onChange={(e) => setTickerInput(e.target.value.toUpperCase())} placeholder="Ticker" className="w-32" aria-label="Replay ticker" />
            <Select value={replayH} onChange={(e) => setReplayH(Number(e.target.value))} aria-label="Replay horizon">
              {[3, 6, 12].map((h) => (
                <option key={h} value={h}>
                  {h}-month horizon
                </option>
              ))}
            </Select>
            <Button type="submit" variant="secondary">
              Run replay
            </Button>
          </form>
          {replay.isFetching && <LoadingRows rows={3} />}
          {replay.error && <ErrorState error={replay.error} />}
          {replay.data && !replay.isFetching && (
            replay.data.error ? (
              <EmptyState title="Replay unavailable">{replay.data.error}</EmptyState>
            ) : replay.data.items.length ? (
              <>
                <SummaryKpis s={replay.data.summary} />
                <BacktestScatter items={replay.data.items} />
                <ResultsTable items={replay.data.items} dateKey="as_of" />
                <p className="text-[11px] text-subtle">{replay.data.method} {replay.data.summary.note}</p>
              </>
            ) : (
              <EmptyState title="Not enough history for this horizon" />
            )
          )}
        </CardContent>
      </Card>
    </div>
  );
}
