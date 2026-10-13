import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DemoBadge, PriceTag, ProvenanceBadge, ReturnText } from "@/components/common";

describe("labels and provenance", () => {
  it("never encodes gain/loss by color alone", () => {
    render(<ReturnText value={0.2} />);
    expect(screen.getByText("+20.0%")).toBeInTheDocument();
  });
  it("labels model forecasts distinctly from observed data", () => {
    render(
      <>
        <ProvenanceBadge kind="model_forecast" />
        <ProvenanceBadge kind="observed" />
      </>,
    );
    expect(screen.getByText(/Model forecast/)).toBeInTheDocument();
    expect(screen.getByText(/Observed/)).toBeInTheDocument();
  });
  it("shows a DEMO badge only in demo mode", () => {
    const { rerender } = render(<DemoBadge mode="demo" />);
    expect(screen.getByText(/DEMO DATA/)).toBeInTheDocument();
    rerender(<DemoBadge mode="live" />);
    expect(screen.queryByText(/DEMO DATA/)).not.toBeInTheDocument();
  });
  it("shows an unavailable state instead of a fabricated price", () => {
    render(<PriceTag p={null} />);
    expect(screen.getByText("Price unavailable")).toBeInTheDocument();
  });
  it("flags stale prices", () => {
    render(
      <PriceTag
        p={{ price: 10, trade_date: "2026-10-01", change_pct: 0.01, volume: 1, provenance: "observed", source: "x", ingested_at: null, stale_sessions: 3 }}
      />,
    );
    expect(screen.getByText(/3 session\(s\) old/)).toBeInTheDocument();
  });
});
