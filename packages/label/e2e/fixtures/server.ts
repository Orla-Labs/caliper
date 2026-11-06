// Starts and stops `caliper label` servers for the suite.
//
// Each server gets a fresh temp working dir and its own HOME / CALIPER_LABEL_HOME, so
// recent-project lists, the demo copy and labeller state never leak between servers or
// into the developer's ~/.caliper-label. Only the child gets the temp HOME: the test
// runner keeps the real one, so Playwright still finds its browser cache.
//
// Ports: CALIPER_E2E_PORTS="8791-8795" restricts servers to a range (first free port
// wins); without it the OS picks a free port.

import { spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../../..");
export const TOKEN = "e2e-fictional-team-token-0001";

export type Server = {
  base: string;
  port: number;
  work: string; // the server's cwd (new projects default to <work>/<slug>)
  home: string; // CALIPER_LABEL_HOME
  log: () => string;
  stop: () => Promise<void>;
};

export type StartOptions = {
  args?: string[]; // after `caliper label`
  env?: Record<string, string>;
  work?: string;
};

function portFree(port: number): Promise<boolean> {
  return new Promise((ok) => {
    const s = createServer();
    s.once("error", () => ok(false));
    s.listen(port, "127.0.0.1", () => s.close(() => ok(true)));
  });
}

function osPort(): Promise<number> {
  return new Promise((ok, fail) => {
    const s = createServer();
    s.once("error", fail);
    s.listen(0, "127.0.0.1", () => {
      const a = s.address();
      const p = typeof a === "object" && a ? a.port : 0;
      s.close(() => ok(p));
    });
  });
}

const taken = new Set<number>();

async function pickPort(): Promise<number> {
  const range = process.env.CALIPER_E2E_PORTS;
  if (!range) return osPort();
  const [lo, hi] = range.split("-").map(Number);
  for (let p = lo; p <= (hi || lo); p++) {
    if (taken.has(p)) continue;
    if (await portFree(p)) return p;
  }
  throw new Error(`no free port in CALIPER_E2E_PORTS=${range}`);
}

function command(): [string, string[]] {
  if (process.env.CALIPER_BIN) return [process.env.CALIPER_BIN, []];
  const venv = join(ROOT, ".venv", process.platform === "win32" ? "Scripts/caliper.exe" : "bin/caliper");
  if (existsSync(venv)) return [venv, []];
  return ["uv", ["run", "--project", ROOT, "caliper"]];
}

export function tempDir(prefix: string): string {
  const base = process.env.CALIPER_E2E_TMP || tmpdir();
  mkdirSync(base, { recursive: true });
  return mkdtempSync(join(base, `${prefix}-`));
}

export async function startServer(opts: StartOptions = {}): Promise<Server> {
  const work = opts.work || tempDir("caliper-e2e-work");
  const root = tempDir("caliper-e2e-home");
  const home = join(root, "caliper-label");
  const port = await pickPort();
  taken.add(port);
  const env: Record<string, string> = {};
  for (const [k, v] of Object.entries(process.env)) {
    // never hand the server a real database or token from the developer's shell
    if (v === undefined || k.startsWith("PG") || k === "DATABASE_URL" || k === "CALIPER_LABEL_TOKEN") continue;
    env[k] = v;
  }
  Object.assign(env, { HOME: root, CALIPER_LABEL_HOME: home, PYTHONUNBUFFERED: "1" }, opts.env || {});
  const [bin, pre] = command();
  const args = [...pre, "label", ...(opts.args || []), "--port", String(port), "--no-browser"];
  const child: ChildProcess = spawn(bin, args, { cwd: work, env, stdio: ["ignore", "pipe", "pipe"], detached: true });
  let out = "";
  child.stdout?.on("data", (d) => (out += d));
  child.stderr?.on("data", (d) => (out += d));
  let exited = false;
  child.on("exit", () => (exited = true));
  const base = `http://127.0.0.1:${port}`;

  const stop = async () => {
    taken.delete(port);
    if (exited || child.pid === undefined) return;
    const gone = new Promise<void>((ok) => child.once("exit", () => ok()));
    try { process.kill(-child.pid, "SIGTERM"); } catch { child.kill("SIGTERM"); }
    const t = setTimeout(() => { try { process.kill(-child.pid!, "SIGKILL"); } catch { /* gone */ } }, 5000);
    await gone;
    clearTimeout(t);
  };

  const deadline = Date.now() + 60_000;
  for (;;) {
    if (exited) throw new Error(`caliper label exited before it was up:\n${out}`);
    try {
      if ((await fetch(`${base}/api/status`)).ok) break;
    } catch { /* not up yet */ }
    if (Date.now() > deadline) { await stop(); throw new Error(`caliper label did not start on ${base}:\n${out}`); }
    await new Promise((r) => setTimeout(r, 150)); // server start-up poll, not a test wait
  }
  return { base, port, work, home, log: () => out, stop };
}
