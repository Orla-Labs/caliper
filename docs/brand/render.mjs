// Rasterize docs/brand/*.svg to PNG with Playwright's Chromium (from packages/label/e2e).
//
//   node docs/brand/render.mjs
//
// The mark is also written at 1024 px for the GitHub org avatar upload.
import { createRequire } from "node:module";
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(join(here, "../../packages/label/e2e/package.json"));
const { chromium } = require("@playwright/test");

const browser = await chromium.launch();
for (const name of readdirSync(here).filter((f) => f.endsWith(".svg")).sort()) {
  const svg = readFileSync(join(here, name), "utf8");
  const [, w, h] = svg.match(/width="(\d+)" height="(\d+)"/);
  const scales = name.includes("-mark-") ? [[1, ""], [2, "-1024"]] : [[1, ""]];
  for (const [scale, suffix] of scales) {
    const page = await browser.newPage({
      viewport: { width: Number(w), height: Number(h) },
      deviceScaleFactor: scale,
    });
    await page.setContent(`<html><body style="margin:0;background:transparent">${svg}</body></html>`);
    const out = join(here, name.replace(".svg", `${suffix}.png`));
    await page.locator("svg").screenshot({ path: out, omitBackground: true });
    console.log(`wrote ${out.slice(here.length + 1)}`);
    await page.close();
  }
}
await browser.close();
