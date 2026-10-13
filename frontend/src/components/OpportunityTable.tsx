import { Link } from "react-router-dom";

import { OutlookBadge, PriceTag, ProvenanceBadge, ReturnText, RiskBadge } from "@/components/common";
import { PathwayBadge } from "@/components/common";
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

export function OpportunityTable({ items, compact = false }: { items: DiscoveryItem[]; compact?: boolean }) {
  return (
    <Table>
      <THead>
        <TR className="hover:bg-transparent">
          <TH>#</TH>
          <TH>Ticker</TH>
          <TH>Last price</TH>
          <TH title="Base-case model target vs latest price">
            Potential <span className="normal-case">(base)</span>
          </TH>
          <TH>Target · date</TH>
          <TH>Outlook</TH>
          <TH>Risk</TH>
          <TH>Horizon</TH>
          {!compact && <TH>Score</TH>}
          {!compact && <TH title="How this stock qualified for the candidate list">Pathway</TH>}
          <TH>Catalyst</TH>
          {!compact && <TH>Analyzed</TH>}
        </TR>
      </THead>
      <TBody>
        {items.map((i) => (
          <TR key={i.ticker}>
            <TD className="text-subtle">{i.rank}</TD>
            <TD>
              <Link to={`/stocks/${i.ticker}`} className="font-semibold text-foreground hover:text-accent">
                {i.ticker}
              </Link>
              <div className="max-w-44 truncate text-[11px] text-muted" title={i.name ?? ""}>
                {i.name}
              </div>
            </TD>
            <TD>
              <PriceTag p={i.latest_price} />
            </TD>
            <TD>
              <ReturnText value={i.potential_return} />
              <div className="text-[11px] text-subtle" title="Bear – bull range">
                {money(i.bear_target, 0)} – {money(i.bull_target, 0)}
              </div>
            </TD>
            <TD>
              <div className="num">{money(i.target_price)}</div>
              <div className="text-[11px] text-subtle">{day(i.target_date)}</div>
            </TD>
            <TD>
              <OutlookBadge outlook={i.outlook} />
            </TD>
            <TD>
              <RiskBadge risk={i.risk_rating} />
            </TD>
            <TD>
              <Badge>{i.horizon_class === "short_term" ? "Short-term" : "Long-term"}</Badge>
            </TD>
            {!compact && (
              <TD title={scoreTooltip(i)}>
                <span className="num font-medium">{i.score}</span>
                <span className="ml-1 text-[11px] text-subtle">{i.score_band}</span>
              </TD>
            )}
            {!compact && (
              <TD>
                <PathwayBadge pathway={i.selection_pathway} />
              </TD>
            )}
            <TD className="max-w-64 whitespace-normal">
              {i.catalyst?.title ? (
                <div className="line-clamp-2 text-xs" title={i.catalyst.title}>
                  {i.catalyst.title}
                  {i.catalyst.event_date && <span className="ml-1 text-subtle">({day(i.catalyst.event_date)})</span>}
                </div>
              ) : (
                <span className="text-xs text-subtle">No linked catalyst</span>
              )}
            </TD>
            {!compact && <TD className="text-xs text-muted">{relative(i.analysis_at)}</TD>}
          </TR>
        ))}
      </TBody>
      <caption className="mt-2 text-left text-[11px] text-subtle">
        <span className="inline-flex flex-wrap items-center gap-2">
          <ProvenanceBadge kind="observed" /> prices · <ProvenanceBadge kind="model_forecast" /> targets, returns, outlook ·{" "}
          hover a score for its factor breakdown
        </span>
      </caption>
    </Table>
  );
}
