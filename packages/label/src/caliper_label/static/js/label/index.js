// caliper label: the labelling screen (#/label[?queue=NAME]).
//
// Keyboard-first, everything clickable. The next cards (and their images) are
// prefetched, so deciding swaps to the next card from memory: the label is
// POSTed in the background (in order), and the prefetch queue is refilled from
// GET /api/next?n=6 after each one. window.__caliperLabel.latencies records
// keypress -> next card painted, in ms.

import {
  announce, askName, button, doneState, el, emptyState, errorState, keyLabel, keyName,
  labellerName, link, loadingState, store, toast, trapFocus,
} from "../common/dom.js";
import { cardImages, preloadImages, renderCard } from "./card.js";
import { decisionText, makePanel } from "./decide.js";
import { renderMarkdown } from "../common/markdown.js";

const PREFETCH = 5;
const QUEUE_KEY = "caliper-label-queue";

const metrics = (window.__caliperLabel = window.__caliperLabel || { latencies: [] });

export async function mount(root, ctx) {
  const { api, query, go } = ctx;
  root.replaceChildren(loadingState("Opening the labelling screen…"));
  let cfg;
  try {
    cfg = await api.get("/api/label-config");
  } catch (err) {
    root.replaceChildren(errorState(err, () => mount(root, ctx)));
    return;
  }

  const S = {
    labeller: labellerName() || cfg.default_labeller || null,
    queue: null,
    item: null,        // the card on screen
    upcoming: [],      // prefetched payloads, in serving order
    sent: new Set(),   // ids labelled from this page (filter stale prefetch)
    chain: Promise.resolve(), // POSTs, in order
    note: "",
    pending: null,     // a decision waiting for its required note
    done: null,        // {reason} when nothing is left
    zoom: -1,
    refilling: null,
    refillAgain: false,
    inflight: 0,       // POSTs not yet answered
    waitingSince: undefined,
    lastQueues: null,
  };
  const qParam = query.get("queue") || store(QUEUE_KEY);
  S.queue = qParam && cfg.queues.includes(qParam) ? qParam : null;

  // ------------------------------------------------------------ layout
  const queueList = el("ul", { class: "q-list" });
  const labellersBox = el("div", { class: "q-labellers" });
  const aside = el("nav", { class: "lbl-queues", "aria-label": "Queues" },
    el("h2", { class: "lbl-h", text: "Queues" }), queueList, labellersBox);
  const progress = el("div", { class: "lbl-progress", "aria-live": "off" });
  const instructions = cfg.instructions
    ? el("details", { class: "lbl-instructions", open: store("caliper-label-instr") !== "closed" },
      el("summary", { text: "Instructions" }), el("div", { class: "instr-body" }, renderMarkdown(cfg.instructions)))
    : null;
  if (instructions) instructions.addEventListener("toggle", () => store("caliper-label-instr", instructions.open ? null : "closed"));
  const card = el("article", { class: "card", id: "card", "aria-label": "Item to label", tabindex: "-1" });
  const panel = makePanel(cfg, (decision, text, chosen) => decide(decision, text, chosen));
  const skipBtn = el("button", { type: "button", class: "btn btn-ghost", onclick: () => skip(), "aria-keyshortcuts": keyName(cfg.keys.skip) },
    el("kbd", { "aria-hidden": "true", text: keyLabel(cfg.keys.skip) }), el("span", { text: "Skip" }));
  const undoBtn = el("button", { type: "button", class: "btn btn-ghost", onclick: () => undo(), "aria-keyshortcuts": keyName(cfg.keys.undo) },
    el("kbd", { "aria-hidden": "true", text: keyLabel(cfg.keys.undo) }), el("span", { text: "Undo" }));
  const noteBtn = el("button", { type: "button", class: "btn btn-ghost", onclick: () => openNote(), "aria-keyshortcuts": keyName(cfg.keys.note), "aria-expanded": "false", "aria-controls": "note-wrap" },
    el("kbd", { "aria-hidden": "true", text: keyLabel(cfg.keys.note) }), el("span", { text: "Note" }));
  const noteInput = el("textarea", { id: "note", rows: "2", maxlength: "2000", placeholder: "Why? (saved with the next decision)" });
  const noteMsg = el("span", { class: "note-msg", "aria-live": "assertive" });
  const noteWrap = el("div", { class: "note-wrap", id: "note-wrap", hidden: true },
    el("label", { for: "note" }, "Note ", el("span", { class: "muted", text: "Enter saves · Esc returns to the keys" })),
    noteMsg, noteInput);
  const decideBox = el("section", { class: "decide", "aria-label": "Decide" },
    panel.el,
    el("div", { class: "secondary" }, skipBtn, undoBtn, noteBtn),
    noteWrap);
  const stage = el("section", { class: "lbl-stage" }, progress, instructions, card, decideBox);
  const keybar = el("footer", { class: "keybar", "aria-label": "Keyboard shortcuts" });
  const layout = el("div", { class: "lbl" + (cfg.queues.length > 1 ? "" : " single-queue") }, aside, stage);
  root.replaceChildren(layout, keybar);

  // app bar: who + help
  const slot = document.getElementById("appbar-slot");
  const whoBtn = el("button", { type: "button", class: "btn btn-ghost who", title: "Change labeller", onclick: () => changeName() });
  const helpBtn = el("button", { type: "button", class: "btn btn-ghost", "aria-label": "Keyboard shortcuts (?)", onclick: () => toggleHelp(), text: "?" });
  slot.replaceChildren(whoBtn, helpBtn);

  // ------------------------------------------------------------ helpers
  const optionText = (d) => decisionText(cfg, d);

  function shortcutRows() {
    const k = cfg.keys;
    return [
      ...panel.keys(),
      [k.skip, "Skip (comes back after the rest)"],
      [k.undo, "Undo your last label"],
      [k.note, "Add a note to the next decision"],
      ["z", "Image full size (click works too)"],
      ["escape", "Close overlay / leave a text box"],
      ["?", "Keyboard shortcuts"],
    ];
  }

  function renderKeys() {
    keybar.replaceChildren(...shortcutRows().map(([key, what]) =>
      el("span", {}, el("kbd", { text: keyLabel(key) }), " " + what.split(" (")[0])));
  }

  function setWho() {
    whoBtn.replaceChildren(el("span", { class: "dot", "aria-hidden": "true" }), el("span", { text: S.labeller || "Name?" }));
    whoBtn.setAttribute("aria-label", `Labelling as ${S.labeller}. Change name`);
  }

  // ------------------------------------------------------------ rendering
  function paint(t0) {
    card.classList.remove("flash");
    if (!S.item) {
      if (S.done) card.replaceChildren(doneView(S.done));
      else card.replaceChildren(el("div", { class: "card-loading", "aria-busy": "true" },
        el("div", { class: "skeleton", style: "height: 18px; width: 30%" }),
        el("div", { class: "skeleton", style: "height: 80px; margin-top: 16px" }),
        el("div", { class: "skeleton", style: "height: 18px; width: 70%; margin-top: 12px" })));
      decideBox.hidden = !!S.done;
      card.classList.toggle("is-done", !!S.done);
      panel.reset(null);
      return;
    }
    card.classList.remove("is-done");
    decideBox.hidden = false;
    card.replaceChildren(...renderCard(S.item, { onZoom: openZoom, optionLabel: optionText }));
    void card.offsetWidth;
    card.classList.add("flash");
    panel.reset(S.item);
    syncZoom();
    if (panel.autofocus && !noteWrap.contains(document.activeElement)) panel.focus();
    if (t0 !== undefined) {
      requestAnimationFrame(() => setTimeout(() => {
        metrics.latencies.push(performance.now() - t0);
      }, 0));
    }
  }

  function doneView(done) {
    const qs = S.lastQueues || [];
    if (done.reason === "empty") {
      return emptyState("No items to label yet",
        cfg.mode === "project"
          ? "This project's snapshot has no items. Check the data source in Settings, then refresh from source."
          : "The data file has no rows.",
        cfg.mode === "project" ? [link("#/settings", "Open settings", true)] : []);
    }
    if (done.reason === "queue_done" && S.queue) {
      const others = qs.filter((q) => q.name !== S.queue && q.left > 0);
      return doneState(`Queue “${S.queue}” is done`,
        `You've labelled everything in this queue that is available to you. ${others.reduce((a, q) => a + q.left, 0)} items are left in other queues.`,
        [button("Label all queues", () => selectQueue(null), "btn btn-primary"),
          ...others.slice(0, 3).map((q) => button(`${q.name} (${q.left} left)`, () => selectQueue(q.name)))]);
    }
    return doneState("All done",
      cfg.double_label > 1
        ? "Nothing is left for you to label. Items still waiting for a second labeller will show up on the dashboard."
        : "Every item has its label. Check agreement on the dashboard, or export the labels.",
      [link("#/export", "Export labels", true), link("#/dashboard", "See the dashboard"),
        button("Undo last", () => undo(), "btn btn-ghost")]);
  }

  async function refreshQueues() {
    const data = await api.get(`/api/queues?labeller=${encodeURIComponent(S.labeller)}`);
    S.lastQueues = data.queues;
    const all = data.queues.reduce((a, q) => ({
      name: null, total: a.total + q.total, complete: a.complete + q.complete, left: a.left + q.left,
    }), { total: 0, complete: 0, left: 0 });
    const rows = data.queues.length > 1 ? [all, ...data.queues] : data.queues.map((q) => ({ ...q, name: null }));
    queueList.replaceChildren(...rows.map((q) => {
      const pct = q.total ? Math.round((100 * q.complete) / q.total) : 0;
      const done = q.left === 0;
      return el("li", { class: done ? "q-done" : "" }, el("button", {
        type: "button",
        "aria-current": q.name === S.queue ? "true" : "false",
        "aria-label": `${q.name === null ? "All items" : q.name}: ${q.complete} of ${q.total} complete, ${q.left} left for you`,
        onclick: () => selectQueue(q.name),
      },
      el("span", { class: "q-name", text: q.name === null ? "All items" : q.name }),
      el("span", { class: "q-left", text: done ? "done" : `${q.left} left` }),
      el("span", { class: "q-bar", "aria-hidden": "true" }, el("span", { style: `width:${pct}%` }))));
    }));
    const pctAll = all.total ? Math.round((100 * all.complete) / all.total) : 0;
    progress.replaceChildren(...[
      el("div", { class: "lbl-progress-bar", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(pctAll), "aria-label": "Items complete" },
        el("span", { style: `width:${pctAll}%` })),
      el("span", { class: "num", text: `${all.complete} / ${all.total} complete` }),
      el("span", { class: "muted num", text: ` · ${all.left} left for you` }),
      cfg.double_label > 1 ? el("span", { class: "muted", text: ` · ${cfg.double_label} labels per item` }) : null].filter(Boolean));
    const entries = Object.entries(data.labellers);
    labellersBox.replaceChildren(...(entries.length
      ? [el("h2", { class: "lbl-h", text: "Labels so far" }), el("ul", {}, ...entries.map(([n, c]) => el("li", {}, el("span", { text: n }), el("b", { class: "num", text: String(c) }))))]
      : []));
  }

  let queuesTimer = null;
  function refreshQueuesSoon() {
    if (queuesTimer) return;
    queuesTimer = setTimeout(() => { queuesTimer = null; refreshQueues().catch(() => {}); }, 600);
  }

  // ------------------------------------------------------------ prefetch
  // One refill in flight at a time; a request made meanwhile runs right after it,
  // so the queue reflects every label already sent.
  function refill() {
    if (S.refilling) { S.refillAgain = true; return S.refilling; }
    const q = S.queue ? `&queue=${encodeURIComponent(S.queue)}` : "";
    const want = S.queue;
    const n = Math.min(20, PREFETCH + 1 + S.inflight);
    S.refilling = api.get(`/api/next?labeller=${encodeURIComponent(S.labeller)}&n=${n}${q}`)
      .then((data) => {
        if (want !== S.queue) return;
        const cur = S.item && S.item.id;
        S.upcoming = data.items.filter((it) => it.id !== cur && !S.sent.has(it.id)).slice(0, PREFETCH);
        preloadImages(S.upcoming);
        S.done = data.done ? { reason: data.reason } : null;
        if (!S.item) {
          S.item = S.upcoming.shift() || null;
          if (S.item) { S.done = null; announceCard(); }
          else if (!S.done && !S.inflight) S.done = { reason: "all_done" };
          if (S.item) { paint(S.waitingSince); S.waitingSince = undefined; } else paint();
        }
      })
      .finally(() => {
        S.refilling = null;
        if (S.refillAgain) { S.refillAgain = false; refill().catch(showError); }
      });
    return S.refilling;
  }

  function announceCard(prefix = "") {
    if (!S.item) return;
    announce(`${prefix}Item ${S.item.id}${S.item.skipped_before ? ", skipped earlier" : ""}.`);
  }

  function advance(t0) {
    S.item = S.upcoming.shift() || null;
    if (!S.item) {
      S.done = null; // unknown until the refill answers
      S.waitingSince = t0; // the latency metric covers the wait too
      metrics.waits = (metrics.waits || 0) + 1;
      paint();
      refill().catch(showError);
      return;
    }
    paint(t0);
  }

  // ------------------------------------------------------------ actions
  function send(item, body, labelText) {
    S.sent.add(item.id);
    S.inflight += 1;
    S.chain = S.chain
      .then(() => api.post("/api/labels", { item_id: item.id, labeller: S.labeller, ...body }))
      .finally(() => { S.inflight -= 1; })
      .then(() => {
        refreshQueuesSoon();
      }, (err) => {
        S.sent.delete(item.id);
        if (err.status === 409 && /already labelled/.test(err.message)) {
          toast(`${item.id} was already labelled by you`);
          return;
        }
        // Put the item back on screen so nothing is lost.
        if (S.item) S.upcoming.unshift(S.item);
        S.item = item;
        S.done = null;
        paint();
        if (err.status === 422 && /note/.test(err.message)) {
          S.pending = { decision: body.decision, text: labelText };
          openNote(`${err.message}. Write why, then press Enter.`);
        } else {
          toast(`Not saved: ${err.message}${err.hint ? `. ${err.hint}` : ""}`, "error");
          announce(`Not saved: ${err.message}`);
        }
      })
      .then(() => refill().catch(showError));
    return S.chain;
  }

  function decide(decision, text, chosen) {
    if (!S.item) return;
    const needsNote = (chosen || []).filter((o) => o.requires_note);
    const note = (S.note || noteInput.value || "").trim();
    if (needsNote.length && !note) {
      S.pending = { decision, text, chosen };
      openNote(`“${needsNote.map((o) => o.label).join(", ")}” needs a note. Write why, then press Enter.`);
      return;
    }
    const t0 = performance.now();
    const item = S.item;
    S.pending = null;
    setNote("");
    advance(t0);
    announce(`Saved ${text} for ${item.id}.${S.item ? ` Next: item ${S.item.id}.` : ""}`);
    send(item, { decision, note: note || null }, text);
  }

  function skip() {
    if (!S.item) return;
    const t0 = performance.now();
    const item = S.item;
    const note = (S.note || "").trim();
    setNote("");
    advance(t0);
    announce(`Skipped ${item.id}.${S.item ? ` Next: item ${S.item.id}.` : ""}`);
    send(item, { skip: true, decision: null, note: note || null }, "skip");
  }

  async function undo() {
    try {
      await S.chain; // every pending label is saved first
      const data = await api.post("/api/undo", { labeller: S.labeller });
      if (!data.undone) { toast("Nothing to undo"); announce("Nothing to undo"); return; }
      S.sent.delete(data.undone.item_id);
      if (S.item && S.item.id !== data.item.id) S.upcoming.unshift(S.item);
      S.upcoming = S.upcoming.filter((it) => it.id !== data.item.id);
      S.item = data.item;
      S.done = null;
      const what = data.undone.skipped ? "skip" : optionText(data.undone.decision);
      toast(`Undid ${what} on ${data.undone.item_id}`);
      announce(`Undid ${what} on ${data.undone.item_id}. Item ${data.item.id} is back.`);
      if (data.undone.note) setNote(data.undone.note, true);
      paint();
      refreshQueuesSoon();
    } catch (err) {
      showError(err);
    }
  }

  function setNote(text, show) {
    S.note = text;
    noteInput.value = text;
    noteBtn.classList.toggle("has-note", !!text);
    noteBtn.querySelector("span").textContent = text ? "Note ✓" : "Note";
    if (!text && !show) { noteWrap.hidden = true; noteMsg.textContent = ""; noteBtn.setAttribute("aria-expanded", "false"); }
    if (show) { noteWrap.hidden = false; noteBtn.setAttribute("aria-expanded", "true"); }
  }

  function openNote(msg) {
    noteWrap.hidden = false;
    noteBtn.setAttribute("aria-expanded", "true");
    noteMsg.textContent = msg || "";
    noteWrap.classList.toggle("needs", !!msg);
    noteInput.focus();
  }

  function closeNote() {
    setNote(noteInput.value.trim(), !!noteInput.value.trim());
    noteMsg.textContent = "";
    noteWrap.classList.remove("needs");
    noteInput.blur();
    if (!S.note) { noteWrap.hidden = true; noteBtn.setAttribute("aria-expanded", "false"); }
    card.focus({ preventScroll: true });
  }

  function showError(err) {
    toast(err && err.message ? `${err.message}${err.hint ? `. ${err.hint}` : ""}` : String(err), "error");
  }

  function selectQueue(name) {
    S.queue = name;
    store(QUEUE_KEY, name);
    S.upcoming = [];
    S.item = null;
    S.done = null;
    paint();
    refreshQueues().catch(showError);
    refill().catch(showError);
  }

  async function changeName() {
    const name = await askName({ current: S.labeller, title: "Who is labelling?", confirm: "Continue" });
    if (name === S.labeller) return;
    await S.chain;
    S.labeller = name;
    setWho();
    selectQueue(S.queue);
  }

  // ------------------------------------------------------------ overlays
  let zoomEl = null;
  let helpEl = null;
  let releaseHelp = null;

  function openZoom(i) {
    const imgs = cardImages(S.item);
    if (!imgs.length) return;
    S.zoom = Math.max(0, Math.min(i, imgs.length - 1));
    const f = imgs[S.zoom];
    if (!zoomEl) {
      zoomEl = el("div", { class: "overlay zoom", role: "dialog", "aria-modal": "true", "aria-label": "Image, full size", onclick: () => closeZoom() },
        el("img", { alt: "" }),
        el("div", { class: "zoom-hint", text: "Esc or Z closes · decision keys still work" }));
      document.body.append(zoomEl);
    }
    const img = zoomEl.querySelector("img");
    img.src = f.src;
    img.alt = f.label || "image";
    img.classList.toggle("vector", /\.svg(\?|$)/i.test(f.value || ""));
    zoomEl.hidden = false;
  }
  function closeZoom() { S.zoom = -1; if (zoomEl) zoomEl.hidden = true; }
  function syncZoom() {
    if (S.zoom < 0) return;
    if (cardImages(S.item).length) openZoom(S.zoom); else closeZoom();
  }

  function toggleHelp() {
    if (helpEl && !helpEl.hidden) { helpEl.hidden = true; if (releaseHelp) releaseHelp(); return; }
    if (!helpEl) {
      helpEl = el("div", { class: "overlay modal-wrap", role: "dialog", "aria-modal": "true", "aria-labelledby": "help-title", onclick: (e) => { if (e.target === helpEl) toggleHelp(); } },
        el("div", { class: "modal" },
          el("h2", { id: "help-title", text: "Keyboard shortcuts" }),
          el("table", { class: "data keys" }, el("tbody", {}, ...shortcutRows().map(([k, w]) => el("tr", {}, el("td", {}, el("kbd", { text: keyLabel(k) })), el("td", { text: w }))))),
          el("p", { class: "muted", text: "Shortcuts are off while you type in a text box. Labels are saved the moment you decide; undo retracts your last label and brings the item back." }),
          el("div", { class: "modal-actions" }, el("button", { type: "button", class: "btn", onclick: () => toggleHelp() }, "Close ", el("kbd", { text: "Esc" })))));
      document.body.append(helpEl);
    }
    helpEl.hidden = false;
    releaseHelp = trapFocus(helpEl, () => toggleHelp());
    helpEl.querySelector("button").focus();
  }

  // ------------------------------------------------------------ keyboard
  function onKey(ev) {
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (document.querySelector(".overlay.modal-wrap:not([hidden]) #name-input")) return;
    const key = ev.key.length === 1 ? ev.key.toLowerCase() : ev.key.toLowerCase();
    if (document.activeElement === noteInput) {
      if (key === "escape" || (key === "enter" && !ev.shiftKey)) {
        ev.preventDefault();
        const pending = S.pending;
        closeNote();
        if (key === "enter" && pending && S.note) decide(pending.decision, pending.text, []);
      }
      return;
    }
    if (panel.typing()) {
      if (key === "escape") { ev.preventDefault(); document.activeElement.blur(); card.focus({ preventScroll: true }); }
      return;
    }
    const tag = (document.activeElement && document.activeElement.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if (key === "escape") {
      if (zoomEl && !zoomEl.hidden) closeZoom();
      return;
    }
    if (ev.key === "?") { ev.preventDefault(); toggleHelp(); return; }
    if (helpEl && !helpEl.hidden) return;
    if (key === "z") { ev.preventDefault(); if (zoomEl && !zoomEl.hidden) closeZoom(); else openZoom(0); return; }
    // Enter on a focused button clicks it; don't also treat it as a shortcut.
    if (key === "enter" && tag === "BUTTON" && !(cfg.type === "multi")) return;
    if (key === " " && tag === "BUTTON") return;
    if (!S.item) {
      if (key === cfg.keys.undo) { ev.preventDefault(); undo(); }
      return;
    }
    if (panel.handleKey(key, ev)) { ev.preventDefault(); return; }
    const k = cfg.keys;
    if (key === k.skip) { ev.preventDefault(); skip(); return; }
    if (key === k.undo) { ev.preventDefault(); undo(); return; }
    if (key === k.note) { ev.preventDefault(); openNote(); return; }
  }

  noteInput.addEventListener("input", () => {
    S.note = noteInput.value.trim();
    noteBtn.classList.toggle("has-note", !!S.note);
  });
  document.addEventListener("keydown", onKey);
  const poll = setInterval(() => { if (S.labeller && !document.hidden) refreshQueues().catch(() => {}); }, 15000);
  const onFocus = () => { if (S.labeller) refreshQueues().catch(() => {}); };
  window.addEventListener("focus", onFocus);

  // ------------------------------------------------------------ start
  renderKeys();
  paint();
  if (!S.labeller) S.labeller = await askName({ current: cfg.default_labeller });
  setWho();
  try {
    await Promise.all([refreshQueues(), refill()]);
  } catch (err) {
    card.replaceChildren(errorState(err, () => go("label")));
    decideBox.hidden = true;
  }
  card.focus({ preventScroll: true });

  return () => {
    document.removeEventListener("keydown", onKey);
    window.removeEventListener("focus", onFocus);
    clearInterval(poll);
    clearTimeout(queuesTimer);
    if (zoomEl) zoomEl.remove();
    if (helpEl) helpEl.remove();
    slot.replaceChildren();
  };
}
