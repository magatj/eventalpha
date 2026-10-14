import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ValuationStatusBadge } from "@/components/common";
import { ValuationExplainer } from "@/components/ValuationExplainer";
import type { ValuationAudit, ValuationScenario } from "@/lib/api";

function scen(target: number | null, ret: number | null, eps: number): ValuationScenario {
  return {
    revenue: 1e9, revenue_growth: 0.05, net_margin: 0.1, net_income: 1e8, eps, free_cash_flow: 8e7, capex: 2e7, stock_based_comp: 1e7,
    shares: 5e7, pe_multiple: 10, book_value_model: null, ffo_model: null, price_to_sales_multiple: null, dcf: null,
    method_weights: { forward_pe: 1 }, method_targets: { forward_pe: target ?? 0, dcf: 99 }, target_price: target,
    potential_return: ret, withheld_target: null, target_date: "2027-10-12",
  };
}

const audit: ValuationAudit = {
  status: "requires_review",
  status_label: "Requires review",
  model_version: "quant-2.0.0",
  method: "earnings_dcf",
  method_label: "Forward P/E + DCF",
  business_model: "operating_company",
  rationale: "Profitable operating company.",
  required_inputs: { revenue: true },
  missing_inputs: [],
  plan_notes: [],
  weights: { forward_pe: 0.6, dcf: 0.4 },
  historical_multiples: {},
  peers: { level: "SIC 7389", tickers: ["AAA", "BBB"], bands: {}, profitable_margin: null },
  multiples: {
    pe: {
      kind: "pe", current: 4.8, bear: 4.8, base_anchor: 10.8, bull: 10.8, low: 4.8, high: 10.8, basis: "company_and_peers", limited: false,
      evidence: { company_history: { kind: "pe", p10: 3.5, p25: 3.9, p50: 4.8, p75: 5.5, p90: 6, n: 22, rejected: 2, source: "company_history", detail: "" }, peers: null, fundamental_anchor: null, sector_reference: [15, 20, 27] },
      notes: [],
    },
    ps: null,
    book: null,
    ffo: null,
  },
  earnings_state: "normal",
  regime: "earnings",
  scenarios_12m: { bear: scen(6.9, 0.13, 0.9), base: scen(20.4, 2.34, 1.7), bull: scen(33.3, 4.45, 2.4) },
  guardrails: {
    status: "requires_review",
    label: "Requires review",
    flags: [{ code: "unsupported_upside", severity: "review", message: "Base-case upside 234% exceeds 100% without support: multiple within company's historical median." }],
    upside_check: { threshold: 1, base_return_12m: 2.34, supported: false, checks: { "multiple within company's historical median": false, "financial data passed validation": true } },
  },
  market_cap: { value: 1.9e9, basis: "price x SEC shares outstanding", shares: 3.1e8 },
  financial_status: "valid",
  validation: [],
  split_adjustments: [],
  financial_coverage: { revenue: true, share_based_comp: false },
  provenance: { net_income: { concept: "NetIncomeLoss", period_end: "2026-06-30", basis: "ttm_4_quarters", filed: "2026-08-01", accession: "0001-26-000001" } },
  data_timestamps: { price_date: "2026-10-12", latest_filing: "2026-08-01", ttm_period_end: "2026-06-30" },
  notes: [],
};

describe("valuation explainability", () => {
  it("labels every valuation status in words, not color alone", () => {
    const { rerender } = render(<ValuationStatusBadge status="valid" />);
    expect(screen.getByText("Valid valuation")).toBeInTheDocument();
    rerender(<ValuationStatusBadge status="unsupported_valuation" />);
    expect(screen.getByText("Unsupported valuation")).toBeInTheDocument();
    rerender(<ValuationStatusBadge status={null} />);
    expect(screen.queryByText(/valuation/i)).not.toBeInTheDocument();
  });

  it("explains method, evidence, guardrails and sources from the audit record", () => {
    render(<ValuationExplainer v={audit} cik="0000012345" />);
    expect(screen.getByText(/How EventAlpha calculated this forecast/)).toBeInTheDocument();
    expect(screen.getByText(/Forward P\/E \+ DCF/)).toBeInTheDocument();
    expect(screen.getByText("Requires review")).toBeInTheDocument();
    expect(screen.getByText(/without support/)).toBeInTheDocument();
    expect(screen.getByText(/✗ multiple within company's historical median/)).toBeInTheDocument();
    expect(screen.getByText(/median 4.8x, 22 obs/)).toBeInTheDocument();
    expect(screen.getByText(/cross-check, not blended/)).toBeInTheDocument(); // DCF value shown but not weighted
    const link = screen.getByRole("link", { name: /0001-26-000001/ });
    expect(link).toHaveAttribute("href", "https://www.sec.gov/Archives/edgar/data/12345/000126000001/");
    expect(screen.getByText(/missing Share Based Comp/)).toBeInTheDocument();
  });
});
