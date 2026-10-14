import { Bot, ExternalLink } from "lucide-react";

import { ValuationStatusBadge } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import type { MultipleBand, MultipleSelection, ValuationAudit } from "@/lib/api";
import { askAnalyst } from "@/lib/chat";
import { compact, day, label, money, num, pct } from "@/lib/format";
import { cn } from "@/lib/utils";

const KIND_LABEL: Record<string, string> = { pe: "P/E", ps: "P/S", pb: "P/B", ptbv: "P/TBV", pffo: "P/FFO" };
const SEVERITY: Record<string, "default" | "warn" | "down" | "accent"> = { info: "default", warning: "warn", review: "down", invalid: "down" };

function x(v: number | null | undefined, d = 1): string {
  return v === null || v === undefined ? "—" : `${num(v, d)}x`;
}

function band(b: MultipleBand | null | undefined): string {
  if (!b) return "not available";
  return `${x(b.p25)} – ${x(b.p75)} (median ${x(b.p50)}, ${b.n} obs.)`;
}

function secUrl(cik: string | null | undefined, accn: string | undefined): string | null {
  if (!cik || !accn) return null;
  return `https://www.sec.gov/Archives/edgar/data/${Number(cik)}/${accn.replace(/-/g, "")}/`;
}

function SelectionRows({ title, sel }: { title: string; sel: MultipleSelection }) {
  const kind = KIND_LABEL[sel.kind] ?? sel.kind;
  return (
    <>
      <TR>
        <TD className="text-muted">{title}: current {kind}</TD>
        <TD>{x(sel.current)}</TD>
      </TR>
      <TR>
        <TD className="text-muted">Company historical {kind}</TD>
        <TD>{band(sel.evidence.company_history)}</TD>
      </TR>
      <TR>
        <TD className="text-muted">Peer {kind} range</TD>
        <TD>{band(sel.evidence.peers)}</TD>
      </TR>
      {sel.evidence.fundamental_anchor !== null && sel.evidence.fundamental_anchor !== undefined && (
        <TR>
          <TD className="text-muted">Justified (fundamental) {kind}</TD>
          <TD>{x(sel.evidence.fundamental_anchor, 2)}</TD>
        </TR>
      )}
      <TR>
        <TD className="text-muted">Selected {kind} (bear / base anchor / bull)</TD>
        <TD>
          {x(sel.bear)} / {x(sel.base_anchor)} / {x(sel.bull)}{" "}
          <Badge variant={sel.limited ? "warn" : "default"}>{label(sel.basis)}</Badge>
        </TD>
      </TR>
    </>
  );
}

export function ValuationExplainer({ v, cik, ticker }: { v: ValuationAudit; cik?: string | null; ticker?: string }) {
  const base = v.scenarios_12m.base;
  const primary = v.multiples.book?.multiples ?? v.multiples.ffo?.multiples ?? (v.method.startsWith("revenue") ? v.multiples.ps : v.multiples.pe);
  const ni = v.provenance?.net_income;
  const rev = v.provenance?.revenue;
  const evidence = [rev, ni, v.provenance?.shares_outstanding, v.provenance?.book_value].filter((p) => p && p.accession);
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          How EventAlpha calculated this forecast <ValuationStatusBadge status={v.status} />
        </CardTitle>
        <CardDescription>
          Every value below comes from SEC filings, market prices or a documented model calculation ({v.model_version}). Upside is never capped; results the
          assumptions do not support are flagged and kept out of ranked recommendations.
        </CardDescription>
        {ticker && (
          <div className="flex flex-wrap gap-1.5 pt-1">
            {[
              base.target_price !== null ? `Why is the 12-month base target ${money(base.target_price)}?` : "Why is there no 12-month target?",
              "How much upside comes from earnings growth versus P/E expansion?",
              "How does the DCF differ from the Forward P/E valuation?",
            ].map((q) => (
              <Button key={q} size="sm" variant="secondary" className="h-7 text-[11px]" onClick={() => askAnalyst(ticker, q)}>
                <Bot className="size-3.5" /> {q}
              </Button>
            ))}
          </div>
        )}
      </CardHeader>
      <CardContent className="flex flex-col gap-4 text-sm">
        <div>
          <div className="font-medium">
            Valuation method: {v.method_label} <span className="text-xs text-subtle">({label(v.business_model)})</span>
          </div>
          <p className="text-muted">{v.rationale}</p>
          {v.missing_inputs.length > 0 && <p className="text-xs text-warn">Missing inputs: {v.missing_inputs.join(", ")}</p>}
          {[...v.plan_notes, ...v.notes].map((n) => (
            <p key={n} className="text-xs text-subtle">
              {n}
            </p>
          ))}
        </div>
        <div className="grid gap-4 lg:grid-cols-2">
          <Table>
            <TBody>
              {primary && <SelectionRows title="Multiple" sel={primary} />}
              {v.multiples.book && (
                <TR>
                  <TD className="text-muted">Normalized {v.multiples.book.kind === "bank" ? "return on tangible equity" : "ROE"}</TD>
                  <TD className="whitespace-normal">
                    {pct(v.multiples.book.normalized_return, 1, false)} <span className="text-[11px] text-subtle">({v.multiples.book.return_basis})</span>
                  </TD>
                </TR>
              )}
              {v.multiples.ffo && (
                <TR>
                  <TD className="text-muted">FFO per share (TTM)</TD>
                  <TD className="whitespace-normal">
                    {money(v.multiples.ffo.ffo_per_share_today)} <span className="text-[11px] text-subtle">({v.multiples.ffo.ffo_basis})</span>
                  </TD>
                </TR>
              )}
              {v.peers && (
                <TR>
                  <TD className="text-muted">Peers ({v.peers.level})</TD>
                  <TD className="whitespace-normal text-xs">{v.peers.tickers.join(", ")}</TD>
                </TR>
              )}
              <TR>
                <TD className="text-muted">Earnings state</TD>
                <TD>{label(v.earnings_state)}</TD>
              </TR>
              <TR>
                <TD className="text-muted">Market cap</TD>
                <TD className="whitespace-normal">
                  {compact(v.market_cap.value)} <span className="text-[11px] text-subtle">({v.market_cap.basis})</span>
                </TD>
              </TR>
            </TBody>
          </Table>
          <Table>
            <THead>
              <TR className="hover:bg-transparent">
                <TH>12 months</TH>
                <TH>Bear</TH>
                <TH>Base</TH>
                <TH>Bull</TH>
              </TR>
            </THead>
            <TBody>
              {(
                [
                  ["Revenue", (s) => compact(s.revenue)],
                  ["Net income", (s) => compact(s.net_income)],
                  ["EPS", (s) => num(s.eps, 2)],
                  ["Free cash flow", (s) => compact(s.free_cash_flow)],
                  ["Capex", (s) => compact(s.capex)],
                  ["Shares", (s) => compact(s.shares, "")],
                  ["Multiple", (s) => (s.book_value_model ? x(s.book_value_model.multiple, 2) : s.ffo_model ? x(s.ffo_model.multiple) : s.price_to_sales_multiple ? x(s.price_to_sales_multiple, 2) : x(s.pe_multiple))],
                  ["Forecast price", (s) => money(s.target_price ?? null)],
                  ["Upside / downside", (s) => pct(s.potential_return)],
                ] as [string, (s: ValuationAudit["scenarios_12m"]["base"]) => string][]
              ).map(([name, fn]) => (
                <TR key={name}>
                  <TD className="text-muted">{name}</TD>
                  {(["bear", "base", "bull"] as const).map((k) => (
                    <TD key={k} className={cn("num", name === "Forecast price" && "font-medium")}>
                      {fn(v.scenarios_12m[k])}
                    </TD>
                  ))}
                </TR>
              ))}
            </TBody>
          </Table>
        </div>
        {base.method_targets && Object.keys(base.method_targets).length > 0 && (
          <p className="text-xs text-muted">
            Base-case method values:{" "}
            {Object.entries(base.method_targets)
              .map(([k, t]) => `${label(k)} ${money(t)}${base.method_weights?.[k] !== undefined ? ` (${pct(base.method_weights[k], 0, false)})` : " (cross-check, not blended)"}`)
              .join(" · ")}{" "}
            · target date {day(base.target_date)}
          </p>
        )}
        <div>
          <div className="mb-1 font-medium">Guardrails</div>
          {v.guardrails.flags.length === 0 ? (
            <p className="text-xs text-subtle">No warnings.</p>
          ) : (
            <ul className="flex flex-col gap-1">
              {v.guardrails.flags.map((f) => (
                <li key={f.code + f.message} className="text-xs">
                  <Badge variant={SEVERITY[f.severity]}>{label(f.severity)}</Badge> {f.message}
                </li>
              ))}
            </ul>
          )}
          {v.guardrails.upside_check?.checks && (
            <ul className="mt-2 grid gap-0.5 text-xs text-muted sm:grid-cols-2">
              {Object.entries(v.guardrails.upside_check.checks).map(([k, ok]) => (
                <li key={k}>
                  {ok ? "✓" : "✗"} {k}
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="flex flex-wrap gap-1.5 text-[11px]">
          <span className="text-subtle">Financial coverage:</span>
          {Object.entries(v.financial_coverage).map(([k, ok]) => (
            <Badge key={k} variant={ok ? "default" : "warn"}>
              {ok ? "" : "missing "}
              {label(k)}
            </Badge>
          ))}
        </div>
        <div className="text-xs text-muted">
          <div className="mb-1 font-medium text-foreground">Supporting evidence</div>
          <ul className="flex flex-col gap-0.5">
            {evidence.map((p) => {
              const url = secUrl(cik, p!.accession);
              return (
                <li key={`${p!.concept}-${p!.accession}`}>
                  {p!.concept} · period ending {day(p!.period_end)} ({label(p!.basis)}) · filed {day(p!.filed)}{" "}
                  {url ? (
                    <a href={url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 hover:text-accent">
                      {p!.accession} <ExternalLink className="size-3" />
                    </a>
                  ) : (
                    p!.accession
                  )}
                </li>
              );
            })}
            <li>
              Price {day(v.data_timestamps.price_date)} · latest filing {day(v.data_timestamps.latest_filing)} · TTM through {day(v.data_timestamps.ttm_period_end)}
            </li>
            {v.split_adjustments.map((s) => (
              <li key={s}>{s}</li>
            ))}
            {v.validation.map((s) => (
              <li key={s} className="text-warn">
                {s}
              </li>
            ))}
          </ul>
        </div>
      </CardContent>
    </Card>
  );
}
