// Progress dashboard (#/dashboard): totals, per-labeller and per-queue progress,
// throughput, kappa overall and by category, golden accuracy, disagreements.

import { el, errorState, fmtKappa, fmtPct, fmtTime, link, loadingState } from "../common/dom.js";

function tile(value, label, sub, cls = "") {
  return el("div", { class: `tile ${cls}` },
    el("div", { class: "tile-value num", text: value }),
    el("div", { class: "tile-label", text: label }),
    sub ? el("div", { class: "tile-sub muted", text: sub }) : null);
}

function bar(frac, label) {
  const pct = Math.max(0, Math.min(100, Math.round((frac || 0) * 100)));
  return el("div", { class: "hbar", role: "img", "aria-label": label || `${pct}%` },
    el("span", { style: `width:${pct}%` }));
}

function kappaWord(k) {
  if (k === null || k === undefined) return null;
  if (k >= 0.8) return ["strong", "tag-good"];
  if (k >= 0.6) return ["substantial", "tag-good"];
  if (k >= 0.4) return ["moderate", "tag-warn"];
  return ["weak", "tag-bad"];
}

function kappaCell(s) {
  const w = kappaWord(s.kappa);
  return el("span", { class: "kappa", title: s.undefined_reason || "" },
    el("b", { class: "num", text: fmtKappa(s) }),
    w ? el("span", { class: `tag ${w[1]}`, text: w[0] }) : el("span", { class: "muted", text: s.undefined_reason ? "undefined" : "" }));
}

function section(title, ...body) {
  return el("section", { class: "panel dash-section", "aria-labelledby": `h-${title.replace(/\W+/g, "-")}` },
    el("h2", { id: `h-${title.replace(/\W+/g, "-")}`, text: title }), ...body);
}

function render(d) {
  const t = d.totals;
  const o = d.agreement.overall;
  const tiles = el("div", { class: "tiles" },
    tile(`${t.complete} / ${t.items}`, "items complete", fmtPct(t.items ? t.complete / t.items : null)),
    tile(String(t.labels), "labels", `${t.labellers} labeller${t.labellers === 1 ? "" : "s"}`),
    tile(fmtKappa(o), !o.n_items ? "κ (agreement)" : d.agreement.method === "fleiss" ? "Fleiss' κ" : "Cohen's κ",
      o.n_items ? `${o.n_items} items with 2+ labels` : (d.double_label > 1 ? "no overlap yet" : "single labelling")),
    tile(String(t.open_disagreements), "to adjudicate", `${t.adjudicated} adjudicated`, t.open_disagreements ? "tile-warn" : ""));

  const people = d.labellers.length
    ? el("table", { class: "data" },
      el("thead", {}, el("tr", {}, ...["Labeller", "Labels", "Last hour", "Per hour", "Skips", "Golden", "Last label"].map((h, i) => el("th", { class: i && i < 5 ? "num" : "", scope: "col", text: h })))),
      el("tbody", {}, ...d.labellers.map((p) => el("tr", {},
        el("th", { scope: "row", text: p.name }),
        el("td", { class: "num", text: String(p.labels) }),
        el("td", { class: "num", text: String(p.last_hour) }),
        el("td", { class: "num", text: p.per_hour === null ? "–" : String(p.per_hour) }),
        el("td", { class: "num", text: String(p.skips) }),
        el("td", {}, p.golden
          ? el("span", { class: "gold" }, el("b", { class: "num", text: fmtPct(p.golden.accuracy) }),
            el("span", { class: "muted", text: ` ${p.golden.correct}/${p.golden.answered}` }),
            p.golden.accuracy !== null && p.golden.accuracy < 0.7 ? el("span", { class: "tag tag-bad", text: "check" }) : null)
          : el("span", { class: "muted", text: "–" })),
        el("td", { class: "muted nowrap", text: fmtTime(p.last_at) })))))
    : el("p", { class: "muted", text: "Nobody has labelled yet." });

  const queues = el("table", { class: "data" },
    el("thead", {}, el("tr", {}, ...["Queue", "Progress", "Complete", "Total", "Last hour"].map((h, i) => el("th", { class: i >= 2 ? "num" : "", scope: "col", text: h })))),
    el("tbody", {}, ...d.queues.map((q) => el("tr", {},
      el("th", { scope: "row" }, el("a", { href: `#/label?queue=${encodeURIComponent(q.name)}`, text: q.name })),
      el("td", { class: "bar-cell" }, bar(q.total ? q.complete / q.total : 0, `${q.complete} of ${q.total} complete`)),
      el("td", { class: "num", text: String(q.complete) }),
      el("td", { class: "num", text: String(q.total) }),
      el("td", { class: "num", text: String(q.last_hour) })))));

  const cats = d.agreement.by_category;
  const agreement = o.n_items
    ? el("div", {},
      el("p", { class: "lead" }, "Overall ", kappaCell(o), el("span", { class: "muted", text: ` · all agree on ${fmtPct(o.percent_all_agree)} of ${o.n_items} items · ${o.n_raters} labellers` })),
      cats.length ? el("table", { class: "data" },
        el("thead", {}, el("tr", {}, ...["Category", "Items", "κ", "All agree"].map((h, i) => el("th", { class: i === 1 ? "num" : "", scope: "col", text: h })))),
        el("tbody", {}, ...cats.map((c) => el("tr", {},
          el("th", { scope: "row", text: c.category }),
          el("td", { class: "num", text: String(c.n_items) }),
          el("td", {}, kappaCell(c)),
          el("td", { class: "bar-cell" }, el("div", { class: "bar-with-num" }, bar(c.percent_all_agree, `all agree ${fmtPct(c.percent_all_agree)}`), el("span", { class: "num", text: fmtPct(c.percent_all_agree) }))))))) : null)
    : el("p", { class: "muted", text: d.double_label > 1
      ? "Agreement appears once two people have labelled the same item."
      : "Each item is labelled once (double_label: 1). Set double labelling in Settings to measure agreement." });

  const dis = d.disagreements.length
    ? el("ul", { class: "dis-list" }, ...d.disagreements.slice(0, 12).map((x) => el("li", {},
      el("code", { text: x.item_id }),
      el("span", { class: "muted", text: x.labels.map((l) => `${l.labeller}: ${l.display}`).join(" · ") }),
      x.adjudication ? el("span", { class: "tag tag-good", text: `final: ${x.adjudication.display}` }) : el("span", { class: "tag tag-warn", text: "open" }))))
    : el("p", { class: "muted", text: "No disagreements." });

  return [
    el("div", { class: "page-head" },
      el("div", {}, el("h1", { text: "Progress" }), el("p", { text: `${d.label_type.replace("_", " ")} labels · ${d.double_label} per item` })),
      el("div", { class: "state-actions" }, link("#/label", "Label"), link("#/adjudicate", `Adjudicate (${t.open_disagreements})`, t.open_disagreements > 0), link("#/export", "Export"))),
    tiles,
    el("div", { class: "dash-grid" },
      section("Labellers", people),
      section("Queues", queues),
      section("Agreement", agreement),
      section("Disagreements", dis, d.disagreements.length ? link("#/adjudicate", "Open adjudication") : null)),
  ];
}

export async function mount(root, { api }) {
  const page = el("div", { class: "page dash" }, loadingState("Loading progress…"));
  root.replaceChildren(page);
  async function load() {
    try {
      const d = await api.get("/api/dashboard");
      page.replaceChildren(...render(d));
    } catch (err) {
      page.replaceChildren(errorState(err, load));
    }
  }
  await load();
  const timer = setInterval(() => { if (!document.hidden) load(); }, 20000);
  return () => clearInterval(timer);
}
