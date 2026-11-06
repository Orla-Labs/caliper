// Founder screenshots (not a test of behaviour). Gated: E2E_SCREENSHOTS=1.
// Writes docs/label-product/*.png at 1440x900, light mode, fictional data only.

import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { type Page } from "@playwright/test";
import { test, expect, card, enterName, pressAndAdvance, waitForDecisions } from "../fixtures/app.ts";
import { ROOT, tempDir } from "../fixtures/server.ts";

test.skip(process.env.E2E_SCREENSHOTS !== "1", "set E2E_SCREENSHOTS=1 to write docs/label-product/*.png");
test.use({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, colorScheme: "light" });

const OUT = join(ROOT, "docs", "label-product");
const ID = /lst-\d{4}/;

async function shot(page: Page, name: string) {
  mkdirSync(OUT, { recursive: true });
  // let fonts, images and transitions settle: wait for images to decode and two frames
  await page.evaluate(async () => {
    await document.fonts.ready;
    await Promise.all([...document.images].map((i) => (i.complete ? null : i.decode().catch(() => null))));
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  });
  await page.mouse.move(0, 0);
  await page.screenshot({ path: join(OUT, name), animations: "disabled", caret: "hide" });
}

// The demo catalog as a double-labelled project (paths absolute, nothing written to examples/).
function demoProject(doubleLabel: number): string {
  const dir = join(tempDir("caliper-shots"), "catalog-review");
  mkdirSync(dir, { recursive: true });
  const src = readFileSync(join(ROOT, "examples/label_demo/caliper.label.yaml"), "utf8")
    .replace("path: ../catalog_cleanup/golden.jsonl", `path: ${join(ROOT, "examples/catalog_cleanup/golden.jsonl")}`)
    .replace("media_roots: [../catalog_cleanup]", `media_roots: [${join(ROOT, "examples/catalog_cleanup")}]`)
    .replace("double_label: 1", `double_label: ${doubleLabel}`)
    .replace("name: Catalog cleanup · tax-code review (demo)", "name: Catalog cleanup · tax-code review");
  writeFileSync(join(dir, "caliper.label.yaml"), src);
  return dir;
}

const TICKETS = [
  ["T-2041", "Refund still not received after 14 days", "I returned the Lumen desk lamp on the 3rd and the refund has not arrived.", "Lumen Lamp", "high", "2026-10-28T09:14:00Z"],
  ["T-2042", "App logs me out every time I open it", "Since the last update the app asks me to sign in again on every launch.", "Pocket Kettle app", "medium", "2026-10-28T10:02:00Z"],
  ["T-2043", "Wrong size delivered", "I ordered the large Trail Mug and received the small one.", "Trail Mug", "low", "2026-10-28T11:47:00Z"],
  ["T-2044", "Promo code AUTUMN10 rejected at checkout", "The code from your newsletter says it is not valid.", "Cloud Pillow", "medium", "2026-10-29T08:21:00Z"],
  ["T-2045", "Parcel left at the wrong door", "Tracking says delivered but it was left at number 41, not 14.", "Lumen Lamp", "high", "2026-10-29T13:05:00Z"],
  ["T-2046", "Kettle stops before the water boils", "The Pocket Kettle switches off after about a minute.", "Pocket Kettle", "urgent", "2026-10-30T07:40:00Z"],
];

test("founder screenshots", async ({ page, browser, launch, request }) => {
  test.setTimeout(240_000);

  // ------------------------------------------------------------ 01 start
  const srv = await launch();
  await page.goto(`${srv.base}/#/start`);
  await expect(page.getByRole("button", { name: /try the demo/i })).toBeVisible();
  await shot(page, "01-start.png");

  // ------------------------------------------------------------ 02-06 wizard
  const dir = tempDir("caliper-shots-csv");
  const csv = join(dir, "support_tickets.csv");
  const q = (v: string) => (/[",]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v);
  writeFileSync(csv, ["ticket_id,subject,message,product,priority,opened_at", ...Array.from({ length: 60 }, (_, i) => {
    const t = TICKETS[i % TICKETS.length];
    return [`T-${2041 + i}`, t[1], t[2], t[3], t[4], t[5]].map(q).join(",");
  })].join("\n") + "\n");
  await page.getByRole("button", { name: /new project/i }).click();
  const chooser = page.waitForEvent("filechooser");
  await page.getByRole("button", { name: /choose a file/i }).click();
  await (await chooser).setFiles(csv);
  await expect(page.getByText(/60 rows/).first()).toBeVisible();
  // show the result (connected summary + preview), with the source picker just above
  await page.getByText(/where is your data/i).evaluate((e) => e.scrollIntoView({ block: "start" }));
  await page.evaluate(() => window.scrollBy(0, 230));
  await shot(page, "02-wizard-connect.png");

  await page.getByRole("button", { name: /^next$/i }).click();
  await expect(page.getByRole("heading", { level: 1, name: /design the card/i })).toBeVisible();
  await shot(page, "03-wizard-card.png");

  await page.getByRole("button", { name: /^next$/i }).click();
  await expect(page.getByRole("heading", { level: 1, name: /^labels$/i })).toBeVisible();
  await expect(page.getByRole("textbox", { name: /^hotkey for/i }).first()).toBeVisible();
  await shot(page, "04-wizard-labels.png");

  await page.getByRole("button", { name: /^next$/i }).click();
  await expect(page.getByRole("heading", { level: 1, name: /workflow/i })).toBeVisible();
  await page.getByRole("radio", { name: /by a column/i }).check();
  await page.getByRole("combobox", { name: /queue column/i }).selectOption("priority");
  await page.getByRole("button", { name: /more labellers per item/i }).click();
  await page.getByRole("textbox", { name: /instructions/i }).fill("**Does the subject match the message?**\n\nUse *Unsure* when the message could fit two subjects, and say why in the note.");
  await shot(page, "05-wizard-workflow.png");

  await page.getByRole("button", { name: /^review$/i }).click();
  await expect(page.getByLabel(/project file that will be written/i)).toContainText("priority");
  await page.getByRole("textbox", { name: /project name/i }).fill("Support tickets · subject check");
  // a tidy, fictional folder for the picture (nothing is created: Create is never pressed)
  await page.getByRole("textbox", { name: /save in folder/i }).fill("/Users/ana/caliper-projects/support-tickets-subject-check");
  await expect(page.getByLabel(/project file that will be written/i)).toContainText("subject check");
  await shot(page, "06-wizard-review.png");

  // ------------------------------------------------------------ 07 labeller (demo catalog, with images)
  const team = await launch({ args: [demoProject(2)] });
  await page.goto(`${team.base}/#/label`);
  await enterName(page, "Ana");
  await expect(card(page)).toContainText(ID);
  for (const k of ["1", "1", "2"]) await pressAndAdvance(page, k, ID);
  await waitForDecisions(request, team.base, "labeller=Ana", 3);
  await page.reload(); // a calm screen: no pressed / saving states left over
  await expect(card(page)).toContainText(ID);
  await expect(card(page).getByRole("img").first()).toBeVisible();
  await shot(page, "07-labeller.png");

  // ------------------------------------------------------------ 08-10 two labellers, some disagreement
  // Ana's first 3 are in; the rest go through the public REST API (the "Script it" path).
  const golden = readFileSync(join(ROOT, "examples/catalog_cleanup/golden.jsonl"), "utf8").trim().split("\n").map((l) => JSON.parse(l));
  const post = async (item_id: string, labeller: string, decision: string, note?: string) => {
    const r = await request.post(`${team.base}/api/labels`, { data: { item_id, labeller, decision, note: note ?? null } });
    expect(r.ok(), await r.text()).toBeTruthy();
  };
  const verdict = (g: { metadata: { production_tax_code: string; true_tax_code: string } }) =>
    g.metadata.production_tax_code === g.metadata.true_tax_code ? "correct" : "wrong";
  const anaDone = new Set((await waitForDecisions(request, team.base, "labeller=Ana", 3)).map((r) => r.item_id));
  for (const [i, g] of golden.slice(0, 32).entries()) {
    const right = verdict(g);
    const other = right === "correct" ? "wrong" : "correct";
    if (!anaDone.has(g.id)) await post(g.id, "Ana", i % 11 === 7 ? "unsure" : right, i % 11 === 7 ? "Could be CTX-420 or CTX-430" : undefined);
    if (i < 26) await post(g.id, "Ben", i % 6 === 2 ? other : right);
  }
  await page.goto(`${team.base}/#/dashboard`);
  await expect(page.getByRole("region", { name: "Agreement", exact: true })).toBeVisible();
  await shot(page, "08-dashboard.png");

  await page.goto(`${team.base}/#/adjudicate`);
  await expect(page.getByRole("list", { name: /what each labeller said/i })).toBeVisible();
  await expect(page.getByRole("article").getByRole("img").first()).toBeVisible();
  await shot(page, "09-adjudicate.png");
  await page.getByRole("button", { name: /code correct, set as the final label/i }).click();
  await expect(page.getByRole("status").filter({ hasText: /final label/i }).first()).toBeAttached();
  await expect(page.getByText(/: final label /)).toBeHidden({ timeout: 15_000 }); // let the toast go

  await page.goto(`${team.base}/#/export`);
  await expect(page.getByRole("button", { name: /download.*csv/i })).toBeVisible();
  await expect(page.getByRole("complementary", { name: /what the export holds/i })).toContainText(/\d/);
  await shot(page, "10-export.png");

  // ------------------------------------------------------------ 11 dark, 12 tablet
  const darkCtx = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: "dark" });
  const dark = await darkCtx.newPage();
  await dark.goto(`${team.base}/#/label`);
  await enterName(dark, "Ben");
  await expect(card(dark)).toContainText(ID);
  await expect(card(dark).getByRole("img").first()).toBeVisible();
  await shot(dark, "11-labeller-dark.png");
  await darkCtx.close();

  const tabCtx = await browser.newContext({ viewport: { width: 820, height: 1180 }, deviceScaleFactor: 1, colorScheme: "light", hasTouch: true, isMobile: true });
  const tab = await tabCtx.newPage();
  await tab.goto(`${team.base}/#/label`);
  await enterName(tab, "Caro");
  await expect(card(tab)).toContainText(ID);
  await expect(card(tab).getByRole("img").first()).toBeVisible();
  await shot(tab, "12-labeller-tablet.png");
  await tabCtx.close();
});
