/**
 * Client for the dashboard API.
 *
 * The token is held in localStorage rather than a cookie because the API is a
 * separate origin and uses bearer auth. That means it is readable by any script
 * on this page — acceptable while the dashboard has no third-party scripts, and
 * something M7 should revisit alongside real accounts (httpOnly cookie + CSRF).
 */

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const TOKEN_KEY = "realtylead_token";

export type LeadStatus =
  | "new"
  | "engaged"
  | "qualified"
  | "booked"
  | "cold"
  | "handed_off"
  | "opted_out";

export type Temperature = "hot" | "warm" | "cold";

export interface ScoreReason {
  factor: string;
  points: number;
  detail: string;
}

export interface LeadSummary {
  id: string;
  name: string | null;
  phone: string;
  status: LeadStatus;
  temperature: Temperature;
  score: number;
  language: string;
  source: string;
  budget_min: string | null;
  budget_max: string | null;
  preferred_locations: string[];
  bhk: number | null;
  timeline_months: number | null;
  last_inbound_at: string | null;
  last_outbound_at: string | null;
  created_at: string;
  consent_status: string;
  follow_up_count: number;
  handoff_reason: string | null;
}

export interface Appointment {
  id: string;
  appointment_type: "call" | "site_visit";
  status: string;
  starts_at: string;
  ends_at: string;
  timezone: string;
  location: string | null;
  notes: string | null;
  google_event_id: string | null;
}

export interface FollowUp {
  id: string;
  attempt_number: number;
  scheduled_for: string;
  status: string;
  template_name: string | null;
  sent_at: string | null;
  outcome_reason: string | null;
}

export interface LeadDetail extends LeadSummary {
  email: string | null;
  property_type: string | null;
  purpose: string;
  loan_preapproved: boolean | null;
  site_visit_willing: boolean | null;
  notes: string | null;
  scored_at: string | null;
  handed_off_at: string | null;
  opted_out_at: string | null;
  timezone: string | null;
  score_reasons: ScoreReason[];
  appointments: Appointment[];
  follow_ups: FollowUp[];
  conversation_id: string | null;
  conversation_status: "active" | "human_takeover" | "closed" | null;
}

export interface Message {
  id: string;
  role: "lead" | "assistant" | "human_agent" | "system";
  direction: "inbound" | "outbound";
  channel: string;
  status: string;
  content: string;
  media_urls: string[];
  created_at: string;
  sent_at: string | null;
}

export interface Transcript {
  conversation_id: string | null;
  status: string | null;
  channel: string | null;
  messages: Message[];
}

export interface PipelineStats {
  total: number;
  by_status: Record<string, number>;
  by_temperature: Record<string, number>;
  needs_attention: number;
  booked_upcoming: number;
}

export interface LeadPage {
  items: LeadSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface Settings {
  name: string;
  email: string;
  phone: string;
  brokerage_name: string | null;
  timezone: string;
  languages: string[];
  working_hours: Record<string, string[]>;
  quiet_hours_start: number;
  quiet_hours_end: number;
  tone_instructions: string | null;
  escalation_budget_threshold: number | null;
  whatsapp_phone_number_id: string | null;
  calendar_connected: boolean;
  onboarded: boolean;
}

export interface Listing {
  id: string;
  title: string;
  property_type: string;
  status: string;
  city: string;
  locality: string | null;
  price: string;
  bhk: number | null;
  carpet_area_sqft: number | null;
  rera_id: string | null;
  is_active: boolean;
}

export interface ImportResult {
  imported: number;
  replaced: number;
  skipped_blank: number;
  errors: { line: number; message: string }[];
}

export interface ChecklistStep {
  key: string;
  label: string;
  done: boolean;
  detail: string | null;
}

export interface OnboardingStatus {
  complete: boolean;
  steps: ChecklistStep[];
}

export interface SessionResponse {
  token: string;
  expires_at: string;
  agent_id: string;
  name: string;
  onboarding_complete: boolean;
}

export interface AgentInfo {
  id: string;
  name: string;
  brokerage_name: string | null;
  timezone: string;
  calendar_connected: string;
  whatsapp_connected: string;
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  window.localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  window.localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  if (!token) throw new ApiError(401, "No API token set");

  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      ...(init.headers ?? {}),
    },
    cache: "no-store",
  });

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      // FastAPI puts the message on `detail`; validation errors make it a list.
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* keep statusText */
    }
    throw new ApiError(response.status, detail);
  }

  return (await response.json()) as T;
}

/** Signup and login are the only calls made without a token. */
async function publicRequest<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const parsed = await response.json();
      detail =
        typeof parsed.detail === "string"
          ? parsed.detail
          : Array.isArray(parsed.detail)
            ? (parsed.detail[0]?.msg ?? "That did not work")
            : JSON.stringify(parsed.detail);
    } catch {
      /* keep statusText */
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export const api = {
  signup: (body: {
    name: string;
    email: string;
    password: string;
    phone: string;
    brokerage_name?: string;
  }) => publicRequest<SessionResponse>("/auth/signup", body),
  login: (body: { email: string; password: string }) =>
    publicRequest<SessionResponse>("/auth/login", body),
  logout: () => request<void>("/auth/logout", { method: "POST" }),
  settings: () => request<Settings>("/api/settings"),
  updateSettings: (body: Partial<Settings>) =>
    request<Settings>("/api/settings", { method: "PATCH", body: JSON.stringify(body) }),
  onboarding: () => request<OnboardingStatus>("/api/onboarding"),
  listings: () => request<Listing[]>("/api/listings"),
  importListings: async (file: File, replace: boolean): Promise<ImportResult> => {
    const token = getToken();
    if (!token) throw new ApiError(401, "No API token set");
    const form = new FormData();
    form.append("file", file);
    // No Content-Type header: the browser must set the multipart boundary.
    const response = await fetch(`${API_URL}/api/listings/import?replace=${replace}`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: form,
    });
    if (!response.ok) {
      throw new ApiError(response.status, `Import failed (${response.status})`);
    }
    return (await response.json()) as ImportResult;
  },
  me: () => request<AgentInfo>("/api/me"),
  stats: () => request<PipelineStats>("/api/stats"),
  leads: (params: { status?: string[]; temperature?: string[]; search?: string } = {}) => {
    const query = new URLSearchParams();
    params.status?.forEach((s) => query.append("status", s));
    params.temperature?.forEach((t) => query.append("temperature", t));
    if (params.search) query.set("search", params.search);
    const suffix = query.toString();
    return request<LeadPage>(`/api/leads${suffix ? `?${suffix}` : ""}`);
  },
  lead: (id: string) => request<LeadDetail>(`/api/leads/${id}`),
  transcript: (id: string) => request<Transcript>(`/api/leads/${id}/transcript`),
  takeover: (id: string, reason?: string) =>
    request<{ ok: boolean; detail: string | null }>(`/api/leads/${id}/takeover`, {
      method: "POST",
      body: JSON.stringify({ reason: reason ?? null }),
    }),
  release: (id: string) =>
    request<{ ok: boolean; detail: string | null }>(`/api/leads/${id}/release`, {
      method: "POST",
    }),
  sendMessage: (id: string, text: string) =>
    request<Message>(`/api/leads/${id}/messages`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
};

/** Indian-format currency, which is what the agents actually read. */
export function formatInr(value: string | null): string | null {
  if (value === null) return null;
  const amount = Number(value);
  if (!Number.isFinite(amount)) return null;
  if (amount >= 10000000) return `₹${(amount / 10000000).toFixed(2).replace(/\.00$/, "")} Cr`;
  if (amount >= 100000) return `₹${(amount / 100000).toFixed(2).replace(/\.00$/, "")} L`;
  return `₹${amount.toLocaleString("en-IN")}`;
}

export function formatBudget(lead: Pick<LeadSummary, "budget_min" | "budget_max">): string {
  const low = formatInr(lead.budget_min);
  const high = formatInr(lead.budget_max);
  if (low && high) return `${low} – ${high}`;
  return low ?? high ?? "Not shared";
}

export function relativeTime(iso: string | null): string {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  const minutes = Math.round((Date.now() - then) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return days === 1 ? "yesterday" : `${days}d ago`;
}
