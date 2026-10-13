/* Charts. Conventions (dataviz): one y-axis only (stock vs. benchmark are indexed to a common
   base), thin 2px lines, rounded bar ends, recessive grid, tooltip on hover, legend for >= 2
   series, and gain/loss never encoded by color alone. */
import { useMemo } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";

import type { BacktestItem, FinPoint, ScenarioResult } from "@/lib/api";
import { compact, day, money, pct } from "@/lib/format";

const C = {
  s1: "var(--color-series-1)",
  s2: "var(--color-series-2)",
  s3: "var(--color-series-3)",
  up: "var(--color-up)",
  down: "var(--color-down)",
  muted: "var(--color-muted)",
  border: "var(--color-border)",
  surface: "var(--color-surface)",
};

const tooltipStyle = {
  contentStyle: { background: "#0f1830", border: "1px solid #22304d", borderRadius: 8, fontSize: 12 },
  labelStyle: { color: "#8a9bb8" },
  itemStyle: { color: "#e6edf7" },
};

export function PriceChart({
  bars,
  benchmark,
  ticker,
  indexed,
}: {
  bars: { date: string; close: number }[];
  benchmark?: { ticker: string; bars: { date: string; close: number }[] } | null;
  ticker: string;
  indexed: boolean;
}) {
  const data = useMemo(() => {
    const b = new Map((benchmark?.bars ?? []).map((x) => [x.date, x.close]));
    const first = bars[0]?.close;
    const bFirst = bars.map((x) => b.get(x.date)).find((v) => v != null);
    return bars.map((x) => ({
      date: x.date,
      price: indexed ? (x.close / first - 1) : x.close,
      bench: indexed && bFirst && b.get(x.date) != null ? b.get(x.date)! / bFirst - 1 : undefined,
    }));
  }, [bars, benchmark, indexed]);
  return (
    <ResponsiveContainer width="100%" height={300}>
      <LineChart data={data} margin={{ top: 8, right: 12, left: 4, bottom: 0 }}>
        <CartesianGrid vertical={false} />
        <XAxis dataKey="date" tickFormatter={(d) => day(d).replace(/, \d{4}$/, "")} minTickGap={40} tickLine={false} axisLine={false} />
        <YAxis
          width={64}
          tickLine={false}
          axisLine={false}
          domain={["auto", "auto"]}
          tickFormatter={(v) => (indexed ? pct(v, 0) : money(v, 0))}
        />
        <Tooltip
          {...tooltipStyle}
          labelFormatter={(d) => day(String(d))}
          formatter={(v, name) => [indexed ? pct(Number(v), 2) : money(Number(v)), name]}
        />
        {indexed && benchmark && <Legend iconType="plainline" wrapperStyle={{ fontSize: 12 }} />}
        <Line type="monotone" dataKey="price" name={ticker} stroke={C.s1} strokeWidth={2} dot={false} isAnimationActive={false} />
        {indexed && benchmark && (
          <Line type="monotone" dataKey="bench" name={`${benchmark.ticker} (S&P 500 benchmark)`} stroke={C.s2} strokeWidth={2} dot={false} isAnimationActive={false} />
        )}
        {indexed && <ReferenceLine y={0} stroke={C.border} />}
      </LineChart>
    </ResponsiveContainer>
  );
}

/** Bear/base/bull target vs. current price. Labels carry the scenario name and return. */
export function ScenarioChart({ scenarios, price }: { scenarios: Record<string, ScenarioResult>; price: number }) {
  const data = (["bear", "base", "bull"] as const).map((s) => ({
    name: s[0].toUpperCase() + s.slice(1),
    target: scenarios[s]?.target_price ?? null,
    ret: scenarios[s]?.potential_return ?? null,
  }));
  return (
    <ResponsiveContainer width="100%" height={220}>
      <BarChart data={data} margin={{ top: 20, right: 12, left: 4, bottom: 0 }} barCategoryGap="30%">
        <CartesianGrid vertical={false} />
        <XAxis dataKey="name" tickLine={false} axisLine={false} />
        <YAxis width={64} tickLine={false} axisLine={false} tickFormatter={(v) => money(v, 0)} />
        <Tooltip {...tooltipStyle} formatter={(v) => [money(Number(v)), "Model target"]} cursor={{ fill: "rgba(255,255,255,0.03)" }} />
        <ReferenceLine y={price} stroke={C.muted} strokeDasharray="4 4" label={{ value: `Current ${money(price)}`, position: "insideTopRight", fill: "#8a9bb8", fontSize: 11 }} />
        <Bar dataKey="target" radius={[4, 4, 0, 0]} maxBarSize={56}>
          {data.map((d) => (
            <Cell key={d.name} fill={d.ret == null ? C.muted : d.ret >= 0 ? C.up : C.down} fillOpacity={0.85} />
          ))}
          <LabelList dataKey="ret" position="top" formatter={(v) => (v == null ? "n/a" : pct(Number(v)))} style={{ fill: "#e6edf7", fontSize: 11 }} />
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

export function FinBarChart({ points, valueFormat = "money", name }: { points: FinPoint[]; valueFormat?: "money" | "pct" | "eps"; name: string }) {
  const data = points.map((p) => ({
    label: p.fiscal_period && p.fiscal_year ? `${p.fiscal_period === "FY" ? "FY" : p.fiscal_period} ${String(p.fiscal_year).slice(2)}` : p.period_end.slice(0, 7),
    value: p.value,
    derived: p.derived,
    end: p.period_end,
  }));
  const fmt = (v: number) => (valueFormat === "pct" ? pct(v, 1, false) : valueFormat === "eps" ? money(v) : compact(v));
  return (
    <ResponsiveContainer width="100%" height={200}>
      <BarChart data={data} margin={{ top: 8, right: 8, left: 4, bottom: 0 }}>
        <CartesianGrid vertical={false} />
        <XAxis dataKey="label" tickLine={false} axisLine={false} interval="preserveStartEnd" />
        <YAxis width={60} tickLine={false} axisLine={false} tickFormatter={fmt} />
        <Tooltip
          {...tooltipStyle}
          cursor={{ fill: "rgba(255,255,255,0.03)" }}
          labelFormatter={(_, p) => (p?.[0] ? `Period ending ${day(p[0].payload.end)}${p[0].payload.derived ? " (derived from YTD)" : ""}` : "")}
          formatter={(v) => [fmt(Number(v)), name]}
        />
        <ReferenceLine y={0} stroke={C.border} />
        <Bar dataKey="value" name={name} radius={[4, 4, 0, 0]} maxBarSize={28}>
          {data.map((d) => (
            <Cell key={d.end} fill={d.value < 0 ? C.down : C.s1} fillOpacity={d.derived ? 0.6 : 0.9} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Predicted (base case) vs realized return; points on the diagonal were perfect. */
export function BacktestScatter({ items }: { items: BacktestItem[] }) {
  const data = items
    .filter((i) => i.predicted_base_return != null)
    .map((i) => ({ x: i.predicted_base_return!, y: i.realized_return, label: i.ticker ?? i.as_of ?? "", date: i.target_date }));
  const lim = Math.max(0.2, ...data.flatMap((d) => [Math.abs(d.x), Math.abs(d.y)])) * 1.1;
  return (
    <ResponsiveContainer width="100%" height={280}>
      <ScatterChart margin={{ top: 8, right: 16, left: 4, bottom: 16 }}>
        <CartesianGrid />
        <XAxis type="number" dataKey="x" name="Predicted (base)" domain={[-lim, lim]} tickFormatter={(v) => pct(v, 0)} tickLine={false}
          label={{ value: "Predicted base-case return", position: "insideBottom", offset: -8, fill: "#8a9bb8", fontSize: 11 }} />
        <YAxis type="number" dataKey="y" name="Realized" domain={[-lim, lim]} tickFormatter={(v) => pct(v, 0)} width={56} tickLine={false}
          label={{ value: "Realized", angle: -90, position: "insideLeft", fill: "#8a9bb8", fontSize: 11 }} />
        <ZAxis range={[64, 64]} />
        <ReferenceLine segment={[{ x: -lim, y: -lim }, { x: lim, y: lim }]} stroke={C.muted} strokeDasharray="4 4" />
        <ReferenceLine x={0} stroke={C.border} />
        <ReferenceLine y={0} stroke={C.border} />
        <Tooltip {...tooltipStyle} formatter={(v, n) => [pct(Number(v)), n]} labelFormatter={() => ""} />
        <Scatter data={data} fill={C.s1} stroke={C.surface} strokeWidth={2} isAnimationActive={false} />
      </ScatterChart>
    </ResponsiveContainer>
  );
}

/** Model's base-case 12m target over successive forecasts vs observed price at forecast time. */
export function TargetHistoryChart({ rows }: { rows: { created_at: string; observed_price: number; base?: number; bear?: number; bull?: number }[] }) {
  const data = rows.map((r) => ({ t: r.created_at.slice(0, 10), price: r.observed_price, base: r.base, bear: r.bear, bull: r.bull }));
  return (
    <ResponsiveContainer width="100%" height={220}>
      <LineChart data={data} margin={{ top: 8, right: 12, left: 4, bottom: 0 }}>
        <CartesianGrid vertical={false} />
        <XAxis dataKey="t" tickFormatter={(d) => day(d)} tickLine={false} axisLine={false} minTickGap={30} />
        <YAxis width={64} tickLine={false} axisLine={false} tickFormatter={(v) => money(v, 0)} domain={["auto", "auto"]} />
        <Tooltip {...tooltipStyle} labelFormatter={(d) => `Forecast made ${day(String(d))}`} formatter={(v, n) => [money(Number(v)), n]} />
        <Legend iconType="plainline" wrapperStyle={{ fontSize: 12 }} />
        <Line dataKey="price" name="Price at forecast" stroke={C.s2} strokeWidth={2} dot={{ r: 4 }} isAnimationActive={false} />
        <Line dataKey="base" name="Base target (12m)" stroke={C.s1} strokeWidth={2} dot={{ r: 4 }} isAnimationActive={false} />
        <Line dataKey="bear" name="Bear" stroke={C.s3} strokeWidth={1.5} strokeDasharray="4 3" dot={false} isAnimationActive={false} />
        <Line dataKey="bull" name="Bull" stroke={C.s3} strokeWidth={1.5} strokeDasharray="1 3" dot={false} isAnimationActive={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}
