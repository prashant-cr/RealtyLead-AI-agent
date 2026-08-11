"use client";

import { useCallback, useEffect, useState } from "react";
import { Header, LeadRow, TokenGate, useAuthedAgent } from "./components";
import { type LeadSummary, type PipelineStats, api } from "@/lib/api";

const FILTERS: { key: string; label: string; statuses: string[] }[] = [
  { key: "all", label: "All", statuses: [] },
  { key: "attention", label: "Needs you", statuses: ["handed_off"] },
  { key: "active", label: "In conversation", statuses: ["new", "engaged", "qualified"] },
  { key: "booked", label: "Booked", statuses: ["booked"] },
  { key: "closed", label: "Cold / opted out", statuses: ["cold", "opted_out"] },
];

export default function PipelinePage() {
  const { agent, state, setState, refresh } = useAuthedAgent();
  const [leads, setLeads] = useState<LeadSummary[]>([]);
  const [stats, setStats] = useState<PipelineStats | null>(null);
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const statuses = FILTERS.find((f) => f.key === filter)?.statuses ?? [];
      const [page, pipelineStats] = await Promise.all([
        api.leads({ status: statuses.length ? statuses : undefined, search: search || undefined }),
        api.stats(),
      ]);
      setLeads(page.items);
      setStats(pipelineStats);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load the pipeline");
    } finally {
      setLoading(false);
    }
  }, [filter, search]);

  useEffect(() => {
    if (state === "ready") void load();
  }, [state, load]);

  if (state === "loading") {
    return <main className="p-6 text-sm text-slate-500">Loading…</main>;
  }
  if (state === "unauthed") {
    return <TokenGate onReady={refresh} />;
  }

  return (
    <>
      <Header agent={agent} />
      <main className="mx-auto max-w-6xl px-6 py-6">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatCard label="Leads" value={stats?.total ?? 0} testId="stat-total" />
          <StatCard
            label="Needs you"
            value={stats?.needs_attention ?? 0}
            testId="stat-attention"
            highlight={(stats?.needs_attention ?? 0) > 0}
          />
          <StatCard label="Hot" value={stats?.by_temperature.hot ?? 0} testId="stat-hot" />
          <StatCard
            label="Upcoming visits"
            value={stats?.booked_upcoming ?? 0}
            testId="stat-booked"
          />
        </div>

        <div className="mt-6 flex flex-wrap items-center gap-2">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              data-testid={`filter-${f.key}`}
              onClick={() => setFilter(f.key)}
              className={`rounded-full px-3 py-1 text-xs font-medium ${
                filter === f.key
                  ? "bg-slate-900 text-white"
                  : "bg-white text-slate-600 ring-1 ring-slate-200 hover:bg-slate-50"
              }`}
            >
              {f.label}
            </button>
          ))}
          <input
            data-testid="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search name or phone"
            className="ml-auto w-56 rounded-lg border border-slate-300 px-3 py-1.5 text-xs outline-none focus:border-slate-900"
          />
        </div>

        <section className="mt-4 overflow-hidden rounded-xl bg-white ring-1 ring-slate-200">
          {error && (
            <p data-testid="pipeline-error" className="px-4 py-6 text-sm text-red-600">
              {error}
            </p>
          )}
          {!error && loading && leads.length === 0 && (
            <p className="px-4 py-6 text-sm text-slate-500">Loading leads…</p>
          )}
          {!error && !loading && leads.length === 0 && (
            <p data-testid="empty-state" className="px-4 py-10 text-center text-sm text-slate-500">
              No leads here yet. New enquiries appear automatically.
            </p>
          )}
          {leads.map((lead) => (
            <LeadRow key={lead.id} lead={lead} />
          ))}
        </section>
      </main>
    </>
  );
}

function StatCard({
  label,
  value,
  testId,
  highlight,
}: {
  label: string;
  value: number;
  testId: string;
  highlight?: boolean;
}) {
  return (
    <div
      className={`rounded-xl bg-white px-4 py-3 ring-1 ${
        highlight ? "ring-amber-300" : "ring-slate-200"
      }`}
    >
      <div className="text-xs text-slate-500">{label}</div>
      <div data-testid={testId} className="mt-0.5 text-2xl font-semibold tabular-nums">
        {value}
      </div>
    </div>
  );
}
