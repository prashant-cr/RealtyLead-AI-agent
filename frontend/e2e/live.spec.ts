/**
 * Drives the real dashboard against the real backend — no stubs.
 *
 * Skipped unless LIVE_TOKEN is set, so the default suite stays hermetic:
 *   LIVE_TOKEN=rl_... LIVE_URL=http://localhost:3200 npx playwright test live
 */
import { expect, test } from "@playwright/test";

const TOKEN = process.env.LIVE_TOKEN;
const BASE = process.env.LIVE_URL ?? "http://localhost:3200";

test.skip(!TOKEN, "set LIVE_TOKEN to run against a real backend");

test("signs in with a real token and lists real leads", async ({ page }) => {
  await page.goto(BASE);

  await page.getByTestId("token-input").fill(TOKEN!);
  await page.getByTestId("token-submit").click();

  await expect(page.getByTestId("lead-row").first()).toBeVisible({ timeout: 15_000 });
  const count = await page.getByTestId("lead-row").count();
  expect(count).toBeGreaterThan(0);
});

test("opens a real lead and shows its score reasons", async ({ page }) => {
  await page.goto(BASE);
  await page.getByTestId("token-input").fill(TOKEN!);
  await page.getByTestId("token-submit").click();
  await expect(page.getByTestId("lead-row").first()).toBeVisible({ timeout: 15_000 });

  await page.getByTestId("lead-row").first().click();

  await expect(page.getByTestId("lead-name")).toBeVisible();
  await expect(page.getByTestId("score-reasons")).toBeVisible();
});

test("takes over a real lead and hands it back", async ({ page }) => {
  await page.goto(BASE);
  await page.getByTestId("token-input").fill(TOKEN!);
  await page.getByTestId("token-submit").click();
  await expect(page.getByTestId("lead-row").first()).toBeVisible({ timeout: 15_000 });
  await page.getByTestId("lead-row").first().click();
  await expect(page.getByTestId("lead-name")).toBeVisible();

  const takeover = page.getByTestId("takeover-button");
  if (await takeover.isVisible()) {
    await takeover.click();
    await expect(page.getByTestId("lead-notice")).toBeVisible({ timeout: 10_000 });
  }

  await expect(page.getByTestId("release-button")).toBeVisible({ timeout: 10_000 });
  await page.getByTestId("release-button").click();
  await expect(page.getByTestId("takeover-button")).toBeVisible({ timeout: 10_000 });
});
