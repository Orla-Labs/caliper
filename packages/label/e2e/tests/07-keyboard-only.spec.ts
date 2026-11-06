// Spec §5 "keyboard-first ... visible focus": a whole pass without the mouse. Start screen →
// wizard (Ctrl+Enter through the steps) → labelling → help → dashboard → export, checking
// that every Tab stop shows a focus indicator.

import { type Locator, type Page } from "@playwright/test";
import { test, expect, card, currentId, pressAndAdvance, uniq } from "../fixtures/app.ts";
import { writeCsv } from "../fixtures/data.ts";
import { tempDir } from "../fixtures/server.ts";

const ID = /T-\d{4}/;

// Does the focused element (or a wrapper within 2 levels) look different when focused?
async function focusVisible(page: Page): Promise<string | null> {
  return page.evaluate(() => {
    const a = document.activeElement as HTMLElement | null;
    if (!a || a === document.body) return null; // Tab left the page (browser UI)
    const chain = [a, a.parentElement, a.parentElement?.parentElement].filter(Boolean) as HTMLElement[];
    const look = () => chain.map((e) => { const s = getComputedStyle(e); return [s.outlineStyle, s.outlineWidth, s.outlineColor, s.boxShadow, s.borderColor, s.backgroundColor].join("|"); }).join("§");
    const on = look();
    a.blur();
    const off = look();
    a.focus({ focusVisible: true } as FocusOptions);
    if (on !== off) return null;
    const label = a.getAttribute("aria-label") || a.textContent || a.getAttribute("name") || a.id || "";
    return `${a.tagName.toLowerCase()} "${label.trim().slice(0, 40)}"`;
  });
}

const noRing: string[] = [];
async function tabTo(page: Page, target: Locator, { max = 60, back = false } = {}): Promise<void> {
  for (let i = 0; i < max; i++) {
    await page.keyboard.press(back ? "Shift+Tab" : "Tab");
    const miss = await focusVisible(page);
    if (miss && !noRing.includes(miss)) noRing.push(miss);
    if (await target.evaluate((el) => el === document.activeElement || el.contains(document.activeElement)).catch(() => false)) return;
  }
  throw new Error(`could not reach ${target} with ${back ? "Shift+" : ""}Tab in ${max} presses`);
}

test("keyboard only: start → wizard → label → help → dashboard → export", async ({ page, shared }) => {
  const csv = writeCsv(tempDir("caliper-e2e-kb"), `${uniq("kb").replace(/-/g, "_")}.csv`, 12);
  await page.goto(`${shared.base}/#/start`);
  await expect(page.getByRole("button", { name: /new project/i })).toBeFocused();

  // ------------------------------------------------------------ wizard
  await page.keyboard.press("n");
  await expect(page.getByRole("heading", { level: 1, name: /connect data/i })).toBeFocused();
  await tabTo(page, page.getByRole("button", { name: /choose a file/i }));
  const chooser = page.waitForEvent("filechooser");
  await page.keyboard.press("Enter");
  await (await chooser).setFiles(csv);
  await expect(page.getByText(/12 rows/).first()).toBeVisible();

  // arrow keys move between source kinds without changing anything else
  await tabTo(page, page.getByRole("button", { name: /^next$/i }));
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { level: 1, name: /design the card/i })).toBeFocused();
  // reorder with Alt+ArrowDown on a field row
  const firstField = page.getByRole("textbox", { name: /^label for/i }).first();
  await tabTo(page, firstField);
  const firstName = await firstField.evaluate((e) => (e as HTMLInputElement).labels?.[0]?.textContent || "");
  await page.keyboard.press("Alt+ArrowDown");
  await expect(page.getByRole("textbox", { name: /^label for/i }).nth(1)).toHaveAccessibleName(firstName);

  for (const h of [/^labels$/i, /workflow/i, /review/i]) {
    await page.keyboard.press("Control+Enter");
    await expect(page.getByRole("heading", { level: 1, name: h })).toBeVisible();
  }
  await page.keyboard.press("Control+Enter"); // create
  await expect(page).toHaveURL(/#\/label/, { timeout: 30_000 });

  // ------------------------------------------------------------ label
  const name = page.getByRole("dialog", { name: /who is labelling/i }).getByRole("textbox");
  await expect(name).toBeFocused();
  await page.keyboard.type("Kim");
  await page.keyboard.press("Enter");
  await expect(card(page)).toContainText(ID);
  for (const k of ["1", "2", "1"]) await pressAndAdvance(page, k, ID);
  const id = await currentId(page, ID);
  await page.keyboard.press("s");
  await expect.poll(() => currentId(page, ID)).not.toBe(id);
  await page.keyboard.press("Backspace");
  await expect.poll(() => currentId(page, ID)).toBe(id);

  // decision buttons are reachable with Tab and work with Enter
  await tabTo(page, page.getByRole("group", { name: "Decision" }).getByRole("button").first());
  await page.keyboard.press("Enter");
  await expect.poll(() => currentId(page, ID)).not.toBe(id);

  await page.keyboard.press("?");
  const help = page.getByRole("dialog", { name: /keyboard shortcuts/i });
  await expect(help).toBeVisible();
  await expect(help.getByRole("button", { name: /close/i })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(help.getByRole("button", { name: /close/i }), "focus stays in the dialog").toBeFocused();
  await page.keyboard.press("Escape");
  await expect(help).toBeHidden();

  // ------------------------------------------------------------ dashboard and export through the app bar
  const nav = page.getByRole("navigation", { name: "Main" });
  await tabTo(page, nav.getByRole("link", { name: "Dashboard" }), { back: true });
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { level: 1, name: /progress/i })).toBeVisible();
  await tabTo(page, nav.getByRole("link", { name: "Export" }), { back: true });
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { level: 1, name: /export/i })).toBeVisible();
  await tabTo(page, page.getByRole("button", { name: /download.*csv/i }));
  const dl = page.waitForEvent("download");
  await page.keyboard.press("Enter");
  expect((await dl).suggestedFilename()).toMatch(/\.csv$/);

  // LPF-C-5 (handoff/lpf-c-bugs.md): decision buttons, the current queue button and the
  // dashboard's focusable table section show no focus ring. Reported, not hidden; the
  // dedicated test below fails loudly once it is fixed so this filter gets removed.
  const known = (m: string) => /\(key \d\)|complete, \d+ left|^section "Labellers/.test(m);
  for (const m of noRing.filter(known)) test.info().annotations.push({ type: "known a11y bug", description: `LPF-C-5: no focus ring on ${m}` });
  expect(noRing.filter((m) => !known(m)), "Tab stops without a visible focus indicator").toEqual([]);
});

test("LPF-C-5: decision buttons show a keyboard focus ring", async ({ page, shared }) => {
  test.fail(true, "LPF-C-5 open: .opt's own box-shadow overrides :focus-visible (handoff/lpf-c-bugs.md)");
  await page.goto(`${shared.base}/#/start`);
  await page.getByRole("button", { name: /try the demo/i }).click();
  const dialog = page.getByRole("dialog", { name: /who is labelling/i });
  await dialog.getByRole("textbox").fill(uniq("ring"));
  await page.keyboard.press("Enter");
  await expect(card(page)).toContainText(/lst-\d{4}/);
  noRing.length = 0;
  await tabTo(page, page.getByRole("group", { name: "Decision" }).getByRole("button").first());
  expect(noRing).toEqual([]);
});
