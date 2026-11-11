// The suite's `test`: servers, page-error tracking and user-level helpers.
//
// Helpers find things the way a person does (roles, labels, visible text). The one
// exception is reading the current item id, which has no role: it is read from the
// text of the "Item to label" card.

import { test as base, expect, type APIRequestContext, type Locator, type Page } from "@playwright/test";
import { startServer, type Server, type StartOptions } from "./server.ts";

type Fixtures = {
  launch: (opts?: StartOptions) => Promise<Server>;
  pageErrors: string[];
};
type WorkerFixtures = { shared: Server };

export const test = base.extend<Fixtures, WorkerFixtures>({
  // The start-screen server: scripts/e2e_label.sh starts one and exports its URL;
  // `npx playwright test` alone starts one per worker.
  shared: [async ({}, use) => {
    const url = process.env.CALIPER_E2E_BASE_URL;
    if (url) {
      await use({ base: url, port: Number(new URL(url).port), work: process.env.CALIPER_E2E_WORK || "", home: process.env.CALIPER_E2E_HOME || "", log: () => "", stop: async () => {} });
      return;
    }
    const s = await startServer();
    await use(s);
    await s.stop();
  }, { scope: "worker" }],

  // Servers a test needs for itself (team mode, legacy serve, a fresh project...).
  launch: async ({}, use, info) => {
    const started: Server[] = [];
    await use(async (opts) => {
      const s = await startServer(opts);
      started.push(s);
      return s;
    });
    for (const s of started) {
      if (info.status !== info.expectedStatus) await info.attach(`server-${s.port}.log`, { body: s.log(), contentType: "text/plain" });
      await s.stop();
    }
  },

  // Uncaught page exceptions fail the test (console 401s in team mode are expected).
  pageErrors: [async ({ page }, use) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await use(errors);
    expect(errors, "uncaught errors in the page").toEqual([]);
  }, { auto: true }],
});

export { expect };

// ------------------------------------------------------------------ labeller helpers

export const card = (page: Page): Locator => page.getByRole("article", { name: "Item to label" });

export async function enterName(page: Page, name: string): Promise<void> {
  const dialog = page.getByRole("dialog", { name: /who is labelling/i });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("textbox", { name: /your name/i }).fill(name);
  await dialog.getByRole("textbox", { name: /your name/i }).press("Enter");
  await expect(dialog).toBeHidden();
}

export async function currentId(page: Page, pattern: RegExp): Promise<string> {
  const c = card(page);
  await expect(c).toContainText(pattern);
  const m = (await c.innerText()).match(pattern);
  if (!m) throw new Error(`no item id matching ${pattern} on the card`);
  return m[0];
}

// Press a key on the labelling screen and wait for the next card.
export async function pressAndAdvance(page: Page, key: string, pattern: RegExp): Promise<string> {
  const before = await currentId(page, pattern);
  await page.keyboard.press(key);
  await expect.poll(() => currentId(page, pattern), { message: `${key} moves past ${before}` }).not.toBe(before);
  return before;
}

export type LabelRow = { item_id: string; labeller: string; decision: string | null; note: string | null; source?: string; skip?: boolean };

export async function labels(request: APIRequestContext, base: string, query = "", headers: Record<string, string> = {}): Promise<LabelRow[]> {
  const res = await request.get(`${base}/api/labels?limit=5000${query ? `&${query}` : ""}`, { headers });
  expect(res.ok(), `GET /api/labels: ${res.status()}`).toBeTruthy();
  return (await res.json()).labels;
}

export const decided = (rows: LabelRow[]) => rows.filter((r) => r.decision !== null && r.decision !== undefined);

// Every label is POSTed in the background; wait until the server has `n` decisions.
export async function waitForDecisions(request: APIRequestContext, base: string, query: string, n: number, headers: Record<string, string> = {}): Promise<LabelRow[]> {
  await expect.poll(async () => decided(await labels(request, base, query, headers)).length, { message: `${n} decisions saved (${query})` }).toBe(n);
  return decided(await labels(request, base, query, headers));
}

export async function tryDemo(page: Page, base: string): Promise<void> {
  await page.goto(`${base}/#/start`);
  await page.getByRole("button", { name: /try the demo/i }).click();
  await expect(page).toHaveURL(/#\/label/);
}

export const uniq = (prefix: string) => `${prefix}-${Date.now().toString(36)}${Math.floor(Math.random() * 1e4)}`;
