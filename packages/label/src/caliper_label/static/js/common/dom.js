// Shared DOM helpers (dev 3 owns this file; anyone may import it).
//
// Every data value goes through textContent / setAttribute: never innerHTML.

export function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "style") node.setAttribute("style", v);
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) node.append(c);
  return node;
}

export const $ = (sel, root = document) => root.querySelector(sel);

// Stable colour from text (badges).
export function hue(text) {
  let h = 0;
  for (const ch of String(text)) h = (h * 31 + ch.codePointAt(0)) % 360;
  return h;
}

const KEY_NAMES = { backspace: "⌫", delete: "Del", " ": "Space", enter: "Enter", tab: "Tab", escape: "Esc" };
export const keyLabel = (k) => (k ? KEY_NAMES[k] || (k.length === 1 ? k.toUpperCase() : k) : "");
export const keyName = (k) => ({ backspace: "Backspace", " ": "Space", escape: "Escape" }[k] || keyLabel(k));

// Screen-reader announcement (the polite live region in index.html).
let announceTimer = null;
export function announce(text) {
  const box = document.getElementById("announcer");
  if (!box) return;
  clearTimeout(announceTimer);
  box.textContent = "";
  announceTimer = setTimeout(() => { box.textContent = text; }, 30);
}

let toastTimer = null;
export function toast(msg, kind) {
  const t = document.getElementById("toast");
  if (!t) return;
  t.textContent = msg;
  t.className = "toast" + (kind ? ` toast-${kind}` : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, kind === "error" ? 5000 : 1800);
}

export function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch { /* private mode: not remembered */ }
  return null;
}

// ---------------------------------------------------------------- states

function stateBox(kind, icon, title, text, actions = [], hint = null) {
  return el("section", { class: `state state-${kind}`, role: kind === "error" ? "alert" : null, "aria-labelledby": "state-title" },
    el("div", { class: "state-icon", "aria-hidden": "true", text: icon }),
    el("h2", { id: "state-title", text: title }),
    text ? el("p", { text }) : null,
    hint ? el("code", { class: "state-hint", text: hint }) : null,
    actions.length ? el("div", { class: "state-actions" }, ...actions) : null,
  );
}

export function loadingState(text = "Loading…") {
  return el("section", { class: "state state-loading", "aria-busy": "true" },
    el("div", { class: "skeleton", style: "height: 14px; width: 60%; margin: 0 auto 12px" }),
    el("div", { class: "skeleton", style: "height: 14px; width: 40%; margin: 0 auto 16px" }),
    el("p", { text }));
}

export function emptyState(title, text, actions, icon = "○") {
  return stateBox("empty", icon, title, text, actions);
}

export function doneState(title, text, actions) {
  return stateBox("done", "✓", title, text, actions);
}

export function link(href, text, primary) {
  return el("a", { class: "btn" + (primary ? " btn-primary" : ""), href, text });
}

export function button(text, onclick, cls = "btn") {
  return el("button", { type: "button", class: cls, onclick, text });
}

// An API error as a designed state: what went wrong, how to fix it, what to do next.
export function errorState(err, retry) {
  if (err && err.status === 409 && /no project open/i.test(err.message)) {
    return stateBox("empty", "○", "No project open",
      "Open a project or create one from the start screen.",
      [link("#/start", "Go to the start screen", true)]);
  }
  if (err && err.status === 401) {
    return stateBox("error", "!", "Access token needed",
      "This server is in team mode. Open the link you were given (it ends in ?token=…).",
      [button("Reload", () => location.reload(), "btn btn-primary")]);
  }
  const actions = [];
  if (retry) actions.push(button("Try again", retry, "btn btn-primary"));
  const offline = err instanceof TypeError; // fetch failed: server down
  return stateBox("error", "!",
    offline ? "Can't reach the labelling server" : "Something went wrong",
    offline ? "Is `caliper label` still running? Start it again, then try again." : (err && err.message) || String(err),
    actions, err && err.hint ? `Fix: ${err.hint}` : null);
}

export function fmtPct(x) { return x === null || x === undefined ? "–" : `${Math.round(x * 100)}%`; }
export function fmtKappa(s) { return !s || s.kappa === null || s.kappa === undefined ? "–" : s.kappa.toFixed(2); }
export function fmtTime(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

// The labeller name, shared by the label, adjudicate and export screens.
export const NAME_KEY = "caliper-label-name";
export function labellerName() { return store(NAME_KEY); }

// Modal dialog asking for the labeller's name. Resolves with the name.
export function askName({ current, title = "Who is labelling?", text, confirm = "Start labelling" } = {}) {
  return new Promise((resolve) => {
    const prev = document.activeElement;
    const input = el("input", { id: "name-input", autocomplete: "nickname", maxlength: "80", required: true, placeholder: "Your name", value: current || "" });
    const form = el("form", { class: "modal narrow", "aria-labelledby": "name-title" },
      el("h2", { id: "name-title", text: title }),
      el("p", { class: "muted", text: text || "Every label records your name and the time. Kept in this browser only." }),
      el("label", { for: "name-input", class: "visually-hidden", text: "Your name" }),
      input,
      el("div", { class: "modal-actions" }, el("button", { type: "submit", class: "btn btn-primary", text: confirm })));
    const wrap = el("div", { class: "overlay modal-wrap", role: "dialog", "aria-modal": "true", "aria-labelledby": "name-title" }, form);
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const name = input.value.trim();
      if (!name) return;
      store(NAME_KEY, name);
      wrap.remove();
      if (prev && prev.focus) prev.focus();
      resolve(name);
    });
    document.body.append(wrap);
    setTimeout(() => { input.focus(); input.select(); }, 0);
  });
}

// Focus trap + Escape for a dialog element; returns a release function.
export function trapFocus(dialog, onEscape) {
  const prev = document.activeElement;
  function onKey(e) {
    if (e.key === "Escape" && onEscape) { e.preventDefault(); e.stopPropagation(); onEscape(); return; }
    if (e.key !== "Tab") return;
    const f = [...dialog.querySelectorAll("button, [href], input, textarea, select, [tabindex]:not([tabindex='-1'])")].filter((x) => !x.disabled && x.offsetParent !== null);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
  dialog.addEventListener("keydown", onKey);
  return () => { dialog.removeEventListener("keydown", onKey); if (prev && prev.focus) prev.focus(); };
}
