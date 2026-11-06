// Spec §1 "Project settings": changing label options after labels exist. Renaming keeps
// history; removing an option that has labels needs confirmation, and its labels are kept
// but flagged (retired).

import { join } from "node:path";
import { test, expect, enterName, card, labels, decided } from "../fixtures/app.ts";
import { parseCsv } from "../fixtures/data.ts";
import { writeProject } from "../fixtures/data.ts";
import { tempDir } from "../fixtures/server.ts";

test("settings: rename keeps history; removing an option needs confirmation, keeps its labels and marks the option retired", async ({ page, launch, request }) => {
  const dir = writeProject(join(tempDir("caliper-e2e-proj"), "triage"));
  const srv = await launch({ args: [dir] });

  // a few labels on "correct" and "wrong" (through the public REST API)
  const ids = ["T-1000", "T-1001", "T-1002", "T-1003"];
  const want = ["correct", "correct", "wrong", "wrong"];
  for (let i = 0; i < ids.length; i++) {
    const r = await request.post(`${srv.base}/api/labels`, { data: { item_id: ids[i], labeller: "Rui", decision: want[i] } });
    expect(r.ok(), await r.text()).toBeTruthy();
  }

  await page.goto(`${srv.base}/#/label`);
  await enterName(page, "Mia");
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Settings" }).click();
  await page.getByRole("tab", { name: /^labels/i }).click();

  // ---------------------------------------------------------------- rename
  const name0 = page.getByRole("textbox", { name: "Name of option 1" });
  await expect(name0).toHaveValue("Correct");
  await expect(page.getByRole("textbox", { name: /stored value of correct/i }), "a value with labels is locked").toBeDisabled();
  await name0.fill("Right subject");
  await expect(page.getByText(/unsaved changes/i)).toBeVisible();
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText(/saved · revision 2/i).first()).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: /renamed \(history kept\)/i }).first()).toBeAttached();

  const after = decided(await labels(request, srv.base, "labeller=Rui"));
  expect(after.map((r) => r.decision).sort(), "the labels keep their stored value").toEqual([...want].sort());

  // ---------------------------------------------------------------- remove (cancel, then confirm)
  await page.getByRole("button", { name: "Remove Wrong" }).click();
  let ask = page.getByRole("alertdialog", { name: /remove .wrong.\?/i });
  await expect(ask).toBeVisible();
  await expect(ask).toContainText(/2 labels/);
  await ask.getByRole("button", { name: /cancel/i }).click();
  await expect(ask).toBeHidden();
  await expect(page.getByRole("textbox", { name: "Hotkey for Wrong" })).toBeEnabled();

  await page.getByRole("button", { name: "Remove Wrong" }).click();
  ask = page.getByRole("alertdialog", { name: /remove .wrong.\?/i });
  await ask.getByRole("button", { name: /retire/i }).click();
  await expect(page.getByRole("row").filter({ hasText: /retired/i })).toHaveCount(1);
  await page.getByRole("button", { name: "Save", exact: true }).click();
  const confirmSave = page.getByRole("alertdialog", { name: /options that have labels/i });
  await expect(confirmSave).toBeVisible();
  await expect(confirmSave).toContainText("wrong");
  await confirmSave.getByRole("button", { name: /retire and save/i }).click();
  await expect(page.getByText(/saved · revision 3/i).first()).toBeVisible();

  // the file on disk says so
  await page.getByRole("tab", { name: /config file/i }).click();
  const yaml = page.getByLabel("caliper.label.yaml");
  await expect(yaml).toContainText("label: Right subject");
  await expect(yaml).toContainText(/value: wrong[\s\S]*retired: true/);
  await expect(yaml).toContainText("revision: 3");
  await expect(yaml).toContainText("# fictional e2e project"); // hand-written comments survive

  // labels kept ...
  const kept = decided(await labels(request, srv.base, "labeller=Rui"));
  expect(kept.filter((r) => r.decision === "wrong"), "labels on the retired option are kept").toHaveLength(2);
  // ... flagged: the option is marked retired wherever labels are read
  const cfg = await (await request.get(`${srv.base}/api/label-config`)).json();
  expect(cfg.options.find((o: { value: string }) => o.value === "wrong")).toMatchObject({ retired: true });

  // ... and the option left the labelling screen, the rename shows up there
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Label" }).click();
  await expect(card(page)).toBeVisible();
  const decisions = page.getByRole("group", { name: "Decision" });
  await expect(decisions.getByRole("button", { name: /right subject/i })).toBeVisible();
  await expect(decisions.getByRole("button", { name: /wrong/i })).toHaveCount(0);
});

// Spec §1 / CONTRACT: "remove → confirm, labels kept but flagged `retired`". The labels are kept
// and the option is marked retired, but neither the label records nor the export rows say so.
test("LPF-C-9: labels on a retired option are flagged in the export", async ({ launch, request }) => {
  const dir = writeProject(join(tempDir("caliper-e2e-proj"), "triage"));
  const srv = await launch({ args: [dir] });
  for (const id of ["T-1000", "T-1001"]) {
    expect((await request.post(`${srv.base}/api/labels`, { data: { item_id: id, labeller: "Rui", decision: id === "T-1000" ? "correct" : "wrong" } })).ok()).toBeTruthy();
  }
  const cur = await (await request.get(`${srv.base}/api/settings`)).json();
  const cfg = structuredClone(cur.config);
  cfg.labels.options.find((o: { value: string }) => o.value === "wrong").retired = true;
  const put = await request.put(`${srv.base}/api/settings`, { data: { config: cfg, revision: cur.revision, confirm: true } });
  expect(put.ok(), await put.text()).toBeTruthy();

  const rows = parseCsv(await (await request.get(`${srv.base}/api/export/download?format=csv`)).text());
  const row = rows.find((r) => r.item_id === "T-1001");
  expect(row, "the label on the retired option is kept").toBeTruthy();
  expect(Object.entries(row!).some(([k, v]) => /retired/i.test(k) ? /true|1|yes/i.test(v) : /retired/i.test(v)), "the export row says the option is retired").toBeTruthy();
});
