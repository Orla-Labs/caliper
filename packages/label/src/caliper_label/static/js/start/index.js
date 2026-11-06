// Start screen (dev 2): #/start  ->  New project / Open project / Try the demo.

import { h, clear, ensureCss, stateBox, errorBox, relTime, announce, kbd, hasAppBar, projectChanged } from "../common/wizard-dom.js";
import { folderPicker } from "../common/wizard-source.js";

export async function mount(el, { api, go }) {
  ensureCss("wizard");
  ensureCss("start");
  document.title = "caliper label";

  const recentBox = h("div", { class: "st-recent", "aria-busy": "true" }, stateBox("loading", "Loading recent projects…"));
  const status = h("div", { class: "st-status", "aria-live": "polite" });
  const current = h("div", { class: "st-current-slot" });
  const pathInput = h("input", { type: "text", id: "st-path", class: "wz-path", spellcheck: "false", placeholder: "/path/to/project", autocomplete: "off" });

  async function openPath(path) {
    if (!path) { pathInput.focus(); return; }
    clear(status, stateBox("loading", "Opening the project…"));
    try {
      await api.post("/api/projects/open", { path });
      announce("Project opened");
      projectChanged();
      go("label");
    } catch (err) {
      clear(status, errorBox(err));
    }
  }

  async function demo(btn) {
    btn.disabled = true;
    clear(status, stateBox("loading", "Preparing the demo…", "80 fictional catalog listings."));
    try {
      await api.post("/api/projects/demo", {});
      projectChanged();
      go("label");
    } catch (err) {
      clear(status, errorBox(err));
      btn.disabled = false;
    }
  }

  const newBtn = h("button", { type: "button", class: "st-action st-new", id: "st-new", onclick: () => { location.hash = "#/wizard/data?new=1"; } },
    h("span", { class: "st-icon", "aria-hidden": "true" }, "+"),
    h("span", { class: "st-title" }, "New project"),
    h("span", { class: "st-desc" }, "Connect a CSV, a bucket or a database, design the card and pick your labels. About two minutes."),
    h("span", { class: "st-key", "aria-hidden": "true" }, kbd("N")));
  const openBtn = h("button", { type: "button", class: "st-action st-open", id: "st-open", onclick: () => { document.getElementById("st-open-panel").scrollIntoView({ behavior: "smooth", block: "nearest" }); pathInput.focus(); } },
    h("span", { class: "st-icon", "aria-hidden": "true" }, "↗"),
    h("span", { class: "st-title" }, "Open project"),
    h("span", { class: "st-desc" }, "Pick up a recent project, or open a folder that holds caliper.label.yaml."),
    h("span", { class: "st-key", "aria-hidden": "true" }, kbd("O")));
  const demoBtn = h("button", { type: "button", class: "st-action st-demo", id: "st-demo" },
    h("span", { class: "st-icon", "aria-hidden": "true" }, "▶"),
    h("span", { class: "st-title" }, "Try the demo"),
    h("span", { class: "st-desc" }, "Review the tax codes a model gave 80 fictional product listings. Nothing to set up."),
    h("span", { class: "st-key", "aria-hidden": "true" }, kbd("D")));
  demoBtn.addEventListener("click", () => demo(demoBtn));

  clear(el, h("div", { class: "wz-root st-root" },
    h("header", { class: "st-hero" },
      hasAppBar() ? null : h("div", { class: "wz-brand st-brand" }, h("span", { class: "wz-logo", "aria-hidden": "true" }, "◧"), " caliper label"),
      h("h1", {}, "Label your data, from wherever it lives."),
      h("p", { class: "wz-lead" }, "Set up in the browser, keep it as a readable caliper.label.yaml, label from the keyboard. Your data stays on this machine.")),
    h("main", { class: "st-main" },
      current,
      h("div", { class: "st-actions" }, newBtn, openBtn, demoBtn),
      status,
      h("section", { class: "wz-panel st-open-panel", id: "st-open-panel", "aria-labelledby": "st-open-h" },
        h("h2", { id: "st-open-h" }, "Open a project"),
        h("form", { class: "wz-path-row", onsubmit: (e) => { e.preventDefault(); openPath(pathInput.value.trim()); } },
          h("label", { for: "st-path", class: "wz-sr" }, "Project folder"),
          pathInput,
          h("button", { type: "button", class: "wz-btn", onclick: async () => {
            const p = await folderPicker(api, { start: pathInput.value, files: false, title: "Choose a project folder" });
            if (p) { pathInput.value = p; openPath(p); }
          } }, "Browse…"),
          h("button", { type: "submit", class: "wz-btn wz-btn-primary" }, "Open")),
        h("h3", { class: "st-recent-h" }, "Recent"),
        recentBox))));

  // A project is already open: say so, and offer the way back to it.
  api.get("/api/status").then((st) => {
    if (!st || !st.project || st.mode !== "project") return;
    clear(current, h("section", { class: "st-current", "aria-label": "Open project" },
      h("div", { class: "st-current-text" },
        h("span", { class: "wz-muted" }, "Open now: "), h("span", { class: "st-current-name" }, st.project.name),
        h("div", { class: "wz-muted" }, "Starting or opening another project keeps this one as it is.")),
      h("a", { href: "#/label", class: "wz-btn", id: "st-continue" }, "Continue labelling →")));
  }).catch(() => { /* no status: nothing to continue */ });

  async function loadRecent() {
    try {
      const res = await api.get("/api/projects");
      demoBtn.disabled = !res.demo_available;
      if (!res.demo_available) demoBtn.querySelector(".st-desc").textContent = "The demo ships with a Caliper source checkout; it is not in this install.";
      recentBox.removeAttribute("aria-busy");
      if (!res.recent.length) {
        clear(recentBox, stateBox("empty", "No recent projects yet", "Projects you create or open show up here."));
        return;
      }
      clear(recentBox, h("ul", { class: "st-list" }, res.recent.map((r) =>
        h("li", { class: `st-item${r.exists ? "" : " st-missing"}` },
          h("button", { type: "button", class: "st-item-open", disabled: !r.exists, onclick: () => openPath(r.path),
            title: r.exists ? `Open ${r.name}` : `${r.name}: folder missing` },
          h("span", { class: "st-item-name" }, r.name),
          h("span", { class: "st-item-path" }, r.path)),
          h("span", { class: "st-item-when" }, r.exists ? relTime(r.opened_at) : "missing"),
          h("button", { type: "button", class: "wz-icon wz-icon-x", "aria-label": `Remove ${r.name} from the recent list`,
            onclick: async () => { await api.post("/api/projects/forget", { path: r.path }); announce(`${r.name} removed from the list`); loadRecent(); } }, "×")))));
    } catch (err) {
      recentBox.removeAttribute("aria-busy");
      clear(recentBox, errorBox(err, loadRecent));
    }
  }
  loadRecent();

  const onKey = (e) => {
    if (e.target.closest("input, textarea, select, dialog") || e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key.toLowerCase();
    if (k === "n") { e.preventDefault(); newBtn.click(); }
    else if (k === "o") { e.preventDefault(); openBtn.click(); }
    else if (k === "d" && !demoBtn.disabled) { e.preventDefault(); demoBtn.click(); }
  };
  document.addEventListener("keydown", onKey);
  newBtn.focus({ preventScroll: true });
  return () => document.removeEventListener("keydown", onKey);
}
