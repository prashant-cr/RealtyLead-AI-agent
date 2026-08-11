import { expect, test } from "@playwright/test";
import {
  HANDED_OFF_LEAD,
  HOT_LEAD,
  leadDetail,
  signIn,
  stubApi,
} from "./fixtures";

test.describe("auth gate", () => {
  test("asks for a token when there is none", async ({ page }) => {
    await stubApi(page);
    await page.goto("/");

    await expect(page.getByTestId("token-input")).toBeVisible();
    await expect(page.getByTestId("lead-row")).toHaveCount(0);
  });

  test("rejects a bad token and does not store it", async ({ page }) => {
    await stubApi(page, { unauthorized: true });
    await page.goto("/");

    await page.getByTestId("token-input").fill("rl_wrong");
    await page.getByTestId("token-submit").click();

    await expect(page.getByTestId("token-error")).toBeVisible();
    expect(await page.evaluate(() => localStorage.getItem("realtylead_token"))).toBeNull();
  });

  test("accepts a good token and shows the pipeline", async ({ page }) => {
    await stubApi(page);
    await page.goto("/");

    await page.getByTestId("token-input").fill("rl_valid");
    await page.getByTestId("token-submit").click();

    await expect(page.getByTestId("lead-row").first()).toBeVisible();
  });

  test("signing out returns to the gate", async ({ page }) => {
    await signIn(page);
    await stubApi(page);
    await page.goto("/");
    await expect(page.getByTestId("lead-row").first()).toBeVisible();

    await page.getByTestId("sign-out").click();

    await expect(page.getByTestId("token-input")).toBeVisible();
  });
});

test.describe("pipeline", () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page);
  });

  test("shows the pipeline counts", async ({ page }) => {
    await stubApi(page);
    await page.goto("/");

    await expect(page.getByTestId("stat-total")).toHaveText("3");
    await expect(page.getByTestId("stat-attention")).toHaveText("1");
    await expect(page.getByTestId("stat-booked")).toHaveText("2");
  });

  test("lists leads with their temperature and score", async ({ page }) => {
    await stubApi(page);
    await page.goto("/");

    const rows = page.getByTestId("lead-row");
    await expect(rows).toHaveCount(3);
    await expect(rows.first()).toContainText("Priya Shah");
    await expect(page.getByTestId("temperature-badge").first()).toContainText("hot");
    await expect(page.getByTestId("temperature-badge").first()).toContainText("80");
  });

  test("filters to leads that need the agent", async ({ page }) => {
    await stubApi(page);
    await page.goto("/");
    await expect(page.getByTestId("lead-row")).toHaveCount(3);

    await page.getByTestId("filter-attention").click();

    await expect(page.getByTestId("lead-row")).toHaveCount(1);
    await expect(page.getByTestId("lead-row")).toContainText("Neha Joshi");
  });

  test("searches by name", async ({ page }) => {
    await stubApi(page);
    await page.goto("/");

    await page.getByTestId("search").fill("Amit");

    await expect(page.getByTestId("lead-row")).toHaveCount(1);
    await expect(page.getByTestId("lead-row")).toContainText("Amit Patel");
  });

  test("shows an empty state rather than a blank page", async ({ page }) => {
    await stubApi(page, { leads: [] });
    await page.goto("/");

    await expect(page.getByTestId("empty-state")).toBeVisible();
  });

  test("opens a lead from the pipeline", async ({ page }) => {
    await stubApi(page);
    await page.goto("/");

    await page.getByTestId("lead-row").first().click();

    await expect(page).toHaveURL(/\/leads\/lead-hot/);
    await expect(page.getByTestId("lead-name")).toHaveText("Priya Shah");
  });
});

test.describe("lead detail", () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page);
  });

  test("shows the transcript in order", async ({ page }) => {
    await stubApi(page);
    await page.goto(`/leads/${HOT_LEAD.id}`);

    const transcript = page.getByTestId("transcript");
    await expect(transcript).toContainText("Is the Bopal flat still available?");
    await expect(transcript).toContainText("What budget range are you working with?");
  });

  test("explains why the lead scored what it did", async ({ page }) => {
    await stubApi(page);
    await page.goto(`/leads/${HOT_LEAD.id}`);

    const reasons = page.getByTestId("score-reasons");
    await expect(reasons).toContainText("Budget matches 2 of 3 active listings");
    await expect(reasons).toContainText("Buying within 2 months");
    await expect(reasons).toContainText("+30");
  });

  test("shows the phone number as a callable link", async ({ page }) => {
    await stubApi(page);
    await page.goto(`/leads/${HOT_LEAD.id}`);

    await expect(page.getByTestId("lead-phone")).toHaveAttribute(
      "href",
      `tel:${HOT_LEAD.phone}`,
    );
  });
});

test.describe("manual takeover", () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page);
  });

  test("taking over calls the API and confirms", async ({ page }) => {
    const posts: { url: string; body: unknown }[] = [];
    await stubApi(page, { onPost: (url, body) => posts.push({ url, body }) });
    await page.goto(`/leads/${HOT_LEAD.id}`);

    await page.getByTestId("takeover-button").click();

    await expect(page.getByTestId("lead-notice")).toBeVisible();
    expect(posts.map((p) => p.url)).toContain(`/api/leads/${HOT_LEAD.id}/takeover`);
  });

  test("a lead under takeover offers to hand back instead", async ({ page }) => {
    await stubApi(page, {
      detail: leadDetail(HANDED_OFF_LEAD, { conversation_status: "human_takeover" }),
    });
    await page.goto(`/leads/${HANDED_OFF_LEAD.id}`);

    await expect(page.getByTestId("release-button")).toBeVisible();
    await expect(page.getByTestId("takeover-button")).toHaveCount(0);
  });

  test("an API refusal is surfaced, not swallowed", async ({ page }) => {
    await stubApi(page, {
      postResponse: { status: 409, body: { detail: "this lead has opted out" } },
    });
    await page.goto(`/leads/${HOT_LEAD.id}`);

    await page.getByTestId("takeover-button").click();

    await expect(page.getByTestId("lead-error")).toContainText("opted out");
  });
});

test.describe("sending a message", () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page);
  });

  test("sends what the agent typed", async ({ page }) => {
    const posts: { url: string; body: unknown }[] = [];
    await stubApi(page, {
      onPost: (url, body) => posts.push({ url, body }),
      postResponse: { status: 200, body: { id: "m3", content: "On my way" } },
    });
    await page.goto(`/leads/${HOT_LEAD.id}`);

    await page.getByTestId("message-input").fill("I'll call you in 10 minutes");
    await page.getByTestId("message-send").click();

    await expect
      .poll(() => posts.find((p) => p.url.endsWith("/messages"))?.body)
      .toEqual({ text: "I'll call you in 10 minutes" });
  });

  test("cannot message a lead who opted out", async ({ page }) => {
    await stubApi(page, {
      detail: leadDetail(HOT_LEAD, { consent_status: "opted_out" }),
    });
    await page.goto(`/leads/${HOT_LEAD.id}`);

    await expect(page.getByTestId("message-input")).toBeDisabled();
    await expect(page.getByTestId("takeover-button")).toBeDisabled();
  });

  test("a rejected send shows why", async ({ page }) => {
    await stubApi(page, {
      postResponse: {
        status: 409,
        body: { detail: "WhatsApp only allows free-form replies within 24h" },
      },
    });
    await page.goto(`/leads/${HOT_LEAD.id}`);

    await page.getByTestId("message-input").fill("hello?");
    await page.getByTestId("message-send").click();

    await expect(page.getByTestId("lead-error")).toContainText("24h");
  });
});
