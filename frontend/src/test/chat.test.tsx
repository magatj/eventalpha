import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Markdown } from "@/components/Markdown";
import { StockResearchChat } from "@/components/StockResearchChat";
import { parseSSE, safeHref } from "@/lib/chat";

describe("safe markdown", () => {
  it("renders formatting without raw HTML and drops unsafe links", () => {
    const { container } = render(
      <Markdown
        text={"**Base** target [S1] <img src=x onerror=alert(1)> [click](javascript:alert(1)) [ok](https://www.sec.gov)\n\n- item one\n- item two"}
        sources={[{ label: "S1", type: "sec_xbrl", title: "SEC facts", url: "https://data.sec.gov/x", published_at: null }]}
      />,
    );
    expect(container.querySelector("img")).toBeNull(); // HTML is shown as text, never parsed
    expect(screen.getByText("Base").tagName).toBe("STRONG");
    expect(screen.getByRole("link", { name: "[S1]" })).toHaveAttribute("href", "https://data.sec.gov/x");
    expect(screen.queryByRole("link", { name: "click" })).toBeNull();
    expect(screen.getByRole("link", { name: "ok" })).toHaveAttribute("rel", expect.stringContaining("noopener"));
    expect(container.querySelectorAll("li")).toHaveLength(2);
    expect(safeHref("data:text/html,x")).toBeNull();
  });
});

describe("server-sent events", () => {
  it("parses complete frames and keeps the remainder", () => {
    const { events, rest } = parseSSE('event: delta\ndata: {"type":"delta","text":"Hi"}\n\nevent: done\ndata: {"type":"do');
    expect(events).toEqual([{ type: "delta", text: "Hi" }]);
    expect(rest).toContain('"do');
  });
});

function sseBody(frames: object[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  return new ReadableStream({
    start(c) {
      frames.forEach((f) => c.enqueue(enc.encode(`event: x\ndata: ${JSON.stringify(f)}\n\n`)));
      c.close();
    },
  });
}

describe("StockResearchChat", () => {
  afterEach(() => vi.restoreAllMocks());

  it("shows suggested prompts, streams an answer with citations and a hypothetical scenario", async () => {
    const history = {
      ticker: "NVDA", configured: true, model: "gpt-5-mini", conversation_id: null, conversation_analysis_id: null,
      latest_analysis_id: 1, forecast_changed: false, messages: [],
      suggested_prompts: ["Explain the 12-month base-case forecast."],
      budget: { chat_spent_usd: 0, chat_budget_usd: 5, chat_remaining_usd: 5, ai_spent_usd: 0, ai_budget_usd: 10 },
    };
    const scenario = {
      label: "USER-GENERATED HYPOTHETICAL SCENARIO", horizon_months: 12, valuation_date: "2027-10-12", reference_price: 226.32,
      overrides: { eps_change: -0.2 }, official_targets: { bear: 236.99, base: 365.81, bull: 598.11 },
      recomputed_baseline: {}, hypothetical: { bear: { target_price: 164.38, return: -0.27 }, base: { target_price: 263.91, return: 0.17 }, bull: { target_price: 441.18, return: 0.95 } },
    };
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST")
        return new Response(
          sseBody([
            { type: "meta", conversation_id: "c1", analysis_id: 1, valuation_status: "valid", sources: [{ label: "S1", type: "eventalpha_analysis", title: "EventAlpha analysis #1", url: null, published_at: null }], freshness: { has_analysis: true, price_date: "2026-10-12", stale: true, analysis_created_at: "2026-10-13T20:00:00Z" } },
            { type: "tool", name: "calculate_what_if_scenario", status: "running" },
            { type: "scenario", scenario },
            { type: "delta", text: "Hypothetical base **263.91** [S1]" },
            { type: "done", message_id: 9, status: "ok", content: "Hypothetical base **263.91** [S1]", citations: [{ label: "S1", type: "eventalpha_analysis", title: "EventAlpha analysis #1", url: null, published_at: null }], follow_ups: ["What are the upcoming catalysts?"], usage: { input_tokens: 1, output_tokens: 1, cost_usd: 0.004 }, budget: history.budget },
          ]),
          { status: 200, headers: { "Content-Type": "text/event-stream" } },
        );
      expect(url).toBe("/api/stocks/NVDA/chat/history");
      return new Response(JSON.stringify(history), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchMock);
    const onRefresh = vi.fn();
    render(
      <QueryClientProvider client={new QueryClient()}>
        <StockResearchChat ticker="NVDA" onRefreshData={onRefresh} />
      </QueryClientProvider>,
    );
    fireEvent.click(await screen.findByText("Explain the 12-month base-case forecast."));
    expect(await screen.findByText(/Hypothetical scenario — not an official EventAlpha forecast/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByText("263.91").length).toBeGreaterThan(0));
    expect(screen.getByText("$263.91")).toBeInTheDocument();
    expect(screen.getByText("What are the upcoming catalysts?")).toBeInTheDocument();
    const body = JSON.parse((fetchMock.mock.calls.find((c) => c[1]?.method === "POST")![1] as RequestInit).body as string);
    expect(body).toEqual({ message: "Explain the 12-month base-case forecast.", conversation_id: null });
    fireEvent.click(screen.getByText(/Refresh data/)); // stale data offers an explicit refresh
    expect(onRefresh).toHaveBeenCalled();
  });
});
