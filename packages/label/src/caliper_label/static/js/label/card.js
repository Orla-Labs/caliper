// Card rendering and image prefetch for the labelling screen.
//
// Fields render in the order the project configures (usability #6). Leading images (the
// configured first fields) sit in a column beside the text, as before; any later image is
// shown inline where it was placed. Adjacent badges / numbers share one row of chips.

import { el } from "../common/dom.js";
import { badgeTone, formatDateTime } from "../common/label-display.js";

// Decoded images for prefetched cards, kept alive so the next card paints from memory.
const imageCache = new Map(); // src -> HTMLImageElement
const MAX_CACHE = 60;
// Why an image failed, per src (GET {src}/check), so each cause is asked for once.
const whyCache = new Map(); // src -> Promise<{detail, hint}>

export function cardImages(item) {
  return item ? item.fields.filter((f) => f.type === "image" && f.src) : [];
}

export function preloadImages(items) {
  for (const it of items) {
    for (const f of cardImages(it)) {
      if (imageCache.has(f.src)) continue;
      const img = new Image();
      img.decoding = "async";
      img.src = f.src;
      img.decode?.().catch(() => {});
      imageCache.set(f.src, img);
    }
  }
  while (imageCache.size > MAX_CACHE) imageCache.delete(imageCache.keys().next().value);
}

function fieldBody(f, options) {
  if (f.value === null || f.value === undefined) return el("div", { class: "f-empty", text: "–", "aria-label": "empty" });
  switch (f.type) {
    case "badge": {
      const t = badgeTone(f.value, options);
      return el("span", { class: `f-badge ${t.cls}`, style: t.color ? `--opt:${t.color}` : null, text: f.value });
    }
    case "number":
      return el("div", { class: "f-number", text: f.value });
    case "datetime": {
      const local = formatDateTime(f.value);
      return local
        ? el("time", { class: "f-text f-datetime", datetime: f.value, title: f.value, text: local })
        : el("div", { class: "f-text", text: f.value });
    }
    case "link":
      return el("div", { class: "f-link" }, f.href
        ? el("a", { href: f.href, target: "_blank", rel: "noopener noreferrer", text: f.value })
        : el("span", { text: f.value }));
    case "json":
      return el("pre", { class: "f-json", text: f.value });
    case "long_text":
      return el("div", { class: "f-text f-long", text: f.value });
    default:
      return el("div", { class: "f-text" + (f.mono ? " mono" : ""), text: f.value });
  }
}

function renderField(f, options) {
  return el("div", { class: `field f-${f.type}` },
    el("div", { class: "field-label", text: f.label }),
    fieldBody(f, options));
}

// The placeholder shown when an image does not load, filled in with the server's reason.
function missingImage(f, explain) {
  const why = el("span", { class: "img-why" });
  const ref = el("small", { class: "muted img-ref", text: f.value || "" });
  const box = el("div", { class: "img-missing", role: "img", "aria-label": `${f.label || "image"}: image not available` },
    el("span", { class: "img-missing-title", text: "Image not available" }), why, ref);
  const isWeb = /^https?:\/\//i.test(f.src || "");
  if (isWeb) {
    // the browser fetched this link itself; the server never sees it
    why.textContent = "The browser could not load this web link.";
    ref.before(el("small", { class: "img-hint", text: "Open it in a new tab: the host may need a login or block embedding." }));
    return box;
  }
  if (!explain) return box;
  why.textContent = "Checking why…";
  let p = whyCache.get(f.src);
  if (!p) {
    p = explain(f).catch((err) => ({ detail: err.message || "the image could not be checked", hint: err.hint || null }));
    whyCache.set(f.src, p);
  }
  p.then((r) => {
    if (!box.isConnected) return; // the labeller has moved on; a later render asks the cache
    why.textContent = r && r.detail ? capital(r.detail) : "The server returned no image.";
    if (r && r.hint) ref.before(el("small", { class: "img-hint", text: `Fix: ${r.hint}` }));
    box.setAttribute("aria-label", `${f.label || "image"}: image not available. ${why.textContent}`);
  });
  return box;
}

const capital = (s) => String(s).charAt(0).toUpperCase() + String(s).slice(1);

function figure(f, i, { onZoom, explain }) {
  const img = el("img", { src: f.src, alt: f.label || "image", decoding: "async" });
  img.addEventListener("error", () => img.closest("button")?.replaceWith(missingImage(f, explain)));
  const btn = el("button", { type: "button", class: "zoom-btn", "aria-label": `${f.label || "Image"}: view full size (Z)`, onclick: () => onZoom && onZoom(i) }, img);
  // caption above, like every other field's label (usability r3)
  return el("figure", {}, el("figcaption", { class: "field-label", text: f.label }), btn);
}

export function renderCard(item, { onZoom, optionLabel, options, explain } = {}) {
  const head = el("div", { class: "card-head" },
    el("span", { class: "card-id", text: item.id }),
    item.queue && item.queue !== "all" ? el("span", { class: "tag", text: item.queue }) : null,
    item.labels_needed > 1 ? el("span", { class: "tag", text: `labels ${item.labels_so_far}/${item.labels_needed}` }) : null,
    item.skipped_before ? el("span", { class: "tag tag-warn", text: "skipped earlier" }) : null,
    item.my_decision ? el("span", { class: "tag tag-accent", text: `you: ${optionLabel ? optionLabel(item.my_decision) : item.my_decision}` }) : null,
  );
  const fields = item.fields;
  const isImg = (f) => f.type === "image" && f.src;
  let lead = 0;
  while (lead < fields.length && isImg(fields[lead])) lead++;
  const media = el("div", { class: "media" });
  const prose = el("div", { class: "fields" });
  let zoomIndex = 0;
  let chips = null;
  fields.forEach((f, idx) => {
    if (isImg(f)) {
      chips = null;
      const fig = figure(f, zoomIndex++, { onZoom, explain });
      if (idx < lead) media.append(fig);
      else prose.append(el("div", { class: "media media-inline" }, fig));
      return;
    }
    const shown = f.type === "image" ? { ...f, type: "text" } : f;
    if (shown.type === "badge" || shown.type === "number") {
      if (!chips) { chips = el("div", { class: "chips" }); prose.append(chips); }
      chips.append(renderField(shown, options));
      return;
    }
    chips = null;
    prose.append(renderField(shown, options));
  });
  const body = el("div", { class: "card-body" + (lead ? " has-media" : "") });
  if (lead) body.append(media);
  body.append(prose);
  return [head, body];
}
