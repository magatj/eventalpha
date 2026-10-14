import { useState } from "react";
import { Link } from "react-router-dom";

import { EmptyState, Kpi, LoadingRows, PathwayBadge, ValuationStatusBadge } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { type CandidateRow, type DiscoveryStats, useDiscoveryCandidates } from "@/lib/api";
import { day, num, relative } from "@/lib/format";

const COVERAGE_LABEL: Record<string, string> = { detected: "Catalyst detected", none_detected: "Checked: none", unknown: "Not yet checked" };

export function SelectionSummary({ stats }: { stats: DiscoveryStats }) {
  const slots = stats.pathway_slots ?? {};
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-6">
      <Kpi label="Universe" value={stats.universe} sub={`${stats.eligible} eligible`} />
      <Kpi label="Candidates" value={stats.selected} sub={`${stats.excluded} excluded after screening`} />
      <Kpi
        label="Pathways"
        value={`${slots.baseline_leader ?? 0}/${slots.new_catalyst ?? 0}/${slots.underexplored ?? 0}`}
        sub={`leaders / new catalysts / underexplored${slots.refill ? ` · ${slots.refill} refill` : ""}`}
      />
      <Kpi label="Never analyzed before" value={stats.previously_unanalyzed_selected} sub={`${stats.previously_unanalyzed_ranked} reached the ranking`} />
      <Kpi label="Sectors (candidates)" value={Object.keys(stats.selected_sectors ?? {}).length} sub={`${Object.keys(stats.ranked_sectors ?? {}).length} in final ranking`} />
      <Kpi
        label="News coverage"
        value={`${stats.coverage?.detected ?? 0}/${stats.universe}`}
        sub={`${stats.coverage?.none_detected ?? 0} checked: none · ${stats.coverage?.unknown ?? 0} unknown`}
      />
    </div>
  );
}

function row(c: CandidateRow) {
  const statusVariant = c.status === "ranked" ? "up" : c.status === "excluded" || c.status === "ineligible" ? "down" : "default";
  return (
    <TR key={c.ticker}>
      <TD>
        <Link to={`/stocks/${c.ticker}`} className="font-semibold hover:text-accent">
          {c.ticker}
        </Link>
      </TD>
      <TD>
        <Badge variant={statusVariant}>{c.status.replace("_", " ")}</Badge>
        {c.final_rank && <span className="ml-1 text-[11px] text-subtle">#{c.final_rank}</span>}
      </TD>
      <TD>
        <PathwayBadge pathway={c.pathway} />
        {c.qualifying_pathways.length > 1 && <span className="ml-1 text-[11px] text-subtle">+{c.qualifying_pathways.length - 1}</span>}
      </TD>
      <TD className="text-right">{c.baseline_score != null ? num(c.baseline_score, 3) : "—"}</TD>
      <TD className="text-xs">{COVERAGE_LABEL[c.catalyst_status] ?? c.catalyst_status}</TD>
      <TD className="text-xs">{c.previously_analyzed ? relative(c.last_analyzed_at) : <span className="text-warn">never</span>}</TD>
      <TD className="text-right">{c.score ?? "—"}</TD>
      <TD className="text-[11px] text-subtle">
        price {day(c.data_timestamps?.price_date)} · filing {day(c.data_timestamps?.financials_as_of)}
      </TD>
      <TD className="max-w-72 whitespace-normal text-xs text-muted">
        {c.exclusion_reason ?? ""}
        {c.exclusion_codes && c.exclusion_codes.length > 1 && (
          <div className="text-[11px] text-subtle">all checks failed: {c.exclusion_codes.map((x) => x.replace(/_/g, " ")).join(", ")}</div>
        )}
        {c.valuation_status && c.valuation_status !== "valid" && (
          <div className="mt-0.5">
            <ValuationStatusBadge status={c.valuation_status} />
          </div>
        )}
      </TD>
    </TR>
  );
}

export function SelectionAudit() {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState<"selected" | "all">("selected");
  const { data, isLoading, error } = useDiscoveryCandidates(open, filter);
  const rows = data?.candidates ?? [];
  return (
    <Card className="mt-4">
      <CardHeader className="flex-row flex-wrap items-center justify-between gap-2">
        <div>
          <CardTitle>Selection audit</CardTitle>
          <CardDescription>Why each universe stock was or wasn't a candidate, the data it was judged on, and any exclusion reason.</CardDescription>
        </div>
        <div className="flex gap-1">
          {open && (
            <>
              <Button size="sm" variant={filter === "selected" ? "secondary" : "ghost"} onClick={() => setFilter("selected")}>
                Candidates
              </Button>
              <Button size="sm" variant={filter === "all" ? "secondary" : "ghost"} onClick={() => setFilter("all")}>
                Whole universe
              </Button>
            </>
          )}
          <Button size="sm" variant="outline" onClick={() => setOpen(!open)}>
            {open ? "Hide" : "Show"}
          </Button>
        </div>
      </CardHeader>
      {open && (
        <CardContent>
          {isLoading ? (
            <LoadingRows />
          ) : error || !data ? (
            <EmptyState title="No audit available">Run discovery to produce one.</EmptyState>
          ) : (
            <div className="max-h-[520px] overflow-y-auto">
              <p className="mb-2 text-[11px] text-subtle">
                Run #{data.run_id} · tie-break seed {data.seed} · showing {rows.length} of {data.total_rows}
                {rows.length < data.total_rows && " (download the CSV for every row)"}
              </p>
              <Table>
                <THead>
                  <TR>
                    <TH>Ticker</TH>
                    <TH>Status</TH>
                    <TH>Pathway</TH>
                    <TH className="text-right">Baseline</TH>
                    <TH>News coverage</TH>
                    <TH>Last analyzed</TH>
                    <TH className="text-right">Score</TH>
                    <TH>Data as of</TH>
                    <TH>Exclusion reason</TH>
                  </TR>
                </THead>
                <TBody>{rows.map(row)}</TBody>
              </Table>
            </div>
          )}
        </CardContent>
      )}
    </Card>
  );
}
