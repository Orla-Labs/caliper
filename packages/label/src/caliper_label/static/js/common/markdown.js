// Tiny markdown for project instructions (dev 3). Builds DOM nodes; never innerHTML.
// Blocks: #/##/### headings, paragraphs, - / * / 1. lists, | tables |, ``` code ```.
// Inline: **bold**, *italic* / _italic_, `code`, [text](http(s) or mailto link).

import { el } from "./dom.js";

const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)\s]+\)|\*[^*\s][^*]*\*|_[^_\s][^_]*_)/g;

function inline(text) {
  const out = [];
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const t = m[0];
    if (t.startsWith("**")) out.push(el("strong", {}, ...inline(t.slice(2, -2))));
    else if (t.startsWith("`")) out.push(el("code", { text: t.slice(1, -1) }));
    else if (t.startsWith("[")) {
      const [, label, href] = t.match(/^\[([^\]]+)\]\(([^)\s]+)\)$/);
      out.push(/^(https?:|mailto:)/i.test(href)
        ? el("a", { href, target: "_blank", rel: "noopener noreferrer" }, ...inline(label))
        : label);
    } else out.push(el("em", {}, ...inline(t.slice(1, -1))));
    last = m.index + t.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const cells = (line) => line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

export function renderMarkdown(src) {
  const lines = String(src || "").replace(/\r\n?/g, "\n").split("\n");
  const root = el("div", { class: "md" });
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (line.startsWith("```")) {
      const body = [];
      i++;
      while (i < lines.length && !lines[i].startsWith("```")) body.push(lines[i++]);
      i++;
      root.append(el("pre", {}, el("code", { text: body.join("\n") })));
      continue;
    }
    const h = line.match(/^(#{1,3})\s+(.*)$/);
    if (h) { root.append(el(`h${h[1].length + 2}`, {}, ...inline(h[2]))); i++; continue; }
    if (/^\s*\|/.test(line)) {
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(lines[i++]);
      const sep = rows.length > 1 && /^[\s|:-]+$/.test(rows[1]);
      const head = sep ? cells(rows[0]) : null;
      const body = rows.slice(sep ? 2 : 0).map(cells);
      root.append(el("table", { class: "md-table" },
        head ? el("thead", {}, el("tr", {}, ...head.map((c) => el("th", {}, ...inline(c))))) : null,
        el("tbody", {}, ...body.map((r) => el("tr", {}, ...r.map((c) => el("td", {}, ...inline(c))))))));
      continue;
    }
    const li = line.match(/^\s*([-*]|\d+[.)])\s+(.*)$/);
    if (li) {
      const ordered = /\d/.test(li[1]);
      const list = el(ordered ? "ol" : "ul");
      while (i < lines.length) {
        const m = lines[i].match(/^\s*([-*]|\d+[.)])\s+(.*)$/);
        if (!m) break;
        list.append(el("li", {}, ...inline(m[2])));
        i++;
      }
      root.append(list);
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,3}\s|```|\s*\||\s*([-*]|\d+[.)])\s)/.test(lines[i])) para.push(lines[i++].trim());
    root.append(el("p", {}, ...inline(para.join(" "))));
  }
  return root;
}
