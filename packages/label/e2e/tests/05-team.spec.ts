// Spec §4 team mode: the token gate, two labellers double-labelling the same items, the
// dashboard's κ, adjudication, and an export that uses the adjudicated label.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test, expect, card, currentId, enterName, pressAndAdvance, waitForDecisions, type LabelRow } from "../fixtures/app.ts";
import { parseCsv, writeProject } from "../fixtures/data.ts";
import { TOKEN, tempDir } from "../fixtures/server.ts";

const ID = /T-\d{4}/;
const AUTH = { Authorization: `Bearer ${TOKEN}` };

test("team mode: token gate → two labellers → dashboard κ → adjudicate → export", async ({ browser, launch, request }) => {
  const dir = writeProject(join(tempDir("caliper-e2e-team"), "triage"), { doubleLabel: 2, name: "Support triage (team)" });
  const srv = await launch({ args: [dir, "--auth", "token"], env: { CALIPER_LABEL_TOKEN: TOKEN } });

  // the API refuses requests without the token
  expect((await request.get(`${srv.base}/api/labels`)).status()).toBe(401);

  // ---------------------------------------------------------------- Ana: pastes the token
  const anaCtx = await browser.newContext();
  const ana = await anaCtx.newPage();
  await ana.goto(`${srv.base}/#/label`);
  const gate = ana.getByRole("heading", { name: /access token needed/i });
  await expect(gate).toBeVisible();
  await ana.getByLabel(/access token/i).fill("not-the-token");
  await ana.getByRole("button", { name: /continue/i }).click();
  await expect(ana.getByText(/not accepted/i)).toBeVisible();
  await ana.getByLabel(/access token/i).fill(TOKEN);
  await ana.getByRole("button", { name: /continue/i }).click();
  await enterName(ana, "Ana");
  await expect(card(ana)).toContainText(ID);

  // ---------------------------------------------------------------- Ben: opens the shared link
  const benCtx = await browser.newContext({ colorScheme: "dark" });
  const ben = await benCtx.newPage();
  await ben.goto(`${srv.base}/?token=${TOKEN}#/label`);
  await enterName(ben, "Ben");
  await expect(card(ben)).toContainText(ID);
  expect(ben.url(), "the token is removed from the address bar").not.toContain(TOKEN);

  // ---------------------------------------------------------------- double labelling
  const anaKeys = ["1", "1", "2", "1", "2", "1"];
  const benKeys = ["1", "2", "2", "2", "2", "2"];
  const anaIds: string[] = [];
  for (const k of anaKeys) anaIds.push(await pressAndAdvance(ana, k, ID));
  await waitForDecisions(request, srv.base, "labeller=Ana", 6, AUTH);
  const benIds: string[] = [];
  for (const k of benKeys) benIds.push(await pressAndAdvance(ben, k, ID));
  expect(benIds, "Ben is served Ana's items first (they need a second label)").toEqual(anaIds);
  await waitForDecisions(request, srv.base, "labeller=Ben", 6, AUTH);
  expect(await currentId(ana, ID), "Ana is never served an item she labelled").not.toMatch(new RegExp(`^(${anaIds.join("|")})$`));

  const disagree = anaIds.filter((_, i) => anaKeys[i] !== benKeys[i]);
  expect(disagree).toHaveLength(3);

  // ---------------------------------------------------------------- dashboard
  await ana.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Dashboard" }).click();
  await expect(ana.getByRole("heading", { level: 1 })).toBeVisible();
  const dash = await (await request.get(`${srv.base}/api/dashboard`, { headers: AUTH })).json();
  const k = dash.agreement.overall.kappa as number;
  expect(typeof k).toBe("number");
  // 6 pairs, Ana c,c,w,c,w,c vs Ben c,w,w,w,w,w: p_o = 1/2.
  // Cohen: p_e = 4/6·1/6 + 2/6·5/6 = 14/36 → κ = 2/11. Fleiss (pooled 5/12, 7/12) → κ = -1/35.
  expect(k).toBeCloseTo(dash.agreement.method === "fleiss" ? -1 / 35 : 2 / 11, 4);
  const agreement = ana.getByRole("region", { name: "Agreement", exact: true });
  await expect(agreement).toContainText(k.toFixed(2));
  await expect(ana.getByText(k.toFixed(2), { exact: true }).first()).toBeVisible();
  const labellers = ana.getByRole("region", { name: "Labellers", exact: true });
  await expect(labellers.getByRole("row", { name: /ana/i })).toContainText("6");
  await expect(labellers.getByRole("row", { name: /ben/i })).toContainText("6");
  const dis = ana.getByRole("region", { name: "Disagreements", exact: true });
  for (const id of disagree) await expect(dis).toContainText(id);

  // ---------------------------------------------------------------- adjudicate
  await ana.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Adjudicate" }).click();
  await expect(ana.getByRole("heading", { level: 1, name: /adjudicate/i })).toBeVisible();
  await expect(ana.getByRole("tab", { name: /to decide \(3\)/i })).toBeVisible();
  const item = ana.getByRole("article", { name: /^Item T-\d{4}$/ });
  const target = ((await item.getAttribute("aria-label")) || "").replace(/^Item /, "");
  expect(disagree).toContain(target);
  const votes = ana.getByRole("list", { name: /what each labeller said/i });
  await expect(votes.getByRole("listitem")).toHaveCount(2);
  await expect(votes).toContainText("Ana");
  await expect(votes).toContainText("Ben");
  await ana.getByRole("textbox", { name: /note/i }).fill("neither: the ticket is ambiguous");
  await ana.getByRole("button", { name: /unsure, set as the final label/i }).click();
  await expect(ana.getByRole("tab", { name: /to decide \(2\)/i })).toBeVisible();
  const adj = (await (await request.get(`${srv.base}/api/labels?all=true&item_id=${target}`, { headers: AUTH })).json()).labels as LabelRow[];
  expect(adj.find((r) => r.source === "adjudicated")).toMatchObject({ decision: "unsure", labeller: "Ana" });

  // ---------------------------------------------------------------- export (download through the cookie)
  await ana.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Export" }).click();
  await expect(ana.getByRole("heading", { level: 1, name: /export/i })).toBeVisible();
  const dl = ana.waitForEvent("download");
  await ana.getByRole("button", { name: /download.*csv/i }).click();
  const file = await dl;
  const out = join(test.info().outputDir, "team-export.csv");
  await file.saveAs(out);
  const rows = parseCsv(readFileSync(out, "utf8"));
  const row = rows.find((r) => r.item_id === target);
  expect(row, `${target} is exported`).toBeTruthy();
  expect(row).toMatchObject({ label: "unsure", source: "adjudicated" });
  // the other two disagreements are left out (default policy), agreed items are in
  for (const id of disagree.filter((x) => x !== target)) expect(rows.find((r) => r.item_id === id)).toBeUndefined();
  expect(rows).toHaveLength(4);

  await anaCtx.close();
  await benCtx.close();
});
