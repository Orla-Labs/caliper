// Adjudication (#/adjudicate[?status=open|done|all]): items labellers disagree on,
// their labels side by side, and the final label (source: adjudicated).
// Keys: J / K next / previous item, the option hotkeys set the final label.

import { announce, askName, button, doneState, el, errorState, fmtTime, keyLabel, labellerName, link, loadingState, toast } from "../common/dom.js";
import { renderCard } from "../label/card.js";
import { decisionText } from "../label/decide.js";

export async function mount(root, { api, query }) {
  const page = el("div", { class: "page adj" }, loadingState("Loading disagreements…"));
  root.replaceChildren(page);
  let cfg;
  try {
    cfg = await api.get("/api/label-config");
  } catch (err) {
    page.replaceChildren(errorState(err, () => mount(root, { api, query })));
    return;
  }
  const status = ["open", "done", "all"].includes(query.get("status")) ? query.get("status") : "open";
  let rows = [];
  let counts = {};
  let index = 0;
  let who = labellerName();
  const options = cfg.options.filter((o) => !o.retired);
  const choosable = ["single", "yes_no_unsure"].includes(cfg.type);

  async function load() {
    try {
      const data = await api.get(`/api/adjudications?status=${status}&limit=200`);
      rows = data.items;
      counts = data.counts;
      index = Math.min(index, Math.max(0, rows.length - 1));
      render();
    } catch (err) {
      page.replaceChildren(errorState(err, load));
    }
  }

  async function settle(decision) {
    const row = rows[index];
    if (!row) return;
    if (!who) who = await askName({ title: "Who is adjudicating?", confirm: "Continue" });
    const note = page.querySelector("#adj-note")?.value.trim() || null;
    try {
      await api.post("/api/adjudications", { item_id: row.item_id, adjudicator: who, decision, note });
      const text = decisionText(cfg, typeof decision === "string" ? decision : JSON.stringify(decision));
      toast(`${row.item_id}: final label ${text}`);
      announce(`Final label for ${row.item_id} set to ${text}.`);
      if (status === "open") rows.splice(index, 1); else await load();
      counts.open = Math.max(0, (counts.open || 1) - (status === "open" ? 1 : 0));
      render();
    } catch (err) {
      toast(`${err.message}${err.hint ? `. ${err.hint}` : ""}`, "error");
    }
  }

  function tabs() {
    return el("div", { class: "tabs", role: "tablist", "aria-label": "Which items" },
      ...["open", "done", "all"].map((s) => el("a", {
        role: "tab", href: `#/adjudicate?status=${s}`, "aria-selected": s === status ? "true" : "false",
        text: `${s === "open" ? "To decide" : s === "done" ? "Decided" : "All"} (${counts[s] ?? 0})`,
      })));
  }

  function render() {
    const head = el("div", { class: "page-head" },
      el("div", {}, el("h1", { text: "Adjudicate" }),
        el("p", { text: "Items where labellers disagree. The final label you pick here is the one exports use (marked as adjudicated)." })),
      tabs());
    if (!rows.length) {
      page.replaceChildren(head, status === "open"
        ? doneState("Nothing to adjudicate", counts.all ? "Every disagreement has a final label." : "Labellers agree on every double-labelled item so far.",
          [link("#/dashboard", "Back to the dashboard", true), link("#/adjudicate?status=done", "See decided items")])
        : doneState("No items here", "Nothing matches this filter yet.", [link("#/adjudicate", "To decide")]));
      return;
    }
    const row = rows[index];
    const labels = el("div", { class: "votes", role: "list", "aria-label": "What each labeller said" },
      ...row.labels.map((l) => el("div", { class: "vote", role: "listitem" },
        el("div", { class: "vote-who", text: l.labeller }),
        el("div", { class: "vote-decision", text: l.display }),
        l.note ? el("div", { class: "vote-note", text: `“${l.note}”` }) : el("div", { class: "vote-note muted", text: "no note" }),
        el("div", { class: "vote-time muted", text: fmtTime(l.labelled_at) }))));
    const final = row.adjudication
      ? el("p", { class: "final" }, el("span", { class: "tag tag-good", text: "final" }), ` ${row.adjudication.display} · by ${row.adjudication.adjudicator}`,
        row.adjudication.note ? el("span", { class: "muted", text: ` · “${row.adjudication.note}”` }) : null)
      : null;
    const choices = choosable
      ? el("div", { class: "decisions", role: "group", "aria-label": "Set the final label" },
        ...options.map((o) => el("button", {
          type: "button", class: "opt" + (o.approved ? " opt-approved" : ""), style: o.color ? `--opt:${o.color}` : null,
          "aria-label": `${o.hotkey ? `${keyLabel(o.hotkey)} ` : ""}${o.label}, set as the final label`,
          onclick: () => settle(o.value),
        }, o.hotkey ? el("kbd", { "aria-hidden": "true", text: keyLabel(o.hotkey) }) : null, o.hotkey ? " " : null, el("span", { text: o.label }))))
      : el("div", { class: "decisions", role: "group", "aria-label": "Pick one of the labels as final" },
        ...[...new Map(row.labels.map((l) => [l.decision, l])).values()].map((l, i) => el("button", {
          type: "button", class: "opt", onclick: () => settle(cfg.type === "multi" ? JSON.parse(l.decision) : l.decision),
        }, el("kbd", { "aria-hidden": "true", text: String(i + 1) }), el("span", { text: l.display }))));
    const item = row.item;
    const card = el("article", { class: "card", "aria-label": `Item ${row.item_id}` }, ...(item ? renderCard(item, { optionLabel: (d) => decisionText(cfg, d), options: cfg.options, explain: (f) => api.get(`${f.src}/check`) }) : [el("p", { text: row.item_id })]));
    const nav = el("div", { class: "adj-nav" },
      button("← Previous", () => { index = Math.max(0, index - 1); render(); }, "btn btn-ghost"),
      el("span", { class: "muted num", text: `${index + 1} of ${rows.length}` }),
      button("Next →", () => { index = Math.min(rows.length - 1, index + 1); render(); }, "btn btn-ghost"));
    page.replaceChildren(head,
      el("div", { class: "adj-grid" },
        el("div", { class: "adj-item" }, card),
        el("div", { class: "adj-side" },
          el("h2", { class: "lbl-h", text: `Labels (${row.labels.length})` }),
          labels,
          final,
          el("h2", { class: "lbl-h", text: row.adjudication ? "Change the final label" : "Final label" }),
          choices,
          el("label", { for: "adj-note", text: "Note (optional)" }),
          el("textarea", { id: "adj-note", rows: "2", maxlength: "2000", placeholder: "Why this label?" }),
          el("p", { class: "muted small", text: `Adjudicating as ${who || "(you will be asked)"} · J / K move between items` }),
          nav)));
  }

  function onKey(ev) {
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const tag = document.activeElement && document.activeElement.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA") return;
    if (!rows.length) return;
    const key = ev.key.toLowerCase();
    if (key === "j") { index = Math.min(rows.length - 1, index + 1); render(); return; }
    if (key === "k") { index = Math.max(0, index - 1); render(); return; }
    if (choosable) {
      const o = options.find((x) => x.hotkey && x.hotkey.toLowerCase() === key);
      if (o) { ev.preventDefault(); settle(o.value); }
    }
  }
  document.addEventListener("keydown", onKey);
  await load();
  return () => document.removeEventListener("keydown", onKey);
}
