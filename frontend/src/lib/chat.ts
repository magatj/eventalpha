import { useQuery } from "@tanstack/react-query";

import { api, ApiError } from "@/lib/api";

export interface ChatSource {
  label: string;
  type: string;
  title: string | null;
  url: string | null;
  published_at: string | null;
}

export interface Freshness {
  has_analysis?: boolean;
  analysis_id?: number;
  analysis_created_at?: string;
  analysis_age_days?: number;
  price_date?: string | null;
  price_stale_sessions?: number | null;
  latest_filing?: string | null;
  forecast_model_version?: string;
  stale?: boolean;
}

export interface WhatIfScenario {
  label: string;
  horizon_months: number;
  valuation_date: string;
  reference_price: number;
  overrides: Record<string, number>;
  official_targets: Record<string, number | null>;
  recomputed_baseline: Record<"bear" | "base" | "bull", { target_price: number | null; return: number | null; eps: number | null; multiple: number | null }>;
  hypothetical: Record<"bear" | "base" | "bull", { target_price: number | null; return: number | null; eps: number | null; multiple: number | null; method_weights?: Record<string, number> }>;
  note?: string | null;
}

export interface ChatMessage {
  id: number | string;
  role: "user" | "assistant" | "notice";
  content: string;
  created_at?: string | null;
  status?: string;
  analysis_id?: number | null;
  forecast_model_version?: string | null;
  ai_model?: string | null;
  cost_usd?: number | null;
  citations: ChatSource[];
  scenario?: WhatIfScenario | null;
  follow_ups: string[];
  tools?: { name: string; status: string }[];
  freshness?: Freshness | null;
}

export interface ChatBudget {
  chat_spent_usd: number;
  chat_budget_usd: number;
  chat_remaining_usd: number;
  ai_spent_usd: number;
  ai_budget_usd: number;
}

export interface ChatHistory {
  ticker: string;
  configured: boolean;
  model: string;
  conversation_id: string | null;
  conversation_analysis_id: number | null;
  latest_analysis_id: number | null;
  forecast_changed: boolean;
  messages: ChatMessage[];
  suggested_prompts: string[];
  budget: ChatBudget;
}

export type ChatEvent =
  | { type: "meta"; conversation_id: string; analysis_id: number | null; freshness: Freshness; valuation_status: string | null; sources: ChatSource[] }
  | { type: "notice"; message: string }
  | { type: "delta"; text: string }
  | { type: "tool"; name: string; status: string }
  | { type: "scenario"; scenario: WhatIfScenario }
  | { type: "error"; code: string; message: string }
  | {
      type: "done";
      message_id: number;
      status: string;
      content: string;
      citations: ChatSource[];
      follow_ups: string[];
      usage: { input_tokens: number; output_tokens: number; cost_usd: number };
      budget: ChatBudget;
    };

export const useChatHistory = (t: string) =>
  useQuery({ queryKey: ["chat", t], queryFn: () => api<ChatHistory>(`/api/stocks/${t}/chat/history`), retry: false });

export const clearChat = (t: string) => api<{ cleared: number }>(`/api/stocks/${t}/chat/history`, { method: "DELETE" });

/** Parse a server-sent-events text buffer; returns complete events and the unconsumed remainder. */
export function parseSSE(buffer: string): { events: ChatEvent[]; rest: string } {
  const events: ChatEvent[] = [];
  const blocks = buffer.split("\n\n");
  const rest = blocks.pop() ?? "";
  for (const block of blocks) {
    const data = block
      .split("\n")
      .filter((l) => l.startsWith("data: "))
      .map((l) => l.slice(6))
      .join("\n");
    if (!data) continue;
    try {
      events.push(JSON.parse(data) as ChatEvent);
    } catch {
      /* ignore malformed frame */
    }
  }
  return { events, rest };
}

/** POST a question and stream events (EventSource cannot POST, so the response body is read directly). */
export async function streamChat(
  ticker: string,
  message: string,
  conversationId: string | null,
  onEvent: (e: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`/api/stocks/${ticker}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ message, conversation_id: conversationId }),
    signal,
  });
  if (!res.ok || !res.body) {
    let code = "error";
    let msg = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      code = body?.error?.code ?? code;
      msg = body?.error?.message ?? msg;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, code, msg);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const { events, rest } = parseSSE(buf);
    buf = rest;
    events.forEach(onEvent);
  }
  if (buf.trim()) parseSSE(buf + "\n\n").events.forEach(onEvent);
}

export const ASK_EVENT = "eventalpha:ask-analyst";

/** Ask the analyst panel on the current stock page a question (used by the forecast explainer). */
export function askAnalyst(ticker: string, question: string) {
  window.dispatchEvent(new CustomEvent(ASK_EVENT, { detail: { ticker, question } }));
}

/** Only absolute http(s) links are rendered (no javascript:, data: or relative URLs). */
export function safeHref(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.toString() : null;
  } catch {
    return null;
  }
}
