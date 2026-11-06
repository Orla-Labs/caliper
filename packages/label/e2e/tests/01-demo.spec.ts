// Spec §0 "Fast first run": the start screen offers New / Open / Try the demo, and the
// demo goes straight to labelling from the keyboard.

import { test, expect, card, enterName, pressAndAdvance, waitForDecisions, tryDemo, uniq } from "../fixtures/app.ts";

const ID = /lst-\d{4}/;

test("start screen → Try the demo → label 5 items with the keys", async ({ page, shared, request }) => {
  await page.goto(`${shared.base}/`);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  for (const name of [/new project/i, /open project/i, /try the demo/i]) {
    await expect(page.getByRole("button", { name })).toBeVisible();
  }

  await tryDemo(page, shared.base);
  const who = uniq("demo");
  await enterName(page, who);
  await expect(card(page)).toContainText(ID);
  await expect(page.getByRole("group", { name: "Decision" }).getByRole("button")).toHaveCount(3);
  await expect(page.getByRole("button", { name: /^1 code correct/i })).toBeVisible();

  const keys = ["1", "2", "1", "1", "2"];
  const ids: string[] = [];
  for (const k of keys) ids.push(await pressAndAdvance(page, k, ID));
  expect(new Set(ids).size, "five different items").toBe(5);

  const saved = await waitForDecisions(request, shared.base, `labeller=${who}`, 5);
  const byItem = Object.fromEntries(saved.map((r) => [r.item_id, r.decision]));
  ids.forEach((id, i) => expect(byItem[id], `decision for ${id}`).toBe(keys[i] === "1" ? "correct" : "wrong"));

  // the screen says it saved (live region), and progress moved
  await expect(page.getByRole("status").filter({ hasText: /saved/i }).first()).toBeAttached();
  await expect(page.getByRole("progressbar", { name: /items complete/i })).toBeVisible();
});
