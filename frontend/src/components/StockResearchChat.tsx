import { useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Bot, Loader2, RefreshCw, Send, Square, Trash2, Wrench } from "lucide-react";
import React, { useCallback, useEffect, useRef, useState } from "react";

import { Markdown } from "@/components/Markdown";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  ASK_EVENT,
  type ChatEvent,
  type ChatMessage,
  type ChatSource,
  clearChat,
  type Freshness,
  streamChat,
  useChatHistory,
  type WhatIfScenario,
} from "@/lib/chat";
import { day, money, pct, relative } from "@/lib/format";
import { cn } from "@/lib/utils";

const TOOL_LABEL: Record<string, string> = {
  get_stock_fundamentals: "Reading SEC financials",
  get_latest_forecast: "Reading the stored forecast",
  get_forecast_assumptions: "Reading forecast assumptions",
  get_recent_events: "Reading news and filings",
  get_forecast_history: "Reading forecast history",
  get_discovery_score: "Reading discovery ranking",
  calculate_what_if_scenario: "Running the valuation engine (hypothetical)",
};

function FreshnessBar({ f, onRefresh }: { f: Freshness | null | undefined; onRefresh?: () => void }) {
  if (!f) return null;
  if (f.has_analysis === false)
    return <p className="text-xs text-warn">No stored analysis yet — run “Refresh analysis” first.</p>;
  return (
    <div className={cn("flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]", f.stale ? "text-warn" : "text-subtle")}>
      {f.stale && <AlertTriangle className="size-3.5" />}
      <span>Price {day(f.price_date)}{f.price_stale_sessions ? ` (${f.price_stale_sessions} session(s) old)` : ""}</span>
      <span>Analysis {relative(f.analysis_created_at)}</span>
      {f.latest_filing && <span>Latest filing {day(f.latest_filing)}</span>}
      {f.forecast_model_version && <span>{f.forecast_model_version}</span>}
      {f.stale && onRefresh && (
        <Button size="sm" variant="ghost" className="h-6 px-2 text-[11px]" onClick={onRefresh}>
          <RefreshCw className="size-3" /> Refresh data
        </Button>
      )}
    </div>
  );
}

function ScenarioCard({ s }: { s: WhatIfScenario }) {
  return (
    <div className="my-2 rounded-md border border-warn/40 bg-warn/5 p-2 text-xs">
      <div className="mb-1 font-medium text-warn">Hypothetical scenario — not an official EventAlpha forecast</div>
      <div className="mb-1 text-subtle">
        {s.horizon_months}m to {day(s.valuation_date)} · from {money(s.reference_price)} ·{" "}
        {Object.entries(s.overrides)
          .map(([k, v]) => `${k.replace(/_/g, " ")} ${v > 0 ? "+" : ""}${pct(v, 1, false)}`)
          .join(", ")}
      </div>
      <table className="w-full">
        <thead>
          <tr className="text-subtle">
            <th className="text-left font-normal" />
            <th className="text-right font-normal">Official</th>
            <th className="text-right font-normal">Hypothetical</th>
          </tr>
        </thead>
        <tbody>
          {(["bear", "base", "bull"] as const).map((k) => (
            <tr key={k}>
              <td className="capitalize">{k}</td>
              <td className="num text-right">{money(s.official_targets[k] ?? null)}</td>
              <td className="num text-right font-medium">
                {money(s.hypothetical[k].target_price)} <span className="text-subtle">({pct(s.hypothetical[k].return)})</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {s.note && <p className="mt-1 text-subtle">{s.note}</p>}
    </div>
  );
}

function Sources({ items }: { items: ChatSource[] }) {
  if (!items.length) return null;
  return (
    <div className="mt-1 flex flex-wrap gap-1 text-[11px] text-subtle">
      {items.map((s) => (
        <span key={s.label} title={s.title ?? ""} className="max-w-full truncate">
          <Badge>{s.label}</Badge> {s.title}
          {s.published_at ? ` (${day(s.published_at)})` : ""}
        </span>
      ))}
    </div>
  );
}

function Bubble({ m, sources }: { m: ChatMessage; sources: ChatSource[] }) {
  if (m.role === "notice")
    return (
      <div className="rounded-md border border-accent/40 bg-accent/5 p-2 text-xs text-muted">
        <AlertTriangle className="mr-1 inline size-3.5 text-accent" />
        {m.content}
      </div>
    );
  const user = m.role === "user";
  return (
    <div className={cn("flex", user ? "justify-end" : "justify-start")}>
      <div className={cn("max-w-[92%] rounded-lg px-3 py-2", user ? "bg-accent/15 text-sm" : "border border-border bg-surface")}>
        {user ? (
          <p className="whitespace-pre-wrap text-sm">{m.content}</p>
        ) : (
          <>
            {m.status === "error" && <p className="text-xs text-down">{m.content}</p>}
            {m.status !== "error" && <Markdown text={m.content} sources={[...sources, ...m.citations]} />}
            {m.scenario && <ScenarioCard s={m.scenario} />}
            <Sources items={m.citations} />
            {(m.ai_model || m.cost_usd) && (
              <div className="mt-1 text-[10px] text-subtle">
                {m.ai_model}
                {m.cost_usd ? ` · $${m.cost_usd.toFixed(4)}` : ""}
                {m.forecast_model_version ? ` · forecast ${m.forecast_model_version}` : ""}
                {m.analysis_id ? ` · analysis #${m.analysis_id}` : ""}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

export function StockResearchChat({ ticker, onRefreshData }: { ticker: string; onRefreshData?: () => void }) {
  const qc = useQueryClient();
  const hist = useChatHistory(ticker);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [tool, setTool] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [freshness, setFreshness] = useState<Freshness | null>(null);
  const [sources, setSources] = useState<ChatSource[]>([]);
  const abortRef = useRef<AbortController | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (hist.data && !busy) {
      setMessages(hist.data.messages);
      setConversationId(hist.data.conversation_id);
      const last = [...hist.data.messages].reverse().find((m) => m.freshness);
      if (last?.freshness) setFreshness(last.freshness);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hist.data]);

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [messages, tool]);

  const send = useCallback(
    async (question: string) => {
      const q = question.trim();
      if (!q || busy) return;
      setError(null);
      setInput("");
      setBusy(true);
      const pendingId = `pending-${Date.now()}`;
      setMessages((ms) => [
        ...ms,
        { id: `u-${Date.now()}`, role: "user", content: q, citations: [], follow_ups: [] },
        { id: pendingId, role: "assistant", content: "", citations: [], follow_ups: [] },
      ]);
      const update = (fn: (m: ChatMessage) => ChatMessage) => setMessages((ms) => ms.map((m) => (m.id === pendingId ? fn(m) : m)));
      const ctl = new AbortController();
      abortRef.current = ctl;
      try {
        await streamChat(
          ticker,
          q,
          conversationId,
          (e: ChatEvent) => {
            if (e.type === "meta") {
              setConversationId(e.conversation_id);
              setFreshness(e.freshness);
              setSources(e.sources);
            } else if (e.type === "notice") {
              setMessages((ms) => {
                const at = ms.findIndex((m) => m.id === pendingId) - 1;
                const notice: ChatMessage = { id: `n-${Date.now()}`, role: "notice", content: e.message, citations: [], follow_ups: [] };
                return [...ms.slice(0, at), notice, ...ms.slice(at)];
              });
            } else if (e.type === "delta") update((m) => ({ ...m, content: m.content + e.text }));
            else if (e.type === "tool") setTool(e.status === "running" ? e.name : null);
            else if (e.type === "scenario") update((m) => ({ ...m, scenario: e.scenario }));
            else if (e.type === "error") setError(e.message);
            else if (e.type === "done")
              update((m) => ({
                ...m,
                id: e.message_id,
                content: e.content || m.content,
                status: e.status,
                citations: e.citations,
                follow_ups: e.follow_ups,
                cost_usd: e.usage.cost_usd,
              }));
          },
          ctl.signal,
        );
      } catch (err) {
        if ((err as Error).name !== "AbortError") setError((err as Error).message);
        update((m) => (m.content ? { ...m, status: "partial" } : { ...m, status: "error", content: "No answer." }));
      } finally {
        setBusy(false);
        setTool(null);
        abortRef.current = null;
        qc.invalidateQueries({ queryKey: ["chat", ticker] });
      }
    },
    [busy, conversationId, qc, ticker],
  );

  useEffect(() => {
    const h = (ev: Event) => {
      const d = (ev as CustomEvent<{ ticker: string; question: string }>).detail;
      if (d?.ticker === ticker) void send(d.question);
    };
    window.addEventListener(ASK_EVENT, h);
    return () => window.removeEventListener(ASK_EVENT, h);
  }, [send, ticker]);

  const clear = async () => {
    abortRef.current?.abort();
    await clearChat(ticker);
    setMessages([]);
    setConversationId(null);
    setError(null);
    qc.invalidateQueries({ queryKey: ["chat", ticker] });
  };

  const lastAssistant = [...messages].reverse().find((m) => m.role === "assistant" && m.status !== "error");
  const suggestions = !messages.length ? (hist.data?.suggested_prompts ?? []) : (lastAssistant?.follow_ups ?? []);
  const configured = hist.data?.configured ?? true;

  return (
    <Card className="flex h-full flex-col">
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2">
          <Bot className="size-4 text-accent" /> AI Analyst · {ticker}
          <span className="ml-auto flex items-center gap-1">
            {messages.length > 0 && (
              <Button size="sm" variant="ghost" onClick={clear} disabled={busy} title="Clear conversation">
                <Trash2 className="size-3.5" /> Clear
              </Button>
            )}
          </span>
        </CardTitle>
        <CardDescription>
          Explains EventAlpha's stored data and forecasts for {ticker} with cited sources. What-if answers are hypothetical, never official targets.
        </CardDescription>
        <FreshnessBar f={freshness} onRefresh={onRefreshData} />
        {hist.data?.forecast_changed && (
          <p className="text-[11px] text-accent">A newer analysis exists than the one this conversation used; your next question will switch to it.</p>
        )}
      </CardHeader>
      <CardContent className="flex min-h-0 flex-1 flex-col gap-2">
        <div className="flex max-h-[60vh] min-h-48 flex-col gap-2 overflow-y-auto pr-1" aria-live="polite">
          {hist.isLoading && <Loader2 className="mx-auto size-4 animate-spin text-subtle" />}
          {!configured && <p className="text-xs text-warn">AI chat needs OPENAI_API_KEY in the backend environment.</p>}
          {messages.map((m) => (
            <Bubble key={m.id} m={m} sources={sources} />
          ))}
          {busy && (
            <div className="flex items-center gap-1.5 text-xs text-subtle">
              {tool ? <Wrench className="size-3.5" /> : <Loader2 className="size-3.5 animate-spin" />}
              {tool ? TOOL_LABEL[tool] ?? tool : "Thinking…"}
            </div>
          )}
          {error && (
            <p role="alert" className="text-xs text-down">
              {error}
            </p>
          )}
          <div ref={endRef} />
        </div>
        {suggestions.length > 0 && !busy && (
          <div className="flex flex-wrap gap-1.5">
            {suggestions.slice(0, messages.length ? 4 : 12).map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => void send(s)}
                className="rounded-full border border-border px-2.5 py-1 text-left text-[11px] text-muted hover:border-accent hover:text-foreground"
              >
                {s}
              </button>
            ))}
          </div>
        )}
        <form
          className="flex items-end gap-2"
          onSubmit={(e: React.FormEvent) => {
            e.preventDefault();
            void send(input);
          }}
        >
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send(input);
              }
            }}
            maxLength={2000}
            rows={2}
            placeholder={`Ask about ${ticker}… e.g. "What if revenue growth slows by 5 points?"`}
            aria-label="Ask the AI analyst"
            className="min-h-10 flex-1 resize-none rounded-md border border-border bg-surface px-2.5 py-2 text-sm outline-none focus:border-accent"
          />
          {busy ? (
            <Button type="button" variant="secondary" onClick={() => abortRef.current?.abort()} title="Stop">
              <Square className="size-3.5" />
            </Button>
          ) : (
            <Button type="submit" disabled={!input.trim() || !configured} title="Send">
              <Send className="size-3.5" />
            </Button>
          )}
        </form>
        {hist.data?.budget && (
          <p className="text-[10px] text-subtle">
            Chat spend this month ${hist.data.budget.chat_spent_usd.toFixed(2)} of ${hist.data.budget.chat_budget_usd.toFixed(2)} · model {hist.data.model}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
