// Project settings (dev 2): #/settings/<tab>
// Everything the wizard set up stays editable. Saving applies label-option migrations
// (rename keeps history; removing an option with labels retires it after confirmation),
// bumps the config revision and reloads the project context.

import { h, clear, ensureCss, stateBox, errorBox, announce, toast, confirmDialog, nextId, plural } from "../common/wizard-dom.js";
import { cardDesigner, labelsEditor, workflowEditor, columnsTable } from "../common/wizard-editors.js";
import { credentialList, cleanConfig } from "../common/wizard-source.js";

const TABS = [
  ["general", "General"],
  ["data", "Data source"],
  ["card", "Card"],
  ["labels", "Labels"],
  ["workflow", "Workflow"],
  ["file", "Config file"],
];

const strip = (cfg) => JSON.stringify(cleanConfig(cfg));

export async function mount(el, { api, go, args }) {
  ensureCss("wizard");
  ensureCss("settings");
  let tab = TABS.some(([t]) => t === args[0]) ? args[0] : "general";
  let data = null; // GET /api/settings
  let cfg = null; // working copy
  let preview = null;

  const root = h("div", { class: "wz-root se-root" });
  clear(el, root);
  clear(root, stateBox("loading", "Loading project settings…"));

  async function load() {
    try {
      data = await api.get("/api/settings");
    } catch (err) {
      if (err.status === 409) {
        clear(root, stateBox("empty", "No project is open", "Open or create a project first.",
          h("button", { type: "button", class: "wz-btn wz-btn-primary", onclick: () => go("start") }, "Go to the start screen")));
      } else clear(root, errorBox(err, load));
      return false;
    }
    cfg = structuredClone(data.config);
    return true;
  }

  async function loadPreview() {
    try {
      preview = await api.post("/api/setup/preview", { source: cleanConfig(cfg.source), project: true, n: 50 });
    } catch (err) {
      preview = { ok: false, columns: [], rows: [], credentials: [], error: { detail: err.message, hint: err.hint } };
    }
  }

  if (!(await load())) return;
  document.title = `Settings · ${cfg.name} · caliper label`;
  await loadPreview();

  const dirty = () => strip(cfg) !== strip(data.config);
  const saveBar = h("div", { class: "se-savebar", role: "region", "aria-label": "Save changes" });
  const panel = h("div", { class: "se-panel", role: "tabpanel", tabindex: "-1" });
  const tablist = h("div", { class: "se-tabs", role: "tablist", "aria-label": "Settings sections", "aria-orientation": "vertical" });

  function onChange() { renderSaveBar(); }

  function renderSaveBar() {
    const d = dirty();
    saveBar.classList.toggle("se-dirty", d);
    clear(saveBar,
      h("span", { class: "se-save-state" }, d ? "Unsaved changes" : `Saved · revision ${data.revision}`),
      h("button", { type: "button", class: "wz-btn", disabled: !d, onclick: () => { cfg = structuredClone(data.config); render(); announce("Changes discarded"); } }, "Discard"),
      h("button", { type: "button", class: "wz-btn wz-btn-primary", id: "se-save", disabled: !d, onclick: saveFlow }, "Save"));
  }

  async function saveFlow() {
    const btn = saveBar.querySelector("#se-save");
    btn.disabled = true;
    try {
      const plan = await api.post("/api/settings/plan", { config: cleanConfig(cfg) });
      const errs = plan.issues.filter((i) => i.severity === "error");
      if (errs.length) {
        toast(`Can't save: ${errs[0].message}`, "error");
        announce(`Can't save: ${errs[0].path}: ${errs[0].message}`);
        btn.disabled = false;
        return;
      }
      let confirm = false;
      if (plan.plan.needs_confirmation) {
        const retired = plan.plan.changes.filter((c) => c.kind === "retired" && c.labels);
        confirm = await confirmDialog({
          title: "Remove options that have labels?",
          body: [h("p", {}, "These options already have labels. The labels are kept and flagged; the options leave the labelling screen and are marked ", h("em", {}, "retired"), "."),
            h("ul", {}, retired.map((c) => h("li", {}, h("strong", {}, c.value), ` · ${plural(c.labels, "label")}`)))],
          ok: "Retire and save", danger: true,
        });
        if (!confirm) { btn.disabled = false; return; }
      }
      const res = await api.put("/api/settings", { config: cleanConfig(cfg), revision: data.revision, confirm });
      data = res;
      cfg = structuredClone(res.config);
      const renamed = (res.changes || []).filter((c) => c.kind === "renamed").length;
      toast(`Saved · revision ${res.revision}${renamed ? ` · ${renamed} renamed (history kept)` : ""}`);
      announce(`Saved. Revision ${res.revision}.`);
      render();
    } catch (err) {
      if (err.status === 409 && /changed since/.test(err.message)) {
        toast("The project changed elsewhere. Reloading…", "error");
        await load();
        render();
      } else {
        toast(err.hint ? `${err.message}. ${err.hint}` : err.message, "error");
      }
      btn.disabled = false;
    }
  }

  function renderTabs() {
    clear(tablist, TABS.map(([t, label]) => h("button", {
      type: "button", role: "tab", id: `se-tab-${t}`, "aria-selected": String(t === tab), tabindex: t === tab ? "0" : "-1",
      "aria-controls": "se-panel", class: "se-tab",
      onclick: () => { tab = t; history.replaceState(null, "", `#/settings/${t}`); render(); panel.focus(); },
      onkeydown: (e) => {
        const i = TABS.findIndex(([x]) => x === t);
        const d = e.key === "ArrowDown" || e.key === "ArrowRight" ? 1 : e.key === "ArrowUp" || e.key === "ArrowLeft" ? -1 : 0;
        if (!d) return;
        e.preventDefault();
        const n = TABS[(i + d + TABS.length) % TABS.length][0];
        tab = n;
        history.replaceState(null, "", `#/settings/${n}`);
        render();
        tablist.querySelector(`#se-tab-${n}`).focus();
      },
    }, label, t === "labels" && cfg.labels.options.some((o) => o.retired) ? h("span", { class: "wz-tag" }, "retired") : null)));
  }

  function general() {
    const n = nextId("n");
    const d = nextId("d");
    return [
      h("h2", {}, "General"),
      h("div", { class: "wz-panel" },
        h("div", { class: "wz-field-block" }, h("label", { for: n }, "Project name"),
          h("input", { id: n, type: "text", value: cfg.name, oninput: (e) => { cfg.name = e.target.value; onChange(); } })),
        h("div", { class: "wz-field-block" }, h("label", { for: d }, "Description ", h("span", { class: "wz-muted" }, "(optional)")),
          h("textarea", { id: d, class: "wz-textarea", rows: "3", oninput: (e) => { cfg.description = e.target.value || null; onChange(); } }, cfg.description || ""))),
      h("div", { class: "wz-panel" },
        h("dl", { class: "wz-summary" },
          h("dt", {}, "Project folder"), h("dd", {}, h("code", {}, data.project_dir)),
          h("dt", {}, "Config file"), h("dd", {}, h("code", {}, data.path)),
          h("dt", {}, "Revision"), h("dd", {}, String(data.revision), h("span", { class: "wz-muted" }, " · every save adds one; labels record the revision they were made under")),
          h("dt", {}, "Labels so far"), h("dd", {}, plural(Object.values(data.label_counts || {}).reduce((a, b) => a + b, 0), "label")))),
    ];
  }

  function dataTab() {
    const s = cfg.source;
    s.credentials = s.credentials || {};
    const result = h("div");
    const showPreview = () => {
      if (!preview) return clear(result);
      const parts = [];
      if (preview.credentials?.length) parts.push(credentialList(preview.credentials));
      if (!preview.ok) parts.push(errorBox({ message: preview.error?.detail || "Could not read the source", hint: preview.error?.hint }, test));
      else parts.push(h("p", { class: "wz-preview-summary" }, h("span", { class: "wz-ok-dot", "aria-hidden": "true" }), h("strong", {}, "Connected. "),
        `${plural(preview.count ?? preview.rows.length, "row")} · ${plural(preview.columns.length, "column")}`),
      columnsTable({ columns: preview.columns, rows: preview.rows, maxRows: 10, caption: "Source preview" }));
      clear(result, parts);
    };
    async function test() {
      clear(result, stateBox("loading", "Testing the connection…"));
      await loadPreview();
      showPreview();
    }
    const inp = (obj, key, label, help, placeholder) => {
      const id = nextId("s");
      return h("div", { class: "wz-field-block" }, h("label", { for: id }, label),
        h("input", { id, type: "text", class: "wz-path", value: obj[key] ?? "", placeholder: placeholder || "", spellcheck: "false",
          oninput: (e) => { obj[key] = e.target.value.trim() || null; onChange(); } }), help ? h("p", { class: "wz-help" }, help) : null);
    };
    const db = ["postgres", "mysql", "sqlite"].includes(s.kind);
    const cloud = ["s3", "gcs", "http"].includes(s.kind);
    const box = [
      h("h2", {}, "Data source"),
      h("p", { class: "wz-help" }, "Labels stay attached to item ids. After changing the source, use Refresh from source to see new, removed and changed items."),
      h("div", { class: "wz-panel" },
        h("p", {}, "Kind: ", h("strong", {}, s.kind)),
        s.kind !== "postgres" && s.kind !== "mysql" ? inp(s, "path", s.kind === "folder" ? "Folder" : s.kind === "sqlite" ? "Database file" : cloud ? "URL" : "File path", "Relative paths are relative to the project folder.") : null,
        db ? inp(s, "table", "Table") : null,
        db ? (() => {
          const id = nextId("q");
          return h("div", { class: "wz-field-block" }, h("label", { for: id }, "SQL query ", h("span", { class: "wz-muted" }, "(instead of a table)")),
            h("textarea", { id, class: "wz-textarea wz-mono", rows: "3", oninput: (e) => { s.query = e.target.value.trim() || null; onChange(); } }, s.query || ""));
        })() : null,
        db || s.kind === "http" ? inp(s.credentials, "env", "Credential env var", "Only the variable name is stored, never its value.", s.kind === "mysql" ? "MYSQL_URL" : "DATABASE_URL") : null,
        s.kind === "s3" ? [inp(s.credentials, "aws_profile", "AWS profile"), inp(s.credentials, "region", "Region"), inp(s.credentials, "endpoint_url", "Endpoint URL (S3-compatible)")] : null,
        (() => {
          const id = nextId("id");
          const cols = (preview?.columns || []).map((c) => c.name);
          return h("div", { class: "wz-field-block" }, h("label", { for: id }, "Item id column"),
            h("select", { id, onchange: (e) => { s.id_column = e.target.value || null; onChange(); } },
              h("option", { value: "" }, "row number"),
              [...new Set([...(s.id_column ? [s.id_column] : []), ...cols])].map((c) => h("option", { value: c, selected: s.id_column === c }, c))),
            h("p", { class: "wz-help" }, "Changing it re-identifies every item: existing labels only match items whose id stays the same."));
        })(),
        h("div", { class: "wz-form-actions" }, h("button", { type: "button", class: "wz-btn", onclick: test }, "Test connection & preview"))),
      result,
    ];
    showPreview();
    return box;
  }

  function render() {
    renderTabs();
    renderSaveBar();
    panel.id = "se-panel";
    panel.setAttribute("aria-labelledby", `se-tab-${tab}`);
    const cols = preview?.columns || [];
    const rows = preview?.rows || [];
    if (tab === "general") clear(panel, general());
    else if (tab === "data") clear(panel, dataTab());
    else if (tab === "card") {
      const box = h("div");
      clear(panel, h("h2", {}, "Card"), preview && !preview.ok ? h("p", { class: "wz-warn" }, "The source could not be read, so the preview is empty. Fields can still be edited.") : null, box);
      cardDesigner(box, { card: cfg.card, columns: cols, rows, onChange });
    } else if (tab === "labels") {
      const box = h("div");
      clear(panel, h("h2", {}, "Labels"),
        h("p", { class: "wz-help" }, "Renaming an option keeps its history (labels store the option's value). Removing an option that has labels retires it: labels are kept and flagged."),
        box);
      labelsEditor(box, { labels: cfg.labels, api, counts: data.label_counts, settings: true, onChange });
    } else if (tab === "workflow") {
      const box = h("div");
      clear(panel, h("h2", {}, "Workflow"), box);
      workflowEditor(box, { workflow: cfg.workflow, labels: cfg.labels, columns: cols, onChange });
    } else {
      clear(panel,
        h("h2", {}, "Config file"),
        h("p", { class: "wz-help" }, "This is the file on disk. Edit it by hand if you like: comments and order are kept when you save here. Reload this page after a hand edit."),
        h("div", { class: "wz-panel" },
          h("div", { class: "wz-row-between" },
            h("code", { class: "se-path" }, data.path),
            h("div", {},
              h("button", { type: "button", class: "wz-btn wz-btn-small", onclick: async () => { try { await navigator.clipboard.writeText(data.path); announce("Path copied"); } catch { announce("Copy failed"); } } }, "Copy path"),
              " ",
              h("button", { type: "button", class: "wz-btn wz-btn-small", onclick: async () => { if (await load()) { await loadPreview(); render(); announce("Reloaded from disk"); } } }, "Reload from disk"))),
          h("pre", { class: "wz-yaml", tabindex: "0", "aria-label": "caliper.label.yaml" }, data.yaml)),
        dirty() ? h("p", { class: "wz-warn" }, "You have unsaved changes; the file above does not include them yet.") : null);
    }
  }

  clear(root,
    h("header", { class: "wz-top" },
      h("a", { href: "#/start", class: "wz-brand" }, h("span", { class: "wz-logo", "aria-hidden": "true" }, "◧"), " caliper label"),
      h("span", { class: "wz-top-title" }, "Settings · ", cfg.name),
      h("a", { href: "#/label", class: "wz-btn wz-btn-small" }, "Back to labelling")),
    h("div", { class: "se-layout" }, h("nav", { class: "se-nav", "aria-label": "Settings" }, tablist), h("main", { class: "se-main" }, panel)),
    saveBar);
  render();

  const beforeUnload = (e) => { if (dirty()) { e.preventDefault(); e.returnValue = ""; } };
  window.addEventListener("beforeunload", beforeUnload);
  const onKey = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") { e.preventDefault(); if (dirty()) saveFlow(); }
  };
  document.addEventListener("keydown", onKey);
  return () => {
    window.removeEventListener("beforeunload", beforeUnload);
    document.removeEventListener("keydown", onKey);
  };
}
