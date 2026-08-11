"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { Header, StatusPill, TemperatureBadge, TokenGate, useAuthedAgent } from "../../components";
import {
  type LeadDetail,
  type Message,
  type Transcript,
  api,
  formatBudget,
  relativeTime,
} from "@/lib/api";

export default function LeadPage() {
  const params = useParams<{ id: string }>();
  const leadId = params.id;
  const { agent, state, setState, refresh } = useAuthedAgent();

  const [lead, setLead] = useState<LeadDetail | null>(null);
  const [transcript, setTranscript] = useState<Transcript | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  const load = useCallback(async () => {
    setError(null);
    try {
      const [detail, thread] = await Promise.all([api.lead(leadId), api.transcript(leadId)]);
      setLead(detail);
      setTranscript(thread);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load this lead");
    }
  }, [leadId]);

  useEffect(() => {
    if (state === "ready") void load();
  }, [state, load]);

  async function act(fn: () => Promise<{ detail: string | null }>) {
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      const result = await fn();
      setNotice(result.detail ?? "Done");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "That did not work");
    } finally {
      setBusy(false);
    }
  }

  async function send(event: React.FormEvent) {
    event.preventDefault();
    const text = draft.trim();
    if (!text) return;
    setBusy(true);
    setError(null);
    try {
      await api.sendMessage(leadId, text);
      setDraft("");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not send");
    } finally {
      setBusy(false);
    }
  }

  if (state === "loading") return <main className="p-6 text-sm text-slate-500">Loading…</main>;
  if (state === "unauthed") return <TokenGate onReady={refresh} />;

  // A lead can be claimed before it has a conversation, so the lead's own status
  // is authoritative — conversation_status is null in that case.
  const underTakeover =
    lead?.status === "handed_off" || lead?.conversation_status === "human_takeover";
  const optedOut = lead?.consent_status === "opted_out";

  return (
    <>
      <Header agent={agent} />
      <main className="mx-auto max-w-6xl px-6 py-6">
        <Link href="/" className="text-xs text-slate-500 hover:underline">
          ← Back to pipeline
        </Link>

        {error && (
          <p data-testid="lead-error" className="mt-4 rounded-lg bg-red-50 px-4 py-3 text-sm text-red-700">
            {error}
          </p>
        )}
        {notice && (
          <p data-testid="lead-notice" className="mt-4 rounded-lg bg-emerald-50 px-4 py-3 text-sm text-emerald-800">
            {notice}
          </p>
        )}

        {lead && (
          <div className="mt-4 grid gap-6 lg:grid-cols-[1fr_22rem]">
            <section className="order-2 lg:order-1">
              <div className="rounded-xl bg-white ring-1 ring-slate-200">
                <div className="flex items-center justify-between border-b border-slate-100 px-4 py-3">
                  <h2 className="text-sm font-semibold">Conversation</h2>
                  <span className="text-xs text-slate-500">
                    {underTakeover ? "You are handling this" : "Assistant is replying"}
                  </span>
                </div>
                <div data-testid="transcript" className="max-h-[28rem] space-y-3 overflow-y-auto px-4 py-4">
                  {transcript?.messages.length === 0 && (
                    <p className="text-sm text-slate-500">No messages yet.</p>
                  )}
                  {transcript?.messages.map((message) => (
                    <Bubble key={message.id} message={message} />
                  ))}
                </div>

                <form onSubmit={send} className="border-t border-slate-100 p-3">
                  <div className="flex gap-2">
                    <input
                      data-testid="message-input"
                      value={draft}
                      onChange={(e) => setDraft(e.target.value)}
                      placeholder={optedOut ? "This lead has opted out" : "Send a message yourself…"}
                      disabled={busy || optedOut}
                      className="flex-1 rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-900 disabled:bg-slate-100"
                    />
                    <button
                      data-testid="message-send"
                      type="submit"
                      disabled={busy || optedOut || !draft.trim()}
                      className="rounded-lg bg-slate-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-40"
                    >
                      Send
                    </button>
                  </div>
                </form>
              </div>
            </section>

            <aside className="order-1 space-y-4 lg:order-2">
              <div className="rounded-xl bg-white p-4 ring-1 ring-slate-200">
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <h1 data-testid="lead-name" className="text-lg font-semibold">
                      {lead.name ?? "Unknown caller"}
                    </h1>
                    <a
                      href={`tel:${lead.phone}`}
                      data-testid="lead-phone"
                      className="text-sm text-slate-600 hover:underline"
                    >
                      {lead.phone}
                    </a>
                  </div>
                  <TemperatureBadge temperature={lead.temperature} score={lead.score} />
                </div>

                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <StatusPill status={lead.status} />
                  {optedOut && (
                    <span className="rounded bg-red-100 px-2 py-0.5 text-xs font-medium text-red-700">
                      Opted out
                    </span>
                  )}
                </div>

                <dl className="mt-4 space-y-1.5 text-sm">
                  <Row label="Budget" value={formatBudget(lead)} />
                  <Row
                    label="Looking in"
                    value={lead.preferred_locations.join(", ") || "Not shared"}
                  />
                  <Row
                    label="Size"
                    value={[lead.bhk ? `${lead.bhk} BHK` : null, lead.property_type]
                      .filter(Boolean)
                      .join(" · ") || "Not shared"}
                  />
                  <Row
                    label="Timeline"
                    value={lead.timeline_months ? `${lead.timeline_months} months` : "Not shared"}
                  />
                  <Row
                    label="Home loan"
                    value={
                      lead.loan_preapproved === null
                        ? "Not shared"
                        : lead.loan_preapproved
                          ? "Pre-approved"
                          : "Not yet"
                    }
                  />
                  <Row label="Last heard" value={relativeTime(lead.last_inbound_at)} />
                </dl>

                <div className="mt-4 flex gap-2">
                  {underTakeover ? (
                    <button
                      data-testid="release-button"
                      onClick={() => act(() => api.release(leadId))}
                      disabled={busy}
                      className="flex-1 rounded-lg border border-slate-300 px-3 py-2 text-sm font-medium hover:bg-slate-50 disabled:opacity-40"
                    >
                      Hand back to assistant
                    </button>
                  ) : (
                    <button
                      data-testid="takeover-button"
                      onClick={() => act(() => api.takeover(leadId, "Taken over from the dashboard"))}
                      disabled={busy || optedOut}
                      className="flex-1 rounded-lg bg-slate-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-40"
                    >
                      Take over
                    </button>
                  )}
                </div>
              </div>

              <div className="rounded-xl bg-white p-4 ring-1 ring-slate-200">
                <h2 className="text-sm font-semibold">
                  Score {lead.score}/100
                  <span className="ml-1 font-normal text-slate-500">({lead.temperature})</span>
                </h2>
                <ul data-testid="score-reasons" className="mt-2 space-y-1.5">
                  {lead.score_reasons.length === 0 && (
                    <li className="text-xs text-slate-500">Not scored yet.</li>
                  )}
                  {lead.score_reasons.map((reason) => (
                    <li key={reason.factor} className="flex gap-2 text-xs">
                      <span className="w-8 shrink-0 tabular-nums font-medium text-slate-900">
                        +{reason.points}
                      </span>
                      <span className="text-slate-600">{reason.detail}</span>
                    </li>
                  ))}
                </ul>
              </div>

              {lead.appointments.length > 0 && (
                <div className="rounded-xl bg-white p-4 ring-1 ring-slate-200">
                  <h2 className="text-sm font-semibold">Appointments</h2>
                  <ul data-testid="appointments" className="mt-2 space-y-2 text-xs">
                    {lead.appointments.map((appointment) => (
                      <li key={appointment.id}>
                        <div className="font-medium">
                          {appointment.appointment_type === "site_visit" ? "Site visit" : "Call"} ·{" "}
                          {new Date(appointment.starts_at).toLocaleString("en-IN", {
                            dateStyle: "medium",
                            timeStyle: "short",
                          })}
                        </div>
                        <div className="text-slate-500">
                          {appointment.status}
                          {appointment.google_event_id ? " · on calendar" : " · not on calendar"}
                        </div>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {lead.follow_ups.length > 0 && (
                <div className="rounded-xl bg-white p-4 ring-1 ring-slate-200">
                  <h2 className="text-sm font-semibold">Follow-ups</h2>
                  <ul data-testid="follow-ups" className="mt-2 space-y-1.5 text-xs">
                    {lead.follow_ups.map((task) => (
                      <li key={task.id} className="flex justify-between gap-2">
                        <span className="text-slate-600">
                          #{task.attempt_number} ·{" "}
                          {new Date(task.scheduled_for).toLocaleDateString("en-IN", {
                            dateStyle: "medium",
                          })}
                        </span>
                        <span className="text-slate-500">{task.status}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </aside>
          </div>
        )}
      </main>
    </>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-3">
      <dt className="shrink-0 text-slate-500">{label}</dt>
      <dd className="text-right">{value}</dd>
    </div>
  );
}

function Bubble({ message }: { message: Message }) {
  const inbound = message.direction === "inbound";
  const fromHuman = message.role === "human_agent";
  return (
    <div className={`flex ${inbound ? "justify-start" : "justify-end"}`}>
      <div
        className={`max-w-[80%] rounded-2xl px-3 py-2 text-sm ${
          inbound
            ? "bg-slate-100 text-slate-900"
            : fromHuman
              ? "bg-emerald-600 text-white"
              : "bg-slate-900 text-white"
        }`}
      >
        <p className="whitespace-pre-wrap">{message.content}</p>
        <p className={`mt-1 text-[10px] ${inbound ? "text-slate-500" : "text-white/60"}`}>
          {fromHuman ? "you" : inbound ? "lead" : "assistant"} · {relativeTime(message.created_at)}
        </p>
      </div>
    </div>
  );
}
