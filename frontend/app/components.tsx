"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  type AgentInfo,
  type LeadSummary,
  type Temperature,
  api,
  clearToken,
  formatBudget,
  getToken,
  relativeTime,
  setToken,
} from "@/lib/api";

const TEMPERATURE_STYLES: Record<Temperature, string> = {
  hot: "bg-hot-bg text-hot-fg ring-hot-ring",
  warm: "bg-warm-bg text-warm-fg ring-warm-ring",
  cold: "bg-cold-bg text-cold-fg ring-cold-ring",
};

const STATUS_LABELS: Record<string, string> = {
  new: "New",
  engaged: "In conversation",
  qualified: "Qualified",
  booked: "Booked",
  cold: "Cold",
  handed_off: "Needs you",
  opted_out: "Opted out",
};

export function TemperatureBadge({ temperature, score }: { temperature: Temperature; score: number }) {
  return (
    <span
      data-testid="temperature-badge"
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset ${TEMPERATURE_STYLES[temperature]}`}
    >
      {temperature}
      <span className="tabular-nums opacity-70">{score}</span>
    </span>
  );
}

export function StatusPill({ status }: { status: string }) {
  const needsAttention = status === "handed_off";
  return (
    <span
      className={`rounded px-2 py-0.5 text-xs font-medium ${
        needsAttention ? "bg-amber-100 text-amber-800" : "bg-slate-100 text-slate-600"
      }`}
    >
      {STATUS_LABELS[status] ?? status}
    </span>
  );
}

/** Token entry. Stands in for real accounts until M7. */
export function TokenGate({ onReady }: { onReady: () => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setToken(value.trim());
    try {
      await api.me();
      onReady();
    } catch (err) {
      clearToken();
      setError(err instanceof ApiError ? err.message : "Could not reach the API");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-6">
      <h1 className="text-xl font-semibold">RealtyLead</h1>
      <p className="mt-2 text-sm text-slate-600">
        Paste your dashboard token to continue. Generate one with{" "}
        <code className="rounded bg-slate-200 px-1 py-0.5 text-xs">make token</code>.
      </p>
      <form onSubmit={submit} className="mt-6 space-y-3">
        <input
          data-testid="token-input"
          type="password"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="rl_..."
          autoComplete="off"
          className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-900"
        />
        {error && (
          <p data-testid="token-error" className="text-sm text-red-600">
            {error}
          </p>
        )}
        <button
          data-testid="token-submit"
          type="submit"
          disabled={!value.trim() || busy}
          className="w-full rounded-lg bg-slate-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-40"
        >
          {busy ? "Checking…" : "Continue"}
        </button>
      </form>
    </main>
  );
}

export function Header({ agent }: { agent: AgentInfo | null }) {
  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-4">
        <div>
          <Link href="/" className="text-base font-semibold">
            RealtyLead
          </Link>
          {agent && (
            <p className="text-xs text-slate-500">
              {agent.name}
              {agent.brokerage_name ? ` · ${agent.brokerage_name}` : ""}
            </p>
          )}
        </div>
        <div className="flex items-center gap-3 text-xs text-slate-500">
          {agent && (
            <>
              <ConnectionDot label="WhatsApp" connected={agent.whatsapp_connected === "yes"} />
              <ConnectionDot label="Calendar" connected={agent.calendar_connected === "yes"} />
            </>
          )}
          <button
            data-testid="sign-out"
            onClick={() => {
              clearToken();
              window.location.reload();
            }}
            className="rounded border border-slate-300 px-2 py-1 hover:bg-slate-50"
          >
            Sign out
          </button>
        </div>
      </div>
    </header>
  );
}

function ConnectionDot({ label, connected }: { label: string; connected: boolean }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className={`h-1.5 w-1.5 rounded-full ${connected ? "bg-emerald-500" : "bg-slate-300"}`}
        aria-hidden
      />
      {label} {connected ? "connected" : "not connected"}
    </span>
  );
}

export function LeadRow({ lead }: { lead: LeadSummary }) {
  return (
    <Link
      href={`/leads/${lead.id}`}
      data-testid="lead-row"
      data-lead-id={lead.id}
      className="flex items-center gap-4 border-b border-slate-100 px-4 py-3 last:border-0 hover:bg-slate-50"
    >
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm font-medium">{lead.name ?? "Unknown caller"}</span>
          <StatusPill status={lead.status} />
        </div>
        <p className="mt-0.5 truncate text-xs text-slate-500">
          {lead.phone} · {formatBudget(lead)}
          {lead.preferred_locations.length > 0 && ` · ${lead.preferred_locations.join(", ")}`}
        </p>
      </div>
      <div className="hidden shrink-0 text-right text-xs text-slate-500 sm:block">
        <div>heard {relativeTime(lead.last_inbound_at)}</div>
        {lead.follow_up_count > 0 && <div>{lead.follow_up_count} nudge(s) sent</div>}
      </div>
      <TemperatureBadge temperature={lead.temperature} score={lead.score} />
    </Link>
  );
}

/** Small hook so pages share the same "do we have a working token?" logic. */
export function useAuthedAgent() {
  const [agent, setAgent] = useState<AgentInfo | null>(null);
  const [state, setState] = useState<"loading" | "unauthed" | "ready">("loading");

  const refresh = useCallback(() => {
    if (!getToken()) {
      setState("unauthed");
      return;
    }
    api
      .me()
      .then((info) => {
        setAgent(info);
        setState("ready");
      })
      .catch(() => {
        clearToken();
        setState("unauthed");
      });
  }, []);

  useEffect(refresh, [refresh]);

  // `refresh`, not just setState("ready"): signing in through the gate must also
  // load the agent, or the header sits empty until the next navigation.
  return { agent, state, setState, refresh };
}
