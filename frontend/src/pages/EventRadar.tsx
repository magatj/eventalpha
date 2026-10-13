import { useQueryClient } from "@tanstack/react-query";
import { Info, RefreshCw } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import { DemoBadge, EmptyState, ErrorState, LoadingRows, PageHeader } from "@/components/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/input";
import { api, useEvents, useHealth } from "@/lib/api";
import { label } from "@/lib/format";
import { EventCard } from "@/pages/StockDetail";

export default function EventRadarPage() {
  const [type, setType] = useState("");
  const [impact, setImpact] = useState("");
  const [status, setStatus] = useState("");
  const [when, setWhen] = useState("");
  const [days, setDays] = useState("7");
  const qs = new URLSearchParams({
    days,
    ...(type && { event_type: type }),
    ...(impact && { impact }),
    ...(status && { status }),
    ...(when && { upcoming: when }),
  }).toString();
  const { data, isLoading, error, refetch } = useEvents(qs);
  const { data: health } = useHealth();
  const qc = useQueryClient();
  const [msg, setMsg] = useState<string | null>(null);
  const ingest = async () => {
    await api("/api/events/ingest", { method: "POST" });
    setMsg("News ingestion queued — results appear when the worker finishes.");
    setTimeout(() => qc.invalidateQueries({ queryKey: ["events"] }), 8000);
  };
  return (
    <div>
      <PageHeader
        title="Event Radar"
        description="Company announcements, SEC filings, earnings, economic releases and regulatory news — deduplicated and linked to affected companies."
        actions={
          <>
            <DemoBadge mode={health?.data_mode} />
            <Button variant="secondary" onClick={ingest}>
              <RefreshCw /> Ingest now
            </Button>
          </>
        }
      />
      {msg && <p className="mb-3 text-xs text-accent">{msg}</p>}
      <div className="mb-4 flex flex-wrap gap-2">
        <Select value={when} onChange={(e) => setWhen(e.target.value)} aria-label="Timing">
          <option value="">Recent + upcoming</option>
          <option value="false">Recent only</option>
          <option value="true">Upcoming only</option>
        </Select>
        <Select value={type} onChange={(e) => setType(e.target.value)} aria-label="Event type">
          <option value="">All types</option>
          {data?.event_types.map((t) => (
            <option key={t} value={t}>
              {label(t)}
            </option>
          ))}
        </Select>
        <Select value={impact} onChange={(e) => setImpact(e.target.value)} aria-label="Impact">
          <option value="">Any impact</option>
          <option value="positive">Positive</option>
          <option value="negative">Negative</option>
          <option value="uncertain">Uncertain</option>
        </Select>
        <Select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
          <option value="">Any status</option>
          <option value="confirmed">Confirmed</option>
          <option value="unconfirmed">Unconfirmed reports</option>
          <option value="scheduled">Scheduled</option>
          <option value="estimated">Estimated dates</option>
        </Select>
        <Select value={days} onChange={(e) => setDays(e.target.value)} aria-label="Lookback">
          <option value="1">Last 24h</option>
          <option value="7">Last 7 days</option>
          <option value="30">Last 30 days</option>
        </Select>
      </div>
      {isLoading ? (
        <LoadingRows rows={8} />
      ) : error ? (
        <ErrorState error={error} onRetry={refetch} />
      ) : data?.events.length ? (
        <div className="flex flex-col gap-3">
          {data.events.map((e) => (
            <div key={e.id}>
              <EventCard e={e} />
              {e.companies && e.companies.length > 0 && (
                <div className="mt-1 flex flex-wrap gap-1.5 pl-3 text-[11px] text-subtle">
                  Affected:
                  {e.companies.map((c) => (
                    <Link key={c.ticker} to={`/stocks/${c.ticker}`}>
                      <Badge variant="accent" title={`${c.name ?? ""} — linked by ${label(c.method)}`}>
                        {c.ticker} · {Math.round(c.confidence * 100)}%
                      </Badge>
                    </Link>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      ) : (
        <EmptyState title="No events match">Try a longer lookback or run ingestion.</EmptyState>
      )}
      {data?.notes && (
        <ul className="mt-4 flex flex-col gap-1 text-[11px] text-subtle">
          {data.notes.map((n) => (
            <li key={n} className="flex gap-1.5">
              <Info className="size-3" /> {n}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
