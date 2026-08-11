import type { Page, Route } from "@playwright/test";

export const API = "http://localhost:8000";
export const TOKEN = "rl_test-token";

export const AGENT = {
  id: "agent-1",
  name: "Rohan Mehta",
  brokerage_name: "Sunrise Homes",
  timezone: "Asia/Kolkata",
  calendar_connected: "yes",
  whatsapp_connected: "yes",
};

export const HOT_LEAD = {
  id: "lead-hot",
  name: "Priya Shah",
  phone: "+919876543210",
  status: "qualified",
  temperature: "hot",
  score: 80,
  language: "en",
  source: "whatsapp",
  budget_min: "6000000",
  budget_max: "9000000",
  preferred_locations: ["Bopal"],
  bhk: 3,
  timeline_months: 2,
  last_inbound_at: new Date(Date.now() - 3600_000).toISOString(),
  last_outbound_at: new Date(Date.now() - 3500_000).toISOString(),
  created_at: new Date(Date.now() - 86400_000).toISOString(),
  consent_status: "opted_in",
  follow_up_count: 0,
  handoff_reason: null,
};

export const COLD_LEAD = {
  ...HOT_LEAD,
  id: "lead-cold",
  name: "Amit Patel",
  phone: "+919812345678",
  status: "cold",
  temperature: "cold",
  score: 20,
  preferred_locations: [],
};

export const HANDED_OFF_LEAD = {
  ...HOT_LEAD,
  id: "lead-attention",
  name: "Neha Joshi",
  phone: "+919812340000",
  status: "handed_off",
  temperature: "warm",
  score: 55,
  handoff_reason: "Wants to negotiate price",
};

export function leadDetail(lead: typeof HOT_LEAD, overrides: Record<string, unknown> = {}) {
  return {
    ...lead,
    email: null,
    property_type: "flat",
    purpose: "self_use",
    loan_preapproved: true,
    site_visit_willing: true,
    notes: null,
    scored_at: new Date().toISOString(),
    handed_off_at: null,
    opted_out_at: null,
    timezone: "Asia/Kolkata",
    score_reasons: [
      { factor: "budget_match", points: 30, detail: "Budget matches 2 of 3 active listings" },
      { factor: "timeline", points: 25, detail: "Buying within 2 months" },
      { factor: "financing", points: 20, detail: "Home loan pre-approved" },
    ],
    appointments: [],
    follow_ups: [],
    conversation_id: "conv-1",
    conversation_status: "active",
    ...overrides,
  };
}

export const TRANSCRIPT = {
  conversation_id: "conv-1",
  status: "active",
  channel: "whatsapp",
  messages: [
    {
      id: "m1",
      role: "lead",
      direction: "inbound",
      channel: "whatsapp",
      status: "received",
      content: "Is the Bopal flat still available?",
      media_urls: [],
      created_at: new Date(Date.now() - 3600_000).toISOString(),
      sent_at: null,
    },
    {
      id: "m2",
      role: "assistant",
      direction: "outbound",
      channel: "whatsapp",
      status: "delivered",
      content: "Yes it is. What budget range are you working with?",
      media_urls: [],
      created_at: new Date(Date.now() - 3500_000).toISOString(),
      sent_at: null,
    },
  ],
};

interface StubOptions {
  leads?: unknown[];
  detail?: Record<string, unknown>;
  transcript?: unknown;
  /** Force /api/me to 401, simulating a bad token. */
  unauthorized?: boolean;
  /** Called whenever the dashboard POSTs somewhere. */
  onPost?: (url: string, body: unknown) => void;
  /** Status + detail to return from POST endpoints. */
  postResponse?: { status: number; body: unknown };
}

/** Route every API call the dashboard makes to in-memory fixtures. */
export async function stubApi(page: Page, options: StubOptions = {}) {
  const leads = options.leads ?? [HOT_LEAD, HANDED_OFF_LEAD, COLD_LEAD];

  await page.route(`${API}/api/**`, async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;

    if (options.unauthorized) {
      return route.fulfill({ status: 401, json: { detail: "invalid token" } });
    }

    if (request.method() === "POST") {
      options.onPost?.(path, request.postDataJSON());
      const response = options.postResponse ?? {
        status: 200,
        body: { ok: true, lead_status: "handed_off", detail: "You are now handling this." },
      };
      return route.fulfill({ status: response.status, json: response.body });
    }

    if (path === "/api/me") return route.fulfill({ json: AGENT });

    if (path === "/api/stats") {
      return route.fulfill({
        json: {
          total: leads.length,
          by_status: { qualified: 1, handed_off: 1, cold: 1 },
          by_temperature: { hot: 1, warm: 1, cold: 1 },
          needs_attention: 1,
          booked_upcoming: 2,
        },
      });
    }

    if (path === "/api/leads") {
      const statuses = url.searchParams.getAll("status");
      const search = url.searchParams.get("search")?.toLowerCase() ?? "";
      let items = leads as (typeof HOT_LEAD)[];
      if (statuses.length) items = items.filter((l) => statuses.includes(l.status));
      if (search) {
        items = items.filter(
          (l) => l.name?.toLowerCase().includes(search) || l.phone.includes(search),
        );
      }
      return route.fulfill({ json: { items, total: items.length, limit: 50, offset: 0 } });
    }

    if (path.endsWith("/transcript")) {
      return route.fulfill({ json: options.transcript ?? TRANSCRIPT });
    }

    if (path.startsWith("/api/leads/")) {
      const id = path.split("/")[3];
      const found = (leads as (typeof HOT_LEAD)[]).find((l) => l.id === id) ?? HOT_LEAD;
      return route.fulfill({ json: options.detail ?? leadDetail(found) });
    }

    return route.fulfill({ status: 404, json: { detail: "not stubbed" } });
  });
}

/**
 * Put a token in localStorage so the app skips the gate.
 *
 * Sets it once rather than via addInitScript: an init script re-injects on every
 * navigation, including the reload after sign-out, which would make signing out
 * look broken. The first goto renders the token gate without any network call
 * (no token yet), so it is cheap.
 */
export async function signIn(page: Page) {
  await page.goto("/");
  await page.evaluate((token) => {
    window.localStorage.setItem("realtylead_token", token);
  }, TOKEN);
}
