// The decision panel, one per label type. Every action has a button and a key.
//
//   single / yes_no_unsure  one button per option; its hotkey decides
//   multi                   hotkeys toggle options; Enter (or the button) submits
//   score                   one button per value (hotkeys) or a number box + Enter
//   free_text               a text box; Enter submits, Shift+Enter is a new line
//
// makePanel(cfg, onDecide) -> { el, handleKey(key, ev), reset(item), typing(), focus() }
// onDecide(decision, labelText, options) where decision is what POST /api/labels takes.

import { el, keyLabel } from "../common/dom.js";

function optionButton(o, attrs = {}) {
  const key = o.hotkey;
  return el("button", {
    type: "button",
    class: "opt" + (o.approved ? " opt-approved" : ""),
    "data-value": o.value,
    "aria-keyshortcuts": key ? key.toUpperCase() : null,
    "aria-label": `${o.label}${key ? ` (key ${keyLabel(key)})` : ""}${o.requires_note ? ", needs a note" : ""}`,
    style: o.color ? `--opt:${o.color}` : null,
    ...attrs,
  },
  key ? el("kbd", { "aria-hidden": "true", text: keyLabel(key) }) : null,
  el("span", { class: "opt-label", text: o.label }),
  o.requires_note ? el("span", { class: "opt-note", "aria-hidden": "true", title: "needs a note", text: "✎" }) : null);
}

function flash(btn) {
  if (!btn) return;
  btn.classList.add("pressed");
  setTimeout(() => btn.classList.remove("pressed"), 140);
}

function single(cfg, onDecide) {
  const opts = cfg.options.filter((o) => !o.retired);
  const box = el("div", { class: "decisions", role: "group", "aria-label": "Decision" });
  const byKey = new Map();
  for (const o of opts) {
    const b = optionButton(o, { onclick: () => { flash(b); onDecide(o.value, o.label, [o]); } });
    box.append(b);
    if (o.hotkey) byKey.set(o.hotkey.toLowerCase(), [o, b]);
  }
  return {
    el: box,
    handleKey(key) {
      const hit = byKey.get(key);
      if (!hit) return false;
      flash(hit[1]);
      onDecide(hit[0].value, hit[0].label, [hit[0]]);
      return true;
    },
    reset(item) {
      for (const b of box.querySelectorAll("button.opt")) b.classList.toggle("mine", !!item && b.dataset.value === item.my_decision);
    },
    typing: () => false,
    keys: () => opts.filter((o) => o.hotkey).map((o) => [o.hotkey, o.label]),
  };
}

function multi(cfg, onDecide) {
  const opts = cfg.options.filter((o) => !o.retired);
  const picked = new Set();
  const box = el("div", { class: "decisions decisions-multi", role: "group", "aria-label": "Pick all that apply" });
  const buttons = new Map();
  const status = el("span", { class: "multi-status muted", "aria-live": "polite" });
  const submit = el("button", { type: "button", class: "btn btn-primary submit", disabled: true, onclick: () => send() },
    el("kbd", { "aria-hidden": "true", text: "Enter" }), el("span", { text: "Save selection" }));
  function sync() {
    for (const [v, b] of buttons) b.setAttribute("aria-pressed", picked.has(v) ? "true" : "false");
    submit.disabled = picked.size === 0;
    status.textContent = picked.size ? `${picked.size} selected` : "Pick one or more, then Enter";
  }
  function toggle(o) {
    if (picked.has(o.value)) picked.delete(o.value); else picked.add(o.value);
    sync();
  }
  function send() {
    if (!picked.size) return;
    const chosen = opts.filter((o) => picked.has(o.value));
    onDecide(chosen.map((o) => o.value), chosen.map((o) => o.label).join(", "), chosen);
  }
  for (const o of opts) {
    const b = optionButton(o, { "aria-pressed": "false", onclick: () => toggle(o) });
    buttons.set(o.value, b);
    box.append(b);
  }
  const wrap = el("div", { class: "decide-multi" }, box, el("div", { class: "multi-bar" }, status, submit));
  sync();
  return {
    el: wrap,
    handleKey(key) {
      if (key === "enter") { send(); return true; }
      const o = opts.find((x) => x.hotkey && x.hotkey.toLowerCase() === key);
      if (!o) return false;
      toggle(o);
      return true;
    },
    reset(item) {
      picked.clear();
      if (item && item.my_decision) {
        try { for (const v of JSON.parse(item.my_decision)) picked.add(v); } catch { /* not a list */ }
      }
      sync();
    },
    typing: () => false,
    keys: () => [...opts.filter((o) => o.hotkey).map((o) => [o.hotkey, `Toggle ${o.label}`]), ["enter", "Save selection"]],
  };
}

function scoreValues(s) {
  if (!s || !(s.step > 0)) return [];
  const n = Math.round((s.max - s.min) / s.step);
  if (n < 0 || n > 10) return [];
  return Array.from({ length: n + 1 }, (_, i) => +(s.min + i * s.step).toFixed(6));
}

function score(cfg, onDecide) {
  const s = cfg.score || { min: 1, max: 5, step: 1 };
  const values = scoreValues(s);
  if (values.length) {
    const digits = values.every((v) => Number.isInteger(v) && v >= 0 && v <= 9);
    const opts = values.map((v, i) => ({ value: v, label: String(v), hotkey: digits ? String(v) : (i < 9 ? String(i + 1) : null) }));
    const box = el("div", { class: "decisions decisions-score", role: "group", "aria-label": `Score from ${s.min} to ${s.max}` });
    const byKey = new Map();
    for (const o of opts) {
      const b = optionButton({ ...o, value: String(o.value) }, { onclick: () => { flash(b); onDecide(o.value, o.label, []); } });
      b.setAttribute("aria-label", `Score ${o.label}${o.hotkey ? ` (key ${o.hotkey})` : ""}`);
      box.append(b);
      if (o.hotkey) byKey.set(o.hotkey, [o, b]);
    }
    const scale = el("div", { class: "score-scale muted", "aria-hidden": "true" }, el("span", { text: `${s.min} = lowest` }), el("span", { text: `${s.max} = highest` }));
    return {
      el: el("div", {}, box, scale),
      handleKey(key) {
        const hit = byKey.get(key);
        if (!hit) return false;
        flash(hit[1]);
        onDecide(hit[0].value, hit[0].label, []);
        return true;
      },
      reset(item) {
        for (const b of box.querySelectorAll("button.opt")) b.classList.toggle("mine", !!item && b.dataset.value === item.my_decision);
      },
      typing: () => false,
      keys: () => opts.filter((o) => o.hotkey).map((o) => [o.hotkey, `Score ${o.label}`]),
    };
  }
  const input = el("input", { type: "number", id: "score-input", min: String(s.min), max: String(s.max), step: String(s.step || "any"), inputmode: "decimal", "aria-describedby": "score-help" });
  const send = () => {
    if (input.value === "" || !input.checkValidity()) { input.reportValidity(); return; }
    onDecide(Number(input.value), input.value, []);
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); send(); } });
  const wrap = el("div", { class: "decide-input" },
    el("label", { for: "score-input", text: "Score" }),
    el("div", { class: "row" }, input, el("button", { type: "button", class: "btn btn-primary", onclick: send }, el("span", { text: "Save" }), el("kbd", { "aria-hidden": "true", text: "Enter" }))),
    el("small", { id: "score-help", class: "muted", text: `${s.min} to ${s.max}${s.step ? `, steps of ${s.step}` : ""}` }));
  return {
    el: wrap,
    handleKey(key) { if (key === "enter" || key === "/") { input.focus(); return true; } return false; },
    reset(item) { input.value = item && item.my_decision ? item.my_decision : ""; },
    typing: () => document.activeElement === input,
    focus: () => input.focus(),
    keys: () => [["/", "Type a score"], ["enter", "Save"]],
  };
}

function freeText(cfg, onDecide) {
  const ta = el("textarea", { id: "answer-input", rows: "3", maxlength: "5000", placeholder: "Type the answer", "aria-describedby": "answer-help" });
  const send = () => {
    const v = ta.value.trim();
    if (!v) { ta.focus(); return; }
    onDecide(v, v.length > 40 ? v.slice(0, 40) + "…" : v, []);
  };
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  const wrap = el("div", { class: "decide-input" },
    el("label", { for: "answer-input", text: "Answer" }),
    ta,
    el("div", { class: "row" },
      el("small", { id: "answer-help", class: "muted", text: "Enter saves · Shift+Enter new line · Esc leaves the box (for skip / undo keys)" }),
      el("button", { type: "button", class: "btn btn-primary", onclick: send }, el("span", { text: "Save" }), el("kbd", { "aria-hidden": "true", text: "Enter" }))));
  return {
    el: wrap,
    handleKey(key) { if (key === "enter" || key === "/") { ta.focus(); return true; } return false; },
    reset(item) { ta.value = item && item.my_decision ? item.my_decision : ""; },
    typing: () => document.activeElement === ta,
    focus: () => ta.focus(),
    autofocus: true,
    keys: () => [["/", "Type the answer"], ["enter", "Save"]],
  };
}

export function makePanel(cfg, onDecide) {
  switch (cfg.type) {
    case "multi": return multi(cfg, onDecide);
    case "score": return score(cfg, onDecide);
    case "free_text": return freeText(cfg, onDecide);
    default: return single(cfg, onDecide);
  }
}

// Human-readable text for a stored decision (multi is a JSON list of values).
export function decisionText(cfg, decision) {
  if (decision === null || decision === undefined) return "";
  const byValue = new Map(cfg.options.map((o) => [o.value, o.label]));
  if (cfg.type === "multi") {
    try { return JSON.parse(decision).map((v) => byValue.get(v) || v).join(", "); } catch { return decision; }
  }
  return byValue.get(decision) || decision;
}
