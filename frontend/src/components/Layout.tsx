import { useQueryClient } from "@tanstack/react-query";
import { Activity, BarChart3, Compass, FlaskConical, History, LayoutDashboard, Menu, Radar, Search, Settings, Star, X } from "lucide-react";
import { useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";

import { Input } from "@/components/ui/input";
import { api, type DataMode, useHealth } from "@/lib/api";
import { cn } from "@/lib/utils";

const NAV = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard, end: true },
  { to: "/discovery", label: "Stock Discovery", icon: Compass },
  { to: "/analyze", label: "Analyze Ticker", icon: Search },
  { to: "/events", label: "Event Radar", icon: Radar },
  { to: "/watchlist", label: "Watchlist", icon: Star },
  { to: "/forecasts", label: "Forecast History", icon: History },
  { to: "/settings", label: "Settings", icon: Settings },
];

export function ModeToggle() {
  const { data } = useHealth();
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  const mode = data?.data_mode;
  const set = async (m: DataMode) => {
    if (m === mode) return;
    setBusy(true);
    try {
      await api("/api/settings", { method: "PUT", body: JSON.stringify({ data_mode: m }) });
      await qc.invalidateQueries();
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex items-center gap-1 rounded-lg border border-border bg-surface p-0.5 text-xs" role="radiogroup" aria-label="Data mode">
      {(["demo", "live"] as const).map((m) => (
        <button
          key={m}
          role="radio"
          aria-checked={mode === m}
          disabled={busy || !data}
          onClick={() => set(m)}
          className={cn(
            "flex items-center gap-1 rounded-md px-2.5 py-1 font-medium transition-colors",
            mode === m ? (m === "demo" ? "bg-demo/20 text-demo" : "bg-up/15 text-up") : "text-muted hover:text-foreground",
          )}
        >
          {m === "demo" ? <FlaskConical className="size-3" /> : <Activity className="size-3" />}
          {m === "demo" ? "Demo" : "Live"}
        </button>
      ))}
    </div>
  );
}

function QuickSearch() {
  const [q, setQ] = useState("");
  const nav = useNavigate();
  return (
    <form
      className="hidden w-56 md:block"
      onSubmit={(e) => {
        e.preventDefault();
        if (q.trim()) nav(`/analyze?ticker=${encodeURIComponent(q.trim().toUpperCase())}`);
        setQ("");
      }}
    >
      <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ticker (e.g. NVDA)" aria-label="Quick ticker search" className="h-8" />
    </form>
  );
}

export function Layout() {
  const { data: health, isError } = useHealth();
  const [open, setOpen] = useState(false);
  return (
    <div className="flex min-h-full">
      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-40 w-60 shrink-0 border-r border-border bg-surface transition-transform md:static md:translate-x-0",
          open ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <div className="flex h-14 items-center gap-2 border-b border-border px-4">
          <div className="flex size-8 items-center justify-center rounded-lg bg-accent/15">
            <BarChart3 className="size-4 text-up" />
          </div>
          <div>
            <div className="text-sm font-semibold tracking-tight">EventAlpha</div>
            <div className="text-[10px] uppercase tracking-widest text-subtle">AI stock research</div>
          </div>
          <button className="ml-auto md:hidden" onClick={() => setOpen(false)} aria-label="Close menu">
            <X className="size-4" />
          </button>
        </div>
        <nav className="flex flex-col gap-0.5 p-2">
          {NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.end}
              onClick={() => setOpen(false)}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2.5 rounded-md px-3 py-2 text-sm transition-colors",
                  isActive ? "bg-surface-3 text-foreground" : "text-muted hover:bg-surface-2 hover:text-foreground",
                )
              }
            >
              <n.icon className="size-4" />
              {n.label}
            </NavLink>
          ))}
        </nav>
        <div className="absolute inset-x-0 bottom-0 border-t border-border p-3 text-[11px] text-subtle">
          <div className="flex items-center gap-1.5">
            <span className={cn("size-2 rounded-full", isError ? "bg-down" : health?.status === "ok" ? "bg-up" : "bg-warn")} />
            {isError ? "API unreachable" : health ? `API ${health.status} · DB ${health.database}` : "Connecting…"}
          </div>
          {health && (
            <div className="mt-1">
              Massive {health.providers.massive_configured ? "✓" : "—"} · OpenAI {health.providers.openai_configured ? "✓" : "—"}
            </div>
          )}
        </div>
      </aside>
      {open && <div className="fixed inset-0 z-30 bg-black/50 md:hidden" onClick={() => setOpen(false)} />}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex h-14 items-center gap-3 border-b border-border bg-background/90 px-4 backdrop-blur">
          <button className="md:hidden" onClick={() => setOpen(true)} aria-label="Open menu">
            <Menu className="size-5" />
          </button>
          <QuickSearch />
          <div className="ml-auto flex items-center gap-3">
            <span className="hidden text-xs text-subtle sm:inline">Data mode</span>
            <ModeToggle />
          </div>
        </header>
        {health?.data_mode === "demo" && (
          <div className="flex items-center gap-2 border-b border-demo/30 bg-demo/10 px-4 py-1.5 text-xs text-demo">
            <FlaskConical className="size-3.5" />
            Demo mode: all prices, financials and news are <strong>synthetic</strong> sample data, clearly separated from live data.
          </div>
        )}
        {health?.data_mode === "live" && !health.providers.massive_configured && (
          <div className="border-b border-warn/30 bg-warn/10 px-4 py-1.5 text-xs text-warn">
            Live mode without MASSIVE_API_KEY: SEC financials work, but no market prices are available, so forecasts can't be computed.
          </div>
        )}
        <main className="mx-auto w-full max-w-7xl flex-1 p-4 md:p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
