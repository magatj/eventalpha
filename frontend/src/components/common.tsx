import { AlertTriangle, ArrowDownRight, ArrowUpRight, Bot, Calculator, Database, FlaskConical, Inbox, Minus, RefreshCw } from "lucide-react";
import type { ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { DataMode, LatestPrice, Provenance } from "@/lib/api";
import { ApiError } from "@/lib/api";
import { day, isNum, money, pct } from "@/lib/format";
import { cn } from "@/lib/utils";

/** Every financial value is labeled with where it came from. */
export function ProvenanceBadge({ kind, className }: { kind: Provenance | "ai"; className?: string }) {
  const map = {
    observed: { icon: Database, text: "Observed", title: "Actual reported or market data", v: "default" as const },
    external_estimate: { icon: Database, text: "External est.", title: "Third-party estimate", v: "warn" as const },
    model_forecast: { icon: Calculator, text: "Model forecast", title: "Computed by EventAlpha's quantitative engine — not observed data", v: "accent" as const },
    ai: { icon: Bot, text: "AI interpretation", title: "Language-model interpretation of cited sources", v: "accent" as const },
  }[kind];
  const Icon = map.icon;
  return (
    <Badge variant={map.v} title={map.title} className={className}>
      <Icon className="size-3" /> {map.text}
    </Badge>
  );
}

export function DemoBadge({ mode }: { mode?: DataMode }) {
  if (mode !== "demo") return null;
  return (
    <Badge variant="demo" title="Synthetic demonstration data — not real market data">
      <FlaskConical className="size-3" /> DEMO DATA
    </Badge>
  );
}

/** Gain/loss text: sign + arrow + color (color alone is not CVD-safe). */
export function ReturnText({ value, className, digits = 1 }: { value: number | null | undefined; className?: string; digits?: number }) {
  if (!isNum(value)) return <span className={cn("text-subtle", className)}>—</span>;
  const up = value > 0.0005;
  const down = value < -0.0005;
  const Icon = up ? ArrowUpRight : down ? ArrowDownRight : Minus;
  return (
    <span className={cn("inline-flex items-center gap-0.5 num font-medium", up && "text-up", down && "text-down", !up && !down && "text-muted", className)}>
      <Icon className="size-3.5" aria-hidden />
      {pct(value, digits)}
    </span>
  );
}

const PATHWAY_LABEL: Record<string, [string, string]> = {
  baseline_leader: ["Baseline leader", "Top baseline score (financial strength + data freshness) across the whole universe"],
  financial_leader: ["Financial leader", "Top market-wide screen for profitability, growth, free cash flow, balance sheet and earnings consistency"],
  valuation_opportunity: ["Valuation", "Attractive sector-relative earnings / FCF / EBIT yield"],
  new_catalyst: ["New catalyst", "Catalyst first detected since the previous discovery run"],
  underexplored: ["Underexplored", "Never analyzed, or analyzed least recently"],
  refill: ["Refill", "Filled an unused pathway slot from the baseline ranking"],
};

const VALUATION_STATUS: Record<string, [string, "up" | "warn" | "down" | "default", string]> = {
  valid: ["Valid valuation", "up", "Supported by company-specific evidence"],
  limited_data: ["Limited data", "warn", "Usable, but relies on fallbacks (sector reference multiples, short history or missing inputs)"],
  requires_review: ["Requires review", "down", "The result is not supported by its assumptions; excluded from ranked recommendations"],
  unsupported_valuation: ["Unsupported valuation", "default", "No suitable valuation method for this business with the available data; no target shown"],
  invalid_inputs: ["Invalid inputs", "down", "Inconsistent or implausible source data; targets withheld"],
};

export function ValuationStatusBadge({ status }: { status: string | null | undefined }) {
  if (!status) return null;
  const [text, variant, title] = VALUATION_STATUS[status] ?? [status, "default", ""];
  return (
    <Badge variant={variant} title={title}>
      {text}
    </Badge>
  );
}

export function PathwayBadge({ pathway }: { pathway: string | null | undefined }) {
  if (!pathway) return <span className="text-subtle">—</span>;
  const [text, title] = PATHWAY_LABEL[pathway] ?? [pathway, ""];
  return (
    <Badge variant={pathway === "underexplored" ? "warn" : pathway === "new_catalyst" ? "accent" : "default"} title={title}>
      {text}
    </Badge>
  );
}

export function OutlookBadge({ outlook }: { outlook: string | null | undefined }) {
  if (!outlook) return <span className="text-subtle">—</span>;
  const v = outlook === "bullish" ? "up" : outlook === "bearish" ? "down" : "default";
  const Icon = outlook === "bullish" ? ArrowUpRight : outlook === "bearish" ? ArrowDownRight : Minus;
  return (
    <Badge variant={v}>
      <Icon className="size-3" /> {outlook[0].toUpperCase() + outlook.slice(1)}
    </Badge>
  );
}

export function RiskBadge({ risk }: { risk: string | null | undefined }) {
  if (!risk) return <span className="text-subtle">—</span>;
  const v = risk === "Low" ? "up" : risk === "High" ? "down" : "warn";
  return (
    <Badge variant={v} title={`${risk} risk`}>
      {risk}
    </Badge>
  );
}

export function EventStatusBadge({ status }: { status: string }) {
  const v = status === "confirmed" ? "default" : status === "unconfirmed" ? "warn" : status === "scheduled" ? "accent" : "default";
  const text = status === "unconfirmed" ? "Unconfirmed report" : status === "estimated" ? "Estimated date" : status[0].toUpperCase() + status.slice(1);
  return (
    <Badge variant={v} title={status === "unconfirmed" ? "Relies on unnamed sources or speculative language — not a confirmed fact" : undefined}>
      {status === "unconfirmed" && <AlertTriangle className="size-3" />}
      {text}
    </Badge>
  );
}

export function ImpactBadge({ impact }: { impact: string }) {
  const v = impact === "positive" ? "up" : impact === "negative" ? "down" : "default";
  const Icon = impact === "positive" ? ArrowUpRight : impact === "negative" ? ArrowDownRight : Minus;
  return (
    <Badge variant={v}>
      <Icon className="size-3" />
      {impact[0].toUpperCase() + impact.slice(1)}
    </Badge>
  );
}

export function PriceTag({ p, size = "md" }: { p: LatestPrice | null | undefined; size?: "md" | "lg" }) {
  if (!p) return <span className="text-sm text-subtle">Price unavailable</span>;
  return (
    <div className="flex flex-col">
      <div className="flex items-baseline gap-2">
        <span className={cn("num font-semibold", size === "lg" ? "text-3xl" : "text-sm")}>{money(p.price)}</span>
        <ReturnText value={p.change_pct} className={size === "lg" ? "text-base" : "text-xs"} digits={2} />
      </div>
      <span className="text-[11px] text-subtle">
        Close {day(p.trade_date)}
        {p.stale_sessions > 0 && <span className="ml-1 text-warn">· {p.stale_sessions} session(s) old</span>}
      </span>
    </div>
  );
}

export function Kpi({ label, value, sub, badge }: { label: string; value: ReactNode; sub?: ReactNode; badge?: ReactNode }) {
  return (
    <div className="rounded-lg border border-border bg-surface-2/60 p-3">
      <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1 text-[11px] uppercase tracking-wide text-subtle">
        <span>{label}</span>
        {badge}
      </div>
      <div className="mt-1 text-lg font-semibold num">{value}</div>
      {sub && <div className="mt-0.5 text-[11px] text-muted">{sub}</div>}
    </div>
  );
}

export function ErrorState({ error, onRetry, title = "Something went wrong" }: { error: unknown; onRetry?: () => void; title?: string }) {
  const msg = error instanceof ApiError || error instanceof Error ? error.message : String(error);
  return (
    <Card className="flex items-start gap-3 border-down/40 bg-down/5 p-4">
      <AlertTriangle className="mt-0.5 size-5 shrink-0 text-down" />
      <div className="flex-1">
        <div className="text-sm font-medium">{title}</div>
        <div className="mt-1 text-sm text-muted">{msg}</div>
      </div>
      {onRetry && (
        <Button size="sm" variant="secondary" onClick={onRetry}>
          <RefreshCw /> Retry
        </Button>
      )}
    </Card>
  );
}

export function EmptyState({ title, children, icon }: { title: string; children?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-border px-6 py-10 text-center">
      {icon ?? <Inbox className="size-6 text-subtle" />}
      <div className="text-sm font-medium">{title}</div>
      {children && <div className="max-w-md text-sm text-muted">{children}</div>}
    </div>
  );
}

export function LoadingRows({ rows = 5 }: { rows?: number }) {
  return (
    <div className="flex flex-col gap-2">
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className="h-9 w-full" />
      ))}
    </div>
  );
}

export function PageHeader({ title, description, actions }: { title: string; description?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {description && <p className="mt-1 text-sm text-muted">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export const Disclaimer = () => (
  <p className="text-[11px] leading-relaxed text-subtle">
    Research tool for personal use — not investment advice. Model forecasts are fundamental value estimates under stated
    assumptions, not predictions that a price will be reached by a date.
  </p>
);
