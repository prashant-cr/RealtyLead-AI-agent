/**
 * Records the dashboard walkthrough used in the README.
 *
 *   make demo-gif
 *
 * Drives the real dashboard against the real backend and writes
 * docs/media/dashboard-demo.gif. Needs the stack running (`make up`, the API,
 * and the dashboard on :3000) and a token from `make token`.
 *
 * The token is injected into localStorage rather than typed into the sign-in
 * form, so it never appears in a frame — the recording is committed to a public
 * repository. The walkthrough therefore starts at the pipeline.
 *
 * Pauses are deliberate and generous: this is a demo, not a test, and a GIF that
 * moves faster than someone can read it is useless.
 */
import { chromium } from "@playwright/test";
import { mkdir, readdir, rename, rm } from "node:fs/promises";
import { join } from "node:path";

const TOKEN = process.env.LIVE_TOKEN;
const BASE = process.env.LIVE_URL ?? "http://localhost:3000";
const OUT_DIR = "../docs/media";
const RAW_DIR = ".demo-recording";

if (!TOKEN) {
  console.error("LIVE_TOKEN is not set. Get one with: make token");
  process.exit(1);
}

const beat = (page, ms) => page.waitForTimeout(ms);

async function main() {
  await rm(RAW_DIR, { recursive: true, force: true });
  await mkdir(OUT_DIR, { recursive: true });

  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    deviceScaleFactor: 1,
    recordVideo: { dir: RAW_DIR, size: { width: 1280, height: 800 } },
  });

  // Runs before any page script, so the token gate never renders.
  await context.addInitScript((token) => {
    window.localStorage.setItem("realtylead_token", token);
  }, TOKEN);

  const page = await context.newPage();

  await page.goto(BASE);
  await page.getByTestId("lead-row").first().waitFor({ timeout: 20_000 });
  await beat(page, 2500); // pipeline: counts and the lead list

  // The live-test lead is the interesting one — a full qualified conversation.
  const hot = page.getByTestId("lead-row").filter({ hasText: "919000000777" }).first();
  const target = (await hot.count()) > 0 ? hot : page.getByTestId("lead-row").first();
  await target.hover();
  await beat(page, 800);
  await target.click();

  await page.getByTestId("lead-name").waitFor({ timeout: 20_000 });
  await beat(page, 2500); // lead header: score, temperature, status

  const reasons = page.getByTestId("score-reasons");
  if (await reasons.count()) {
    await reasons.scrollIntoViewIfNeeded();
    await beat(page, 2500); // why it scored what it scored
  }

  // Walk the transcript slowly enough to read a message or two.
  for (let i = 0; i < 4; i += 1) {
    await page.mouse.wheel(0, 320);
    await beat(page, 900);
  }
  await beat(page, 1200);

  // Only shown when the assistant still owns the lead. A lead that has already
  // been escalated renders "Hand back to assistant" instead, so this is skipped
  // rather than forced — the recording should not mutate the lead it is filming.
  const takeover = page.getByTestId("takeover-button");
  if (await takeover.count()) {
    await takeover.scrollIntoViewIfNeeded();
    await beat(page, 600);
    await takeover.click();
    await beat(page, 2200); // the AI is now silenced for this lead

    const release = page.getByTestId("release-button");
    if (await release.count()) {
      await release.click();
      await beat(page, 1800); // handed back to the agent
    }
  }

  await page.goBack();
  await page.getByTestId("lead-row").first().waitFor({ timeout: 20_000 });
  await beat(page, 2000);

  await context.close();
  await browser.close();

  const [video] = (await readdir(RAW_DIR)).filter((f) => f.endsWith(".webm"));
  if (!video) throw new Error("Playwright produced no video");
  await rename(join(RAW_DIR, video), join(RAW_DIR, "demo.webm"));
  console.log(`Recorded ${join(RAW_DIR, "demo.webm")}`);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
