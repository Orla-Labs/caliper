// End-to-end browser check for project setup (dev 2; optional, not part of pytest).
//
//   PLAYWRIGHT_CORE=/path/to/node_modules/playwright-core \
//     node packages/label/e2e/setup_e2e.mjs [--out DIR] [--port 8783]
//
// Starts `caliper label` (start screen) in a temp folder, then in Chromium:
//   start screen -> New project -> upload a fictional CSV -> card (click a column, reorder)
//   -> labels (hotkey clash shown, then fixed) -> workflow -> review -> create -> first label
//   -> settings: rename an option and retire one that has labels -> YAML shows both.
// Also opens the demo. Prints the CSV -> first label time. Exits 0 with "skipped" when
// playwright-core cannot be found.

import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const arg = (name, def) => {
  const i = process.argv.indexOf(name);
  return i > 0 ? process.argv[i + 1] : def;
};
const PORT = Number(arg("--port", "8783"));
const OUT = resolve(arg("--out", join(tmpdir(), "caliper-label-setup-e2e")));
const BASE = `http://127.0.0.1:${PORT}`;

let chromium;
try {
  ({ chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_CORE || "playwright-core"));
} catch {
  console.log("skipped: playwright-core not found (set PLAYWRIGHT_CORE to its folder)");
  process.exit(0);
}

function findChromium() {
  if (process.env.CHROMIUM_PATH) return process.env.CHROMIUM_PATH;
  const cache = process.env.PLAYWRIGHT_BROWSERS_PATH || join(homedir(), "Library/Caches/ms-playwright");
  if (!existsSync(cache)) return undefined;
  const builds = readdirSync(cache).filter((d) => /^chromium-\d+$/.test(d))
    .sort((a, b) => Number(b.split("-")[1]) - Number(a.split("-")[1]));
  for (const b of builds) {
    for (const p of [
      "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
      "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
      "chrome-linux/chrome",
    ]) {
      const exe = join(cache, b, p);
      if (existsSync(exe)) return exe;
    }
  }
  return undefined;
}

const check = (cond, msg) => {
  if (!cond) throw new Error(`FAIL ${msg}`);
  console.log(`  ok  ${msg}`);
};

mkdirSync(OUT, { recursive: true });
const work = mkdtempSync(join(tmpdir(), "caliper-label-setup-"));
const home = join(work, "home");
const csv = join(work, "tickets.csv");
const subjects = ["Refund not received", "App crashes on login", "Wrong size delivered", "Promo code rejected"];
writeFileSync(csv, "ticket_id,subject,body,priority\n" + Array.from({ length: 40 }, (_, i) =>
  `T-${1000 + i},${subjects[i % 4]},"Fictional ticket ${i}: the customer explains the problem in a sentence.",p${(i % 3) + 1}`).join("\n") + "\n");

const server = spawn("uv", ["run", "--project", ROOT, "caliper", "label", "--port", String(PORT), "--no-browser"], {
  // CALIPER_LABEL_PROJECTS: new projects default to <it>/<name> instead of ~/caliper-projects
  cwd: work, env: { ...process.env, CALIPER_LABEL_HOME: home, CALIPER_LABEL_PROJECTS: join(work, "projects") }, stdio: ["ignore", "pipe", "pipe"],
});
let log = "";
server.stdout.on("data", (d) => (log += d));
server.stderr.on("data", (d) => (log += d));
const stop = () => { try { server.kill("SIGTERM"); } catch { /* gone */ } };
process.on("exit", stop);

async function waitUp() {
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(`${BASE}/api/status`)).ok) return; } catch { /* not yet */ }
    await new Promise((r) => setTimeout(r, 150));
  }
  throw new Error(`server did not start:\n${log}`);
}

let browser;
try {
  await waitUp();
  browser = await chromium.launch({ executablePath: findChromium() });
  const page = await browser.newPage({ viewport: { width: 1366, height: 860 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  const shot = (n) => page.screenshot({ path: join(OUT, `${n}.png`) });

  console.log("start screen -> wizard");
  const t0 = Date.now();
  await page.goto(`${BASE}/`);
  await page.waitForSelector("#st-new");
  await shot("setup-01-start");
  await page.keyboard.press("n");
  await page.waitForSelector(".wz-drop");
  const [chooser] = await Promise.all([page.waitForEvent("filechooser"), page.click(".wz-drop button")]);
  await chooser.setFiles(csv);
  await page.waitForSelector(".wz-preview-summary");
  check((await page.textContent(".wz-preview-summary")).includes("40 rows"), "preview shows the row count");
  check(await page.$eval("select", (s) => s.value) === "ticket_id", "id column guessed");
  await shot("setup-02-data");
  await page.reload();
  await page.waitForSelector(".wz-preview-summary");
  check(!page.url().includes("new=1"), "a reload keeps the draft (upload and preview)");

  await page.click("#wz-next");
  await page.waitForSelector(".wz-designer");
  const before = await page.$$eval(".wz-field-col", (els) => els.map((e) => e.textContent));
  check(!before.includes("ticket_id") && before.includes("subject"), "card suggested without the id column");
  await page.click('button.wz-colbtn:has-text("priority")'); // toggle off (suggested) ...
  await page.click('button.wz-colbtn:has-text("priority")'); // ... and back on, at the end
  await page.focus('[data-fid="label-0"]');
  await page.keyboard.press("Alt+ArrowDown");
  const after = await page.$$eval(".wz-field-col", (els) => els.map((e) => e.textContent));
  check(after[1] === before[0], "Alt+ArrowDown reorders a field");
  await page.click('[aria-label="Next row"]');
  check((await page.textContent(".wz-preview-pos")).startsWith("Row 2"), "live preview steps to the next row");
  await shot("setup-03-card");

  await page.click("#wz-next");
  await page.waitForSelector(".wz-opts");
  await page.click('[data-fid="ohot-1"]');
  await page.keyboard.press("1");
  check(await page.isVisible(".wz-field-error"), "hotkey clash is shown");
  check((await page.getAttribute("#wz-next", "aria-disabled")) === "true", "Next is blocked while keys clash");
  await page.click('[data-fid="ohot-1"]');
  await page.keyboard.press("Shift");
  check((await page.inputValue('[data-fid="ohot-1"]')) === "1", "a modifier key alone is not bound");
  await page.keyboard.press("2");
  await shot("setup-04-labels");

  await page.click("#wz-next");
  await page.waitForSelector('[data-fid="q-column"]');
  await page.check('[data-fid="q-column"]');
  check(await page.$eval('[data-fid="qcol"]', (s) => s.value) === "priority", "queue column guessed");
  await page.fill('[data-fid="instructions"]', "**Is the subject right?**\n\n- Use *Unsure* when two fit.");
  await page.click("#wz-next");
  await page.waitForSelector(".wz-yaml");
  const yaml = await page.textContent(".wz-yaml");
  check(yaml.includes("path: data/tickets.csv") && yaml.includes("by: priority"), "review shows the YAML");
  await shot("setup-05-review");
  await page.click("#wz-create");
  await page.waitForSelector("#name-input", { timeout: 20000 });
  await page.fill("#name-input", "Ana");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(250);
  await page.keyboard.press("3"); // Unsure needs a note: the labeller asks for one
  await page.waitForTimeout(200);
  await page.keyboard.press("Escape");
  await page.keyboard.press("1");
  await page.waitForFunction(async () => (await (await fetch("/api/labels")).json()).total > 0, null, { timeout: 10000 });
  const secs = (Date.now() - t0) / 1000;
  check(true, `CSV -> first label in ${secs.toFixed(1)}s (scripted)`);
  await shot("setup-06-first-label");

  console.log("settings");
  // a label on "wrong" so retiring it needs confirmation
  const next = await (await fetch(`${BASE}/api/next?labeller=Bo`)).json();
  await fetch(`${BASE}/api/labels`, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ item_id: next.item.id, labeller: "Bo", decision: "wrong" }) });
  await page.goto(`${BASE}/#/settings/labels`);
  await page.waitForSelector(".wz-opts");
  await page.fill('[data-fid="olabel-0"]', "Right subject");
  check(await page.isDisabled('[data-fid="ovalue-0"]'), "stored value locked once labels exist");
  await page.click('[data-fid="orm-1"]');
  await page.waitForSelector("dialog[open]");
  await page.click("dialog[open] .wz-btn-danger");
  await page.waitForSelector(".wz-retired");
  await shot("setup-07-settings-labels");
  await page.click("#se-save");
  await page.waitForSelector("dialog[open]");
  await page.click("dialog[open] .wz-btn-danger");
  await page.waitForFunction(() => document.querySelector(".se-save-state")?.textContent.includes("revision 2"));
  check(true, "saved as revision 2");
  await page.goto(`${BASE}/#/settings/file`);
  await page.waitForSelector(".wz-yaml");
  const saved = await page.textContent(".wz-yaml");
  check(saved.includes("label: Right subject") && /value: wrong.*retired: true/.test(saved), "YAML keeps the rename and the retired option");
  check(saved.includes("revision: 2"), "revision bumped in the file");
  await shot("setup-08-settings-file");

  console.log("demo");
  await page.goto(`${BASE}/#/settings/general`);
  await page.click("#se-switch");
  await page.waitForSelector("#st-demo");
  check(true, "Settings -> Switch project reaches the start screen with a project open");
  check((await page.textContent(".st-list")).includes("Tickets"), "recent list shows the new project");
  await page.click("#st-demo");
  await page.waitForFunction(() => location.hash.startsWith("#/label"));
  const st = await (await fetch(`${BASE}/api/status`)).json();
  check(st.project && st.project.name.startsWith("Catalog cleanup"), "demo opens");
  check(errors.length === 0, `no page errors (${errors.join("; ")})`);
  console.log(`screenshots in ${OUT}`);
} finally {
  if (browser) await browser.close();
  stop();
}
