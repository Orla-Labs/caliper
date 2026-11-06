// Shared editors for the setup wizard and Project settings (dev 2):
// columns table, card designer, labels editor, workflow editor.
//
// Each editor edits a part of a ProjectConfig object in place and calls `onChange()`.
// They re-render themselves and put focus back on the control that had it.

import { h, clear, nextId, announce, slug, markdown, dialog, kbd, confirmDialog } from "./wizard-dom.js";
import { DISPLAY_TYPES, renderCard } from "./wizard-card.js";

export const PALETTE = ["#2e7d32", "#c62828", "#a15c00", "#1565c0", "#6a1b9a", "#00695c", "#ad1457", "#5d4037", "#455a64"];
const RESERVED = { z: "zoom", "?": "help", escape: "Esc", enter: "Enter", tab: "Tab", " ": "Space" };
const SPECIAL = new Set(["backspace", "delete", "escape", "enter", "tab", "space"]);
const MODIFIERS = new Set(["Shift", "Control", "Alt", "Meta", "CapsLock", "Fn", "AltGraph", "Dead", "Unidentified"]);

// Keydown on a hotkey cell: returns the key to bind, null to clear, or undefined to ignore.
function capturedKey(e) {
  if (e.key === "Tab" || e.metaKey || e.ctrlKey || e.altKey || MODIFIERS.has(e.key)) return undefined;
  e.preventDefault();
  if (e.key === "Escape") { e.target.blur(); return undefined; }
  if (e.key === "Backspace" || e.key === "Delete") return null;
  return e.key.toLowerCase();
}

// What a key looks like to a person ("backspace" -> "⌫ Backspace").
const KEY_NAMES = {
  backspace: "⌫ Backspace", delete: "Delete", escape: "Esc", enter: "Enter", tab: "Tab", " ": "Space", space: "Space",
  arrowleft: "←", arrowright: "→", arrowup: "↑", arrowdown: "↓", home: "Home", end: "End", pageup: "Page Up", pagedown: "Page Down",
};
export function keyLabel(k) {
  if (k === null || k === undefined || k === "") return "";
  const n = normKey(k);
  return KEY_NAMES[n] || (n.length === 1 ? n.toUpperCase() : n.charAt(0).toUpperCase() + n.slice(1));
}
// Key cells show the friendly name and grow to fit it.
const keySize = (k) => Math.max(3, keyLabel(k).length + 1);

export function normKey(k) {
  if (k === null || k === undefined) return "";
  k = String(k);
  if (k.length === 1) return k.toLowerCase();
  let low = k.trim().toLowerCase();
  if (low === "esc") low = "escape";
  return low === "space" ? " " : low;
}

// Mirror of project_setup.hotkey_issues: {index|keyname -> message}
export function hotkeyIssues(labels) {
  const out = {};
  const used = new Map();
  const binds = [];
  (labels.options || []).forEach((o, i) => {
    if (!o.retired && o.hotkey) binds.push([o.hotkey, `opt${i}`, `“${o.label || o.value}”`]);
  });
  const keys = labels.keys || { skip: "s", undo: "backspace", note: "n" };
  for (const n of ["skip", "undo", "note"]) binds.push([keys[n], `key-${n}`, n]);
  for (const [key, id, what] of binds) {
    const k = normKey(key);
    if (k.length !== 1 && !SPECIAL.has(k)) { out[id] = `“${key}” is not a single key`; continue; }
    if (RESERVED[k]) { out[id] = `${RESERVED[k] === k ? k.toUpperCase() : RESERVED[k]} is reserved by the labelling screen`; continue; }
    if (used.has(k)) { out[id] = `Same key as ${used.get(k)}`; continue; }
    used.set(k, what);
  }
  return out;
}

export function nextFreeKey(labels) {
  const used = new Set((labels.options || []).filter((o) => !o.retired && o.hotkey).map((o) => normKey(o.hotkey)));
  const keys = labels.keys || {};
  for (const n of ["skip", "undo", "note"]) if (keys[n]) used.add(normKey(keys[n]));
  return [..."1234567890"].find((k) => !used.has(k)) || null;
}

// Keep focus on "the same" control across re-renders (controls carry data-fid).
function withFocus(root, render) {
  const a = document.activeElement;
  const fid = a && root.contains(a) ? a.dataset.fid : null;
  const sel = a && "selectionStart" in a ? [a.selectionStart, a.selectionEnd] : null;
  render();
  if (fid) {
    const el = root.querySelector(`[data-fid="${CSS.escape(fid)}"]`);
    if (el) {
      el.focus();
      if (sel && "setSelectionRange" in el) { try { el.setSelectionRange(...sel); } catch { /* not a text input */ } }
    }
  }
}

// Element.append() would print "null" for a skipped optional child.
function append(el, ...children) {
  el.append(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
}

function truncate(v, n = 60) {
  if (v === null || v === undefined) return "";
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

// --------------------------------------------------------------------------- columns table

export function columnsTable({ columns, rows, selected, onToggle, maxRows = 50, caption }) {
  const wrap = h("div", { class: "wz-table-wrap", tabindex: "0", role: "region", "aria-label": caption || "Data preview" });
  const head = h("tr", {}, columns.map((c) => {
    const on = selected ? selected.has(c.name) : false;
    const th = h("th", { scope: "col", class: on ? "wz-col-on" : "" });
    if (onToggle) {
      th.append(h("button", {
        type: "button", class: "wz-colbtn", "aria-pressed": String(on), "data-fid": `col-${c.name}`,
        title: on ? `Remove ${c.name} from the card` : `Add ${c.name} to the card`,
        onclick: () => onToggle(c),
      },
      h("span", { class: "wz-colbtn-mark", "aria-hidden": "true" }, on ? "✓" : "+"),
      h("span", { class: "wz-colbtn-name" }, c.name),
      h("span", { class: "wz-colbtn-type" }, `${c.display || "text"} · ${String(c.dtype || "").toLowerCase()}`)));
    } else {
      th.append(h("span", { class: "wz-colbtn-name" }, c.name), h("span", { class: "wz-colbtn-type" }, `${c.display || "text"} · ${String(c.dtype || "").toLowerCase()}`));
    }
    return th;
  }));
  const body = h("tbody", {}, rows.slice(0, maxRows).map((r) =>
    h("tr", {}, columns.map((c) => {
      const td = h("td", { class: selected && selected.has(c.name) ? "wz-col-on" : "", title: truncate(r[c.name], 400) }, truncate(r[c.name]));
      if (onToggle) td.addEventListener("click", () => onToggle(c));
      return td;
    }))));
  wrap.append(h("table", { class: "wz-table" }, caption ? h("caption", { class: "wz-sr" }, caption) : null, h("thead", {}, head), body));
  return wrap;
}

// --------------------------------------------------------------------------- card designer

export function cardDesigner(el, ctx) {
  // ctx: {card, columns, rows, onChange}
  let rowIdx = 0;
  let dragFrom = null;
  const card = ctx.card;
  card.fields = card.fields || [];

  const colInfo = (name) => (ctx.columns || []).find((c) => c.name === name);

  function toggle(c) {
    const i = card.fields.findIndex((f) => f.column === c.name);
    if (i >= 0) {
      card.fields.splice(i, 1);
      announce(`${c.name} removed from the card`);
    } else {
      card.fields.push({ column: c.name, type: c.display || "text" });
      announce(`${c.name} added to the card, position ${card.fields.length}`);
    }
    changed();
  }

  function move(i, d) {
    const j = i + d;
    if (j < 0 || j >= card.fields.length) return;
    const [f] = card.fields.splice(i, 1);
    card.fields.splice(j, 0, f);
    announce(`${f.label || f.column} moved to position ${j + 1} of ${card.fields.length}`);
    changed(`up-${j}`, d < 0 ? `up-${j}` : `down-${j}`);
  }

  function changed() {
    ctx.onChange && ctx.onChange();
    render();
  }

  function fieldRow(f, i) {
    const name = f.column || f.template;
    const li = h("li", {
      class: "wz-field", draggable: "true", "data-i": i,
      ondragstart: (e) => { dragFrom = i; li.classList.add("wz-dragging"); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", String(i)); },
      ondragend: () => { li.classList.remove("wz-dragging"); dragFrom = null; },
      ondragover: (e) => { if (dragFrom !== null) { e.preventDefault(); li.classList.add("wz-dropover"); } },
      ondragleave: () => li.classList.remove("wz-dropover"),
      ondrop: (e) => {
        e.preventDefault();
        li.classList.remove("wz-dropover");
        if (dragFrom === null || dragFrom === i) return;
        const [m] = card.fields.splice(dragFrom, 1);
        card.fields.splice(i, 0, m);
        announce(`${m.label || m.column} moved to position ${i + 1}`);
        changed();
      },
      onkeydown: (e) => {
        if (e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
          e.preventDefault();
          move(i, e.key === "ArrowUp" ? -1 : 1);
        }
      },
    });
    const lid = nextId("fl");
    const typeSel = h("select", {
      "aria-label": `Display type for ${name}`, "data-fid": `type-${i}`,
      onchange: (e) => { f.type = e.target.value; if (f.type !== "number") delete f.decimals; changed(); },
    }, DISPLAY_TYPES.map(([v, t]) => h("option", { value: v, selected: (f.type || "text") === v }, t)));
    append(li,
      h("span", { class: "wz-handle", "aria-hidden": "true", title: "Drag to reorder" }, "⠿"),
      h("span", { class: "wz-field-pos", "aria-hidden": "true" }, String(i + 1)),
      h("div", { class: "wz-field-main" },
        h("label", { for: lid, class: "wz-sr" }, `Label for ${name}`),
        h("input", {
          id: lid, type: "text", value: f.label || "", placeholder: name, "data-fid": `label-${i}`,
          oninput: (e) => { f.label = e.target.value || null; if (!f.label) delete f.label; ctx.onChange && ctx.onChange(); refreshPreview(); },
        }),
        h("code", { class: "wz-field-col", title: colInfo(f.column)?.dtype || "" }, name)),
      typeSel,
      f.type === "number"
        ? h("input", {
          type: "number", min: "0", max: "10", class: "wz-decimals", value: f.decimals ?? "", placeholder: "dec.",
          "aria-label": `Decimals for ${name}`, "data-fid": `dec-${i}`,
          oninput: (e) => { const v = e.target.value; if (v === "") delete f.decimals; else f.decimals = Number(v); ctx.onChange && ctx.onChange(); refreshPreview(); },
        })
        : null,
      h("div", { class: "wz-field-btns" },
        h("button", { type: "button", class: "wz-icon", "aria-label": `Move ${name} up`, disabled: i === 0, "data-fid": `up-${i}`, onclick: () => move(i, -1) }, "↑"),
        h("button", { type: "button", class: "wz-icon", "aria-label": `Move ${name} down`, disabled: i === card.fields.length - 1, "data-fid": `down-${i}`, onclick: () => move(i, 1) }, "↓"),
        h("button", { type: "button", class: "wz-icon wz-icon-x", "aria-label": `Remove ${name}`, "data-fid": `rm-${i}`,
          onclick: () => { card.fields.splice(i, 1); announce(`${name} removed`); changed(); } }, "×")),
    );
    return li;
  }

  const previewBox = h("div", { class: "wz-preview-card" });
  const navLabel = h("span", { class: "wz-preview-pos", "aria-live": "polite" });

  function refreshPreview() {
    const rows = ctx.rows || [];
    const row = rows[rowIdx];
    clear(previewBox, renderCard(card.fields, row));
    navLabel.textContent = rows.length ? `Row ${rowIdx + 1} of ${rows.length}` : "No rows";
  }

  function render() {
    withFocus(el, () => {
      const selected = new Set(card.fields.map((f) => f.column).filter(Boolean));
      const rows = ctx.rows || [];
      clear(el,
        h("div", { class: "wz-designer" },
          h("section", { class: "wz-panel wz-designer-cols", "aria-labelledby": "wz-cols-h" },
            h("h3", { id: "wz-cols-h" }, "Columns"),
            h("p", { class: "wz-help" }, "Click a column (or any of its cells) to add it to the card. Click again to remove it."),
            ctx.columns && ctx.columns.length
              ? columnsTable({ columns: ctx.columns, rows, selected, onToggle: toggle, maxRows: 8, caption: "Columns of your data; first rows" })
              : h("p", { class: "wz-muted" }, "Connect data first to see its columns.")),
          h("section", { class: "wz-panel wz-designer-fields", "aria-labelledby": "wz-fields-h" },
            h("h3", { id: "wz-fields-h" }, "On the card ", h("span", { class: "wz-count" }, String(card.fields.length))),
            card.fields.length
              ? h("ol", { class: "wz-fields", "aria-describedby": "wz-fields-help" }, card.fields.map(fieldRow))
              : h("div", { class: "wz-empty-inline" }, "Nothing on the card yet. Click a column on the left."),
            h("p", { id: "wz-fields-help", class: "wz-help" }, "Drag ", h("span", { "aria-hidden": "true" }, "⠿"), " to reorder, or use ↑ ↓ (", kbd("Alt"), " + ", kbd("↑"), "/", kbd("↓"), " on a row).")),
          h("section", { class: "wz-panel wz-designer-preview", "aria-labelledby": "wz-prev-h" },
            h("div", { class: "wz-preview-head" },
              h("h3", { id: "wz-prev-h" }, "Live preview"),
              h("div", { class: "wz-preview-nav" },
                h("button", { type: "button", class: "wz-icon", "aria-label": "Previous row", "data-fid": "prev-row", disabled: rowIdx <= 0,
                  onclick: () => { rowIdx = Math.max(0, rowIdx - 1); render(); } }, "‹"),
                navLabel,
                h("button", { type: "button", class: "wz-icon", "aria-label": "Next row", "data-fid": "next-row", disabled: rowIdx >= rows.length - 1,
                  onclick: () => { rowIdx = Math.min(rows.length - 1, rowIdx + 1); render(); } }, "›"))),
            previewBox)));
      refreshPreview();
    });
  }
  render();
  return { render, setRows(rows, columns) { ctx.rows = rows; ctx.columns = columns; rowIdx = 0; render(); } };
}

// --------------------------------------------------------------------------- labels editor

const LABEL_TYPES = [
  ["single", "Single choice", "One option per item (default)."],
  ["multi", "Multiple choice", "Any number of options per item."],
  ["yes_no_unsure", "Yes / No / Unsure", "The quick yes/no preset."],
  ["free_text", "Free text", "Labellers type an answer."],
  ["score", "Numeric score", "A number in a range, e.g. 1 to 10."],
];

const TEMPLATE_BUTTONS = [
  ["correct_wrong_unsure", "Correct / Wrong / Unsure"],
  ["true_uncertain_false", "True / Uncertain / False"],
  ["categories", "Category list…"],
  ["rating_1_5", "1–5 rating"],
];

export function labelsEditor(el, ctx) {
  // ctx: {labels, api, counts?, settings?: bool, onChange}
  const L = ctx.labels;
  L.type = L.type || "single";
  L.options = L.options || [];
  L.keys = L.keys || { skip: "s", undo: "backspace", note: "n" };
  const counts = ctx.counts || {};

  function changed() { ctx.onChange && ctx.onChange(); render(); }

  // Options exactly as the last template made them: replacing those loses nothing.
  const optsKey = (opts) => JSON.stringify((opts || []).map((o) => Object.fromEntries(Object.entries(o).filter(([k, v]) => !k.startsWith("_") && v !== null && v !== undefined && v !== false))));
  const untouched = () => ctx.fromTemplate !== undefined && ctx.fromTemplate !== null && optsKey(L.options) === ctx.fromTemplate;

  if (ctx.fromTemplate === null) { // the caller just loaded these options from a template
    ctx.fromTemplate = optsKey(L.options);
    ctx.onTemplate && ctx.onTemplate(ctx.fromTemplate);
  }

  async function applyTemplate(id, categories) {
    const hasLabels = L.options.some((o) => counts[o.value]);
    if (L.options.length && (hasLabels || !untouched()) && !(await confirmDialog({
      title: "Replace the options?",
      body: hasLabels
        ? h("p", {}, "Some options already have labels. They will be kept and flagged as retired when you save.")
        : h("p", {}, "The current options will be replaced by the template."),
      ok: "Replace",
    }))) return;
    try {
      const res = await ctx.api.post(`/api/setup/templates/${id}`, categories ? { categories } : {});
      const keepRetired = L.options.filter((o) => o.retired);
      L.type = res.labels.type;
      L.options = [...res.labels.options, ...keepRetired];
      ctx.fromTemplate = optsKey(L.options);
      ctx.onTemplate && ctx.onTemplate(ctx.fromTemplate);
      announce(`${res.labels.options.length} options from the template`);
      changed();
    } catch (err) {
      announce(`Template failed: ${err.message}`);
    }
  }

  function pasteCategories() {
    const ta = h("textarea", { rows: "8", class: "wz-textarea", "aria-label": "Categories, one per line", placeholder: "Billing\nShipping\nReturns\nOther" });
    const d = dialog({
      title: "Category list",
      body: [h("p", { class: "wz-help" }, "Paste or type one category per line. The first nine get hotkeys 1–9."), ta],
      actions: [
        h("button", { type: "button", class: "wz-btn", onclick: () => d.close() }, "Cancel"),
        h("button", { type: "button", class: "wz-btn wz-btn-primary", onclick: () => { const v = ta.value; d.close(); if (v.trim()) applyTemplate("categories", v); } }, "Use these categories"),
      ],
    });
    ta.focus();
  }

  function addOption() {
    const n = L.options.filter((o) => !o.retired).length;
    let name = `Option ${n + 1}`;
    let value = slug(name);
    while (L.options.some((o) => o.value === value)) value += "_";
    L.options.push({ value, label: name, hotkey: nextFreeKey(L), color: PALETTE[L.options.length % PALETTE.length] });
    announce(`${name} added`);
    ctx.onChange && ctx.onChange();
    render();
    const inp = el.querySelector(`[data-fid="olabel-${L.options.length - 1}"]`);
    if (inp) { inp.focus(); inp.select(); }
  }

  async function removeOption(i) {
    const o = L.options[i];
    const n = counts[o.value] || 0;
    if (n) {
      const ok = await confirmDialog({
        title: `Remove “${o.label || o.value}”?`,
        body: h("p", {}, `${n} label${n === 1 ? "" : "s"} already use this option. They are kept and flagged as `, h("em", {}, "retired"), "; the option disappears from the labelling screen."),
        ok: "Retire option", danger: true,
      });
      if (!ok) return;
      o.retired = true;
      o.hotkey = null;
      announce(`${o.label || o.value} retired`);
    } else {
      L.options.splice(i, 1);
      announce(`${o.label || o.value} removed`);
    }
    changed();
  }

  function optionRow(o, i, issues) {
    const n = counts[o.value] || 0;
    const valueLocked = ctx.settings && n > 0;
    const issue = issues[`opt${i}`];
    const errId = issue ? nextId("err") : null;
    const tr = h("tr", { class: o.retired ? "wz-retired" : "" });
    const name = o.label || o.value;
    append(tr,
      h("td", {}, h("input", {
        type: "color", value: o.color || PALETTE[i % PALETTE.length], "aria-label": `Colour of ${name}`, "data-fid": `ocolor-${i}`, disabled: o.retired,
        oninput: (e) => { o.color = e.target.value; ctx.onChange && ctx.onChange(); renderButtons(); },
      })),
      h("td", { class: "wz-opt-name" },
        h("input", {
          type: "text", value: o.label ?? "", placeholder: o.value, "aria-label": `Name of option ${i + 1}`, "data-fid": `olabel-${i}`, disabled: o.retired,
          oninput: (e) => {
            const old = slug(o.label || "");
            o.label = e.target.value;
            if (!valueLocked && !o._valueEdited && (o.value === old || !o.value || o.value.startsWith("option"))) {
              let v = slug(o.label);
              while (L.options.some((x, j) => j !== i && x.value === v)) v += "_";
              o.value = v;
              const vi = tr.querySelector(".wz-opt-value");
              if (vi) vi.value = v;
            }
            ctx.onChange && ctx.onChange();
            renderButtons();
          },
        }),
        h("input", {
          type: "text", class: "wz-opt-value", value: o.value, "aria-label": `Stored value of ${name}`, "data-fid": `ovalue-${i}`,
          disabled: valueLocked || o.retired, title: valueLocked ? "Has labels: the stored value cannot change (rename the option instead)" : "Stored value (what exports contain)",
          oninput: (e) => { o.value = e.target.value; o._valueEdited = true; ctx.onChange && ctx.onChange(); },
        }),
        o.retired ? h("span", { class: "wz-tag" }, "retired") : null,
        n ? h("span", { class: "wz-tag wz-tag-count" }, `${n} label${n === 1 ? "" : "s"}`) : null),
      h("td", {}, h("input", {
        type: "text", class: `wz-hotkey${issue ? " wz-invalid" : ""}`, value: keyLabel(o.hotkey), size: keySize(o.hotkey), disabled: o.retired,
        "aria-label": `Hotkey for ${name}`, "aria-invalid": issue ? "true" : null, "aria-describedby": errId, "data-fid": `ohot-${i}`,
        onkeydown: (e) => {
          // press a key to bind it (Backspace/Delete clear, Esc leaves, Tab moves on)
          const k = capturedKey(e);
          if (k === undefined) return;
          o.hotkey = k;
          changed();
        },
      }), issue ? h("div", { id: errId, class: "wz-field-error", role: "alert" }, issue) : null),
      h("td", { class: "wz-center" }, h("input", {
        type: "checkbox", checked: !!o.approved, "aria-label": `${name} counts as a positive (accepted) answer`, "aria-describedby": "wz-positive-help", "data-fid": `oappr-${i}`, disabled: o.retired,
        onchange: (e) => { o.approved = e.target.checked; changed(); },
      })),
      h("td", { class: "wz-center" }, h("input", {
        type: "checkbox", checked: !!o.requires_note, "aria-label": `${name} requires a note`, "data-fid": `onote-${i}`, disabled: o.retired,
        onchange: (e) => { o.requires_note = e.target.checked; changed(); },
      })),
      h("td", { class: "wz-opt-btns" },
        h("button", { type: "button", class: "wz-icon", "aria-label": `Move ${name} up`, disabled: i === 0, "data-fid": `oup-${i}`,
          onclick: () => { [L.options[i - 1], L.options[i]] = [L.options[i], L.options[i - 1]]; changed(); } }, "↑"),
        h("button", { type: "button", class: "wz-icon", "aria-label": `Move ${name} down`, disabled: i === L.options.length - 1, "data-fid": `odown-${i}`,
          onclick: () => { [L.options[i + 1], L.options[i]] = [L.options[i], L.options[i + 1]]; changed(); } }, "↓"),
        o.retired
          ? h("button", { type: "button", class: "wz-btn wz-btn-small", "data-fid": `orestore-${i}`,
            onclick: () => { o.retired = false; o.hotkey = nextFreeKey(L); announce(`${name} restored`); changed(); } }, "Restore")
          : h("button", { type: "button", class: "wz-icon wz-icon-x", "aria-label": `Remove ${name}`, "data-fid": `orm-${i}`, onclick: () => removeOption(i) }, "×")),
    );
    return tr;
  }

  const buttonsBox = h("div", { class: "wz-decisions", "aria-label": "How the decision buttons will look" });
  function renderButtons() {
    if (L.type === "free_text") return clear(buttonsBox, h("div", { class: "wz-free" }, "Labellers type an answer in a text box."));
    if (L.type === "score") {
      const s = L.score || { min: 1, max: 5, step: 1 };
      return clear(buttonsBox, h("div", { class: "wz-free" }, `Labellers enter a number from ${s.min} to ${s.max} (step ${s.step}).`));
    }
    clear(buttonsBox, L.options.filter((o) => !o.retired).map((o) =>
      h("span", { class: "wz-decision", style: { "--opt": o.color || "#455a64" } },
        o.hotkey ? kbd(o.hotkey) : null, h("span", {}, o.label || o.value),
        o.approved ? h("span", { class: "wz-decision-flag", title: "counts as a positive answer" }, "✓") : null)));
  }

  function render() {
    withFocus(el, () => {
      const issues = hotkeyIssues(L);
      const choice = ["single", "multi", "yes_no_unsure"].includes(L.type);
      const typeName = nextId("ltype");
      clear(el,
        h("fieldset", { class: "wz-panel wz-radios" },
          h("legend", {}, "Label type"),
          h("div", { class: "wz-radio-row" }, LABEL_TYPES.map(([v, t, d]) =>
            h("label", { class: `wz-radio-card${L.type === v ? " wz-on" : ""}` },
              h("input", {
                type: "radio", name: typeName, value: v, checked: L.type === v, "data-fid": `ltype-${v}`,
                onchange: async () => {
                  L.type = v;
                  if (v === "score" && !L.score) L.score = { min: 1, max: 5, step: 1 };
                  if (v === "yes_no_unsure" && !L.options.some((o) => !o.retired)) { await applyTemplate("yes_no_unsure"); return; }
                  changed();
                },
              }),
              h("span", { class: "wz-radio-title" }, t), h("span", { class: "wz-radio-desc" }, d))))),
        choice ? h("section", { class: "wz-panel" },
          h("div", { class: "wz-row-between" },
            h("h3", {}, "Options"),
            h("div", { class: "wz-templates", role: "group", "aria-label": "Templates" },
              h("span", { class: "wz-muted" }, "Templates:"),
              TEMPLATE_BUTTONS.map(([id, t]) => h("button", {
                type: "button", class: "wz-btn wz-btn-small", "data-fid": `tpl-${id}`,
                onclick: () => (id === "categories" ? pasteCategories() : applyTemplate(id)),
              }, t)))),
          L.options.length
            ? h("div", { class: "wz-table-wrap wz-opts-wrap" }, h("table", { class: "wz-opts" },
              h("thead", {}, h("tr", {},
                h("th", { scope: "col" }, h("span", { class: "wz-sr" }, "Colour")),
                h("th", { scope: "col" }, "Name ", h("span", { class: "wz-muted" }, "· stored value")),
                h("th", { scope: "col" }, "Hotkey"),
                h("th", { scope: "col", class: "wz-center", title: "Counts as a positive / accepted answer. Used by eval exports." }, "Positive"),
                h("th", { scope: "col", class: "wz-center" }, "Needs note"),
                h("th", { scope: "col" }, h("span", { class: "wz-sr" }, "Actions")))),
              h("tbody", {}, L.options.map((o, i) => optionRow(o, i, issues)))))
            : h("div", { class: "wz-empty-inline" }, "No options yet. Pick a template above or add your own."),
          L.options.length ? h("p", { class: "wz-help", id: "wz-positive-help" },
            h("strong", {}, "Positive"), " marks an answer that accepts the item (e.g. Correct). Eval exports (golden, rollouts) score positive answers as 1. ",
            "For plain categories such as a priority, leave it unticked.") : null,
          h("div", { class: "wz-row-between" },
            h("button", { type: "button", class: "wz-btn", "data-fid": "add-opt", onclick: addOption }, "+ Add option"),
            h("p", { class: "wz-help" }, "Hotkey cells: press the key to bind it (Backspace clears, Esc leaves). Reserved: ", kbd("Z"), " zoom, ", kbd("?"), " help, ", kbd("Esc"), ", ", kbd("Enter"), ", ", kbd("Tab"), ", ", kbd("Space"), "."))) : null,
        L.type === "score" ? scoreRange() : null,
        L.type !== "free_text" ? keysBox(issues) : null,
        h("section", { class: "wz-panel" }, h("h3", {}, "On the labelling screen"), buttonsBox),
      );
      renderButtons();
    });
  }

  function scoreRange() {
    L.score = L.score || { min: 1, max: 5, step: 1 };
    const num = (k, label) => {
      const id = nextId("sc");
      return h("div", { class: "wz-inline-field" }, h("label", { for: id }, label),
        h("input", { id, type: "number", step: "any", value: L.score[k], "data-fid": `score-${k}`,
          oninput: (e) => { L.score[k] = Number(e.target.value); ctx.onChange && ctx.onChange(); renderButtons(); } }));
    };
    const bad = L.score.max <= L.score.min;
    return h("section", { class: "wz-panel" }, h("h3", {}, "Score range"),
      h("div", { class: "wz-inline-fields" }, num("min", "Min"), num("max", "Max"), num("step", "Step")),
      bad ? h("p", { class: "wz-field-error", role: "alert" }, "Max must be above min.") : null);
  }

  function keysBox(issues) {
    const k = (name, label) => {
      const id = nextId("key");
      const issue = issues[`key-${name}`];
      return h("div", { class: "wz-inline-field" }, h("label", { for: id }, label),
        h("input", {
          id, type: "text", class: `wz-hotkey wz-hotkey-wide${issue ? " wz-invalid" : ""}`, value: keyLabel(L.keys[name]), size: keySize(L.keys[name]),
          "data-fid": `key-${name}`, "aria-invalid": issue ? "true" : null, title: "Press the key to use",
          onkeydown: (e) => {
            const k = capturedKey(e);
            if (!k) return; // skip/undo/note always need a key
            L.keys[name] = k;
            changed();
          },
        }), issue ? h("div", { class: "wz-field-error", role: "alert" }, issue) : null);
    };
    return h("details", { class: "wz-panel wz-details" }, h("summary", {}, "Skip, undo and note keys"),
      h("div", { class: "wz-inline-fields" }, k("skip", "Skip"), k("undo", "Undo"), k("note", "Note")));
  }

  render();
  return { render };
}

// --------------------------------------------------------------------------- workflow editor

export function guessQueueColumn(columns) {
  const names = columns.map((c) => c.name);
  return names.find((n) => /^(queue|priority|stratum|category|segment|team|channel|source|split)$/i.test(n))
    || (columns.find((c) => c.display === "badge") || {}).name
    || names.find((n) => !/(^|_)id$/i.test(n)) || "";
}

export function workflowEditor(el, ctx) {
  // ctx: {workflow, labels, columns, onChange}
  const W = ctx.workflow;
  W.queues = W.queues || {};
  W.double_label = W.double_label || 1;
  W.assignment = W.assignment || "open";
  let mdTab = "write";
  const cols = () => (ctx.columns || []).map((c) => c.name);

  function changed() { ctx.onChange && ctx.onChange(); render(); }

  function select(id, value, onchange, placeholder, fid) {
    return h("select", { id, "data-fid": fid, onchange: (e) => onchange(e.target.value || null) },
      h("option", { value: "" }, placeholder),
      cols().map((c) => h("option", { value: c, selected: c === value }, c)),
      value && !cols().includes(value) ? h("option", { value, selected: true }, `${value} (not in preview)`) : null);
  }

  function render() {
    withFocus(el, () => {
      const qmode = !W.queues.by ? "none" : W.queues.by === "stratum" ? "strata" : "column";
      const qname = nextId("q");
      const aname = nextId("a");
      const qcol = nextId("qc");
      const dl = nextId("dl");
      const instr = nextId("in");
      const gcol = nextId("gc");
      const grate = nextId("gr");
      const ccol = nextId("cc");
      const hasStratum = cols().includes("stratum");
      clear(el,
        h("fieldset", { class: "wz-panel wz-radios" },
          h("legend", {}, "Queues"),
          h("p", { class: "wz-help" }, "Split the work into queues labellers can pick from."),
          h("div", { class: "wz-radio-row" },
            [["none", "One queue", "Everything in data order."],
              ["column", "By a column", "One queue per value, e.g. priority."],
              ["strata", "By sample group", "For data from caliper sample: one queue per group (its stratum column)."]].map(([v, t, d]) =>
              h("label", { class: `wz-radio-card${qmode === v ? " wz-on" : ""}` },
                h("input", {
                  type: "radio", name: qname, value: v, checked: qmode === v, "data-fid": `q-${v}`,
                  onchange: () => {
                    if (v === "none") W.queues.by = null;
                    else if (v === "strata") W.queues.by = "stratum";
                    else W.queues.by = guessQueueColumn(ctx.columns || []);
                    changed();
                  },
                }), h("span", { class: "wz-radio-title" }, t), h("span", { class: "wz-radio-desc" }, d)))),
          qmode === "column" ? h("div", { class: "wz-inline-field" }, h("label", { for: qcol }, "Queue column"),
            select(qcol, W.queues.by, (v) => { W.queues.by = v; changed(); }, "choose a column", "qcol")) : null,
          qmode === "strata" && !hasStratum && cols().length
            ? h("p", { class: "wz-warn", role: "note" }, "No ", h("code", {}, "stratum"), " column in this data. Run ", h("code", {}, "caliper sample"), " first, or split by another column.")
            : null),
        h("section", { class: "wz-panel wz-grid-2" },
          h("div", {},
            h("h3", {}, "Labels per item"),
            h("div", { class: "wz-inline-field" },
              h("label", { for: dl }, "Each item is labelled by"),
              h("div", { class: "wz-stepper" },
                h("button", { type: "button", class: "wz-icon", "aria-label": "Fewer labellers per item", "data-fid": "dl-minus", disabled: W.double_label <= 1,
                  onclick: () => { W.double_label = Math.max(1, W.double_label - 1); changed(); } }, "−"),
                h("input", { id: dl, type: "number", min: "1", max: "10", value: W.double_label, "data-fid": "dl",
                  oninput: (e) => { const n = Math.max(1, Math.min(10, Number(e.target.value) || 1)); W.double_label = n; ctx.onChange && ctx.onChange(); } }),
                h("button", { type: "button", class: "wz-icon", "aria-label": "More labellers per item", "data-fid": "dl-plus", disabled: W.double_label >= 10,
                  onclick: () => { W.double_label = Math.min(10, W.double_label + 1); changed(); } }, "+")),
              h("span", { class: "wz-muted" }, W.double_label === 1 ? "person" : "different people")),
            W.double_label > 1 ? h("p", { class: "wz-help" }, "The dashboard shows how often labellers agree (Cohen's κ) and lists the items they disagree on, so someone can settle them.") : null),
          h("fieldset", { class: "wz-radios wz-plain" },
            h("legend", {}, "Assignment"),
            [["open", "Open", "Anyone takes the next unlabelled item."],
              ["round_robin", "Round-robin", "Items are dealt out evenly between labellers."]].map(([v, t, d]) =>
              h("label", { class: "wz-radio-line" },
                h("input", { type: "radio", name: aname, value: v, checked: W.assignment === v, "data-fid": `as-${v}`, onchange: () => { W.assignment = v; changed(); } }),
                h("span", {}, h("strong", {}, t), " ", h("span", { class: "wz-muted" }, d)))))),
        h("section", { class: "wz-panel" },
          h("div", { class: "wz-row-between" },
            h("h3", {}, h("label", { for: instr }, "Instructions")),
            h("div", { class: "wz-tabs", role: "tablist", "aria-label": "Instructions editor" },
              ["write", "preview"].map((t) => h("button", {
                type: "button", role: "tab", "aria-selected": String(mdTab === t), class: "wz-tab", "data-fid": `md-${t}`,
                onclick: () => { mdTab = t; render(); },
              }, t === "write" ? "Write" : "Preview")))),
          mdTab === "write"
            ? h("textarea", {
              id: instr, class: "wz-textarea", rows: "7", "data-fid": "instructions",
              placeholder: "What should labellers look for? Markdown works: **bold**, - lists, | tables |.",
              oninput: (e) => { W.instructions = e.target.value || null; ctx.onChange && ctx.onChange(); },
            }, W.instructions || "")
            : h("div", { class: "wz-markdown", role: "tabpanel", tabindex: "0" },
              W.instructions ? (() => { const d = h("div"); d.innerHTML = markdown(W.instructions); return d; })() : h("p", { class: "wz-muted" }, "No instructions yet."))),
        h("section", { class: "wz-panel wz-grid-2" },
          h("div", {},
            h("h3", {}, "Quality checks ", h("span", { class: "wz-muted" }, "(optional, “golden” items)")),
            h("label", { class: "wz-check" },
              h("input", { type: "checkbox", checked: !!W.golden, "data-fid": "golden-on",
                onchange: (e) => { W.golden = e.target.checked ? { answer_column: cols()[0] || "", rate: 0.05 } : null; changed(); } }),
              " Mix in hidden items with known answers and track each labeller's accuracy"),
            W.golden ? h("div", { class: "wz-inline-fields" },
              h("div", { class: "wz-inline-field" }, h("label", { for: gcol }, "Answer column"),
                select(gcol, W.golden.answer_column, (v) => { W.golden.answer_column = v || ""; changed(); }, "choose a column", "gcol")),
              h("div", { class: "wz-inline-field" }, h("label", { for: grate }, "Share of items"),
                h("input", { id: grate, type: "number", min: "0", max: "100", step: "1", value: Math.round((W.golden.rate ?? 0.05) * 100), "data-fid": "grate",
                  oninput: (e) => { W.golden.rate = Math.max(0, Math.min(100, Number(e.target.value) || 0)) / 100; ctx.onChange && ctx.onChange(); } }),
                h("span", { class: "wz-muted" }, "%"))) : null,
            W.golden ? h("p", { class: "wz-help" }, "Rows with a value in the answer column are golden items; the answer must match an option's stored value.") : null),
          ctx.labels ? h("div", {},
            h("h3", {}, "Agreement by group ", h("span", { class: "wz-muted" }, "(optional)")),
            h("div", { class: "wz-inline-field" }, h("label", { for: ccol }, "Category column"),
              select(ccol, ctx.labels.category_column, (v) => { ctx.labels.category_column = v; changed(); }, "none", "ccol")),
            h("p", { class: "wz-help" }, "The dashboard shows labeller agreement separately for each value of this column. Useful with 2+ labels per item.")) : null),
      );
    });
  }
  render();
  return { render };
}
