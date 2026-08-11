"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { Header, useAuthedAgent } from "../components";
import {
  API_URL,
  type ImportResult,
  type OnboardingStatus,
  type Settings,
  api,
} from "@/lib/api";

const DAYS: { key: string; label: string }[] = [
  { key: "mon", label: "Mon" },
  { key: "tue", label: "Tue" },
  { key: "wed", label: "Wed" },
  { key: "thu", label: "Thu" },
  { key: "fri", label: "Fri" },
  { key: "sat", label: "Sat" },
  { key: "sun", label: "Sun" },
];

export default function OnboardingPage() {
  const { agent, state } = useAuthedAgent();
  const [settings, setSettings] = useState<Settings | null>(null);
  const [status, setStatus] = useState<OnboardingStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [current, checklist] = await Promise.all([api.settings(), api.onboarding()]);
      setSettings(current);
      setStatus(checklist);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load your settings");
    }
  }, []);

  useEffect(() => {
    if (state === "ready") void load();
  }, [state, load]);

  async function save(patch: Partial<Settings>, label: string) {
    setError(null);
    try {
      const updated = await api.updateSettings(patch);
      setSettings(updated);
      setSaved(label);
      setStatus(await api.onboarding());
      window.setTimeout(() => setSaved(null), 2500);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save");
    }
  }

  if (state === "loading") return <main className="p-6 text-sm text-slate-500">Loading…</main>;
  if (state === "unauthed") {
    return (
      <main className="mx-auto max-w-md p-6 text-sm">
        <p>
          Please{" "}
          <Link href="/signin" className="font-medium underline">
            sign in
          </Link>{" "}
          to continue.
        </p>
      </main>
    );
  }

  return (
    <>
      <Header agent={agent} />
      <main className="mx-auto max-w-3xl px-6 py-6">
        <h1 className="text-lg font-semibold">Set up your assistant</h1>
        <p className="mt-1 text-sm text-slate-600">
          Four things and it can start answering enquiries for you.
        </p>

        {error && (
          <p data-testid="setup-error" className="mt-4 rounded-lg bg-red-50 px-4 py-3 text-sm text-red-700">
            {error}
          </p>
        )}
        {saved && (
          <p data-testid="setup-saved" className="mt-4 rounded-lg bg-emerald-50 px-4 py-3 text-sm text-emerald-800">
            {saved} saved.
          </p>
        )}

        {status && (
          <ol data-testid="checklist" className="mt-5 space-y-2">
            {status.steps.map((step) => (
              <li
                key={step.key}
                data-testid={`step-${step.key}`}
                data-done={step.done}
                className="flex items-center gap-3 rounded-lg bg-white px-4 py-2.5 text-sm ring-1 ring-slate-200"
              >
                <span
                  className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs ${
                    step.done ? "bg-emerald-500 text-white" : "bg-slate-200 text-slate-500"
                  }`}
                  aria-hidden
                >
                  {step.done ? "✓" : ""}
                </span>
                <span className={step.done ? "text-slate-500 line-through" : "font-medium"}>
                  {step.label}
                </span>
                {step.detail && <span className="ml-auto text-xs text-slate-400">{step.detail}</span>}
              </li>
            ))}
          </ol>
        )}

        {status?.complete && (
          <div
            data-testid="setup-complete"
            className="mt-5 rounded-xl bg-emerald-50 px-4 py-4 text-sm text-emerald-900 ring-1 ring-emerald-200"
          >
            <p className="font-medium">You are ready to go.</p>
            <p className="mt-1">
              New enquiries will be answered automatically.{" "}
              <Link href="/" className="font-medium underline">
                Open your pipeline
              </Link>
            </p>
          </div>
        )}

        {settings && (
          <div className="mt-8 space-y-6">
            <WorkingHours settings={settings} onSave={save} />
            <Listings onImported={load} />
            <WhatsAppSetup settings={settings} onSave={save} />
            <Tone settings={settings} onSave={save} />
          </div>
        )}
      </main>
    </>
  );
}

function Card({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-xl bg-white p-5 ring-1 ring-slate-200">
      <h2 className="text-sm font-semibold">{title}</h2>
      <p className="mt-0.5 text-xs text-slate-500">{description}</p>
      <div className="mt-4">{children}</div>
    </section>
  );
}

function WorkingHours({
  settings,
  onSave,
}: {
  settings: Settings;
  onSave: (patch: Partial<Settings>, label: string) => Promise<void>;
}) {
  const [hours, setHours] = useState<Record<string, string[]>>(settings.working_hours);

  function setDay(day: string, open: boolean) {
    setHours((prev) => ({ ...prev, [day]: open ? (prev[day]?.length ? prev[day] : ["09:30", "19:00"]) : [] }));
  }

  function setTime(day: string, index: 0 | 1, value: string) {
    setHours((prev) => {
      const current = prev[day]?.length ? [...prev[day]] : ["09:30", "19:00"];
      current[index] = value;
      return { ...prev, [day]: current };
    });
  }

  return (
    <Card
      title="Working hours"
      description="Site visits and calls are only offered inside these hours, in your timezone."
    >
      <div className="space-y-2">
        {DAYS.map(({ key, label }) => {
          const open = (hours[key] ?? []).length === 2;
          return (
            <div key={key} className="flex items-center gap-3 text-sm">
              <label className="flex w-24 items-center gap-2">
                <input
                  data-testid={`day-${key}`}
                  type="checkbox"
                  checked={open}
                  onChange={(e) => setDay(key, e.target.checked)}
                  className="h-4 w-4 rounded border-slate-300"
                />
                {label}
              </label>
              {open ? (
                <>
                  <input
                    data-testid={`open-${key}`}
                    type="time"
                    value={hours[key][0]}
                    onChange={(e) => setTime(key, 0, e.target.value)}
                    className="rounded border border-slate-300 px-2 py-1 text-xs"
                  />
                  <span className="text-xs text-slate-400">to</span>
                  <input
                    data-testid={`close-${key}`}
                    type="time"
                    value={hours[key][1]}
                    onChange={(e) => setTime(key, 1, e.target.value)}
                    className="rounded border border-slate-300 px-2 py-1 text-xs"
                  />
                </>
              ) : (
                <span className="text-xs text-slate-400">Closed</span>
              )}
            </div>
          );
        })}
      </div>
      <button
        data-testid="save-hours"
        onClick={() => onSave({ working_hours: hours }, "Working hours")}
        className="mt-4 rounded-lg bg-slate-900 px-3 py-2 text-sm font-medium text-white"
      >
        Save hours
      </button>
    </Card>
  );
}

function Listings({ onImported }: { onImported: () => Promise<void> }) {
  const fileInput = useRef<HTMLInputElement>(null);
  const [result, setResult] = useState<ImportResult | null>(null);
  const [replace, setReplace] = useState(false);
  const [busy, setBusy] = useState(false);

  async function upload(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    setBusy(true);
    setResult(null);
    try {
      const outcome = await api.importListings(file, replace);
      setResult(outcome);
      if (outcome.imported > 0) await onImported();
    } catch (err) {
      setResult({
        imported: 0,
        replaced: 0,
        skipped_blank: 0,
        errors: [{ line: 0, message: err instanceof Error ? err.message : "Upload failed" }],
      });
    } finally {
      setBusy(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  return (
    <Card
      title="Your listings"
      description="Upload a CSV. The assistant only ever quotes properties from this list."
    >
      <div className="flex flex-wrap items-center gap-3">
        <input
          ref={fileInput}
          data-testid="listing-file"
          type="file"
          accept=".csv,text/csv"
          onChange={upload}
          disabled={busy}
          className="text-xs file:mr-3 file:rounded-lg file:border-0 file:bg-slate-900 file:px-3 file:py-2 file:text-xs file:font-medium file:text-white"
        />
        <label className="flex items-center gap-2 text-xs text-slate-600">
          <input
            data-testid="replace-toggle"
            type="checkbox"
            checked={replace}
            onChange={(e) => setReplace(e.target.checked)}
            className="h-4 w-4 rounded border-slate-300"
          />
          Replace everything I have
        </label>
        <a
          href={`${API_URL}/api/listings/sample.csv`}
          data-testid="sample-csv"
          className="ml-auto text-xs text-slate-500 underline"
        >
          Download a sample file
        </a>
      </div>

      {busy && <p className="mt-3 text-xs text-slate-500">Reading your file…</p>}

      {result && result.errors.length === 0 && (
        <p data-testid="import-success" className="mt-3 rounded-lg bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
          Imported {result.imported} listing(s)
          {result.replaced > 0 && `, replacing ${result.replaced}`}.
        </p>
      )}
      {result && result.errors.length > 0 && (
        <div data-testid="import-errors" className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">
          <p className="font-medium">Nothing was imported — please fix these and try again:</p>
          <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs">
            {result.errors.slice(0, 8).map((issue, index) => (
              <li key={index}>
                {issue.line > 0 ? `Line ${issue.line}: ` : ""}
                {issue.message}
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}

function WhatsAppSetup({
  settings,
  onSave,
}: {
  settings: Settings;
  onSave: (patch: Partial<Settings>, label: string) => Promise<void>;
}) {
  const [value, setValue] = useState(settings.whatsapp_phone_number_id ?? "");

  return (
    <Card
      title="Connect WhatsApp"
      description="Paste the Phone Number ID from your Meta WhatsApp Business account."
    >
      <div className="flex gap-2">
        <input
          data-testid="whatsapp-id"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="e.g. 123456789012345"
          className="flex-1 rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-900"
        />
        <button
          data-testid="save-whatsapp"
          onClick={() => onSave({ whatsapp_phone_number_id: value.trim() }, "WhatsApp number")}
          disabled={!value.trim()}
          className="rounded-lg bg-slate-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-40"
        >
          Save
        </button>
      </div>
      <p className="mt-2 text-xs text-slate-500">
        Point your Meta webhook at <code>{API_URL}/webhooks/whatsapp</code>.
      </p>
    </Card>
  );
}

function Tone({
  settings,
  onSave,
}: {
  settings: Settings;
  onSave: (patch: Partial<Settings>, label: string) => Promise<void>;
}) {
  const [tone, setTone] = useState(settings.tone_instructions ?? "");

  return (
    <Card
      title="How the assistant should sound (optional)"
      description="Describe your voice in a sentence or two. It writes as your assistant, never as you."
    >
      <textarea
        data-testid="tone-input"
        value={tone}
        onChange={(e) => setTone(e.target.value)}
        rows={3}
        maxLength={2000}
        placeholder="Warm and direct. Use the customer's first name once. Never push for a visit twice in the same conversation."
        className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-900"
      />
      <div className="mt-3 flex items-center gap-3">
        <button
          data-testid="save-tone"
          onClick={() => onSave({ tone_instructions: tone }, "Tone")}
          className="rounded-lg bg-slate-900 px-3 py-2 text-sm font-medium text-white"
        >
          Save tone
        </button>
        <span className="text-xs text-slate-400">{tone.length}/2000</span>
      </div>
    </Card>
  );
}
