// Export (#/export): pick a format and a conflict policy, see what is left out,
// then download, or write to a file in the project / S3 / a database table.

import { el, errorState, link, loadingState, toast } from "../common/dom.js";

// "table" is not a format choice: picking "Write to a database table" under Where sends it
// (usability #12). Plain names first; the technical detail is the secondary line (#11).
const FORMAT_ORDER = ["csv", "jsonl", "parquet", "golden", "rollouts", "records"];
const TABULAR = ["csv", "jsonl", "parquet"];
const NICE = { csv: "CSV", jsonl: "JSON lines", parquet: "Parquet", golden: "answer key", rollouts: "human scores", records: "label history", table: "database table" };
const EXT = { csv: "csv", jsonl: "jsonl", parquet: "parquet", golden: "jsonl", rollouts: "jsonl", records: "jsonl" };

export async function mount(root, { api }) {
  const page = el("div", { class: "page exp" }, loadingState("Loading export options…"));
  root.replaceChildren(page);
  let meta;
  try {
    meta = await api.get("/api/export/formats");
  } catch (err) {
    page.replaceChildren(errorState(err, () => mount(root, { api })));
    return;
  }
  const S = { format: "csv", conflicts: "exclude", rows: "items", include: false, dest: "download", path: "", s3: "", table: "" };
  const fmtMeta = (f) => meta.formats.find((x) => x.name === f) || { name: f };
  const sent = () => (S.dest === "table" ? "table" : S.format); // what the API gets
  const summaryBox = el("div", { class: "exp-summary", "aria-live": "polite" });
  const result = el("div", { class: "exp-result", role: "status" });

  function radio(name, value, label, help, checked, onchange, tech) {
    const id = `${name}-${value}`;
    return el("label", { class: "choice", for: id },
      el("input", { type: "radio", id, name, value, checked, onchange }),
      el("span", { class: "choice-text" }, el("b", { text: label }), help ? el("small", { class: "muted", text: help }) : null,
        tech ? el("small", { class: "tech", text: tech }) : null));
  }

  const tableNote = el("p", { class: "muted small", hidden: true, text: "A database table always gets the label table: one row per item (or per label), the same columns as CSV." });
  const formats = el("fieldset", { class: "panel exp-block", id: "format-block" }, el("legend", { text: "1. Format" }),
    tableNote,
    el("div", { class: "choices" }, ...FORMAT_ORDER.filter((f) => meta.formats.some((x) => x.name === f)).map((f) => {
      const m = fmtMeta(f);
      return radio("format", f, m.title || NICE[f], m.plain || m.help, f === S.format, () => { S.format = f; sync(); },
        m.title && !TABULAR.includes(f) ? m.help : null);
    })));

  const policy = el("fieldset", { class: "panel exp-block" }, el("legend", { text: "2. Items labellers disagree on" }),
    el("p", { class: "muted small", text: "Adjudicated items always use the adjudicator's label. This choice is for disagreements nobody has settled." }),
    el("div", { class: "choices" },
      radio("conflicts", "exclude", "Leave them out", "safest: only agreed and adjudicated labels", true, () => { S.conflicts = "exclude"; sync(); refresh(); }),
      radio("conflicts", "majority", "Strict majority", "2 of 3 wins; ties are still left out", false, () => { S.conflicts = "majority"; sync(); refresh(); })));

  const rowsBlock = el("fieldset", { class: "panel exp-block", id: "rows-block" }, el("legend", { text: "3. Rows" }),
    el("div", { class: "choices" },
      radio("rows", "items", "One row per item", "its final label, who decided and when", true, () => { S.rows = "items"; sync(); }),
      radio("rows", "labels", "One row per label", "every labeller's label, for your own analysis", false, () => { S.rows = "labels"; sync(); })),
    el("label", { class: "check" }, el("input", { type: "checkbox", onchange: (e) => { S.include = e.target.checked; sync(); } }), " Also include items without a final label (marked by status)"));

  const destInput = (id, ph, key) => el("input", { id, type: "text", placeholder: ph, oninput: (e) => { S[key] = e.target.value.trim(); sync(); } });
  const destBlock = el("fieldset", { class: "panel exp-block" }, el("legend", { text: "4. Where" }),
    el("div", { class: "choices", id: "dest-choices" },
      radio("dest", "download", "Download", "save the file from your browser", true, () => { S.dest = "download"; sync(); }),
      radio("dest", "file", "File in the project folder", "e.g. exports/labels.csv", false, () => { S.dest = "file"; sync(); }),
      radio("dest", "s3", "Upload to S3", "uses the server's AWS credentials (profile / SSO / env)", false, () => { S.dest = "s3"; sync(); }),
      meta.source ? radio("dest", "table", "Write to a database table", `the label table, into the project's ${meta.source.kind} database`, false, () => { S.dest = "table"; sync(); }) : null),
    el("div", { class: "dest-field", id: "field-file" }, el("label", { for: "dest-path", text: "Path inside the project" }), destInput("dest-path", "exports/labels.csv", "path")),
    el("div", { class: "dest-field", id: "field-s3" }, el("label", { for: "dest-s3-url", text: "S3 URL" }), destInput("dest-s3-url", "s3://bucket/labels/labels.parquet", "s3")),
    el("div", { class: "dest-field", id: "field-table" }, el("label", { for: "dest-table-name", text: "Table name" }), destInput("dest-table-name", "labels_export", "table")));

  const go = el("button", { type: "button", class: "btn btn-primary btn-lg", onclick: () => run() });
  const cliCode = el("code", { class: "cli-code" });
  const cli = el("div", { class: "muted small cli-hint", title: "Run it in the project folder" }, "Same as: ", cliCode);

  // The equivalent `caliper label export` command, from the current choices (usability #5).
  function cliText() {
    const f = sent();
    const parts = ["caliper label export", `--format ${f}`];
    if (S.dest === "table") parts.push(`--table ${S.table || "labels_export"}`);
    else if (S.dest === "s3") parts.push(`--out ${S.s3 || "s3://bucket/labels." + EXT[f]}`);
    else if (S.dest === "file") parts.push(`--out ${S.path || "exports/labels." + EXT[f]}`);
    else parts.push(`--out labels.${EXT[f]}`);
    if ((TABULAR.includes(f) || f === "table") && S.rows === "labels") parts.push("--rows labels");
    if (S.conflicts !== "exclude") parts.push(`--conflicts ${S.conflicts}`);
    if (S.include && f !== "rollouts" && f !== "records") parts.push("--include-unlabelled");
    return parts.join(" ");
  }

  function sync() {
    destBlock.querySelector("#field-file").hidden = S.dest !== "file";
    destBlock.querySelector("#field-s3").hidden = S.dest !== "s3";
    destBlock.querySelector("#field-table").hidden = S.dest !== "table";
    for (const i of destBlock.querySelectorAll('input[name="dest"]')) i.checked = i.value === S.dest;
    const toTable = S.dest === "table";
    // a table always gets the label table: the file formats are not a choice then
    tableNote.hidden = !toTable;
    for (const i of formats.querySelectorAll('input[name="format"]')) i.disabled = toTable;
    formats.classList.toggle("is-off", toTable);
    rowsBlock.hidden = !(toTable || TABULAR.includes(S.format));
    const what = toTable ? NICE.table : (TABULAR.includes(S.format) ? NICE[S.format] : fmtMeta(S.format).title || NICE[S.format]);
    go.textContent = S.dest === "download" ? `Download ${what}` : `Export to ${toTable ? "the database" : S.dest === "s3" ? "S3" : "the project folder"}`;
    cliCode.textContent = cliText();
  }

  async function refresh() {
    try {
      const s = await api.get(`/api/export/summary?conflicts=${S.conflicts}`);
      const left = s.left_out;
      // replaceChildren() would print a null child as the text "null" (usability #4)
      summaryBox.replaceChildren(...[
        el("div", { class: "exp-counts" },
          el("div", {}, el("b", { class: "num", text: String(s.resolved) }), el("span", { text: "items with a final label" })),
          el("div", {}, el("b", { class: "num", text: String(s.adjudicated) }), el("span", { text: "adjudicated" })),
          el("div", { class: s.conflicts ? "warn" : "" }, el("b", { class: "num", text: String(s.conflicts) }), el("span", { text: "disagreements left out" })),
          el("div", {}, el("b", { class: "num", text: String(s.incomplete) }), el("span", { text: "waiting for more labels" })),
          el("div", {}, el("b", { class: "num", text: String(s.unlabelled) }), el("span", { text: "not labelled" }))),
        left.length ? el("details", { class: "left-out" },
          el("summary", { text: `What is left out (${left.length})` }),
          el("ul", {}, ...left.slice(0, 100).map((x) => el("li", {}, el("code", { text: x.item_id }), " ",
            el("span", { class: "muted", text: x.why === "conflict" ? Object.entries(x.labels).map(([k, v]) => `${k}: ${v}`).join(" · ") : `${x.labels_so_far} label(s) so far` })))),
          s.conflicts ? link("#/adjudicate", "Settle disagreements") : null) : null].filter(Boolean));
    } catch (err) {
      summaryBox.replaceChildren(errorState(err, refresh));
    }
  }

  async function run() {
    result.replaceChildren();
    const q = `format=${sent()}&conflicts=${S.conflicts}&rows=${S.rows}&include_unlabelled=${S.include}`;
    if (S.dest === "download") {
      // A plain link: the auth cookie rides along, and the browser handles the file.
      const a = el("a", { href: `/api/export/download?${q}`, download: "" });
      document.body.append(a);
      a.click();
      a.remove();
      result.replaceChildren(el("p", { class: "ok", text: "Downloading…" }));
      return;
    }
    const body = { format: sent(), conflicts: S.conflicts, rows: S.rows, include_unlabelled: S.include };
    if (S.dest === "file") body.destination = { kind: "file", path: S.path || undefined };
    if (S.dest === "s3") {
      if (!S.s3.startsWith("s3://")) { toast("Enter an s3:// URL", "error"); return; }
      body.destination = { kind: "s3", path: S.s3 };
    }
    if (S.dest === "table") {
      if (!S.table) { toast("Enter a table name", "error"); return; }
      body.destination = { ...(meta.source || {}), table: S.table };
    }
    go.disabled = true;
    try {
      const r = await api.post("/api/export", body);
      result.replaceChildren(el("p", { class: "ok" }, el("b", { text: `Wrote ${r.written} row(s)` }), ` to ${r.destination}`));
    } catch (err) {
      result.replaceChildren(el("div", { class: "err", role: "alert" }, el("b", { text: err.message }), err.hint ? el("p", { text: `Fix: ${err.hint}` }) : null));
    } finally {
      go.disabled = false;
    }
  }

  page.replaceChildren(
    el("div", { class: "page-head" },
      el("div", {}, el("h1", { text: "Export labels" }),
        el("p", { text: "Every row says who labelled it, when, any note, and which version of the project settings was in use." })),
      cli),
    el("div", { class: "exp-grid" },
      el("div", { class: "exp-form" }, formats, policy, rowsBlock, destBlock, el("div", { class: "exp-go" }, go, result)),
      el("aside", { class: "panel exp-side", "aria-label": "What the export holds" }, el("h2", { text: "What you'll get" }), summaryBox)));
  sync();
  await refresh();
}
