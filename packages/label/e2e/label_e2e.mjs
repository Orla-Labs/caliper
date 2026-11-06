// End-to-end browser check for `caliper label serve` (optional; not part of pytest).
//
//   PLAYWRIGHT_CORE=/path/to/node_modules/playwright-core \
//     node packages/label/e2e/label_e2e.mjs [--out DIR] [--port 8769]
//
// Starts the legacy server on the catalog_cleanup example with a double-labelling
// TOML config (the same UI serves projects), drives two labellers through the
// labelling loop with the keyboard, measures keypress -> next card painted,
// checks the dashboard, adjudication and export screens, laptop / tablet / phone
// layouts, and writes screenshots plus latency.json to --out. Exits 0 with a
// "skipped" message when playwright-core cannot be found.

import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, writeFileSync, readFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const arg = (name, def) => {
  const i = process.argv.indexOf(name);
  return i > 0 ? process.argv[i + 1] : def;
};
const PORT = Number(arg("--port", "8788"));
const OUT = resolve(arg("--out", join(tmpdir(), "caliper-label-e2e")));
const BASE = `http://127.0.0.1:${PORT}`;

let chromium;
try {
  const req = createRequire(import.meta.url);
  const where = process.env.PLAYWRIGHT_CORE || "playwright-core";
  ({ chromium } = req(where));
} catch {
  console.log("skipped: playwright-core not found (set PLAYWRIGHT_CORE to its folder)");
  process.exit(0);
}

// The installed playwright-core may expect a browser build that is not in the cache;
// fall back to the newest cached Chromium (or CHROMIUM_PATH).
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

const fail = (msg) => { throw new Error(msg); };
const check = (cond, msg) => { if (!cond) fail(msg); console.log(`  ok  ${msg}`); };

mkdirSync(OUT, { recursive: true });
const work = mkdtempSync(join(tmpdir(), "caliper-label-"));
const store = join(work, "labels.jsonl");
const yamlText = readFileSync(join(ROOT, "examples/catalog_cleanup/label.yaml"), "utf8");
// Same project as TOML (exercises the TOML loader) with double labelling on.
const toml = [
  'title = "Catalog cleanup · tax-code review"',
  'instructions = "Is the tax code production assigned right for this listing? Use Unsure when two codes fit, and add a note (N)."',
  'queue_key = "stratum"',
  'category_key = "metadata.production_tax_code"',
  'stage = "tax_code_review"',
  "double_label = 2",
  'fields = [',
  '  { template = "cards/{id}.svg", type = "image", label = "Product" },',
  '  { key = "metadata.production_tax_code", type = "badge", label = "Production code" },',
  '  { key = "metadata.production_score", type = "number", decimals = 2, label = "Confidence" },',
  '  { key = "reference_outcome", type = "badge", label = "Outcome" },',
  '  { key = "metadata.supplier", type = "badge", label = "Supplier" },',
  '  { key = "request.messages.-1.content", type = "text", label = "Listing" },',
  ']',
  'decisions = [',
  '  { key = "1", value = "correct", label = "Code correct", approved = true },',
  '  { key = "2", value = "wrong", label = "Wrong code" },',
  '  { key = "3", value = "unsure", label = "Unsure" },',
  ']',
].join("\n");
check(yamlText.includes("decisions:"), "example label.yaml present");
// Images resolve against the data file's folder, so the config can live anywhere.
const cfg = join(work, "label.toml");
writeFileSync(cfg, toml);

const server = spawn(
  "uv",
  ["run", "--quiet", "caliper", "label", "serve", "examples/catalog_cleanup/golden.jsonl",
    "--config", cfg, "--store", store, "--port", String(PORT), "--no-browser"],
  { cwd: ROOT, stdio: ["ignore", "pipe", "pipe"], detached: true },
);
let serverLog = "";
server.stdout.on("data", (d) => { serverLog += d; });
server.stderr.on("data", (d) => { serverLog += d; });

async function waitUp() {
  for (let i = 0; i < 120; i++) {
    try { if ((await fetch(`${BASE}/api/status`)).ok) return; } catch { /* not yet */ }
    await new Promise((r) => setTimeout(r, 250));
  }
  fail(`server did not start:\n${serverLog}`);
}
const getJSON = async (p) => (await fetch(BASE + p)).json();
const records = () => readFileSync(store, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l));
const settle = (ms = 250) => new Promise((r) => setTimeout(r, ms));

const consoleErrors = [];
async function newPage(browser, opts = {}) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 860 }, ...opts });
  const page = await ctx.newPage();
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(`${m.text()} ${m.location().url || ""}`); });
  page.on("pageerror", (e) => consoleErrors.push(String(e)));
  return page;
}
const cardId = (page) => page.locator("#card .card-id").textContent();

async function enterName(page, name, hash = "#/label") {
  await page.goto(BASE + "/" + hash);
  await page.locator("#name-input").waitFor();
  await page.fill("#name-input", name);
  await page.keyboard.press("Enter");
  await page.locator("#card .card-id").waitFor();
}

async function press(page, key) {
  const before = await cardId(page).catch(() => null);
  await page.keyboard.press(key);
  await page.waitForFunction((b) => {
    const el = document.querySelector("#card .card-id");
    return (el && el.textContent !== b) || document.querySelector("#card .state");
  }, before);
}

const pct = (xs, p) => { const s = [...xs].sort((a, b) => a - b); return s[Math.min(s.length - 1, Math.floor((p / 100) * s.length))]; };

let browser;
try {
  await waitUp();
  try {
    browser = await chromium.launch();
  } catch {
    browser = await chromium.launch({ executablePath: findChromium() });
  }

  console.log("labeller ana (light, 1280px)");
  const ana = await newPage(browser, { colorScheme: "light" });
  await enterName(ana, "ana");
  check((await ana.locator(".who").textContent()).includes("ana"), "name chip shows ana");
  const first = await cardId(ana);
  check(first === "lst-0000", "first card is the first row");
  check(await ana.locator("#card .media img").isVisible(), "card shows the product image");
  check((await ana.locator(".q-list li").count()) === 6, "sidebar: All + 5 queues");
  check((await ana.locator(".decisions .opt").count()) === 3, "three decision buttons");
  const aria = await ana.locator(".decisions .opt").first().getAttribute("aria-label");
  check(/Code correct \(key 1\)/.test(aria), `decision buttons have screen-reader labels ("${aria}")`);
  await ana.screenshot({ path: join(OUT, "label-card.png") });

  // usability fixes (lpf-b): config order, badges by meaning, decisions above the key bar
  const apiItem = await getJSON(`/api/items/${encodeURIComponent(first)}`);
  const want = apiItem.item.fields.map((f) => f.label);
  const shown = await ana.evaluate(() => [...document.querySelectorAll("#card figcaption, #card .field-label")]
    .sort((a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1)).map((e) => e.textContent));
  check(JSON.stringify(shown) === JSON.stringify(want), `card fields follow the configured order (${shown.join(", ")})`);
  const badgeTones = await ana.evaluate(() => [...document.querySelectorAll("#card span.f-badge")].map((b) => b.className));
  check(badgeTones.length > 0 && badgeTones.every((c) => /tone-/.test(c)), `badges carry a tone class, not a hash colour (${badgeTones.join(" | ")})`);
  for (const [w, h] of [[1366, 800], [1280, 720]]) {
    await ana.setViewportSize({ width: w, height: h });
    await ana.waitForTimeout(100);
    const gap = await ana.evaluate(() => document.querySelector(".keybar").getBoundingClientRect().top - document.querySelector(".secondary").getBoundingClientRect().bottom);
    check(gap >= 0, `Skip / Undo / Note sit above the key bar at ${w}x${h} (gap ${Math.round(gap)}px)`);
  }
  await ana.setViewportSize({ width: 1280, height: 860 });

  await press(ana, "1");
  check((await cardId(ana)) !== first, "key 1 labels and advances");
  await ana.waitForFunction(() => /Saved/.test(document.getElementById("announcer").textContent));
  check(true, "the live region announces the saved label");
  await ana.waitForFunction(() => /Saved/.test(document.querySelector(".save-state").textContent));
  check(true, "a visible 'Saved' tick follows the decision");

  // zoom: Z opens, a decision key still works while open, Esc closes
  await ana.keyboard.press("z");
  check(await ana.locator(".overlay.zoom").isVisible(), "Z opens the image full size");
  await ana.screenshot({ path: join(OUT, "label-zoom.png") });
  const zoomed = await cardId(ana);
  await press(ana, "2");
  check((await cardId(ana)) !== zoomed, "decision key works while zoom is open");
  check(await ana.locator(".overlay.zoom").isVisible(), "zoom stays open on the next item");
  await ana.keyboard.press("Escape");
  check(await ana.locator(".overlay.zoom").isHidden(), "Esc closes the zoom");
  await ana.locator("#card .zoom-btn").first().click();
  check(await ana.locator(".overlay.zoom").isVisible(), "clicking the image opens the zoom");
  await ana.keyboard.press("Escape");

  // skip then undo brings the item back
  const skipped = await cardId(ana);
  await press(ana, "s");
  check((await cardId(ana)) !== skipped, "S skips");
  await press(ana, "Backspace");
  check((await cardId(ana)) === skipped, "Backspace undo brings the skipped item back");

  // note goes with the next decision; typing does not trigger shortcuts
  await ana.keyboard.press("n");
  check(await ana.locator("#note").isVisible(), "N opens the note box");
  await ana.keyboard.type("two codes fit 1 2 3 s z");
  check(await ana.locator(".overlay.zoom").isHidden(), "keys typed in the note do not fire shortcuts");
  check((await cardId(ana)) === skipped, "still on the same item while typing");
  await ana.keyboard.press("Enter");
  await press(ana, "3");
  await settle();
  const noted = records().find((r) => r.item_id === skipped && r.decision === "unsure");
  check(noted && noted.note === "two codes fit 1 2 3 s z" && noted.labeller === "ana", "note saved with labeller");

  // every action is clickable too
  const beforeClick = await cardId(ana);
  await ana.locator(".decisions .opt", { hasText: "Wrong code" }).click();
  await ana.waitForFunction((b) => document.querySelector("#card .card-id").textContent !== b, beforeClick);
  await ana.locator(".secondary button", { hasText: "Undo" }).click();
  await ana.waitForFunction((b) => document.querySelector("#card .card-id").textContent === b, beforeClick);
  check(true, "clicking a decision and Undo work like the keys");

  // help overlay
  await ana.keyboard.press("?");
  const help = ana.locator('[aria-labelledby="help-title"]');
  check(await help.isVisible(), "? opens the shortcut overlay");
  await ana.screenshot({ path: join(OUT, "label-help.png") });
  await ana.keyboard.press("Escape");
  check(await help.isHidden(), "Esc closes the overlay");

  // speed: keypress -> next card painted, from the prefetch queue
  console.log("latency: 30 cards by keyboard");
  await ana.evaluate(() => { window.__caliperLabel.latencies = []; });
  for (let i = 0; i < 30; i++) await press(ana, i % 3 === 2 ? "2" : "1");
  await settle(300);
  const lat = await ana.evaluate(() => window.__caliperLabel.latencies);
  const p50 = pct(lat, 50), p95 = pct(lat, 95), max = Math.max(...lat);
  writeFileSync(join(OUT, "latency.json"), JSON.stringify({ n: lat.length, p50, p95, max, all: lat }, null, 1));
  console.log(`  card-to-card: n=${lat.length} p50=${p50.toFixed(1)}ms p95=${p95.toFixed(1)}ms max=${max.toFixed(1)}ms`);
  check(lat.length === 30 && p95 < 100, "next card paints under 100 ms (p95)");
  await settle(500);
  check(records().filter((r) => r.labeller === "ana" && r.decision && !r.undoes).length >= 33, "all 30 labels reached the server");

  // queues
  const rareBefore = (await getJSON("/api/queues?labeller=ana")).queues.find((q) => q.name === "rare_code").mine;
  await ana.locator(".q-list li button", { hasText: "rare_code" }).click();
  await ana.waitForFunction(() => [...document.querySelectorAll("#card .card-head .tag")].some((t) => t.textContent === "rare_code"));
  check(true, "picking a queue serves items from it");
  for (const k of ["1", "2", "1", "1"]) await press(ana, k);
  await settle();
  const rare = (await getJSON("/api/queues?labeller=ana")).queues.find((q) => q.name === "rare_code");
  check(rare.mine === rareBefore + 4, "queue progress counts ana's labels");
  await ana.locator(".q-list li button", { hasText: "All items" }).click();

  console.log("labeller ben (dark) double-labels the same items");
  const ben = await newPage(browser, { colorScheme: "dark" });
  await enterName(ben, "ben");
  const seen = [];
  for (let i = 0; i < 8; i++) {
    seen.push(await cardId(ben));
    await press(ben, i % 2 ? "1" : "2");
  }
  await settle();
  const anaIds = new Set(records().filter((r) => r.labeller === "ana" && r.decision).map((r) => r.item_id));
  check(seen.every((id) => anaIds.has(id)), "ben is served items still needing a second label first");
  await ben.screenshot({ path: join(OUT, "label-dark.png") });
  const anaNext = await getJSON("/api/next?labeller=ana");
  check(!anaIds.has(anaNext.item.id), "ana is never served an item she labelled");

  console.log("dashboard, adjudication, export");
  await ana.goto(BASE + "/#/dashboard");
  await ana.locator(".tiles").waitFor();
  const dash = await getJSON("/api/dashboard");
  const kappaTile = await ana.locator(".tile-value").nth(2).textContent();
  check(kappaTile === (dash.agreement.overall.kappa === null ? "–" : dash.agreement.overall.kappa.toFixed(2)), `dashboard kappa ${kappaTile} matches the API`);
  check((await ana.locator(".dash-section").count()) === 4, "labellers, queues, agreement, disagreements");
  await ana.screenshot({ path: join(OUT, "dashboard.png"), fullPage: true });

  const open = (await getJSON("/api/adjudications")).counts.open;
  check(open > 0, `there are disagreements to adjudicate (${open})`);
  await ana.goto(BASE + "/#/adjudicate");
  await ana.locator(".votes").waitFor();
  await ana.screenshot({ path: join(OUT, "adjudicate.png") });
  const target = (await ana.locator(".adj .card .card-id").textContent()).trim();
  await ana.keyboard.press("1");
  await ana.waitForFunction((t) => !document.querySelector(".adj .card-id") || document.querySelector(".adj .card-id").textContent !== t, target);
  await settle();
  const adj = records().find((r) => r.item_id === target && r.source === "adjudicated");
  check(adj && adj.decision === "correct" && adj.labeller === "ana", "key 1 sets the final label (source: adjudicated)");

  await ana.goto(BASE + "/#/export");
  await ana.locator(".exp-counts").waitFor();
  check(await ana.locator("#field-file").isHidden(), "export: destination fields follow the choice");
  check(!/\bnull\b/.test(await ana.locator(".exp-side").innerText()), "export summary has no stray 'null'");
  check((await ana.locator('input[name="format"][value="table"]').count()) === 0, "'Database table' is not offered as a format");
  await ana.locator("#format-jsonl").check();
  const hint = await ana.locator(".cli-code").textContent();
  check(hint.includes("--format jsonl") && hint.includes("labels.jsonl"), `the CLI hint follows the choices (${hint})`);
  await ana.locator("#rows-labels").check();
  check((await ana.locator(".cli-code").textContent()).includes("--rows labels"), "the CLI hint shows --rows labels");
  await ana.locator("#format-csv").check();
  await ana.locator("#rows-items").check();
  await ana.screenshot({ path: join(OUT, "export.png"), fullPage: true });
  const csv = await (await fetch(`${BASE}/api/export/download?format=csv`)).text();
  const header = csv.split("\n")[0].split(",");
  check(["labeller", "labelled_at", "note", "source", "stage", "project_version"].every((c) => header.includes(c)), "csv export carries provenance columns");
  check(csv.includes(`${target},`) && csv.includes("adjudicated"), "export uses the adjudicated label");

  console.log("tablet and phone");
  for (const [w, h, name] of [[1024, 768, "tablet-landscape"], [768, 1024, "tablet-portrait"], [390, 844, "phone"]]) {
    const p = await newPage(browser, { viewport: { width: w, height: h }, deviceScaleFactor: 2, colorScheme: "light", hasTouch: true });
    await enterName(p, `tab-${w}`);
    const docWidth = await p.evaluate(() => document.documentElement.scrollWidth);
    check(docWidth <= w, `no horizontal scroll at ${w}px (scrollWidth ${docWidth})`);
    const tap = await p.locator(".decisions .opt").first().boundingBox();
    check(tap.height >= 44, `decision buttons are a comfortable tap target at ${w}px (${Math.round(tap.height)}px)`);
    const before = await cardId(p);
    await p.locator(".decisions .opt").first().tap();
    await p.waitForFunction((b) => document.querySelector("#card .card-id").textContent !== b, before);
    await p.screenshot({ path: join(OUT, `label-${name}.png`) });
  }
  await settle();
  check(records().some((r) => r.labeller === "tab-390"), "tapping a decision button labels");

  // visible focus ring on keyboard focus
  await ana.goto(BASE + "/#/label");
  await ana.locator("#card .card-id").waitFor();
  await ana.keyboard.press("Tab");
  await ana.keyboard.press("Tab");
  const ring = await ana.evaluate(() => getComputedStyle(document.activeElement).boxShadow);
  check(ring && ring !== "none", "keyboard focus shows a focus ring");

  check(consoleErrors.length === 0, `no console errors${consoleErrors.length ? ": " + consoleErrors.join(" | ") : ""}`);
  console.log(`screenshots in ${OUT}`);
} finally {
  if (browser) await browser.close();
  try { process.kill(-server.pid, "SIGTERM"); } catch { server.kill("SIGTERM"); }
}
