// The host's browser saves the soundcheck at Go Live and the live panel while
// on air; another host sees both read-only on the episode page, also after
// the show. Against the local mock (it keeps the saved log like the backend).
import { test, expect, type Page } from "@playwright/test";
import { pickInput, runSoundcheck } from "../lib/studio";

const MOCK = process.env.STUDIO_MOCK_URL ?? "http://localhost:8000";

interface StudioLog {
  soundcheck?: { input: string; checks: { name: string; state: string }[] };
  on_air?: { input: string; ended: boolean };
}

async function savedLog(page: Page): Promise<StudioLog> {
  return (await (await page.request.get(`${MOCK}/api/shows/1`)).json())
    .studio_log;
}

test.afterEach(async ({ page }) => {
  // The next test starts off air, as the episode's host.
  await page.request.get(`${MOCK}/mock/mode/ok`);
});

test("another host sees the saved soundcheck and live panel, read-only", async ({
  page,
  context,
}) => {
  test.slow();
  await page.request.get(`${MOCK}/mock/mode/ok`);
  await page.goto("/admin/#/shows/1");
  await page.getByRole("tab", { name: /Soundcheck/ }).click();
  await pickInput(page);
  await runSoundcheck(page);
  const goLive = page.getByRole("button", { name: "Go Live" });
  await expect(goLive).toBeEnabled({ timeout: 180_000 });
  await goLive.click();
  await expect(page.locator(".on-air-pill")).toBeVisible({ timeout: 30_000 });

  // Go Live saved the input and the checks; the live panel saves at once.
  await expect
    .poll(async () => (await savedLog(page)).soundcheck?.checks.length)
    .toBe(4);
  const log = await savedLog(page);
  expect(log.soundcheck!.checks.map((c) => [c.name, c.state])).toEqual([
    ["Music", "ok"],
    ["Mic", "skipped"],
    ["Server", "ok"],
    ["Return feed", "ok"],
  ]);
  await expect
    .poll(async () => (await savedLog(page)).on_air?.ended)
    .toBe(false);
  const input = (await savedLog(page)).on_air!.input;
  expect(input).not.toBe("Audio input");

  // Another host opens the episode: the live panel read-only, no controls.
  await page.request.get(`${MOCK}/mock/viewer/2`);
  const other = await context.newPage();
  await other.goto("/admin/#/shows/1");
  await expect(
    other.getByText("Read only · only its hosts can edit"),
  ).toBeVisible();
  const view = other.locator(".on-air");
  await expect(view.locator(".onair-badge")).toHaveText("On air");
  await expect(view).toContainText(`Input ${input}`);
  await expect(view.getByRole("link", { name: /Listen live/ })).toBeVisible();
  await expect(view.locator("button, input, select, textarea")).toHaveCount(0);
  await other.screenshot({
    path: "test-results/studio-log-other-live.png",
    fullPage: true,
  });
  // Its Soundcheck tab: the Studio card with what the host went live with.
  await other.getByRole("tab", { name: /Soundcheck/ }).click();
  const studio = other.getByLabel("Studio, read-only");
  await expect(studio).toContainText("Soundcheck · saved at Go Live");
  await expect(studio).toContainText(input);
  await expect(
    studio.getByRole("listitem").filter({ hasText: "Music" }),
  ).toContainText("Good");
  await expect(studio.locator("button, select")).toHaveCount(0);
  await other.screenshot({
    path: "test-results/studio-log-other-soundcheck.png",
    fullPage: true,
  });

  // The host ends the episode: the last save holds the values from the end.
  await page.request.get(`${MOCK}/mock/viewer/1`);
  await page.getByRole("button", { name: "End Episode" }).click();
  await page.getByRole("button", { name: "End Episode" }).last().click();
  await expect
    .poll(async () => (await savedLog(page)).on_air?.ended)
    .toBe(true);

  await page.request.get(`${MOCK}/mock/viewer/2`);
  await other.reload();
  await other.getByRole("tab", { name: /Show time/ }).click();
  await expect(other.locator(".on-air .onair-badge")).toHaveText("Aired");
  await expect(other.locator(".on-air")).toContainText(`Input ${input}`);
  await other.screenshot({
    path: "test-results/studio-log-other-aired.png",
    fullPage: true,
  });
});
