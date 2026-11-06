// Small DOM + UI helpers for the start screen, wizard and settings (dev 2).
// No framework: `h()` builds elements, everything user-provided goes in as text.

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k === "style" && typeof v === "object") {
      for (const [sk, sv] of Object.entries(v)) {
        if (sk.startsWith("--")) el.style.setProperty(sk, String(sv));
        else el.style[sk] = sv;
      }
    }
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function clear(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

// Load a screen stylesheet once (index.html may already list it).
export function ensureCss(name) {
  const href = `/static/css/${name}.css`;
  if ([...document.querySelectorAll("link[rel=stylesheet]")].some((l) => l.getAttribute("href") === href)) return;
  document.head.append(h("link", { rel: "stylesheet", href }));
}

let uid = 0;
export const nextId = (p = "wz") => `${p}-${++uid}`;

// Polite live region shared by the setup screens.
export function announce(text) {
  let live = document.getElementById("wz-live");
  if (!live) {
    live = h("div", { id: "wz-live", class: "wz-sr", "aria-live": "polite", role: "status" });
    document.body.append(live);
  }
  live.textContent = "";
  setTimeout(() => (live.textContent = text), 30);
}

export function toast(text, kind = "ok") {
  const t = h("div", { class: `wz-toast wz-toast-${kind}`, role: kind === "error" ? "alert" : "status" }, text);
  document.body.append(t);
  setTimeout(() => t.classList.add("wz-toast-out"), 2600);
  setTimeout(() => t.remove(), 3200);
}

// Designed loading / empty / error states.
export function stateBox(kind, title, body, ...actions) {
  const role = kind === "error" ? "alert" : kind === "loading" ? "status" : null;
  return h(
    "div",
    { class: `wz-state wz-state-${kind}`, role },
    kind === "loading" ? h("span", { class: "wz-spinner", "aria-hidden": "true" }) : null,
    h("div", { class: "wz-state-text" },
      h("strong", {}, title),
      body ? h("p", {}, body) : null,
      actions.length ? h("div", { class: "wz-state-actions" }, actions) : null),
  );
}

export function errorBox(err, retry) {
  const hint = err && err.hint;
  return stateBox(
    "error",
    (err && err.message) || String(err),
    hint ? `How to fix: ${hint}` : null,
    retry ? h("button", { type: "button", class: "wz-btn", onclick: retry }, "Try again") : null,
  );
}

export function debounce(fn, ms = 250) {
  let t;
  return (...a) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...a), ms);
  };
}

export function slug(text) {
  const s = String(text || "")
    .normalize("NFKD").replace(/[̀-ͯ]/g, "")
    .toLowerCase().replace(/[^0-9a-z]+/g, "_").replace(/^_+|_+$/g, "");
  return s || "option";
}

export function plural(n, word, many) {
  return `${n.toLocaleString()} ${n === 1 ? word : many || word + "s"}`;
}

export function relTime(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

// Minimal, safe markdown for instructions: escapes everything, then allows headings, lists,
// bold/italic, inline code, links (http/https only), tables and paragraphs.
export function markdown(src) {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const inline = (s) =>
    esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  const lines = String(src || "").replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const ln = lines[i];
    if (!ln.trim()) { i++; continue; }
    const hm = ln.match(/^(#{1,4})\s+(.*)$/);
    if (hm) { const n = Math.min(hm[1].length + 2, 6); out.push(`<h${n}>${inline(hm[2])}</h${n}>`); i++; continue; }
    if (/^\s*[-*]\s+/.test(ln)) {
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) items.push(`<li>${inline(lines[i++].replace(/^\s*[-*]\s+/, ""))}</li>`);
      out.push(`<ul>${items.join("")}</ul>`); continue;
    }
    if (/^\s*\d+[.)]\s+/.test(ln)) {
      const items = [];
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) items.push(`<li>${inline(lines[i++].replace(/^\s*\d+[.)]\s+/, ""))}</li>`);
      out.push(`<ol>${items.join("")}</ol>`); continue;
    }
    if (/^\s*\|.*\|\s*$/.test(ln)) {
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(lines[i++]);
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const body = rows.filter((r) => !/^\s*\|?\s*:?-{2,}/.test(r));
      const [head, ...rest] = body;
      out.push("<table><thead><tr>" + cells(head).map((c) => `<th>${inline(c)}</th>`).join("") + "</tr></thead><tbody>" +
        rest.map((r) => "<tr>" + cells(r).map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>").join("") + "</tbody></table>");
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|\s*[-*]\s|\s*\d+[.)]\s|\s*\|)/.test(lines[i])) para.push(lines[i++]);
    out.push(`<p>${inline(para.join(" "))}</p>`);
  }
  return out.join("\n");
}

// A modal dialog (native <dialog>): focus is trapped by the browser, Esc closes.
export function dialog({ title, body, actions = [], alert = false, wide = false }) {
  const d = h("dialog", {
    class: `wz-dialog${wide ? " wz-dialog-wide" : ""}`,
    role: alert ? "alertdialog" : "dialog",
    "aria-labelledby": "wz-dialog-title",
  },
    h("h2", { id: "wz-dialog-title" }, title),
    h("div", { class: "wz-dialog-body" }, body),
    h("div", { class: "wz-dialog-actions" }, actions),
  );
  document.body.append(d);
  d.addEventListener("close", () => d.remove());
  d.showModal();
  return d;
}

export function confirmDialog({ title, body, ok = "Confirm", cancel = "Cancel", danger = false }) {
  return new Promise((resolve) => {
    let result = false;
    const okBtn = h("button", { type: "button", class: `wz-btn ${danger ? "wz-btn-danger" : "wz-btn-primary"}`,
      onclick: () => { result = true; d.close(); } }, ok);
    const d = dialog({
      title, body, alert: true,
      actions: [h("button", { type: "button", class: "wz-btn", onclick: () => d.close() }, cancel), okBtn],
    });
    d.addEventListener("close", () => resolve(result));
    okBtn.focus();
  });
}

export function kbd(key) {
  const names = { " ": "Space", backspace: "Backspace", delete: "Delete", escape: "Esc", enter: "Enter", tab: "Tab" };
  return h("kbd", {}, names[String(key).toLowerCase()] || String(key).toUpperCase());
}

// Dev 3's shell: a global app bar (#appbar-slot is free for the current screen).
export const hasAppBar = () => !!document.querySelector(".appbar");

export function appbarSlot(...children) {
  const slot = document.getElementById("appbar-slot");
  if (!slot) return () => {};
  slot.replaceChildren(...children.flat().filter(Boolean));
  return () => slot.replaceChildren();
}

export function projectChanged() {
  window.dispatchEvent(new Event("caliper:project-changed"));
}
