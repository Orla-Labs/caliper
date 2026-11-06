// Capture the frames for docs/demo.gif.
//
// Drives the running editor (scripts/dev.sh) with Playwright and renders the
// CLI demo output into a terminal-style page, screenshotting both into
// $FRAMES_DIR plus an ffmpeg concat list (frames.txt) carrying per-frame
// durations. demo/record/record.sh orchestrates servers + ffmpeg.
//
// playwright-core is NOT a repo dependency. Point PLAYWRIGHT_CORE at an
// installed copy (the package directory itself).
//
//   PLAYWRIGHT_CORE=/path/to/node_modules/playwright-core \
//   FRAMES_DIR=build/record/frames node demo/record/record.mjs

import { createRequire } from "node:module";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..", "..");
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_CORE ?? "playwright-core");

const EDITOR_URL = process.env.EDITOR_URL ?? "http://127.0.0.1:3456/?evalSet=invoice_spot_check";
const FRAMES = path.resolve(ROOT, process.env.FRAMES_DIR ?? "build/record/frames");
const W = 1200;
const H = 760;

const read = (p) => fs.readFileSync(path.join(ROOT, p), "utf8");
const DEMO_OUT = read("demo/expected_output.txt").replace(/\n$/, "").split("\n");
const NAIVE_SRC = read("demo/record/editor_naive.py");
const FIXED_SRC = read("demo/record/editor_fixed.py");

// ---------------------------------------------------------------- frames

fs.rmSync(FRAMES, { recursive: true, force: true });
fs.mkdirSync(FRAMES, { recursive: true });
const list = [];
let n = 0;
let total = 0;

async function shot(page, seconds) {
  const name = `${String(++n).padStart(5, "0")}.png`;
  await page.screenshot({ path: path.join(FRAMES, name) });
  list.push(`file '${name}'`, `duration ${seconds.toFixed(3)}`);
  total += seconds;
}

function finish() {
  // concat demuxer ignores the last duration unless the file is repeated
  list.push(list[list.length - 2]);
  fs.writeFileSync(path.join(FRAMES, "frames.txt"), list.join("\n") + "\n");
  console.log(`frames=${n} duration=${total.toFixed(1)}s -> ${path.relative(ROOT, FRAMES)}`);
}

// ---------------------------------------------------------------- browser

function headlessShell() {
  const cache = path.join(os.homedir(), "Library", "Caches", "ms-playwright");
  if (!fs.existsSync(cache)) return undefined;
  const dirs = fs
    .readdirSync(cache)
    .filter((d) => d.startsWith("chromium_headless_shell-"))
    .sort((a, b) => Number(b.split("-")[1]) - Number(a.split("-")[1]));
  for (const d of dirs) {
    for (const sub of fs.readdirSync(path.join(cache, d))) {
      const exe = path.join(cache, d, sub, "chrome-headless-shell");
      if (fs.existsSync(exe)) return exe;
    }
  }
  return undefined;
}

// ---------------------------------------------------------------- slides

const FONT = `"SF Mono", Menlo, Monaco, Consolas, monospace`;
const BASE_CSS = `
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { width: ${W}px; height: ${H}px; background: #0d1117; overflow: hidden; }
`;

function card(title, subtitle, foot) {
  return `<!doctype html><html><head><meta charset="utf-8"><style>${BASE_CSS}
    body { display: flex; flex-direction: column; align-items: center; justify-content: center;
           font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; color: #e6edf3; }
    h1 { font-size: 64px; font-weight: 700; letter-spacing: -1px; }
    h2 { margin-top: 18px; font-size: 26px; font-weight: 400; color: #9da7b3; text-align: center; max-width: 900px; line-height: 1.4; }
    .foot { position: absolute; bottom: 48px; font-family: ${FONT}; font-size: 18px; color: #7ee787; }
  </style></head><body><h1>${title}</h1><h2>${subtitle}</h2><div class="foot">${foot}</div></body></html>`;
}

const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

function colorize(line) {
  let h = esc(line);
  if (/^\$ /.test(line)) return `<span class="prompt">$</span><span class="cmd">${h.slice(1)}</span>`;
  if (/^=+$/.test(line)) return `<span class="rule">${h}</span>`;
  if (/^ \[\d\/5\]/.test(line)) return `<span class="head">${h}</span>`;
  if (/^\[caliper\]/.test(line)) return `<span class="dim">${h}</span>`;
  h = h.replace(/(SURVIVED \(\d+ rows?\))/g, `<span class="bad">$1</span>`);
  h = h.replace(/\bkilled\b/g, `<span class="good">killed</span>`);
  h = h.replace(/^(RESULT: FAIL)/, `<span class="bad b">$1</span>`);
  h = h.replace(/^(RESULT: PASS)/, `<span class="good b">$1</span>`);
  h = h.replace(/(&lt;- .*)$/, `<span class="warn">$1</span>`);
  return h;
}

function terminal(lines, caption) {
  const body = lines.map((l) => `<div class="l">${colorize(l) || "&nbsp;"}</div>`).join("");
  return `<!doctype html><html><head><meta charset="utf-8"><style>${BASE_CSS}
    body { padding: 22px; font-family: ${FONT}; }
    .win { height: 100%; border-radius: 10px; background: #161b22; border: 1px solid #30363d;
           display: flex; flex-direction: column; overflow: hidden; box-shadow: 0 10px 40px #0008; }
    .bar { height: 34px; display: flex; align-items: center; padding: 0 14px; gap: 8px;
           background: #21262d; border-bottom: 1px solid #30363d; }
    .dot { width: 12px; height: 12px; border-radius: 50%; }
    .cap { margin-left: 16px; color: #c9d1d9; font-size: 14px; font-family: -apple-system, Arial, sans-serif; }
    .scr { flex: 1; padding: 12px 18px; display: flex; flex-direction: column; justify-content: flex-end;
           overflow: hidden; font-size: 15.5px; line-height: 20px; color: #c9d1d9; white-space: pre; }
    .prompt { color: #7ee787; font-weight: 700; } .cmd { color: #e6edf3; font-weight: 600; }
    .rule { color: #30363d; } .head { color: #79c0ff; font-weight: 700; }
    .bad { color: #ff7b72; font-weight: 700; } .good { color: #7ee787; } .b { font-weight: 700; }
    .warn { color: #e3b341; } .dim { color: #8b949e; }
  </style></head><body><div class="win">
    <div class="bar"><span class="dot" style="background:#ff5f56"></span><span class="dot" style="background:#ffbd2e"></span>
    <span class="dot" style="background:#27c93f"></span><span class="cap">${esc(caption)}</span></div>
    <div class="scr">${body}</div></div></body></html>`;
}

const VISIBLE = 32;

// Reveal `lines` progressively (as if printed), holding at every section end.
async function playTerminal(page, prefix, lines, caption, holds) {
  const shown = [...prefix];
  const render = async (secs, cap = caption) => {
    await page.setContent(terminal(shown.slice(-VISIBLE), cap));
    await shot(page, secs);
  };
  // A section starts with "" + "====" + " [k/5]"; pause before each one.
  const starts = lines
    .map((l, i) => (l === "" && /^=+$/.test(lines[i + 1] ?? "") ? i : -1))
    .filter((i) => i > 0);
  let i = 0;
  let k = 0;
  while (i < lines.length) {
    if (starts.includes(i)) await render(holds[k] ?? 3.5);
    if (starts.includes(i)) k++;
    const next = Math.min(i + 3, ...starts.filter((s) => s > i), lines.length);
    shown.push(...lines.slice(i, next));
    i = next;
    await render(0.12);
  }
  await render(holds.final ?? 5);
}

// ---------------------------------------------------------------- editor

async function caption(page, text) {
  await page.evaluate((t) => {
    let el = document.getElementById("__cap");
    if (!el) {
      el = document.createElement("div");
      el.id = "__cap";
      Object.assign(el.style, {
        position: "fixed", right: "130px", top: "10px",
        background: "rgba(13,17,23,0.92)", color: "#e6edf3", border: "1px solid #3578c4",
        borderRadius: "8px", padding: "9px 16px", font: "600 16px -apple-system, Arial, sans-serif",
        zIndex: 9999, maxWidth: "640px", textAlign: "center", boxShadow: "0 6px 24px #000a",
      });
      document.body.appendChild(el);
    }
    el.style.display = t ? "block" : "none";
    el.textContent = t;
  }, text);
}

async function typeSource(page, src, perLine) {
  const lines = src.replace(/\n$/, "").split("\n");
  for (let k = 1; k <= lines.length; k++) {
    await page.evaluate((text) => {
      const ed = window.monaco.editor.getEditors()[0];
      ed.getModel().setValue(text);
      const last = ed.getModel().getLineCount();
      ed.setPosition({ lineNumber: last, column: ed.getModel().getLineMaxColumn(last) });
      ed.focus();
    }, lines.slice(0, k).join("\n") + "\n");
    if (lines[k - 1].trim() !== "" || k === lines.length) await shot(page, perLine);
  }
}

async function dryRun(page) {
  const done = page.waitForResponse((r) => r.url().endsWith("/api/dry-run") && r.ok());
  await page.getByRole("button", { name: "Dry run" }).click();
  await done;
  await page.getByRole("button", { name: "Dry run" }).waitFor();
  await page.waitForTimeout(400);
}

// Recording-only CSS: hide the (long) invoice prompt on each result card so
// the response + grader explanation fit, and the Next.js dev badge.
const RECORDING_CSS = `
  div[style*="margin-top: 4px"][style*="color: rgb(207, 207, 207)"][style*="font-size: 12px"] { display: none !important; }
  nextjs-portal { display: none !important; }
`;

async function scrollResults(page, y) {
  await page.evaluate((top) => {
    const panel = [...document.querySelectorAll("div")].find(
      (d) => d.firstElementChild?.textContent?.startsWith("Dry-run results"),
    );
    panel.scrollTop = top;
  }, y);
  await page.waitForTimeout(100);
}

// ---------------------------------------------------------------- script

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM ?? headlessShell() });
const ctx = await browser.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: 1 });
const page = await ctx.newPage();

// 1. Title
await page.setContent(
  card("Caliper", "Graders you can trust for RL fine-tuning:<br>catch reward hacks before your model does", "github.com/Orla-Labs/caliper"),
);
await shot(page, 3.0);

// 2. Start the stack
await playTerminal(
  page,
  [],
  [
    "$ uv sync && scripts/dev.sh",
    "[caliper] server pid=…  http://127.0.0.1:8000",
    "[caliper] editor pid=…  http://127.0.0.1:3456",
    "[caliper] Ctrl-C to stop both.",
  ],
  "dry-run server + Monaco editor",
  { final: 2.0 },
);

// 3. Editor: naive grader
await page.goto(EDITOR_URL, { waitUntil: "networkidle" });
await page.waitForFunction(() => window.monaco?.editor?.getEditors?.().length > 0, null, { timeout: 120_000 });
await page.getByRole("button", { name: "Dry run" }).waitFor();
await page.addStyleTag({ content: RECORDING_CSS });
await page.waitForTimeout(500);
await caption(page, "A first grader: reward 1.0 if the gold total appears anywhere in the answer");
await typeSource(page, NAIVE_SRC, 0.25);
await shot(page, 1.5);
await caption(page, "Dry run on 9 human-labeled answers (3 approved, 6 rejected)");
await dryRun(page);
await shot(page, 2.5);
await scrollResults(page, 10_000);
await caption(page, "8 of 9 score 1.0, including answers humans rejected");
await shot(page, 5.0);

// 4. Editor: fixed grader
await scrollResults(page, 0);
await caption(page, "The fix: exactly one <total> tag, holding one number equal to gold");
await typeSource(page, FIXED_SRC, 0.18);
await shot(page, 2.0);
await caption(page, "Dry run again");
await dryRun(page);
await shot(page, 2.5);
await scrollResults(page, 10_000);
await caption(page, "3 of 9 pass: exactly the human-approved answers");
await shot(page, 5.0);
await caption(page, "");

// 5. CLI: mutation testing, kappa, compile to TRL
await playTerminal(
  page,
  ["$ scripts/demo.sh"],
  DEMO_OUT,
  "scripts/demo.sh: mutation testing, kappa, compile to TRL",
  { 1: 2.5, 2: 6.0, 3: 5.0, 4: 6.0, final: 6.0 },
);

// 6. End card
await page.setContent(
  card("Caliper", "Mutation-test your graders, measure agreement with humans,<br>ship the same grader to TRL or Bedrock", "uv sync &amp;&amp; scripts/demo.sh  ·  orlalabs.com"),
);
await shot(page, 3.5);

finish();
await browser.close();
