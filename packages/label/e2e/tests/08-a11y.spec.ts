// Spec §5 "Accessible: visible focus, contrast AA, screen-reader labels": axe-core
// (WCAG 2.1 A/AA) on every screen. Every violation is reported (attachment + summary file
// test-results/axe-summary.md); serious and critical ones fail the test, except nodes that
// match a known, filed bug (KNOWN, ids from handoff/lpf-c-bugs.md). Those are reported as
// annotations, never hidden: delete the entry when the bug is fixed.

import AxeBuilder from "@axe-core/playwright";
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { type Page } from "@playwright/test";
import { test, expect, card, enterName, tryDemo, uniq } from "../fixtures/app.ts";
import { writeCsv, writeProject } from "../fixtures/data.ts";
import { tempDir } from "../fixtures/server.ts";

const KNOWN: { bug: string; rule: string; target: RegExp }[] = [
  { bug: "LPF-C-1", rule: "color-contrast", target: /wz-btn-primary|#wz-next|#wz-create|#st-new/ },
  { bug: "LPF-C-2", rule: "label", target: /#file-\d+/ },
  { bug: "LPF-C-3", rule: "color-contrast", target: /\.wz-colbtn-type/ },
  { bug: "LPF-C-4", rule: "label-content-name-mismatch", target: /\.st-item-open|button\[aria-label="Open / },
  { bug: "LPF-C-6", rule: "label-content-name-mismatch", target: /\.opt|button\[data-value=|button\[aria-current=|Final label:/ },
  { bug: "LPF-C-7", rule: "scrollable-region-focusable", target: /\.instr-body/ },
  { bug: "LPF-C-8", rule: "color-contrast", target: /\.choice-text > \.tech/ },
];

type Found = { screen: string; id: string; impact: string; help: string; nodes: number; targets: string[]; detail: string };
const HERE = dirname(fileURLToPath(import.meta.url));
// A failed test restarts the worker (and this module), so results go to a file, not memory.
const OUT = join(HERE, "..", "test-results");
const LOG = join(OUT, "axe-found.jsonl");

async function scan(page: Page, screen: string) {
  // contrast is measured on the settled page: let entry animations / theme transitions finish
  // (finite ones only: loading skeletons loop forever)
  await page.evaluate(() => Promise.all(document.getAnimations()
    .filter((a) => Number.isFinite(Number(a.effect?.getTiming().iterations ?? 1)))
    .map((a) => a.finished.catch(() => null))));
  const res = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  mkdirSync(OUT, { recursive: true });
  for (const v of res.violations) {
    appendFileSync(LOG, JSON.stringify({ screen, id: v.id, impact: v.impact || "minor", help: v.help, nodes: v.nodes.length, targets: v.nodes.slice(0, 4).map((n) => n.target.join(" ")), detail: (v.nodes[0]?.any[0]?.message || v.nodes[0]?.failureSummary || "").split("\n")[0] } satisfies Found) + "\n");
  }
  await test.info().attach(`axe-${screen}.json`, { body: JSON.stringify(res.violations, null, 1), contentType: "application/json" });
  const bad: string[] = [];
  const known = new Set<string>();
  for (const v of res.violations.filter((x) => x.impact === "serious" || x.impact === "critical")) {
    for (const n of v.nodes) {
      const t = n.target.join(" ");
      const k = KNOWN.find((x) => x.rule === v.id && x.target.test(t));
      if (k) known.add(`${k.bug} ${v.id}`); else bad.push(`${v.id} (${v.impact}) ${t}: ${v.help}`);
    }
  }
  for (const k of known) test.info().annotations.push({ type: "known a11y bug", description: `${screen}: ${k}` });
  expect.soft(bad, `serious/critical axe violations on ${screen} (not a known bug)`).toEqual([]);
}


test.afterAll(() => {
  const found: Found[] = existsSync(LOG) ? readFileSync(LOG, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l)) : [];
  const lines = ["# axe-core results (WCAG 2.1 A/AA)", "", "| screen | rule | impact | nodes | help | first targets | detail (first node) |", "|---|---|---|---|---|---|---|",
    ...found.map((f) => `| ${f.screen} | ${f.id} | ${f.impact} | ${f.nodes} | ${f.help} | \`${f.targets.join("`, `")}\` | ${f.detail.replace(/\|/g, "/")} |`)];
  if (!found.length) lines.push("| (none) | | | | | | |");
  writeFileSync(join(OUT, "axe-summary.md"), lines.join("\n") + "\n");
  writeFileSync(join(OUT, "axe-summary.json"), JSON.stringify(found, null, 1));
});

test("axe: start screen and every wizard step", async ({ page, shared }) => {
  await page.goto(`${shared.base}/#/start`);
  await expect(page.getByRole("button", { name: /new project/i })).toBeVisible();
  await scan(page, "start");

  await page.getByRole("button", { name: /new project/i }).click();
  await expect(page.getByRole("heading", { level: 1, name: /connect data/i })).toBeVisible();
  await scan(page, "wizard-connect-empty");
  const chooser = page.waitForEvent("filechooser");
  await page.getByRole("button", { name: /choose a file/i }).click();
  await (await chooser).setFiles(writeCsv(tempDir("caliper-e2e-axe"), `${uniq("axe").replace(/-/g, "_")}.csv`, 12));
  await expect(page.getByText(/12 rows/).first()).toBeVisible();
  await scan(page, "wizard-connect");
  for (const [screen, h] of [["wizard-card", /design the card/i], ["wizard-labels", /^labels$/i], ["wizard-workflow", /workflow/i], ["wizard-review", /review/i]] as const) {
    await page.getByRole("button", { name: /^(next|review)$/i }).click();
    await expect(page.getByRole("heading", { level: 1, name: h })).toBeVisible();
    if (screen === "wizard-review") await expect(page.getByLabel(/project file that will be written/i)).toBeVisible();
    await scan(page, screen);
  }
});

test("axe: labeller (demo, light and dark), help and zoom overlays", async ({ page, shared }) => {
  await tryDemo(page, shared.base);
  await expect(page.getByRole("dialog", { name: /who is labelling/i })).toBeVisible();
  await scan(page, "name-dialog");
  await enterName(page, uniq("axe"));
  await expect(card(page)).toContainText(/lst-\d{4}/);
  await scan(page, "labeller");
  await page.emulateMedia({ colorScheme: "dark" });
  await scan(page, "labeller-dark");
  await page.emulateMedia({ colorScheme: "light" });
  await page.keyboard.press("?");
  await expect(page.getByRole("dialog", { name: /keyboard shortcuts/i })).toBeVisible();
  await scan(page, "help-overlay");
  await page.keyboard.press("Escape");
  await page.keyboard.press("z");
  await expect(page.getByRole("dialog", { name: /image, full size/i })).toBeVisible();
  await scan(page, "zoom-overlay");
});

test("axe: dashboard, adjudication, export, settings, token gate", async ({ page, launch, request }) => {
  const dir = writeProject(join(tempDir("caliper-e2e-axe-team"), "triage"), { doubleLabel: 2 });
  const srv = await launch({ args: [dir] });
  const votes: [string, string, string][] = [["T-1000", "correct", "correct"], ["T-1001", "correct", "wrong"], ["T-1002", "wrong", "wrong"], ["T-1003", "correct", "wrong"]];
  for (const [id, a, b] of votes) {
    for (const [who, d] of [["Ana", a], ["Ben", b]]) {
      const r = await request.post(`${srv.base}/api/labels`, { data: { item_id: id, labeller: who, decision: d } });
      expect(r.ok(), await r.text()).toBeTruthy();
    }
  }
  await page.goto(`${srv.base}/#/dashboard`);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.getByRole("region", { name: "Agreement", exact: true })).toBeVisible();
  await scan(page, "dashboard");
  await page.goto(`${srv.base}/#/adjudicate`);
  await expect(page.getByRole("list", { name: /what each labeller said/i })).toBeVisible();
  await scan(page, "adjudicate");
  await page.goto(`${srv.base}/#/export`);
  await expect(page.getByRole("button", { name: /download.*csv/i })).toBeVisible();
  await scan(page, "export");
  for (const tab of ["general", "data", "card", "labels", "workflow", "file"]) {
    await page.goto(`${srv.base}/#/settings/${tab}`);
    await expect(page.getByRole("tablist", { name: /settings sections/i }).getByRole("tab", { selected: true })).toBeVisible();
    await scan(page, `settings-${tab}`);
  }

  const team = await launch({ args: [writeProject(join(tempDir("caliper-e2e-axe-gate"), "triage")), "--auth", "token"], env: { CALIPER_LABEL_TOKEN: "axe-only-token" } });
  await page.goto(`${team.base}/`);
  await expect(page.getByRole("heading", { name: /access token needed/i })).toBeVisible();
  await scan(page, "token-gate");
});
