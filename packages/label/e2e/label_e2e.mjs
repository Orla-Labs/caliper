// End-to-end browser check for `caliper label serve` (optional; not part of pytest).
//
//   PLAYWRIGHT_CORE=/path/to/node_modules/playwright-core \
//     node packages/label/e2e/label_e2e.mjs [--out DIR] [--port 8769]
//
// Starts the server on the catalog_cleanup example with a double-labelling TOML
// config, drives two labellers through the UI with the keyboard, checks the
// results through the API, and writes screenshots to --out. Exits 0 with a
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
const PORT = Number(arg("--port", "8769"));
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
    try { if ((await fetch(`${BASE}/api/config`)).ok) return; } catch { /* not yet */ }
    await new Promise((r) => setTimeout(r, 250));
  }
  fail(`server did not start:\n${serverLog}`);
}
const getJSON = async (p) => (await fetch(BASE + p)).json();
const records = () => readFileSync(store, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l));

const consoleErrors = [];
async function newPage(browser, opts = {}) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 860 }, ...opts });
  const page = await ctx.newPage();
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(`${m.text()} ${m.location().url || ""}`); });
  page.on("pageerror", (e) => consoleErrors.push(String(e)));
  return page;
}
const cardId = (page) => page.locator(".card-id").textContent();

async function enterName(page, name) {
  await page.goto(BASE + "/");
  await page.locator("#name-input").waitFor();
  await page.fill("#name-input", name);
  await page.keyboard.press("Enter");
  await page.locator(".card-id").waitFor();
}

async function press(page, key) {
  const before = await cardId(page).catch(() => null);
  await page.keyboard.press(key);
  await page.waitForFunction((b) => {
    const el = document.querySelector(".card-id");
    return !el || el.textContent !== b;
  }, before);
}

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
  check((await ana.locator("#who-btn").textContent()).includes("ana"), "name chip shows ana");
  const first = await cardId(ana);
  check(first === "lst-0000", "first card is the first row");
  check(await ana.locator(".media img").isVisible(), "card shows the product image");
  check((await ana.locator(".queues li").count()) === 6, "sidebar: All + 5 queues");
  await ana.screenshot({ path: join(OUT, "label-card.png") });

  await press(ana, "1");
  check((await cardId(ana)) !== first, "key 1 labels and advances");

  // zoom: Z opens, a decision key still works while open, Esc closes
  await ana.keyboard.press("z");
  check(await ana.locator("#zoom").isVisible(), "Z opens the image full size");
  await ana.screenshot({ path: join(OUT, "label-zoom.png") });
  const zoomed = await cardId(ana);
  await press(ana, "2");
  check((await cardId(ana)) !== zoomed, "decision key works while zoom is open");
  check(await ana.locator("#zoom").isVisible(), "zoom stays open on the next item");
  await ana.keyboard.press("Escape");
  check(await ana.locator("#zoom").isHidden(), "Esc closes the zoom");
  await ana.locator(".media img").click();
  check(await ana.locator("#zoom").isVisible(), "clicking the image opens the zoom");
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
  check(await ana.locator("#zoom").isHidden(), "keys typed in the note do not fire shortcuts");
  check((await cardId(ana)) === skipped, "still on the same item while typing");
  await ana.keyboard.press("Enter");
  await press(ana, "3");
  const noted = records().find((r) => r.item_id === skipped && r.decision === "unsure");
  check(noted && noted.note === "two codes fit 1 2 3 s z" && noted.labeller === "ana", "note saved with labeller");

  // help overlay
  await ana.keyboard.press("?");
  check(await ana.locator("#help").isVisible(), "? opens the shortcut overlay");
  await ana.screenshot({ path: join(OUT, "label-help.png") });
  await ana.keyboard.press("Escape");
  check(await ana.locator("#help").isHidden(), "Esc closes the overlay");

  // queues
  await ana.locator(".queues li button", { hasText: "rare_code" }).click();
  await ana.waitForFunction(() => [...document.querySelectorAll(".card-head .tag")].some((t) => t.textContent === "rare_code"));
  check(true, "picking a queue serves items from it");
  for (const k of ["1", "2", "1", "1"]) await press(ana, k);
  const rare = (await getJSON("/api/queues?labeller=ana")).queues.find((q) => q.name === "rare_code");
  check(rare.mine === 4, "queue progress counts ana's labels");
  await ana.locator(".queues li button", { hasText: "All items" }).click();

  console.log("labeller ben (dark) double-labels the same items");
  const ben = await newPage(browser, { colorScheme: "dark" });
  await enterName(ben, "ben");
  const seen = new Set();
  for (let i = 0; i < 4; i++) {
    const id = await cardId(ben);
    seen.add(id);
    await press(ben, i === 1 ? "1" : "2");
  }
  check(!seen.has(undefined), "ben gets cards");
  const anaIds = new Set(records().filter((r) => r.labeller === "ana" && r.decision).map((r) => r.item_id));
  check([...seen].every((id) => anaIds.has(id)), "ben is served items still needing a second label first");
  await ben.locator(".queues li button", { hasText: "rare_code" }).click();
  await ben.waitForFunction(() => [...document.querySelectorAll(".card-head .tag")].some((t) => t.textContent === "rare_code"));
  for (const k of ["1", "2", "2", "1"]) await press(ben, k);
  await ben.screenshot({ path: join(OUT, "label-dark.png") });

  // ana never sees an item twice
  const anaNext = await getJSON("/api/next?labeller=ana");
  check(!anaIds.has(anaNext.item.id), "ana is never served an item she labelled");

  await ana.locator("#agreement-btn").click();
  await ana.locator(".kappa-hero").waitFor();
  const kappaShown = await ana.locator(".kappa-hero b").first().textContent();
  const agr = await getJSON("/api/agreement");
  const by = (who) => new Set(records().filter((r) => r.labeller === who && r.decision).map((r) => r.item_id));
  const benIds = by("ben");
  const overlap = [...by("ana")].filter((id) => benIds.has(id)).length;
  check(overlap === 7 && agr.overall.n_items === overlap && agr.method === "cohen",
    `agreement over the ${overlap} double-labelled items (Cohen)`);
  check(kappaShown === (agr.overall.kappa === null ? "undefined" : agr.overall.kappa.toFixed(2)), `UI kappa ${kappaShown} matches API`);
  check(agr.by_category.length >= 2, "kappa by category");
  await ana.screenshot({ path: join(OUT, "label-agreement.png") });
  await ana.keyboard.press("Escape");
  await ana.reload();
  await ana.locator(".card-id").waitFor();
  await ana.screenshot({ path: join(OUT, "label-hero.png") });

  console.log("phone width");
  const phone = await newPage(browser, { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, colorScheme: "light", hasTouch: true });
  await enterName(phone, "cam");
  const docWidth = await phone.evaluate(() => document.documentElement.scrollWidth);
  check(docWidth <= 390, `no horizontal scroll at 390px (scrollWidth ${docWidth})`);
  await phone.locator(".decisions button").first().tap();
  await phone.waitForFunction(() => document.querySelector("#toast") && !document.querySelector("#toast").hidden);
  check(records().some((r) => r.labeller === "cam"), "tapping a decision button labels");
  await phone.screenshot({ path: join(OUT, "label-phone.png") });

  check(consoleErrors.length === 0, `no console errors${consoleErrors.length ? ": " + consoleErrors.join(" | ") : ""}`);
  console.log(`screenshots in ${OUT}`);
} finally {
  if (browser) await browser.close();
  try { process.kill(-server.pid, "SIGTERM"); } catch { server.kill("SIGTERM"); }
}
