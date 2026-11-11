// Spec §1 + §3: CSV → wizard (connect, card with reorder, template labels with hotkey
// clash detection, workflow, review YAML) → start → label 10 → export CSV, then read the file.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test, expect, card, currentId, enterName, pressAndAdvance, waitForDecisions, uniq, type LabelRow } from "../fixtures/app.ts";
import { parseCsv, writeCsv } from "../fixtures/data.ts";
import { tempDir } from "../fixtures/server.ts";

const ID = /T-\d{4}/;

test("CSV wizard end to end: upload → card → labels → workflow → review → label 10 → export", async ({ page, shared, request }) => {
  const stem = uniq("tickets").replace(/-/g, "_");
  const csv = writeCsv(tempDir("caliper-e2e-csv"), `${stem}.csv`, 40);
  const t0 = Date.now();

  // ------------------------------------------------------------------ start → connect
  await page.goto(`${shared.base}/#/start`);
  await page.getByRole("button", { name: /new project/i }).click();
  await expect(page.getByRole("heading", { level: 1, name: /connect data/i })).toBeVisible();
  await expect(page.getByRole("radio", { name: /upload a file/i })).toBeChecked();
  const chooser = page.waitForEvent("filechooser");
  await page.getByRole("button", { name: /choose a file/i }).click();
  await (await chooser).setFiles(csv);

  await expect(page.getByText(/connected/i).filter({ visible: true }).first()).toBeVisible();
  await expect(page.getByText(/40 rows/).first()).toBeVisible();
  await expect(page.getByRole("combobox", { name: /item id column/i })).toHaveValue("ticket_id");
  const preview = page.getByRole("region", { name: /preview/i });
  await expect(preview.getByRole("columnheader")).toHaveCount(6);
  await expect(preview).toContainText("T-1000");
  await expect(preview).toContainText(/number|decimal|double/i); // detected types are shown

  // ------------------------------------------------------------------ card
  await page.getByRole("button", { name: /^next$/i }).click();
  await expect(page.getByRole("heading", { level: 1, name: /design the card/i })).toBeVisible();
  const onCard = page.getByRole("list").filter({ has: page.getByRole("textbox", { name: /^label for/i }) });
  const order = async () => (await onCard.getByRole("textbox", { name: /^label for/i }).evaluateAll((els) =>
    els.map((e) => ((e as HTMLInputElement).labels?.[0]?.textContent || e.getAttribute("aria-label") || "").replace(/^label for /i, ""))));
  const suggested = await order();
  expect(suggested, "the id column is not suggested").not.toContain("ticket_id");
  expect(suggested).toContain("subject");

  // click a column to add / remove it
  if (suggested.includes("amount")) await page.getByRole("button", { name: /^amount\b/ }).click();
  await page.getByRole("button", { name: /^amount\b/ }).click();
  expect(await order()).toContain("amount");

  // reorder by drag: amount to the top
  const rows = onCard.getByRole("listitem");
  const amountRow = rows.filter({ has: page.getByRole("textbox", { name: "Label for amount" }) });
  // drag by the handle: the row's centre can land on its text input, which does not start a drag
  await amountRow.locator(".wz-handle").dragTo(rows.first());
  await expect.poll(order).toEqual(expect.arrayContaining(["amount"]));
  expect((await order())[0], "drag moved amount to the top").toBe("amount");
  // ... and by button: move subject down one
  const before = await order();
  const si = before.indexOf("subject");
  await page.getByRole("button", { name: "Move subject down" }).click();
  const after = await order();
  expect(after[si + 1]).toBe("subject");
  await page.getByRole("textbox", { name: "Label for subject" }).fill("Subject line");

  // live preview shows the real first row
  const live = page.getByRole("region", { name: /live preview/i });
  await expect(live).toContainText("Row 1 of");
  await expect(live).toContainText("Refund not received");
  await live.getByRole("button", { name: /next row/i }).click();
  await expect(live).toContainText("Row 2 of");
  const cardOrder = await order();

  // ------------------------------------------------------------------ labels
  await page.getByRole("button", { name: /^next$/i }).click();
  await expect(page.getByRole("heading", { level: 1, name: /^labels$/i })).toBeVisible();
  await expect(page.getByRole("radio", { name: /single choice/i })).toBeChecked();
  await page.getByRole("button", { name: /true \/ uncertain \/ false/i }).click();
  const replace = page.getByRole("alertdialog", { name: /replace the options/i });
  if (await replace.isVisible()) await replace.getByRole("button", { name: /replace/i }).click();
  for (const n of ["True", "Uncertain", "False"]) await expect(page.getByRole("textbox", { name: `Hotkey for ${n}` })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Hotkey for True" })).toHaveValue("1");

  // clash: give Uncertain the key True already has
  const hk = page.getByRole("textbox", { name: "Hotkey for Uncertain" });
  await hk.focus();
  await page.keyboard.press("1");
  await expect(page.getByRole("alert").filter({ hasText: /same key/i })).toBeVisible();
  const next = page.getByRole("button", { name: /^next$/i });
  await expect(next).toHaveAttribute("aria-disabled", "true");
  await next.click({ force: true }); // aria-disabled: still clickable, explains why
  await expect(page.getByText(/fix the hotkey clashes/i).first()).toBeVisible();
  await expect(page.getByRole("heading", { level: 1, name: /^labels$/i }), "Next is blocked while keys clash").toBeVisible();
  // a reserved key is refused too
  await hk.focus();
  await page.keyboard.press("z");
  await expect(page.getByRole("alert").filter({ hasText: /reserved/i })).toBeVisible();
  await hk.focus();
  await page.keyboard.press("2");
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(next).toHaveAttribute("aria-disabled", "false");

  // ------------------------------------------------------------------ workflow
  await next.click();
  await expect(page.getByRole("heading", { level: 1, name: /workflow/i })).toBeVisible();
  await page.getByRole("radio", { name: /by a column/i }).check();
  await page.getByRole("combobox", { name: /queue column/i }).selectOption("priority");
  await page.getByRole("textbox", { name: /instructions/i }).fill("**Is the subject right?** Use *Uncertain* when two fit.");
  await page.getByRole("button", { name: /^review$/i }).click();

  // ------------------------------------------------------------------ review
  await expect(page.getByRole("heading", { level: 1, name: /review/i })).toBeVisible();
  const yaml = page.getByLabel(/project file that will be written/i);
  await expect(yaml).toContainText(`${stem}.csv`);
  await expect(yaml).toContainText("by: priority");
  await expect(yaml).toContainText("label: Uncertain");
  await expect(yaml).toContainText("label: Subject line");
  const name = `Wizard e2e ${stem}`;
  await page.getByRole("textbox", { name: /project name/i }).fill(name);
  await page.getByRole("button", { name: /create project/i }).click();

  // ------------------------------------------------------------------ label 10
  await expect(page).toHaveURL(/#\/label/, { timeout: 30_000 });
  const who = uniq("wiz");
  await enterName(page, who);
  await expect(card(page)).toContainText(ID);
  const firstLabel = (Date.now() - t0) / 1000;
  test.info().annotations.push({ type: "timing", description: `CSV → labeller in ${firstLabel.toFixed(1)}s (scripted)` });

  // the card keeps the order set in the wizard (stumble #6)
  const text = (await card(page).innerText()).toLowerCase();
  const pos = cardOrder.map((c) => text.indexOf(c === "subject" ? "subject line" : c));
  expect(pos.every((p) => p >= 0), `every card field is shown (${cardOrder.join(", ")})`).toBeTruthy();
  expect.soft(pos, "card fields in the wizard's order (usability stumble #6)").toEqual([...pos].sort((a, b) => a - b));

  // Uncertain needs a note (the template says so): the labeller asks, then saves.
  const keys = ["1", "2", "3", "1", "1", "2", "1", "3", "1", "3"];
  for (const k of keys) {
    if (k !== "2") { await pressAndAdvance(page, k, ID); continue; }
    const id = await currentId(page, ID);
    await page.keyboard.press(k);
    await expect(page.getByRole("textbox", { name: /note/i })).toBeFocused();
    await page.keyboard.type("two subjects fit");
    await page.keyboard.press("Enter");
    await expect.poll(() => currentId(page, ID)).not.toBe(id);
  }
  const saved: LabelRow[] = await waitForDecisions(request, shared.base, `labeller=${who}`, 10);

  // ------------------------------------------------------------------ export
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Export" }).click();
  await expect(page.getByRole("heading", { level: 1, name: /export/i })).toBeVisible();
  await page.getByRole("radio", { name: /\(csv\)/i }).check();
  const dl = page.waitForEvent("download");
  await page.getByRole("button", { name: /download.*csv/i }).click();
  const file = await dl;
  const out = join(test.info().outputDir, file.suggestedFilename() || "labels.csv");
  await file.saveAs(out);
  const rowsOut = parseCsv(readFileSync(out, "utf8"));
  expect(rowsOut, "one row per labelled item").toHaveLength(10);
  for (const col of ["item_id", "label", "labeller", "labelled_at", "note", "source", "stage", "project_version"]) {
    expect(Object.keys(rowsOut[0]), `export has a ${col} column`).toContain(col);
  }
  const want = Object.fromEntries(saved.map((r) => [r.item_id, r.decision]));
  for (const r of rowsOut) {
    expect(r.labeller).toBe(who);
    expect(r.label).toBe(want[r.item_id]);
  }
  expect(Object.keys(rowsOut[0]).some((c) => c.includes("subject")), "item fields ride along").toBeTruthy();
});
