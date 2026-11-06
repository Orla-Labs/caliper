// New-project wizard (dev 2): #/wizard/<step>
//   data -> card -> labels -> workflow -> review
// The draft lives in sessionStorage, so a reload or Back keeps everything.

import { h, clear, ensureCss, announce, stateBox, errorBox, plural, debounce, nextId, kbd, confirmDialog, hasAppBar, appbarSlot, projectChanged, focusHeading } from "../common/wizard-dom.js";
import { cardDesigner, labelsEditor, workflowEditor, columnsTable, hotkeyIssues } from "../common/wizard-editors.js";
import { SOURCE_KINDS, sourceForm, credentialList, cleanConfig, uploadFile, folderPicker, suggestFields } from "../common/wizard-source.js";

const KEY = "caliper-label-wizard";
const STEPS = [
  ["data", "Connect data"],
  ["card", "Design the card"],
  ["labels", "Labels"],
  ["workflow", "Workflow"],
  ["review", "Review & start"],
];

function fresh() {
  return {
    kind: "upload",
    draft: null,
    upload: null,
    preview: null,
    touched: {},
    dir: "",
    dirEdited: false,
    config: {
      version: 1,
      name: "",
      source: { kind: "file", path: null },
      card: { fields: [] },
      labels: { type: "single", options: [] },
      workflow: { queues: {}, double_label: 1, assignment: "open" },
    },
  };
}

function load() {
  try {
    const s = JSON.parse(sessionStorage.getItem(KEY) || "null");
    return s && s.config ? s : fresh();
  } catch {
    return fresh();
  }
}

function save(state) {
  try { sessionStorage.setItem(KEY, JSON.stringify(state)); } catch { /* private mode: keep in memory */ }
}

export function resetWizard() {
  try { sessionStorage.removeItem(KEY); } catch { /* ignore */ }
}

export async function mount(el, ctx) {
  ensureCss("wizard");
  const { api, go } = ctx;
  const state = load();
  let step = STEPS.some(([s]) => s === ctx.args[0]) ? ctx.args[0] : "data";
  if (ctx.query.get("new") === "1") {
    Object.assign(state, fresh());
    save(state);
    history.replaceState(null, "", `#/wizard/${ctx.args[0] || "data"}`); // a reload keeps the draft
  }
  const persist = debounce(() => save(state), 150);
  const changed = () => { persist(); updateNav(); };
  const saveNow = () => save(state);
  window.addEventListener("pagehide", saveNow); // a reload right after a change keeps it

  // can the user go past each step?
  function blocker(s) {
    const cfg = state.config;
    if (s === "data") {
      if (!state.preview || !state.preview.ok) return "Connect your data and load the preview first.";
    }
    if (s === "card" && !cfg.card.fields.length) return "Add at least one column to the card.";
    if (s === "labels") {
      const L = cfg.labels;
      if (["single", "multi", "yes_no_unsure"].includes(L.type) && !L.options.some((o) => !o.retired)) return "Add at least one option, or pick a template.";
      if (Object.keys(hotkeyIssues(L)).length) return "Fix the hotkey clashes first.";
      const vals = L.options.map((o) => o.value);
      if (new Set(vals).size !== vals.length) return "Two options have the same stored value.";
      if (L.type === "score" && L.score && L.score.max <= L.score.min) return "Score max must be above min.";
    }
    if (s === "workflow") {
      const W = cfg.workflow;
      if (W.golden && !W.golden.answer_column) return "Choose the golden answer column, or turn golden checks off.";
      if (W.queues.by === "") return "Choose the queue column.";
    }
    return null;
  }
  const idx = (s) => STEPS.findIndex(([x]) => x === s);
  const reachable = (s) => STEPS.slice(0, idx(s)).every(([x]) => !blocker(x));
  if (!reachable(step)) step = STEPS.find(([x]) => blocker(x))?.[0] || "data";

  const root = h("div", { class: "wz-root wz-wizard" });
  const body = h("div", { class: "wz-step-body" });
  const nav = h("div", { class: "wz-footer" });
  const stepper = h("ol", { class: "wz-stepper-bar" });
  const startOver = h("button", { type: "button", class: "wz-btn wz-btn-ghost wz-btn-small", onclick: async () => {
    if (await confirmDialog({ title: "Start over?", body: h("p", {}, "This clears everything you set up in this wizard."), ok: "Start over", danger: true })) {
      resetWizard();
      location.hash = "#/wizard/data?new=1";
    }
  } }, "Start over");
  const shellBar = hasAppBar();
  const clearSlot = shellBar ? appbarSlot(h("span", { class: "wz-slot-title" }, "New project"), startOver) : () => {};
  clear(root,
    shellBar ? null : h("header", { class: "wz-top" },
      h("a", { href: "#/start", class: "wz-brand" }, h("span", { class: "wz-logo", "aria-hidden": "true" }, "◧"), " caliper label"),
      h("span", { class: "wz-top-title" }, "New project"),
      startOver),
    h("nav", { "aria-label": "Wizard steps" }, stepper),
    h("main", { class: "wz-main", id: "wz-main" }, body),
    nav,
  );
  clear(el, root);

  function renderStepper() {
    clear(stepper, STEPS.map(([s, t], i) => {
      const can = reachable(s);
      const cur = s === step;
      const done = i < idx(step) || (can && !blocker(s) && i < idx(step));
      return h("li", { class: `wz-step${cur ? " wz-step-cur" : ""}${done ? " wz-step-done" : ""}` },
        h("button", {
          type: "button", "aria-current": cur ? "step" : null, disabled: !can && !cur,
          onclick: () => go("wizard", s),
        }, h("span", { class: "wz-step-n", "aria-hidden": "true" }, done ? "✓" : String(i + 1)), h("span", { class: "wz-step-t" }, t)));
    }));
  }

  const nextBtn = h("button", { type: "button", class: "wz-btn wz-btn-primary", id: "wz-next" });
  const backBtn = h("button", { type: "button", class: "wz-btn", id: "wz-back" }, "Back");
  const why = h("span", { class: "wz-why", id: "wz-why", "aria-live": "polite" });
  nav.append(backBtn, why, nextBtn);
  backBtn.addEventListener("click", () => {
    const i = idx(step);
    if (i <= 0) go("start"); else go("wizard", STEPS[i - 1][0]);
  });
  nextBtn.addEventListener("click", () => {
    const b = blocker(step);
    if (b) { announce(b); why.textContent = b; return; }
    const i = idx(step);
    if (i < STEPS.length - 1) go("wizard", STEPS[i + 1][0]);
  });

  function updateNav() {
    const b = step === "review" ? null : blocker(step);
    nextBtn.hidden = step === "review";
    nextBtn.textContent = step === "workflow" ? "Review" : "Next";
    nextBtn.setAttribute("aria-disabled", b ? "true" : "false");
    nextBtn.classList.toggle("wz-btn-disabled", !!b);
    if (b) { nextBtn.setAttribute("aria-describedby", "wz-why"); why.textContent = b; } else { nextBtn.removeAttribute("aria-describedby"); why.textContent = ""; }
    backBtn.textContent = idx(step) === 0 ? "Cancel" : "Back";
    renderStepper();
  }

  const onKey = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      if (step === "review") root.querySelector("#wz-create")?.click(); else nextBtn.click();
    }
  };
  document.addEventListener("keydown", onKey);

  // ------------------------------------------------------------------ steps
  async function renderData() {
    const result = h("div", { class: "wz-result" });
    const formBox = h("div", { class: "wz-source-form" });
    const kindName = nextId("kind");

    async function runPreview() {
      clear(result, stateBox("loading", "Reading your data…", "Checking credentials and loading the first 50 rows."));
      const src = state.config.source;
      try {
        const res = await api.post("/api/setup/preview", { source: cleanConfig(src), draft: state.draft });
        state.preview = res;
        if (res.ok) {
          if (!src.id_column && !state.touched.id) src.id_column = guessId(res.columns);
          clear(result, stateBox("loading", "Checking that images will load…", "Trying a few image links the way the labeller will."));
          state.mediaIssues = await checkImages(res);
          if (!state.touched.card) state.config.card.fields = suggestFields(res.columns, src.id_column, Object.keys(state.mediaIssues));
          if (!state.config.name) state.config.name = defaultName();
          announce(`Connected. ${plural(res.count ?? res.rows.length, "row")}, ${plural(res.columns.length, "column")}.`);
        } else announce(`Could not read the data: ${res.error && res.error.detail}`);
      } catch (err) {
        state.preview = { ok: false, credentials: [], error: { detail: err.message, hint: err.hint } };
      }
      changed();
      saveNow();
      showResult();
      if (state.kind === "upload") renderForm(); // the drop zone repeats the row count
      // the confirmation is below the source form: bring it into view (#17)
      const sum = result.querySelector(".wz-preview-summary") || result.firstElementChild;
      if (sum && sum.scrollIntoView) sum.scrollIntoView({ block: "center", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    }

    // r3 #1: image columns whose links the server signs (s3://, gs://) are tried now, so a
    // missing credential shows up at the card step instead of on every card later.
    async function checkImages(res) {
      const issues = {};
      const signed = /^(s3|gs|gcs|r2):\/\//i;
      const cols = res.columns.filter((c) => c.display === "image" && res.rows.some((r) => signed.test(String(r[c.name] ?? ""))));
      for (const c of cols) {
        try {
          const body = { source: cleanConfig(state.config.source), column: c.name, n: 3 };
          if (state.kind === "upload" && state.draftDir) body.project_dir = state.draftDir;
          const chk = await api.post("/api/source/media-check", body);
          if (chk.ok === false && chk.failed > 0) issues[c.name] = { detail: chk.detail || "the image links could not be loaded", hint: chk.hint || null };
        } catch { /* the check itself is unavailable: say nothing rather than guess */ }
      }
      return issues;
    }

    function defaultName() {
      const src = state.config.source;
      if (src.table) return humanize(src.table.split(".").pop());
      if (src.query) return "Query results";
      const p = state.upload?.name || src.path || "";
      return humanize(String(p).split(/[\\/]/).pop().replace(/\.[a-z0-9]+$/i, ""));
    }

    function humanize(s) {
      const base = String(s || "").replace(/[*?]/g, "").replace(/[_-]+/g, " ").trim();
      return base ? base.charAt(0).toUpperCase() + base.slice(1) : "Untitled project";
    }

    function showResult() {
      const p = state.preview;
      if (!p) {
        clear(result, stateBox("empty", "No data yet", state.kind === "upload" ? "Choose a file above. CSV, TSV, JSONL, JSON, Parquet and Excel work." : "Fill in the connection above and press Connect."));
        return;
      }
      const parts = [];
      const creds = p.credentials && p.credentials.length ? credentialList(p.credentials) : null;
      if (!p.ok) {
        parts.push(creds);
        parts.push(errorBox({ message: (p.error && p.error.detail) || "Could not read the data", hint: p.error && p.error.hint }, runPreview));
        return clear(result, parts);
      }
      const idSel = nextId("id");
      parts.push(
        h("div", { class: "wz-preview-summary" },
          h("span", { class: "wz-ok-dot", "aria-hidden": "true" }), h("strong", {}, "Connected. "),
          `${plural(p.count ?? p.rows.length, "row")} · ${plural(p.columns.length, "column")}`,
          p.count && p.count > p.rows.length ? h("span", { class: "wz-muted" }, ` · showing the first ${p.rows.length}`) : null),
        creds,
        h("div", { class: "wz-inline-field" },
          h("label", { for: idSel }, "Item id column"),
          h("select", { id: idSel, onchange: (e) => {
            state.config.source.id_column = e.target.value || null;
            state.touched.id = true;
            if (!state.touched.card) state.config.card.fields = suggestFields(p.columns, state.config.source.id_column, Object.keys(state.mediaIssues || {}));
            changed();
          } },
            h("option", { value: "" }, "row number (no id column)"),
            p.columns.map((c) => h("option", { value: c.name, selected: state.config.source.id_column === c.name }, c.name))),
          h("span", { class: "wz-help" }, "A stable id keeps labels attached if the data is re-sorted or refreshed.")),
        columnsTable({ columns: p.columns, rows: p.rows, caption: "Preview of the first rows" }),
      );
      clear(result, parts);
    }

    function guessId(cols) {
      const names = cols.map((c) => c.name);
      return names.find((n) => /^(id|item_id|uuid|key)$/i.test(n)) || names.find((n) => /(^|_)id$/i.test(n)) || null;
    }

    async function onFile(file) {
      if (!file) return;
      clear(result, stateBox("loading", `Uploading ${file.name}…`, `${(file.size / 1e6).toFixed(1)} MB`));
      try {
        const up = await uploadFile(file, state.draft, (pct) => {
          const t = result.querySelector(".wz-state-text p");
          if (t) t.textContent = `${pct}% of ${(file.size / 1e6).toFixed(1)} MB`;
        });
        state.draft = up.draft;
        state.draftDir = up.dir || null;
        state.upload = { name: file.name, size: up.size };
        state.config.source = { kind: "file", path: up.path };
        state.config.name = "";
        state.touched.card = false;
        state.preview = null;
        renderForm();
        state.touched.id = false;
        await runPreview();
      } catch (err) {
        clear(result, errorBox(err, () => renderForm()));
      }
    }

    function renderForm() {
      clear(formBox, sourceForm(state, { onChange: changed, onFile, onConnect: runPreview, api }));
    }

    const kinds = SOURCE_KINDS;
    const picker = h("fieldset", { class: "wz-panel wz-radios" },
      h("legend", {}, "Where is your data?"),
      h("div", { class: "wz-kind-grid" }, kinds.map(([k, title, sub]) =>
        h("label", { class: `wz-kind${state.kind === k ? " wz-on" : ""}` },
          h("input", {
            type: "radio", name: kindName, value: k, checked: state.kind === k,
            onchange: () => {
              state.kind = k;
              state.preview = null;
              const map = { upload: "file", local: "file", folder: "folder" };
              const kind = map[k] || k;
              const keepPath = (k === "local" && state.config.source.kind === "file" && !state.upload) ? state.config.source.path : null;
              state.config.source = { kind, path: keepPath };
              if (k !== "upload") { state.upload = null; }
              picker.querySelectorAll(".wz-kind").forEach((l) => l.classList.toggle("wz-on", l.querySelector("input").checked));
              renderForm();
              showResult();
              changed();
            },
          }),
          h("span", { class: "wz-kind-title" }, title),
          h("span", { class: "wz-kind-sub" }, sub)))));

    clear(body,
      h("h1", { class: "wz-h1" }, "Connect data"),
      h("p", { class: "wz-lead" }, "Pick a file or a connection. Credentials stay in your environment: the project file only names the variable."),
      picker, formBox, result);
    renderForm();
    showResult();
  }

  function renderCard() {
    const p = state.preview || { columns: [], rows: [] };
    const box = h("div");
    clear(body,
      h("h1", { class: "wz-h1" }, "Design the card"),
      h("p", { class: "wz-lead" }, "Choose what a labeller sees for each item. Types are detected; change them if they look wrong."),
      box);
    cardDesigner(box, { card: state.config.card, columns: p.columns, rows: p.rows, options: () => state.config.labels.options, mediaIssues: state.mediaIssues || {}, onChange: () => { state.touched.card = true; changed(); } });
  }

  async function renderLabels() {
    const box = h("div");
    clear(body,
      h("h1", { class: "wz-h1" }, "Labels"),
      h("p", { class: "wz-lead" }, "What can a labeller answer? Each option gets a hotkey so labelling stays on the keyboard."),
      box);
    const L = state.config.labels;
    if (!state.touched.labels && !L.options.length && ["single", "multi"].includes(L.type)) {
      try {
        const res = await api.post("/api/setup/templates/correct_wrong_unsure", {});
        L.options = res.labels.options;
        state.fromTemplate = null; // set by the editor below from the options as loaded
        state.touched.labels = true;
        changed();
      } catch { /* empty state explains */ }
    }
    const ed = { labels: L, api, fromTemplate: state.fromTemplate, onTemplate: (k) => { state.fromTemplate = k; changed(); },
      suggestions: optionSuggestions(), card: state.config.card,
      onChange: () => { state.touched.labels = true; changed(); } };
    labelsEditor(box, ed);
  }

  // #14: columns with a handful of short values, as label option candidates.
  function optionSuggestions() {
    const p = state.preview;
    if (!p || !p.ok) return [];
    const idCol = state.config.source.id_column;
    const hint = /(priority|severity|category|label|sentiment|class|type|status|intent|topic|verdict|rating)/i;
    const out = [];
    for (const c of p.columns) {
      if (c.name === idCol || !["badge", "text"].includes(c.display || "text")) continue;
      const freq = new Map();
      let n = 0;
      for (const r of p.rows) {
        const v = r[c.name];
        if (v === null || v === undefined || v === "" || typeof v === "object") continue;
        const s = String(v).trim();
        if (!s || s.length > 40) { n = -1; break; }
        freq.set(s, (freq.get(s) || 0) + 1);
        n++;
      }
      if (n < 4 || freq.size < 2 || freq.size > 12 || freq.size > n / 2) continue;
      const values = [...freq.entries()].sort((a, b) => b[1] - a[1]).map(([v]) => v);
      out.push({ column: c.name, values, score: hint.test(c.name) ? 0 : 1 });
    }
    return out.sort((a, b) => a.score - b.score);
  }

  function renderWorkflow() {
    const box = h("div");
    clear(body,
      h("h1", { class: "wz-h1" }, "Workflow"),
      h("p", { class: "wz-lead" }, "How the work is split, who labels what, and what labellers should know."),
      box);
    workflowEditor(box, {
      workflow: state.config.workflow, labels: state.config.labels,
      columns: state.preview ? state.preview.columns : [], onChange: changed,
    });
  }

  async function renderReview() {
    const cfg = state.config;
    const nameId = nextId("nm");
    const dirId = nextId("dir");
    const yamlBox = h("div", { class: "wz-yaml-wrap" }, stateBox("loading", "Building caliper.label.yaml…"));
    const issuesBox = h("div");
    const createBtn = h("button", { type: "button", class: "wz-btn wz-btn-primary wz-btn-big", id: "wz-create" }, "Create project & start labelling");
    const status = h("div", { "aria-live": "polite" });

    let projects = null;
    try { projects = await api.get("/api/projects"); } catch { /* fall back to a relative folder */ }
    const sep = (projects && projects.sep) || (projects && projects.cwd && projects.cwd.includes("\\") ? "\\" : "/");
    const slugOf = (name) => (name || "project").toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "project";
    // The folder is <parent>/<slug of the name>. The parent defaults to the last one used
    // (else ~/caliper-projects), never to the server's working directory.
    if (!state.dirEdited) {
      const parent = state.parent || (projects && (projects.default_parent || projects.cwd)) || "";
      state.dir = parent ? `${parent.replace(/[\\/]+$/, "")}${sep}${slugOf(cfg.name)}` : slugOf(cfg.name);
    }

    const dirWarn = h("div", { class: "wz-dir-warn", "aria-live": "polite" });
    const checkDir = debounce(async () => {
      try {
        const res = await api.post("/api/setup/check-dir", { dir: state.dir });
        clear(dirWarn, (res.warnings || []).map((w) => h("p", { class: "wz-warn", role: "note" }, w)));
      } catch { clear(dirWarn); }
    }, 250);

    const dirInput = h("input", { id: dirId, type: "text", value: state.dir, class: "wz-path", spellcheck: "false",
      "aria-describedby": `${dirId}-help`,
      oninput: (e) => { state.dir = e.target.value; state.dirEdited = true; changed(); checkDir(); } });

    const refresh = debounce(async () => {
      try {
        const res = await api.post("/api/setup/validate", { config: cleanConfig(cfg) });
        clear(yamlBox,
          h("div", { class: "wz-row-between" },
            h("h3", {}, "caliper.label.yaml"),
            h("button", { type: "button", class: "wz-btn wz-btn-small", onclick: async () => {
              try { await navigator.clipboard.writeText(res.yaml || ""); announce("YAML copied"); } catch { announce("Copy failed: select the text instead"); }
            } }, "Copy")),
          h("pre", { class: "wz-yaml", tabindex: "0", "aria-label": "The project file that will be written" }, res.yaml || "(invalid config)"));
        const issues = res.issues || [];
        clear(issuesBox, issues.length
          ? h("ul", { class: "wz-issues" }, issues.map((i) => h("li", { class: `wz-issue wz-issue-${i.severity}` },
            h("strong", {}, i.severity === "error" ? "Fix: " : "Note: "), `${i.message}`, i.hint ? h("span", { class: "wz-muted" }, ` (${i.hint})`) : null,
            h("button", { type: "button", class: "wz-link", onclick: () => go("wizard", stepFor(i.path)) }, "Go to step"))))
          : null);
        createBtn.disabled = !res.ok;
      } catch (err) {
        clear(yamlBox, errorBox(err, refresh));
      }
    }, 200);

    const p = state.preview || {};
    const opts = cfg.labels.options.filter((o) => !o.retired);
    const W = cfg.workflow;
    clear(body,
      h("h1", { class: "wz-h1" }, "Review & start"),
      h("div", { class: "wz-review" },
        h("div", { class: "wz-review-left" },
          h("section", { class: "wz-panel" },
            h("div", { class: "wz-field-block" }, h("label", { for: nameId }, "Project name"),
              h("input", { id: nameId, type: "text", value: cfg.name, oninput: (e) => {
                cfg.name = e.target.value;
                if (!state.dirEdited) {
                  // keep the chosen parent, follow the name
                  const cut = Math.max(state.dir.lastIndexOf("/"), state.dir.lastIndexOf("\\"));
                  state.dir = cut >= 0 ? `${state.dir.slice(0, cut)}${sep}${slugOf(cfg.name)}` : slugOf(cfg.name);
                  dirInput.value = state.dir;
                  checkDir();
                }
                changed(); refresh();
              } })),
            h("div", { class: "wz-field-block" }, h("label", { for: dirId }, "Save in folder"),
              h("div", { class: "wz-path-row" }, dirInput,
                h("button", { type: "button", class: "wz-btn", onclick: async () => {
                  const cut = Math.max(state.dir.lastIndexOf("/"), state.dir.lastIndexOf("\\"));
                  const picked = await folderPicker(api, { start: cut > 0 ? state.dir.slice(0, cut) : state.dir, title: "Choose where to put the project folder", files: false });
                  if (picked) {
                    state.parent = picked;
                    state.dir = `${picked.replace(/[\\/]+$/, "")}${sep}${slugOf(cfg.name)}`;
                    state.dirEdited = false; // still follows the name
                    dirInput.value = state.dir; changed(); checkDir();
                  }
                } }, "Browse…")),
              h("p", { class: "wz-help", id: `${dirId}-help` }, "A new folder named after the project. It gets caliper.label.yaml (commit it) and .caliper-label/ (labels; ignored by git)."),
              dirWarn)),
          h("section", { class: "wz-panel" },
            h("h3", {}, "Summary"),
            h("dl", { class: "wz-summary" },
              h("dt", {}, "Data"), h("dd", {}, state.upload ? `${state.upload.name} (uploaded, copied into the project)` : (cfg.source.path || cfg.source.table || cfg.source.kind),
                p.count !== undefined && p.count !== null ? h("span", { class: "wz-muted" }, ` · ${plural(p.count, "item")}`) : null),
              h("dt", {}, "Card"), h("dd", {}, cfg.card.fields.map((f) => f.label || f.column || f.template).join(", ") || "—"),
              h("dt", {}, "Labels"), h("dd", {}, cfg.labels.type === "free_text" ? "Free text" : cfg.labels.type === "score" ? `Score ${cfg.labels.score?.min}–${cfg.labels.score?.max}` :
                opts.map((o, i) => [i ? ", " : "", o.hotkey ? kbd(o.hotkey) : null, " ", o.label || o.value,
                  o.requires_note ? h("span", { class: "wz-needs-note" }, " (needs a note)") : null])),
              h("dt", {}, "Workflow"), h("dd", {},
                [W.queues.by ? `queues by ${W.queues.by}` : "one queue", W.double_label > 1 ? `${W.double_label} labels per item` : "1 label per item",
                  W.assignment === "round_robin" ? "round-robin" : "open assignment", W.golden ? `golden checks (${Math.round(W.golden.rate * 100)}%)` : null].filter(Boolean).join(" · ")))),
          issuesBox,
          h("div", { class: "wz-create-row" }, createBtn, h("span", { class: "wz-help" }, kbd("Ctrl"), " + ", kbd("Enter"))),
          status),
        h("div", { class: "wz-review-right" }, yamlBox)));

    checkDir();
    createBtn.addEventListener("click", async () => {
      createBtn.disabled = true;
      clear(status, stateBox("loading", "Creating the project…", "Writing caliper.label.yaml and taking a snapshot of the items."));
      try {
        const res = await api.post("/api/setup/create", { dir: state.dir, config: cleanConfig(cfg), draft: state.draft });
        const n = res.snapshot && (res.snapshot.n_items ?? res.snapshot.items ?? res.snapshot.count);
        announce(`Project created${n ? ` with ${n} items` : ""}. Opening the labeller.`);
        resetWizard();
        projectChanged();
        go("label");
      } catch (err) {
        clear(status, errorBox(err));
        createBtn.disabled = false;
      }
    });
    refresh();
  }

  function stepFor(path) {
    if (path.startsWith("source")) return "data";
    if (path.startsWith("card")) return "card";
    if (path.startsWith("labels")) return "labels";
    if (path.startsWith("workflow")) return "workflow";
    return "review";
  }

  const renderers = { data: renderData, card: renderCard, labels: renderLabels, workflow: renderWorkflow, review: renderReview };
  updateNav();
  await renderers[step]();
  updateNav();
  const h1 = body.querySelector("h1");
  focusHeading(h1);
  document.title = `${STEPS[idx(step)][1]} · New project · caliper label`;

  return () => {
    document.removeEventListener("keydown", onKey);
    window.removeEventListener("pagehide", saveNow);
    clearSlot();
    save(state);
  };
}
