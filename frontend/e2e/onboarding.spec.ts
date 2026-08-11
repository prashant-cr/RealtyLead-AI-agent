import { type Page, type Route, expect, test } from "@playwright/test";
import { API, AGENT, TOKEN, signIn } from "./fixtures";

const SETTINGS = {
  name: "Neha Joshi",
  email: "neha@bluekey.example",
  phone: "+919876500010",
  brokerage_name: "Blue Key Realty",
  timezone: "Asia/Kolkata",
  languages: ["en"],
  working_hours: {
    mon: ["09:30", "19:00"],
    tue: ["09:30", "19:00"],
    wed: ["09:30", "19:00"],
    thu: ["09:30", "19:00"],
    fri: ["09:30", "19:00"],
    sat: ["10:00", "17:00"],
    sun: [],
  },
  quiet_hours_start: 21,
  quiet_hours_end: 9,
  tone_instructions: null,
  escalation_budget_threshold: null,
  whatsapp_phone_number_id: null,
  calendar_connected: false,
  onboarded: false,
};

function checklist(done: Record<string, boolean> = {}) {
  const steps = [
    { key: "account", label: "Create your account", done: true, detail: SETTINGS.email },
    { key: "hours", label: "Set your working hours", done: true, detail: "6 day(s) open" },
    { key: "listings", label: "Add your listings", done: false, detail: "0 listing(s)" },
    { key: "whatsapp", label: "Connect WhatsApp", done: false, detail: null },
    { key: "calendar", label: "Connect Google Calendar (optional)", done: false, detail: null },
    { key: "tone", label: "Set the assistant's tone (optional)", done: false, detail: null },
  ].map((step) => ({ ...step, done: done[step.key] ?? step.done }));
  const required = ["account", "hours", "listings", "whatsapp"];
  return { complete: steps.filter((s) => required.includes(s.key)).every((s) => s.done), steps };
}

interface Options {
  settings?: Record<string, unknown>;
  status?: ReturnType<typeof checklist>;
  importResult?: Record<string, unknown>;
  signupError?: { status: number; detail: string };
  loginError?: { status: number; detail: string };
  onCall?: (method: string, path: string, body: unknown) => void;
}

async function stubOnboarding(page: Page, options: Options = {}) {
  let settings = { ...SETTINGS, ...(options.settings ?? {}) };

  await page.route(`${API}/**`, async (route: Route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    let body: unknown = null;
    try {
      body = request.postDataJSON();
    } catch {
      body = null;
    }
    options.onCall?.(method, path, body);

    if (path === "/auth/signup") {
      if (options.signupError) {
        return route.fulfill({
          status: options.signupError.status,
          json: { detail: options.signupError.detail },
        });
      }
      return route.fulfill({
        status: 201,
        json: {
          token: TOKEN,
          expires_at: new Date(Date.now() + 86400_000).toISOString(),
          agent_id: "agent-1",
          name: "Neha Joshi",
          onboarding_complete: false,
        },
      });
    }

    if (path === "/auth/login") {
      if (options.loginError) {
        return route.fulfill({
          status: options.loginError.status,
          json: { detail: options.loginError.detail },
        });
      }
      return route.fulfill({
        json: {
          token: TOKEN,
          expires_at: new Date(Date.now() + 86400_000).toISOString(),
          agent_id: "agent-1",
          name: "Neha Joshi",
          onboarding_complete: true,
        },
      });
    }

    if (path === "/api/me") return route.fulfill({ json: AGENT });
    if (path === "/api/settings") {
      if (method === "PATCH") {
        settings = { ...settings, ...(body as Record<string, unknown>) };
      }
      return route.fulfill({ json: settings });
    }
    if (path === "/api/onboarding") {
      return route.fulfill({
        json:
          options.status ??
          checklist({
            listings: Boolean(options.importResult),
            whatsapp: Boolean(settings.whatsapp_phone_number_id),
            tone: Boolean(settings.tone_instructions),
          }),
      });
    }
    if (path === "/api/listings/import") {
      return route.fulfill({
        json: options.importResult ?? {
          imported: 3,
          replaced: 0,
          skipped_blank: 0,
          errors: [],
        },
      });
    }
    if (path === "/api/stats") {
      return route.fulfill({
        json: {
          total: 0,
          by_status: {},
          by_temperature: {},
          needs_attention: 0,
          booked_upcoming: 0,
        },
      });
    }
    if (path === "/api/leads") {
      return route.fulfill({ json: { items: [], total: 0, limit: 50, offset: 0 } });
    }

    return route.fulfill({ status: 404, json: { detail: "not stubbed" } });
  });
}

test.describe("signup", () => {
  test("creates an account and lands on onboarding", async ({ page }) => {
    const calls: { method: string; path: string; body: unknown }[] = [];
    await stubOnboarding(page, {
      onCall: (method, path, body) => calls.push({ method, path, body }),
    });
    await page.goto("/signin?mode=signup");

    await page.getByTestId("field-name").fill("Neha Joshi");
    await page.getByTestId("field-phone").fill("+919876500010");
    await page.getByTestId("field-email").fill("neha@bluekey.example");
    await page.getByTestId("field-password").fill("correct-horse-battery");
    await page.getByTestId("auth-submit").click();

    await expect(page).toHaveURL(/\/onboarding/);
    const signup = calls.find((c) => c.path === "/auth/signup");
    expect(signup?.body).toMatchObject({ email: "neha@bluekey.example", name: "Neha Joshi" });
  });

  test("shows the server's reason when signup is refused", async ({ page }) => {
    await stubOnboarding(page, {
      signupError: { status: 409, detail: "That email address cannot be used." },
    });
    await page.goto("/signin?mode=signup");

    await page.getByTestId("field-name").fill("Neha");
    await page.getByTestId("field-phone").fill("+919876500010");
    await page.getByTestId("field-email").fill("taken@example.com");
    await page.getByTestId("field-password").fill("correct-horse-battery");
    await page.getByTestId("auth-submit").click();

    await expect(page.getByTestId("auth-error")).toContainText("cannot be used");
    await expect(page).toHaveURL(/\/signin/);
  });

  test("a returning agent goes straight to the pipeline", async ({ page }) => {
    await stubOnboarding(page);
    await page.goto("/signin");

    await page.getByTestId("field-email").fill("neha@bluekey.example");
    await page.getByTestId("field-password").fill("correct-horse-battery");
    await page.getByTestId("auth-submit").click();

    await expect(page).toHaveURL(/localhost:\d+\/$/);
  });

  test("a wrong password is surfaced", async ({ page }) => {
    await stubOnboarding(page, {
      loginError: { status: 401, detail: "Incorrect email or password." },
    });
    await page.goto("/signin");

    await page.getByTestId("field-email").fill("neha@bluekey.example");
    await page.getByTestId("field-password").fill("wrong");
    await page.getByTestId("auth-submit").click();

    await expect(page.getByTestId("auth-error")).toContainText("Incorrect email or password");
  });

  test("can switch between signing in and signing up", async ({ page }) => {
    await stubOnboarding(page);
    await page.goto("/signin");
    await expect(page.getByTestId("field-name")).toHaveCount(0);

    await page.getByTestId("auth-toggle").click();

    await expect(page.getByTestId("field-name")).toBeVisible();
  });
});

test.describe("onboarding checklist", () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page);
  });

  test("shows what is still outstanding", async ({ page }) => {
    await stubOnboarding(page);
    await page.goto("/onboarding");

    await expect(page.getByTestId("step-account")).toHaveAttribute("data-done", "true");
    await expect(page.getByTestId("step-listings")).toHaveAttribute("data-done", "false");
    await expect(page.getByTestId("step-whatsapp")).toHaveAttribute("data-done", "false");
    await expect(page.getByTestId("setup-complete")).toHaveCount(0);
  });

  test("celebrates once the essentials are done", async ({ page }) => {
    await stubOnboarding(page, {
      status: checklist({ listings: true, whatsapp: true }),
    });
    await page.goto("/onboarding");

    await expect(page.getByTestId("setup-complete")).toBeVisible();
  });

  test("optional steps do not block completion", async ({ page }) => {
    await stubOnboarding(page, { status: checklist({ listings: true, whatsapp: true }) });
    await page.goto("/onboarding");

    await expect(page.getByTestId("step-calendar")).toHaveAttribute("data-done", "false");
    await expect(page.getByTestId("step-tone")).toHaveAttribute("data-done", "false");
    await expect(page.getByTestId("setup-complete")).toBeVisible();
  });
});

test.describe("working hours", () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page);
  });

  test("saves the days and times the agent picked", async ({ page }) => {
    const calls: { method: string; path: string; body: unknown }[] = [];
    await stubOnboarding(page, {
      onCall: (method, path, body) => calls.push({ method, path, body }),
    });
    await page.goto("/onboarding");

    await page.getByTestId("day-sun").check();
    await page.getByTestId("open-mon").fill("10:00");
    await page.getByTestId("save-hours").click();

    await expect(page.getByTestId("setup-saved")).toBeVisible();
    const patch = calls.find((c) => c.method === "PATCH");
    const hours = (patch?.body as { working_hours: Record<string, string[]> }).working_hours;
    expect(hours.mon[0]).toBe("10:00");
    expect(hours.sun).toHaveLength(2);
  });

  test("unchecking a day marks it closed", async ({ page }) => {
    const calls: { method: string; path: string; body: unknown }[] = [];
    await stubOnboarding(page, {
      onCall: (method, path, body) => calls.push({ method, path, body }),
    });
    await page.goto("/onboarding");

    await page.getByTestId("day-sat").uncheck();
    await page.getByTestId("save-hours").click();

    await expect(page.getByTestId("setup-saved")).toBeVisible();
    const patch = calls.find((c) => c.method === "PATCH");
    expect((patch?.body as { working_hours: Record<string, string[]> }).working_hours.sat).toEqual(
      [],
    );
  });
});

test.describe("listing import", () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page);
  });

  test("reports how many listings were imported", async ({ page }) => {
    await stubOnboarding(page);
    await page.goto("/onboarding");

    await page.getByTestId("listing-file").setInputFiles({
      name: "listings.csv",
      mimeType: "text/csv",
      buffer: Buffer.from("title,city,price\nFlat,Ahmedabad,85 lakh\n"),
    });

    await expect(page.getByTestId("import-success")).toContainText("3 listing(s)");
  });

  test("shows every bad row so the agent can fix the file", async ({ page }) => {
    await stubOnboarding(page, {
      importResult: {
        imported: 0,
        replaced: 0,
        skipped_blank: 0,
        errors: [
          { line: 3, message: "'about a crore' is not a price" },
          { line: 7, message: "city is required" },
        ],
      },
    });
    await page.goto("/onboarding");

    await page.getByTestId("listing-file").setInputFiles({
      name: "listings.csv",
      mimeType: "text/csv",
      buffer: Buffer.from("title,city,price\nFlat,,about a crore\n"),
    });

    const errors = page.getByTestId("import-errors");
    await expect(errors).toContainText("Line 3");
    await expect(errors).toContainText("is not a price");
    await expect(errors).toContainText("Line 7");
    await expect(errors).toContainText("Nothing was imported");
  });

  test("offers a sample file to start from", async ({ page }) => {
    await stubOnboarding(page);
    await page.goto("/onboarding");

    await expect(page.getByTestId("sample-csv")).toHaveAttribute(
      "href",
      `${API}/api/listings/sample.csv`,
    );
  });
});

test.describe("tone and WhatsApp", () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page);
  });

  test("saves the assistant's tone", async ({ page }) => {
    const calls: { method: string; path: string; body: unknown }[] = [];
    await stubOnboarding(page, {
      onCall: (method, path, body) => calls.push({ method, path, body }),
    });
    await page.goto("/onboarding");

    await page.getByTestId("tone-input").fill("Warm and direct. Never push twice.");
    await page.getByTestId("save-tone").click();

    await expect(page.getByTestId("setup-saved")).toBeVisible();
    expect(calls.find((c) => c.method === "PATCH")?.body).toMatchObject({
      tone_instructions: "Warm and direct. Never push twice.",
    });
  });

  test("saves the WhatsApp phone number id", async ({ page }) => {
    const calls: { method: string; path: string; body: unknown }[] = [];
    await stubOnboarding(page, {
      onCall: (method, path, body) => calls.push({ method, path, body }),
    });
    await page.goto("/onboarding");

    await page.getByTestId("whatsapp-id").fill("123456789012345");
    await page.getByTestId("save-whatsapp").click();

    await expect(page.getByTestId("setup-saved")).toBeVisible();
    expect(calls.find((c) => c.method === "PATCH")?.body).toMatchObject({
      whatsapp_phone_number_id: "123456789012345",
    });
  });
});
