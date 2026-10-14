import { Link } from "react-router-dom";

import { OutlookBadge, PathwayBadge, ProvenanceBadge, ReturnText, RiskBadge, ValuationStatusBadge } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import type { DiscoveryItem } from "@/lib/api";
import { day, money, relative } from "@/lib/format";

const FACTOR_LABELS: Record<string, string> = {
  financial_strength: "Financial strength",
  catalyst_materiality: "Catalyst materiality",
  valuation: "Valuation (base 12m)",
  data_freshness: "Data freshness",
  forecast_uncertainty: "Forecast uncertainty (−)",
  downside_risk: "Downside risk (−)",
};

function scoreTooltip(i: DiscoveryItem): string {
  const c = i.factors?.contributions ?? {};
  return Object.entries(c)
    .map(([k, v]) => `${FACTOR_LABELS[k] ?? k}: ${v > 0 ? "+" : ""}${v} pts`)
    .join("\n");
}

/** Compact price: price + change, then the close date (and staleness) — fits a narrow column. */
function PriceCell({ i }: { i: DiscoveryItem }) {
  const p = i.latest_price;
  if (!p) return <span className="text-xs text-subtle">Unavailable</span>;
  return (
    <div title={`Close ${day(p.trade_date)}${p.stale_sessions > 0 ? ` · ${p.stale_sessions} session(s) old` : ""}`}>
      <div className="font-semibold">{money(p.price)}</div>
      <ReturnText value={p.change_pct} className="text-[11px]" digits={2} />
      <div className="text-[10px] text-subtle">
        {day(p.trade_date)}
        {p.stale_sessions > 0 && <span className="text-warn"> · {p.stale_sessions}d old</span>}
      </div>
    </div>
  );
}

/**
 * Ranked opportunities. Columns are stacked so the full table fits the card without horizontal
 * scrolling: values that belong together (price/change, potential/target, outlook/risk/horizon,
 * score/pathway, reasons/catalyst) share a cell, and text cells wrap.
 */
export function OpportunityTable({ items, compact = false }: { items: DiscoveryItem[]; compact?: boolean }) {
  return (
    <Table className="table-fixed">
      <colgroup>
        <col className="w-8" />
        <col className={compact ? "w-[24%]" : "w-[15%]"} />
        <col className={compact ? "w-[17%]" : "w-[10%]"} />
        <col className={compact ? "w-[20%]" : "w-[13%]"} />
        <col className={compact ? "w-[17%]" : "w-[11%]"} />
        {!compact && <col className="w-[11%]" />}
        <col />
      </colgroup>
      <THead>
        <TR className="hover:bg-transparent">
          <TH className="px-2">#</TH>
          <TH className="px-2">Stock</TH>
          <TH className="px-2">Price</TH>
          <TH className="px-2" title="Base-case model target vs latest price; bear – bull range below">
            Potential <span className="normal-case">(base)</span>
          </TH>
          <TH className="px-2" title="Quantitative outlook, risk rating and horizon">
            Signals
          </TH>
          {!compact && (
            <TH className="px-2" title="Discovery score (hover for the factor breakdown) and how the stock qualified">
              Score
            </TH>
          )}
          <TH className="px-2">{compact ? "Catalyst" : "Why discovered · catalyst"}</TH>
        </TR>
      </THead>
      <TBody>
        {items.map((i) => (
          <TR key={i.ticker}>
            <TD className="px-2 align-top text-subtle">{i.rank}</TD>
            <TD className="whitespace-normal px-2 align-top">
              <Link to={`/stocks/${i.ticker}`} className="font-semibold text-foreground hover:text-accent">
                {i.ticker}
              </Link>
              <div className="line-clamp-2 text-[11px] leading-tight text-muted" title={i.name ?? ""}>
                {i.name}
              </div>
              {!compact && i.sector && <div className="truncate text-[10px] text-subtle">{i.sector}</div>}
            </TD>
            <TD className="whitespace-normal px-2 align-top">
              <PriceCell i={i} />
            </TD>
            <TD className="whitespace-normal px-2 align-top">
              <ReturnText value={i.potential_return} />
              <div className="text-[11px]">
                {money(i.target_price)} <span className="text-subtle">· {day(i.target_date)}</span>
              </div>
              <div className="text-[10px] text-subtle" title="Bear – bull range">
                {money(i.bear_target, 0)} – {money(i.bull_target, 0)}
              </div>
              {i.valuation_status && i.valuation_status !== "valid" && (
                <div className="mt-0.5" title={i.valuation_method ?? undefined}>
                  <ValuationStatusBadge status={i.valuation_status} />
                </div>
              )}
            </TD>
            <TD className="whitespace-normal px-2 align-top">
              <div className="flex flex-wrap gap-1">
                <OutlookBadge outlook={i.outlook} />
                <RiskBadge risk={i.risk_rating} />
                <Badge>{i.horizon_class === "short_term" ? "Short-term" : "Long-term"}</Badge>
              </div>
            </TD>
            {!compact && (
              <TD className="whitespace-normal px-2 align-top" title={scoreTooltip(i)}>
                <span className="font-medium">{i.score_display ?? Math.round(i.score / 5) * 5}</span>
                <span className="ml-1 text-[11px] text-subtle">{i.score_band}</span>
                <div className="mt-0.5">
                  <PathwayBadge pathway={i.selection_pathway} />
                </div>
                <div className="mt-0.5 text-[10px] text-subtle">analyzed {relative(i.analysis_at)}</div>
              </TD>
            )}
            <TD className="whitespace-normal px-2 align-top">
              {!compact && (i.reasons ?? []).length > 0 && (
                <ul className="mb-1 flex flex-col gap-0.5 text-[11px] text-muted" title={(i.reasons ?? []).map((r) => r.text).join(" · ")}>
                  {(i.reasons ?? []).slice(0, 2).map((r) => (
                    <li key={r.code} className="line-clamp-2">
                      •{" "}
                      {r.source_url ? (
                        <a href={r.source_url} target="_blank" rel="noopener noreferrer" className="hover:text-accent">
                          {r.text}
                        </a>
                      ) : (
                        r.text
                      )}
                    </li>
                  ))}
                  {(i.reasons ?? []).length > 2 && <li className="text-subtle">+{(i.reasons ?? []).length - 2} more</li>}
                </ul>
              )}
              {i.catalyst?.title ? (
                <div className="line-clamp-2 text-xs" title={i.catalyst.title}>
                  {!compact && <span className="text-subtle">Catalyst: </span>}
                  {i.catalyst.title}
                  {i.catalyst.event_date && <span className="ml-1 text-subtle">({day(i.catalyst.event_date)})</span>}
                </div>
              ) : (
                <span className="text-xs text-subtle">No linked catalyst</span>
              )}
            </TD>
          </TR>
        ))}
      </TBody>
      <caption className="mt-2 text-left text-[11px] text-subtle">
        <span className="inline-flex flex-wrap items-center gap-2">
          <ProvenanceBadge kind="observed" /> prices · <ProvenanceBadge kind="model_forecast" /> targets, returns, outlook
          {!compact && " · hover a score for its factor breakdown"}
        </span>
      </caption>
    </Table>
  );
}
