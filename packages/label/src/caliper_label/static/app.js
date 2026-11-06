// caliper label: vanilla JS, no build step. Every data value goes through textContent.
"use strict";

const $ = (id) => document.getElementById(id);
const S = {
  config: null,
  labeller: null,
  queue: null, // null = all queues
  item: null,
  note: "",
  busy: false,
  zoomIndex: -1, // index into the current card's images when the zoom overlay is open
};

const KEY_NAMES = { backspace: "⌫ Backspace", delete: "Del", " ": "Space", enter: "Enter", tab: "Tab" };
const keyLabel = (k) => KEY_NAMES[k] || k.toUpperCase();

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch (_) { /* private mode: fine, just not remembered */ }
  return null;
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${res.status}`);
  return data;
}

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "style") node.setAttribute("style", v);
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) if (c !== null && c !== undefined) node.append(c);
  return node;
}

function hue(text) {
  let h = 0;
  for (const ch of String(text)) h = (h * 31 + ch.codePointAt(0)) % 360;
  return h;
}

let toastTimer = null;
function toast(msg, error) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.toggle("error", !!error);
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, error ? 4000 : 1800);
}

// ------------------------------------------------------------------ rendering

function renderField(f) {
  const label = el("div", { class: "field-label", text: f.label });
  if (f.value === null) return el("div", { class: "field" }, label, el("div", { class: "f-empty", text: "—" }));
  let body;
  if (f.type === "badge") {
    body = el("span", { class: "f-badge", style: `--h:${hue(f.value)}`, text: f.value });
  } else if (f.type === "number") {
    body = el("div", { class: "f-number", text: f.value });
  } else if (f.type === "link") {
    body = el("div", { class: "f-link" }, f.href
      ? el("a", { href: f.href, target: "_blank", rel: "noopener noreferrer", text: f.value })
      : el("span", { text: f.value }));
  } else {
    body = el("div", { class: "f-text" + (f.mono ? " mono" : ""), text: f.value });
  }
  return el("div", { class: "field f-" + f.type }, label, body);
}

function images() {
  return S.item ? S.item.fields.filter((f) => f.type === "image" && f.src) : [];
}

function renderCard() {
  const card = $("card");
  card.replaceChildren();
  card.classList.remove("flash");
  void card.offsetWidth;
  card.classList.add("flash");
  const it = S.item;
  if (!it) {
    renderDone(card);
    return;
  }
  const head = el("div", { class: "card-head" },
    el("span", { class: "card-id", text: it.id }),
    it.queue !== "all" ? el("span", { class: "tag", text: it.queue }) : null,
    it.labels_needed > 1 ? el("span", { class: "tag", text: `labels ${it.labels_so_far}/${it.labels_needed}` }) : null,
    it.skipped_before ? el("span", { class: "tag warn", text: "skipped earlier" }) : null,
    it.my_decision ? el("span", { class: "tag", text: `you: ${it.my_decision}` }) : null,
  );
  const media = el("div", { class: "media" });
  const prose = el("div", { class: "fields" });
  const chips = el("div", { class: "chips" });
  images().forEach((f, i) => {
    const img = el("img", { src: f.src, alt: f.label, loading: "eager", onclick: () => openZoom(i) });
    img.addEventListener("error", () => { img.replaceWith(el("div", { class: "f-empty", text: "image not available" })); });
    media.append(el("figure", {}, img, el("figcaption", { text: f.label })));
  });
  for (const f of it.fields) {
    if (f.type === "image") {
      if (!f.src) prose.append(renderField({ ...f, type: "text" }));
      continue;
    }
    if (f.type === "badge" || f.type === "number") chips.append(renderField(f));
    else prose.append(renderField(f));
  }
  if (chips.childNodes.length) prose.prepend(chips);
  const body = el("div", { class: "card-body" + (media.childNodes.length ? " has-media" : "") });
  if (media.childNodes.length) body.append(media);
  body.append(prose);
  card.append(head, body);
  document.querySelectorAll(".decisions button").forEach((b) => {
    b.classList.toggle("mine", b.dataset.value === it.my_decision);
  });
}

function renderDone(card) {
  const q = S.queue ? `queue “${S.queue}”` : "every queue";
  card.append(el("div", { class: "done" },
    el("h3", { text: "Nothing left to label" }),
    el("p", { class: "muted", text: `You have labelled everything available to you in ${q}. Pick another queue, or press ${keyLabel(S.config.keys.undo)} to undo the last one.` }),
  ));
}

function renderDecisions() {
  const box = $("decisions");
  box.replaceChildren();
  for (const d of S.config.decisions) {
    box.append(el("button", {
      type: "button",
      class: d.approved ? "approved" : "",
      "data-value": d.value,
      "data-key": d.key,
      title: `${d.label} (${keyLabel(d.key)})`,
      onclick: () => decide(d.value),
    }, el("kbd", { text: keyLabel(d.key) }), el("span", { text: d.label })));
  }
  const k = S.config.keys;
  $("skip-btn").replaceChildren(el("kbd", { text: keyLabel(k.skip) }), " Skip");
  $("undo-btn").replaceChildren(el("kbd", { text: keyLabel(k.undo) }), " Undo");
  $("note-btn").replaceChildren(el("kbd", { text: keyLabel(k.note) }), " Note");
}

function shortcutRows() {
  const k = S.config.keys;
  return [
    ...S.config.decisions.map((d) => [d.key, d.label]),
    [k.skip, "Skip (comes back after the rest)"],
    [k.undo, "Undo your last label"],
    [k.note, "Add a note to the next decision"],
    ["z", "Image full size (click works too)"],
    ["escape", "Close overlay / leave the note box"],
    ["?", "This help"],
  ];
}

function renderKeys() {
  const bar = $("keybar");
  bar.replaceChildren();
  for (const [key, what] of shortcutRows()) {
    const name = key === "escape" ? "Esc" : keyLabel(key);
    bar.append(el("span", {}, el("kbd", { text: name }), " " + what.split(" (")[0]));
  }
  const table = $("help-keys");
  table.replaceChildren();
  for (const [key, what] of shortcutRows()) {
    const name = key === "escape" ? "Esc" : keyLabel(key);
    table.append(el("tr", {}, el("td", {}, el("kbd", { text: name })), el("td", { text: what })));
  }
}

async function refreshQueues() {
  const data = await api(`/api/queues?labeller=${encodeURIComponent(S.labeller)}`);
  const list = $("queue-list");
  list.replaceChildren();
  const all = data.queues.reduce((a, q) => ({
    name: null, total: a.total + q.total, complete: a.complete + q.complete, left: a.left + q.left,
  }), { total: 0, complete: 0, left: 0 });
  const rows = data.queues.length > 1 ? [all, ...data.queues] : data.queues.map((q) => ({ ...q, name: null }));
  for (const q of rows) {
    const pct = q.total ? Math.round((100 * q.complete) / q.total) : 0;
    const done = q.left === 0;
    const btn = el("button", {
      type: "button",
      "aria-current": (q.name === S.queue) ? "true" : "false",
      title: `${q.complete} of ${q.total} complete`,
      onclick: () => selectQueue(q.name),
    },
    el("span", { class: "q-name", text: q.name === null ? "All items" : q.name }),
    el("span", { class: "q-left", text: done ? "done" : `${q.left} left` }),
    el("span", { class: "q-bar" }, el("span", { style: `width:${pct}%` })));
    list.append(el("li", { class: done ? "q-done" : "" }, btn));
  }
  $("overall").textContent = `${all.complete} / ${all.total} complete · ${all.left} left for you`
    + (S.config.double_label > 1 ? ` · ${S.config.double_label} labels per item` : "");
  const who = $("labellers");
  who.replaceChildren();
  const entries = Object.entries(data.labellers);
  if (entries.length) {
    who.append("Labels so far: ");
    entries.forEach(([name, n], i) => {
      if (i) who.append(", ");
      who.append(el("b", { text: name }), ` ${n}`);
    });
  }
}

// ------------------------------------------------------------------ actions

async function loadNext() {
  const q = S.queue ? `&queue=${encodeURIComponent(S.queue)}` : "";
  const data = await api(`/api/next?labeller=${encodeURIComponent(S.labeller)}${q}`);
  S.item = data.item;
  renderCard();
  syncZoom();
}

function selectQueue(name) {
  S.queue = name;
  store("caliper-label-queue", name === null ? null : name);
  refreshQueues().catch(showError);
  loadNext().catch(showError);
}

function flashButton(value) {
  const b = document.querySelector(`.decisions button[data-value="${CSS.escape(value)}"]`);
  if (!b) return;
  b.classList.add("pressed");
  setTimeout(() => b.classList.remove("pressed"), 140);
}

async function decide(value, skip) {
  if (S.busy || !S.item) return;
  S.busy = true;
  const item = S.item;
  if (!skip) flashButton(value);
  try {
    await api("/api/label", {
      item_id: item.id, labeller: S.labeller, decision: skip ? null : value, skip: !!skip, note: S.note || null,
    });
    toast(skip ? `Skipped ${item.id}` : `${item.id} → ${value}`);
    setNote("");
    await loadNext();
    refreshQueues().catch(showError);
  } catch (e) {
    showError(e);
  } finally {
    S.busy = false;
  }
}

async function undo() {
  if (S.busy) return;
  S.busy = true;
  try {
    const data = await api("/api/undo", { labeller: S.labeller });
    if (!data.undone) {
      toast("Nothing to undo");
    } else {
      S.item = data.item;
      const what = data.undone.skipped ? "skip" : data.undone.decision;
      toast(`Undid ${what} on ${data.undone.item_id}`);
      if (data.undone.note) setNote(data.undone.note);
      renderCard();
      syncZoom();
      refreshQueues().catch(showError);
    }
  } catch (e) {
    showError(e);
  } finally {
    S.busy = false;
  }
}

function setNote(text) {
  S.note = text;
  $("note").value = text;
  $("note-btn").classList.toggle("has-note", !!text);
  if (!text) $("note-wrap").hidden = true;
}

function openNote() {
  $("note-wrap").hidden = false;
  $("note").focus();
}

function showError(e) {
  toast(e && e.message ? e.message : String(e), true);
}

// ------------------------------------------------------------------ overlays

function openZoom(i) {
  const imgs = images();
  if (!imgs.length) return;
  S.zoomIndex = Math.max(0, Math.min(i, imgs.length - 1));
  $("zoom-img").src = imgs[S.zoomIndex].src;
  $("zoom-img").alt = imgs[S.zoomIndex].label;
  // Vector images have no natural "full size": fill the screen instead.
  $("zoom-img").classList.toggle("vector", /\.svg(\?|$)/i.test(imgs[S.zoomIndex].value || ""));
  $("zoom").hidden = false;
}

function closeZoom() {
  S.zoomIndex = -1;
  $("zoom").hidden = true;
}

// After a decision the zoom stays open on the next item's image, so image-heavy
// queues can be labelled entirely in full size.
function syncZoom() {
  if (S.zoomIndex < 0) return;
  if (images().length) openZoom(S.zoomIndex);
  else closeZoom();
}

function openModal(id) { $(id).hidden = false; }
function closeModal(id) { $(id).hidden = true; }

function pct(x) { return x === null || x === undefined ? "—" : `${Math.round(x * 100)}%`; }
function kappaText(s) { return s.kappa === null ? "undefined" : s.kappa.toFixed(2); }

async function showAgreement() {
  refreshQueues().catch(() => {});
  const body = $("agreement-body");
  body.replaceChildren(el("p", { class: "muted", text: "Loading…" }));
  openModal("agreement");
  try {
    const a = await api("/api/agreement");
    body.replaceChildren();
    const box = el("div", { class: "agreement" });
    if (!a.overall.n_items) {
      box.append(el("p", { class: "muted", text:
        a.double_label > 1
          ? "No item has two labels yet. Agreement appears once two people have labelled the same item."
          : "This project labels each item once (double_label: 1). Set double_label: 2 in the config to measure agreement between labellers." }));
    } else {
      const o = a.overall;
      box.append(el("div", { class: "kappa-hero" },
        el("div", {}, el("b", { text: kappaText(o) }), el("span", { text: `${a.method === "fleiss" ? "Fleiss'" : "Cohen's"} kappa` })),
        el("div", {}, el("b", { text: pct(o.percent_all_agree) }), el("span", { text: "items where all agree" })),
        el("div", {}, el("b", { text: String(o.n_items) }), el("span", { text: "items with 2+ labels" })),
        el("div", {}, el("b", { text: String(o.n_raters) }), el("span", { text: "labellers" })),
      ));
      if (o.undefined && o.undefined_reason) box.append(el("p", { class: "muted", text: `Kappa undefined: ${o.undefined_reason}` }));
      if (a.by_category.length) {
        box.append(el("h3", { text: "By category" }));
        const t = el("table", { class: "stats" }, el("tr", {},
          el("th", { text: "Category" }), el("th", { class: "num", text: "Items" }),
          el("th", { class: "num", text: "Kappa" }), el("th", { class: "num", text: "All agree" })));
        for (const c of a.by_category) {
          t.append(el("tr", {}, el("td", { text: c.category }), el("td", { class: "num", text: String(c.n_items) }),
            el("td", { class: "num", text: kappaText(c), title: c.undefined_reason || "" }),
            el("td", { class: "num", text: pct(c.percent_all_agree) })));
        }
        box.append(t);
      }
      if (a.disagreements.length) {
        box.append(el("h3", { text: `Disagreements (${a.disagreements.length})` }));
        const ul = el("ul", { class: "disagree" });
        for (const d of a.disagreements.slice(0, 50)) {
          const who = d.labels.map((l) => `${l.labeller}: ${l.decision}${l.note ? ` (“${l.note}”)` : ""}`).join(" · ");
          ul.append(el("li", {}, el("code", { text: d.item_id }), " ", el("span", { class: "muted", text: who })));
        }
        box.append(ul);
      }
    }
    body.append(box);
  } catch (e) {
    body.replaceChildren(el("p", { text: String(e.message || e) }));
  }
}

function askName() {
  $("name-input").value = S.labeller || store("caliper-label-name") || S.config.default_labeller || "";
  openModal("name");
  setTimeout(() => $("name-input").focus(), 0);
}

// ------------------------------------------------------------------ keyboard

function onKey(ev) {
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
  const key = ev.key.length === 1 ? ev.key.toLowerCase() : ev.key.toLowerCase();
  if (!$("name").hidden) return; // the name form handles its own keys
  const typing = document.activeElement === $("note");
  if (typing) {
    if (key === "escape" || (key === "enter" && !ev.shiftKey)) {
      ev.preventDefault();
      setNote($("note").value.trim());
      $("note").blur();
      if (S.note) $("note-wrap").hidden = false;
    }
    return;
  }
  if (key === "escape") {
    if (!$("zoom").hidden) closeZoom();
    else if (!$("help").hidden) closeModal("help");
    else if (!$("agreement").hidden) closeModal("agreement");
    return;
  }
  if (ev.key === "?") { ev.preventDefault(); $("help").hidden ? openModal("help") : closeModal("help"); return; }
  if (!$("agreement").hidden || !$("help").hidden) return;
  if (key === "z") { ev.preventDefault(); $("zoom").hidden ? openZoom(0) : closeZoom(); return; }
  const k = S.config.keys;
  const d = S.config.decisions.find((x) => x.key === key);
  if (d) { ev.preventDefault(); decide(d.value); return; }
  if (key === k.skip) { ev.preventDefault(); decide(null, true); return; }
  if (key === k.undo) { ev.preventDefault(); undo(); return; }
  if (key === k.note) { ev.preventDefault(); openNote(); return; }
}

// ------------------------------------------------------------------ boot

async function start() {
  $("who-btn").textContent = S.labeller;
  store("caliper-label-name", S.labeller);
  await refreshQueues();
  await loadNext();
}

async function boot() {
  S.config = await api("/api/config");
  document.title = `${S.config.title} · caliper label`;
  $("title").textContent = S.config.title;
  if (S.config.instructions) {
    $("instructions").textContent = S.config.instructions;
    $("instructions").hidden = false;
  }
  const q = store("caliper-label-queue");
  S.queue = q && S.config.queues.includes(q) ? q : null;
  renderDecisions();
  renderKeys();
  document.addEventListener("keydown", onKey);
  $("skip-btn").onclick = () => decide(null, true);
  $("undo-btn").onclick = () => undo();
  $("note-btn").onclick = () => openNote();
  $("note").addEventListener("input", () => { S.note = $("note").value.trim(); $("note-btn").classList.toggle("has-note", !!S.note); });
  $("help-btn").onclick = () => openModal("help");
  $("agreement-btn").onclick = () => showAgreement();
  // Other labellers move the numbers too: refresh progress now and then.
  setInterval(() => { if (S.labeller && !document.hidden) refreshQueues().catch(() => {}); }, 15000);
  window.addEventListener("focus", () => { if (S.labeller) refreshQueues().catch(() => {}); });
  $("who-btn").onclick = () => askName();
  $("zoom").onclick = () => closeZoom();
  document.querySelectorAll("[data-close]").forEach((b) => { b.onclick = () => closeModal(b.closest(".overlay").id); });
  document.querySelectorAll(".modal-wrap").forEach((w) => w.addEventListener("click", (e) => { if (e.target === w && w.id !== "name") closeModal(w.id); }));
  $("name-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const name = $("name-input").value.trim();
    if (!name) return;
    S.labeller = name;
    closeModal("name");
    start().catch(showError);
  });
  const saved = store("caliper-label-name");
  if (saved) {
    S.labeller = saved;
    await start();
  } else {
    askName();
  }
}

boot().catch(showError);
