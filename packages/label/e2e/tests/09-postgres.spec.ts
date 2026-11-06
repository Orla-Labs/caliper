// Spec §2 databases: the wizard on a Postgres table (docker compose test service, fictional
// seed data). Gated: CALIPER_LABEL_E2E_DOCKER=1, with CALIPER_LABEL_PG_PORT (default 55432).
// The connection URL lives in an env var; only the variable's name reaches the project file.

import { readFileSync } from "node:fs";
import { test, expect, card, enterName, pressAndAdvance, uniq, waitForDecisions } from "../fixtures/app.ts";
import { parseCsv } from "../fixtures/data.ts";
import { join } from "node:path";

const PORT = process.env.CALIPER_LABEL_PG_PORT || "55432";
const SECRET = "caliper-local-only"; // the compose file's fictional local password

test.skip(process.env.CALIPER_LABEL_E2E_DOCKER !== "1", "set CALIPER_LABEL_E2E_DOCKER=1 (and start packages/label/docker/compose.yml)");

test("Postgres wizard: env-var connection → table → label → export", async ({ page, launch, request }) => {
  const srv = await launch({ env: { APP_DB_URL: `postgresql://caliper:${SECRET}@127.0.0.1:${PORT}/shop` } });
  await page.goto(`${srv.base}/`);
  await page.getByRole("button", { name: /new project/i }).click();
  await page.getByRole("radio", { name: /postgres/i }).check();
  await page.getByRole("textbox", { name: /^table$/i }).fill("support.tickets");
  const env = page.getByRole("textbox", { name: /connection env var/i });
  await env.fill("APP_DB_URL");
  await page.getByRole("button", { name: /connect & preview/i }).click();

  await expect(page.getByText(/connected/i).first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/APP_DB_URL/).first()).toBeVisible(); // which variable is used ...
  await expect(page.locator("body")).not.toContainText(SECRET); // ... never its value
  await expect(page.getByRole("combobox", { name: /item id column/i })).toHaveValue("ticket_id");

  for (const h of [/design the card/i, /^labels$/i, /workflow/i]) {
    await page.getByRole("button", { name: /^next$/i }).click();
    await expect(page.getByRole("heading", { level: 1, name: h })).toBeVisible();
  }
  await page.getByRole("button", { name: /^review$/i }).click();
  const yaml = page.getByLabel(/project file that will be written/i);
  await expect(yaml).toContainText("kind: postgres");
  await expect(yaml).toContainText("env: APP_DB_URL");
  await expect(yaml).not.toContainText(SECRET);
  await page.getByRole("textbox", { name: /project name/i }).fill(`Tickets ${uniq("pg")}`);
  await page.getByRole("button", { name: /create project/i }).click();
  await expect(page).toHaveURL(/#\/label/, { timeout: 30_000 });

  const who = uniq("pg");
  await enterName(page, who);
  const ID = /PG-\d{3}/;
  await expect(card(page)).toBeVisible();
  const ids: string[] = [];
  for (const k of ["1", "2", "1", "1", "2"]) ids.push(await pressAndAdvance(page, k, ID));
  await waitForDecisions(request, srv.base, `labeller=${who}`, 5);

  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Export" }).click();
  const dl = page.waitForEvent("download");
  await page.getByRole("button", { name: /download csv/i }).click();
  const out = join(test.info().outputDir, "pg-export.csv");
  await (await dl).saveAs(out);
  const rows = parseCsv(readFileSync(out, "utf8"));
  expect(rows).toHaveLength(5);
  expect(rows.every((r) => r.labeller === who)).toBeTruthy();
});
