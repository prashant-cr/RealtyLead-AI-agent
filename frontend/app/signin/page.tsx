"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { ApiError, api, setToken } from "@/lib/api";

function SignInForm() {
  const router = useRouter();
  const params = useSearchParams();
  const [mode, setMode] = useState<"login" | "signup">(
    params.get("mode") === "signup" ? "signup" : "login",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({
    name: "",
    email: "",
    password: "",
    phone: "",
    brokerage_name: "",
  });

  function update(key: keyof typeof form) {
    return (event: React.ChangeEvent<HTMLInputElement>) =>
      setForm((prev) => ({ ...prev, [key]: event.target.value }));
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result =
        mode === "signup"
          ? await api.signup({
              name: form.name.trim(),
              email: form.email.trim(),
              password: form.password,
              phone: form.phone.trim(),
              brokerage_name: form.brokerage_name.trim() || undefined,
            })
          : await api.login({ email: form.email.trim(), password: form.password });

      setToken(result.token);
      router.push(result.onboarding_complete ? "/" : "/onboarding");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the server");
    } finally {
      setBusy(false);
    }
  }

  const signup = mode === "signup";

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-6 py-10">
      <h1 className="text-xl font-semibold">RealtyLead</h1>
      <p className="mt-1 text-sm text-slate-600">
        {signup
          ? "Set up your assistant. It takes about five minutes."
          : "Sign in to your pipeline."}
      </p>

      <form onSubmit={submit} className="mt-6 space-y-3" data-testid="auth-form">
        {signup && (
          <>
            <Field
              label="Your name"
              testId="field-name"
              value={form.name}
              onChange={update("name")}
              autoComplete="name"
              required
            />
            <Field
              label="Brokerage (optional)"
              testId="field-brokerage"
              value={form.brokerage_name}
              onChange={update("brokerage_name")}
              autoComplete="organization"
            />
            <Field
              label="Your WhatsApp number"
              testId="field-phone"
              value={form.phone}
              onChange={update("phone")}
              placeholder="+919876543210"
              autoComplete="tel"
              required
            />
          </>
        )}
        <Field
          label="Email"
          testId="field-email"
          type="email"
          value={form.email}
          onChange={update("email")}
          autoComplete="email"
          required
        />
        <Field
          label="Password"
          testId="field-password"
          type="password"
          value={form.password}
          onChange={update("password")}
          autoComplete={signup ? "new-password" : "current-password"}
          hint={signup ? "At least 10 characters." : undefined}
          required
        />

        {error && (
          <p data-testid="auth-error" className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">
            {error}
          </p>
        )}

        <button
          data-testid="auth-submit"
          type="submit"
          disabled={busy}
          className="w-full rounded-lg bg-slate-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-40"
        >
          {busy ? "Just a moment…" : signup ? "Create account" : "Sign in"}
        </button>
      </form>

      <p className="mt-4 text-center text-sm text-slate-600">
        {signup ? "Already have an account? " : "New here? "}
        <button
          data-testid="auth-toggle"
          onClick={() => {
            setMode(signup ? "login" : "signup");
            setError(null);
          }}
          className="font-medium text-slate-900 underline"
        >
          {signup ? "Sign in" : "Create one"}
        </button>
      </p>

      <p className="mt-6 text-center text-xs text-slate-400">
        Using an API token instead?{" "}
        <Link href="/" className="underline">
          Go to the pipeline
        </Link>
      </p>
    </main>
  );
}

function Field({
  label,
  testId,
  hint,
  ...props
}: {
  label: string;
  testId: string;
  hint?: string;
} & React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <label className="block">
      <span className="text-xs font-medium text-slate-600">{label}</span>
      <input
        data-testid={testId}
        {...props}
        className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-900"
      />
      {hint && <span className="mt-1 block text-xs text-slate-400">{hint}</span>}
    </label>
  );
}

export default function SignInPage() {
  return (
    <Suspense fallback={<main className="p-6 text-sm text-slate-500">Loading…</main>}>
      <SignInForm />
    </Suspense>
  );
}
