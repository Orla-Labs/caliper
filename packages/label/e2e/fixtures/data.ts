// Fictional test data. Nothing here describes a real company, person or ticket.

import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const SUBJECTS = ["Refund not received", "App crashes on login", "Wrong size delivered", "Promo code rejected", "Parcel left at wrong door"];
const PRODUCTS = ["Lumen Lamp", "Pocket Kettle", "Trail Mug", "Cloud Pillow"];
const PRIORITIES = ["low", "medium", "high"];

export type Ticket = { ticket_id: string; subject: string; body: string; product: string; priority: string; amount: string };

export function tickets(n = 40): Ticket[] {
  return Array.from({ length: n }, (_, i) => ({
    ticket_id: `T-${1000 + i}`,
    subject: SUBJECTS[i % SUBJECTS.length],
    body: `Fictional ticket ${i}: the customer describes the problem in one sentence.`,
    product: PRODUCTS[i % PRODUCTS.length],
    priority: PRIORITIES[i % PRIORITIES.length],
    amount: (12.5 + i * 3.25).toFixed(2),
  }));
}

export function ticketsCsv(n = 40): string {
  const rows = tickets(n);
  const cols = Object.keys(rows[0]) as (keyof Ticket)[];
  const q = (v: string) => (/[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v);
  return [cols.join(","), ...rows.map((r) => cols.map((c) => q(r[c])).join(","))].join("\n") + "\n";
}

export function writeCsv(dir: string, name: string, n = 40): string {
  mkdirSync(dir, { recursive: true });
  const p = join(dir, name);
  writeFileSync(p, ticketsCsv(n));
  return p;
}

// A ready project folder (as the wizard would write it), for settings and team mode.
export function writeProject(dir: string, { doubleLabel = 1, name = "Support triage (e2e)" } = {}): string {
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "tickets.csv"), ticketsCsv(24));
  writeFileSync(join(dir, "caliper.label.yaml"), [
    "# fictional e2e project",
    "version: 1",
    `name: ${name}`,
    "source:",
    "  kind: file",
    "  path: tickets.csv",
    "  id_column: ticket_id",
    "card:",
    "  fields:",
    "    - {column: subject, type: text, label: Subject}",
    "    - {column: body, type: long_text, label: Message}",
    "    - {column: product, type: badge, label: Product}",
    "labels:",
    "  type: single",
    "  options:",
    '    - {value: correct, label: Correct, hotkey: "1", approved: true}',
    '    - {value: wrong, label: Wrong, hotkey: "2"}',
    '    - {value: unsure, label: Unsure, hotkey: "3", requires_note: true}',
    "  category_column: product",
    "workflow:",
    `  double_label: ${doubleLabel}`,
    "  assignment: open",
    "  instructions: Is the ticket subject right for the message?",
    "",
  ].join("\n"));
  return dir;
}

// Minimal CSV parser (quoted fields, doubled quotes, newlines inside quotes).
export function parseCsv(text: string): Record<string, string>[] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') { cell += '"'; i++; } else if (c === '"') quoted = false; else cell += c;
    } else if (c === '"') quoted = true;
    else if (c === ",") { row.push(cell); cell = ""; }
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(cell); rows.push(row); row = []; cell = "";
    } else cell += c;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  const [head, ...body] = rows.filter((r) => r.length > 1 || r[0] !== "");
  return body.map((r) => Object.fromEntries(head.map((h, i) => [h, r[i] ?? ""])));
}
