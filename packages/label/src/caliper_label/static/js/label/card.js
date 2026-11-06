// Card rendering and image prefetch for the labelling screen.

import { el, hue } from "../common/dom.js";

// Decoded images for prefetched cards, kept alive so the next card paints from memory.
const imageCache = new Map(); // src -> HTMLImageElement
const MAX_CACHE = 60;

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

function fieldBody(f) {
  if (f.value === null || f.value === undefined) return el("div", { class: "f-empty", text: "–", "aria-label": "empty" });
  switch (f.type) {
    case "badge":
      return el("span", { class: "f-badge", style: `--h:${hue(f.value)}`, text: f.value });
    case "number":
      return el("div", { class: "f-number", text: f.value });
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

function renderField(f) {
  return el("div", { class: `field f-${f.type}` },
    el("div", { class: "field-label", text: f.label }),
    fieldBody(f));
}

export function renderCard(item, { onZoom, optionLabel } = {}) {
  const head = el("div", { class: "card-head" },
    el("span", { class: "card-id", text: item.id }),
    item.queue && item.queue !== "all" ? el("span", { class: "tag", text: item.queue }) : null,
    item.labels_needed > 1 ? el("span", { class: "tag", text: `labels ${item.labels_so_far}/${item.labels_needed}` }) : null,
    item.skipped_before ? el("span", { class: "tag tag-warn", text: "skipped earlier" }) : null,
    item.my_decision ? el("span", { class: "tag tag-accent", text: `you: ${optionLabel ? optionLabel(item.my_decision) : item.my_decision}` }) : null,
  );
  const media = el("div", { class: "media" });
  const prose = el("div", { class: "fields" });
  const chips = el("div", { class: "chips" });
  cardImages(item).forEach((f, i) => {
    const img = el("img", { src: f.src, alt: f.label || "image", decoding: "async" });
    img.addEventListener("error", () => {
      img.replaceWith(el("div", { class: "img-missing", role: "img", "aria-label": `${f.label}: image not available` },
        el("span", { text: "Image not available" }),
        el("small", { class: "muted", text: f.value || "" })));
    });
    const btn = el("button", { type: "button", class: "zoom-btn", "aria-label": `${f.label || "Image"}: view full size (Z)`, onclick: () => onZoom && onZoom(i) }, img);
    media.append(el("figure", {}, btn, el("figcaption", { text: f.label })));
  });
  for (const f of item.fields) {
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
  return [head, body];
}
