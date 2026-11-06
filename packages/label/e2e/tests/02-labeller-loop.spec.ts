// Spec §5 keyboard-first labelling, on the demo project (images, a requires-note option):
// undo, skip, requires-note and image zoom (Z / Esc), each also clickable.

import { test, expect, card, currentId, enterName, labels, decided, pressAndAdvance, tryDemo, uniq, waitForDecisions } from "../fixtures/app.ts";

const ID = /lst-\d{4}/;

test.beforeEach(async ({ page, shared }) => {
  await tryDemo(page, shared.base);
  await enterName(page, uniq("loop"));
  await expect(card(page)).toContainText(ID);
});

test("Z opens the image full size, Esc closes it; clicking the image works too", async ({ page }) => {
  const zoom = page.getByRole("dialog", { name: /image, full size/i });
  await page.keyboard.press("z");
  await expect(zoom).toBeVisible();
  await expect(zoom.getByRole("img")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(zoom).toBeHidden();

  await card(page).getByRole("button", { name: /view full size/i }).first().click();
  await expect(zoom).toBeVisible();
  // a decision key still works while zoomed, and zoom follows to the next item
  await pressAndAdvance(page, "2", ID);
  await expect(zoom).toBeVisible();
  await page.keyboard.press("z");
  await expect(zoom).toBeHidden();
});

test("S skips, Backspace undoes the skip and brings the item back", async ({ page }) => {
  const skipped = await pressAndAdvance(page, "s", ID);
  await page.keyboard.press("Backspace");
  await expect.poll(() => currentId(page, ID)).toBe(skipped);
  await expect(page.getByRole("status").filter({ hasText: /undid skip/i }).first()).toBeAttached();
});

test("undo retracts the last decision (key and button)", async ({ page, request, shared }) => {
  const who = await page.getByRole("button", { name: /labelling as/i }).innerText();
  const first = await pressAndAdvance(page, "1", ID);
  await waitForDecisions(request, shared.base, `labeller=${who.trim()}`, 1);
  await page.keyboard.press("Backspace");
  await expect.poll(() => currentId(page, ID)).toBe(first);
  await waitForDecisions(request, shared.base, `labeller=${who.trim()}`, 0);

  // the same with the mouse
  await page.getByRole("button", { name: /wrong code/i }).click();
  await expect.poll(() => currentId(page, ID)).not.toBe(first);
  await page.getByRole("button", { name: "Undo", exact: true }).click();
  await expect.poll(() => currentId(page, ID)).toBe(first);
});

test("an option that requires a note asks for one before saving", async ({ page, request, shared }) => {
  const who = (await page.getByRole("button", { name: /labelling as/i }).innerText()).trim();
  const id = await currentId(page, ID);
  await expect(page.getByRole("button", { name: /unsure.*needs a note/i })).toBeVisible();
  await page.keyboard.press("3");
  const note = page.getByRole("textbox", { name: /note/i });
  await expect(note).toBeFocused();
  await expect(page.getByText(/needs a note/i).first()).toBeVisible();
  expect(await currentId(page, ID), "still on the same item").toBe(id);
  expect(decided(await labels(request, shared.base, `labeller=${who}`))).toHaveLength(0);

  // typing in the note does not fire shortcuts
  await page.keyboard.type("two codes fit 1 2 s z");
  await expect(page.getByRole("dialog", { name: /image, full size/i })).toBeHidden();
  expect(await currentId(page, ID)).toBe(id);
  await page.keyboard.press("Enter");
  await expect.poll(() => currentId(page, ID)).not.toBe(id);

  const [rec] = await waitForDecisions(request, shared.base, `labeller=${who}`, 1);
  expect(rec).toMatchObject({ item_id: id, decision: "unsure", note: "two codes fit 1 2 s z" });
});

test("N adds a note to the next decision; ? shows the shortcuts", async ({ page, request, shared }) => {
  const who = (await page.getByRole("button", { name: /labelling as/i }).innerText()).trim();
  await page.keyboard.press("?");
  const help = page.getByRole("dialog", { name: /keyboard shortcuts/i });
  await expect(help).toBeVisible();
  await expect(help.getByRole("table")).toContainText(/undo/i);
  await page.keyboard.press("Escape");
  await expect(help).toBeHidden();

  await page.keyboard.press("n");
  await expect(page.getByRole("textbox", { name: /note/i })).toBeFocused();
  await page.keyboard.type("looks right");
  await page.keyboard.press("Escape");
  const id = await pressAndAdvance(page, "1", ID);
  const [rec] = await waitForDecisions(request, shared.base, `labeller=${who}`, 1);
  expect(rec).toMatchObject({ item_id: id, decision: "correct", note: "looks right" });
});
