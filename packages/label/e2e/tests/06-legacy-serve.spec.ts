// Back-compat (spec "Constraints"): `caliper label serve data.jsonl --config label.yaml`
// keeps working, writes the same JSONL label store, and the same UI serves it.

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { test, expect, card, enterName, pressAndAdvance } from "../fixtures/app.ts";
import { ROOT, tempDir } from "../fixtures/server.ts";

const ID = /lst-\d{4}/;

test("legacy serve data.jsonl --config label.yaml still labels, undoes and exports", async ({ page, launch, request }) => {
  const store = join(tempDir("caliper-e2e-legacy"), "labels.jsonl");
  const srv = await launch({
    args: ["serve", join(ROOT, "examples/catalog_cleanup/golden.jsonl"), "--config", join(ROOT, "examples/catalog_cleanup/label.yaml"), "--store", store],
  });
  const status = await (await request.get(`${srv.base}/api/status`)).json();
  expect(status.mode).toBe("legacy");

  await page.goto(`${srv.base}/`);
  await expect(page).toHaveURL(/#\/label/);
  await enterName(page, "Lea");
  await expect(card(page)).toContainText(ID);
  await expect(card(page).getByRole("img").first()).toBeVisible();
  await expect(page.getByRole("button", { name: /^1 code correct/i })).toBeVisible();
  // Settings is a project-only screen
  await expect(page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Settings" })).toBeHidden();

  const a = await pressAndAdvance(page, "1", ID);
  const b = await pressAndAdvance(page, "2", ID);
  await pressAndAdvance(page, "1", ID);
  await page.keyboard.press("Backspace"); // undo the third

  const read = () => existsSync(store) ? readFileSync(store, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l)) : [];
  await expect.poll(() => read().filter((r) => r.undoes).length, { message: "the undo is written to the store" }).toBe(1);
  const recs = read().filter((r) => r.decision && !r.undoes);
  expect(recs.slice(0, 2).map((r) => [r.item_id, r.decision, r.labeller])).toEqual([[a, "correct", "Lea"], [b, "wrong", "Lea"]]);

  const csv = await (await request.get(`${srv.base}/api/export/download?format=csv`)).text();
  expect(csv.split("\n")[0]).toContain("labeller");
  expect(csv).toContain(a);
  expect(csv).toContain(b);
});
