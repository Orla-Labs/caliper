// Live card preview (dev 2): renders card fields for one preview row, the way the labeller
// shows them. Values are always inserted as text.

import { h } from "./wizard-dom.js";

export const DISPLAY_TYPES = [
  ["text", "Text"],
  ["long_text", "Long text"],
  ["number", "Number"],
  ["badge", "Badge"],
  ["datetime", "Date / time"],
  ["link", "Link"],
  ["image", "Image"],
  ["json", "JSON"],
];

export function getPath(obj, path) {
  let cur = obj;
  if (cur && Object.prototype.hasOwnProperty.call(cur, path)) return cur[path]; // literal "a.b" column
  for (const part of String(path).split(".")) {
    if (cur === null || cur === undefined) return undefined;
    if (Array.isArray(cur)) {
      const n = Number(part);
      if (!Number.isInteger(n)) return undefined;
      cur = cur[n < 0 ? cur.length + n : n];
    } else if (typeof cur === "object") {
      cur = cur[part];
    } else return undefined;
  }
  return cur;
}

export function fieldValue(field, row) {
  if (!row) return undefined;
  if (field.template) {
    let missing = false;
    const out = field.template.replace(/\{([^{}]+)\}/g, (_, p) => {
      const v = getPath(row, p);
      if (v === undefined || v === null) { missing = true; return ""; }
      return String(v);
    });
    return missing ? undefined : out;
  }
  let v = getPath(row, field.column);
  if (typeof v === "string" && /^[[{]/.test(v.trim())) {
    try { v = JSON.parse(v); } catch { /* keep the string */ }
  }
  return v;
}

function hue(s) {
  let x = 0;
  for (const ch of String(s)) x = (x * 31 + ch.codePointAt(0)) >>> 0;
  return x % 360;
}

// ISO-8601 text -> the viewer's local date/time (date-only values stay dates, no timezone shift).
export function formatDateTime(raw) {
  const s = String(raw).trim();
  const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(s);
  const d = dateOnly ? new Date(`${s}T00:00:00`) : new Date(s.replace(" ", "T"));
  if (Number.isNaN(d.getTime())) return null;
  return dateOnly
    ? d.toLocaleDateString(undefined, { dateStyle: "medium" })
    : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

const isUrl = (s) => /^https?:\/\//i.test(s);

export function renderValue(field, raw) {
  const type = field.type || "text";
  if (raw === undefined || raw === null || raw === "") return h("span", { class: "wz-missing", "aria-label": "no value" }, "—");
  const asText = typeof raw === "string" ? raw : JSON.stringify(raw, null, 2);
  switch (type) {
    case "number": {
      const n = Number(raw);
      const txt = Number.isFinite(n) && field.decimals !== null && field.decimals !== undefined ? n.toFixed(field.decimals) : asText;
      return h("span", { class: "wz-v-number" }, txt);
    }
    case "badge":
      return h("span", { class: "wz-v-badge", style: { "--badge-h": hue(asText) } }, asText);
    case "link":
      return isUrl(asText)
        ? h("a", { class: "wz-v-link", href: asText, target: "_blank", rel: "noopener noreferrer" }, asText)
        : h("span", {}, asText);
    case "image": {
      if (isUrl(asText)) {
        const img = h("img", { class: "wz-v-image", src: asText, alt: `${field.label || field.column || "image"}`, loading: "lazy" });
        img.addEventListener("error", () => img.replaceWith(h("span", { class: "wz-v-imgph" }, "Image did not load: ", h("code", {}, asText))));
        return img;
      }
      const where = /^s3:\/\//i.test(asText) ? "S3 image: shown through a short-lived link while labelling"
        : /^gs:\/\//i.test(asText) ? "GCS image: shown while labelling"
          : "Local image: served from the project folder while labelling";
      return h("span", { class: "wz-v-imgph" }, h("span", { "aria-hidden": "true" }, "🖼 "), where, h("br"), h("code", {}, asText));
    }
    case "datetime": {
      const local = formatDateTime(asText);
      return local
        ? h("time", { class: "wz-v-datetime", datetime: asText, title: asText }, local)
        : h("span", {}, asText);
    }
    case "json":
      return h("pre", { class: "wz-v-json" }, typeof raw === "string" ? raw : JSON.stringify(raw, null, 2));
    case "long_text":
      return h("div", { class: `wz-v-long${field.mono ? " wz-mono" : ""}` }, asText);
    default:
      return h("div", { class: `wz-v-text${field.mono ? " wz-mono" : ""}` }, asText);
  }
}

export function renderCard(fields, row, { title } = {}) {
  const shown = (fields || []).filter((f) => !f.hidden);
  if (!shown.length) {
    return h("div", { class: "wz-card wz-card-empty" },
      h("p", {}, "The card is empty."),
      h("p", { class: "wz-muted" }, "Click a column in the table to add it."));
  }
  return h("article", { class: "wz-card", "aria-label": title || "Card preview" },
    shown.map((f) =>
      h("div", { class: `wz-card-field wz-card-${f.type || "text"}` },
        h("div", { class: "wz-card-label" }, f.label || f.column || f.template),
        h("div", { class: "wz-card-value" }, renderValue(f, fieldValue(f, row))))));
}
