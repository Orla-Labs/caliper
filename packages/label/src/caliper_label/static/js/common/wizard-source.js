// Data-source forms, credential status, uploads and the folder/file picker (dev 2).

import { h, clear, nextId, stateBox, errorBox, announce, dialog } from "./wizard-dom.js";

export const SOURCE_KINDS = [
  ["upload", "Upload a file", "CSV, TSV, JSONL, Parquet, Excel"],
  ["local", "Local file", "A path on this machine"],
  ["folder", "Folder of images", "One item per image"],
  ["s3", "Amazon S3", "Also R2 / MinIO"],
  ["gcs", "Google Cloud Storage", "gs:// files or globs"],
  ["http", "HTTP(S) URL", "A file on the web"],
  ["postgres", "Postgres", "A table or a query"],
  ["mysql", "MySQL", "A table or a query"],
  ["sqlite", "SQLite", "A .sqlite / .db file"],
];

const FORMATS = [["", "Detect from the extension"], ["csv", "CSV"], ["tsv", "TSV"], ["jsonl", "JSON Lines"], ["json", "JSON"], ["parquet", "Parquet"], ["xlsx", "Excel"]];

export function cleanConfig(obj) {
  if (Array.isArray(obj)) return obj.map(cleanConfig);
  if (obj && typeof obj === "object") {
    const out = {};
    for (const [k, v] of Object.entries(obj)) if (!k.startsWith("_")) out[k] = cleanConfig(v);
    return out;
  }
  return obj;
}

export function suggestFields(columns, idCol) {
  const skip = (n) => n === idCol || /^(id|uuid|_?row_?id)$/i.test(n);
  const rank = { image: 0, long_text: 1, text: 1, badge: 2, datetime: 3, number: 3, link: 4, json: 5 };
  return columns
    .filter((c) => !skip(c.name))
    .slice(0, 8)
    .map((c, i) => ({ c, i }))
    .sort((a, b) => (rank[a.c.display] ?? 9) - (rank[b.c.display] ?? 9) || a.i - b.i)
    .slice(0, 4)
    .sort((a, b) => a.i - b.i)
    .map(({ c }) => ({ column: c.name, type: c.display || "text" }));
}

function token() {
  try { return localStorage.getItem("caliper-label-token"); } catch { return null; }
}

// Raw-body upload with progress (no multipart dependency on the server).
export function uploadFile(file, draft, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const q = new URLSearchParams({ filename: file.name });
    if (draft) q.set("draft", draft);
    xhr.open("PUT", `/api/setup/upload?${q}`);
    const t = token();
    if (t) xhr.setRequestHeader("Authorization", `Bearer ${t}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(Math.round((e.loaded / e.total) * 100)); };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
      if (xhr.status >= 200 && xhr.status < 300) return resolve(data);
      const err = new Error((data && data.detail) || `Upload failed (${xhr.status})`);
      err.hint = data && data.hint;
      reject(err);
    };
    xhr.onerror = () => { const e = new Error("Upload failed: the server did not answer"); e.hint = "check that caliper label is still running"; reject(e); };
    xhr.send(file);
  });
}

export function credentialList(creds) {
  // "local files" / "plugin-defined" are not credentials: nothing to show (#17). Keep the
  // informative ones (an S3 endpoint, env vars, AWS chains) and anything unresolved.
  creds = (creds || []).filter((c) => !(c.kind === "none" && c.resolved && !/^endpoint /.test(c.name)));
  if (!creds.length) return null;
  return h("ul", { class: "wz-creds", "aria-label": "Credentials" }, creds.map((c) =>
    h("li", { class: `wz-cred ${c.resolved ? "wz-cred-ok" : "wz-cred-missing"}` },
      h("span", { class: "wz-cred-icon", "aria-hidden": "true" }, c.resolved ? "✓" : "!"),
      h("span", { class: "wz-cred-text" },
        h("strong", {}, c.name), " ",
        h("span", { class: "wz-muted" }, c.resolved ? "found" : "not found"),
        h("span", { class: "wz-sr" }, c.resolved ? " (resolved)" : " (not resolved)"),
        c.hint ? h("span", { class: "wz-cred-hint" }, c.hint) : null))));
}

// --------------------------------------------------------------------------- forms

function field(label, input, help) {
  const id = input.id || (input.id = nextId("f"));
  return h("div", { class: "wz-field-block" }, h("label", { for: id }, label), input, help ? h("p", { class: "wz-help" }, help) : null);
}

function text(src, key, { onChange, ...attrs } = {}) {
  return h("input", { type: "text", value: src[key] ?? "", spellcheck: "false", autocomplete: "off", ...attrs,
    oninput: (e) => { src[key] = e.target.value.trim() || null; onChange && onChange(); } });
}

export function sourceForm(state, { onChange, onFile, onConnect, api }) {
  const src = state.config.source;
  src.credentials = src.credentials || {};
  const kind = state.kind;
  const t = (key, attrs) => text(src, key, { ...attrs, onChange });
  const credText = (key, attrs) => text(src.credentials, key, { ...attrs, onChange });
  const connect = h("button", { type: "submit", class: "wz-btn wz-btn-primary" }, "Connect & preview");

  if (kind === "upload") {
    const input = h("input", { type: "file", class: "wz-sr", "aria-label": "Data file", tabindex: "-1", accept: ".csv,.tsv,.jsonl,.ndjson,.json,.parquet,.xlsx", id: nextId("file"),
      onchange: (e) => onFile(e.target.files[0]) });
    const zone = h("div", { class: "wz-drop", ondragover: (e) => { e.preventDefault(); zone.classList.add("wz-drop-on"); },
      ondragleave: () => zone.classList.remove("wz-drop-on"),
      ondrop: (e) => { e.preventDefault(); zone.classList.remove("wz-drop-on"); const f = e.dataTransfer.files[0]; if (f) onFile(f); } },
    h("div", { class: "wz-drop-icon", "aria-hidden": "true" }, "⇪"),
    state.upload
      ? h("p", {}, h("strong", {}, state.upload.name), ` · ${(state.upload.size / 1024).toFixed(1)} KB uploaded`,
        state.preview && state.preview.ok
          ? h("span", { class: "wz-drop-ok" }, h("span", { "aria-hidden": "true" }, " ✓ "), `${state.preview.count ?? state.preview.rows.length} rows read`)
          : null)
      : h("p", {}, h("strong", {}, "Drop a file here"), " or"),
    h("button", { type: "button", class: "wz-btn wz-btn-primary", onclick: () => input.click() }, state.upload ? "Choose another file" : "Choose a file"),
    h("p", { class: "wz-help" }, "CSV, TSV, JSONL, JSON, Parquet or Excel. The file is copied into the project folder."),
    input);
    return zone;
  }

  const form = h("form", { class: "wz-panel wz-form", onsubmit: (e) => { e.preventDefault(); onConnect(); } });
  const browse = (key, files) => h("button", { type: "button", class: "wz-btn", onclick: async () => {
    const picked = await folderPicker(api, { start: src[key] || "", files, title: files ? "Choose a data file" : "Choose a folder" });
    if (picked) { src[key] = picked; form.querySelector(`[data-key=${key}]`).value = picked; onChange(); onConnect(); }
  } }, "Browse…");

  const tableOrQuery = () => {
    const mode = src.query ? "query" : "table";
    const name = nextId("tq");
    const box = h("div");
    const draw = (m) => clear(box, m === "table"
      ? field("Table", t("table", { placeholder: "schema.table", "data-key": "table" }))
      : field("SQL query", h("textarea", { class: "wz-textarea wz-mono", rows: "4", placeholder: "select * from tickets where created_at > now() - interval '7 days'",
        oninput: (e) => { src.query = e.target.value.trim() || null; onChange(); } }, src.query || "")));
    const radios = h("div", { class: "wz-radio-inline", role: "radiogroup", "aria-label": "Read a table or a query" },
      ["table", "query"].map((m) => h("label", {}, h("input", { type: "radio", name, value: m, checked: mode === m,
        onchange: () => { if (m === "table") src.query = null; else src.table = null; draw(m); onChange(); } }), m === "table" ? " Table" : " SQL query")));
    draw(mode);
    return [radios, box];
  };

  const advanced = (...children) => h("details", { class: "wz-details" }, h("summary", {}, "More options"), children);

  switch (kind) {
    case "local":
      form.append(
        field("File path", h("div", { class: "wz-path-row" }, t("path", { placeholder: "/data/tickets.csv or data/*.parquet", "data-key": "path", class: "wz-path" }), browse("path", true)),
          "A path on the machine running caliper label. Globs like exports/*.parquet read several files."),
        advanced(field("Format", h("select", { onchange: (e) => { src.format = e.target.value || null; onChange(); } },
          FORMATS.map(([v, l]) => h("option", { value: v, selected: (src.format || "") === v }, l))))));
      break;
    case "folder":
      form.append(
        field("Folder", h("div", { class: "wz-path-row" }, t("path", { placeholder: "/data/photos", "data-key": "path", class: "wz-path" }), browse("path", false)),
          "Each image file becomes one item."),
        field("Sidecar CSV (optional)", t("sidecar", { placeholder: "labels.csv" }), "Joined on the file name, to show extra columns."));
      break;
    case "s3":
      form.append(
        field("S3 URL", t("path", { placeholder: "s3://bucket/exports/*.parquet" }), "A file or a glob. Images in S3 are shown through short-lived signed links."),
        h("div", { class: "wz-grid-2" },
          field("AWS profile (optional)", credText("aws_profile", { placeholder: "default chain" }), "Empty: AWS_PROFILE, env keys, SSO or instance role."),
          field("Region (optional)", credText("region", { placeholder: "eu-west-1" }))),
        advanced(field("Endpoint URL (S3-compatible)", credText("endpoint_url", { placeholder: "https://<account>.r2.cloudflarestorage.com" }), "For Cloudflare R2, MinIO and others.")));
      break;
    case "gcs":
      form.append(field("GCS URL", t("path", { placeholder: "gs://bucket/exports/*.parquet" }), "Uses Google Application Default Credentials (gcloud auth application-default login)."));
      break;
    case "http":
      form.append(
        field("URL", t("path", { placeholder: "https://example.test/data/tickets.csv" })),
        advanced(field("Token env var (optional)", credText("env", { placeholder: "API_TOKEN" }), "Sent as a bearer token. Only the variable name is saved.")));
      break;
    case "postgres":
    case "mysql": {
      form.append(
        ...tableOrQuery(),
        field("Connection env var", credText("env", { placeholder: kind === "postgres" ? "DATABASE_URL" : "MYSQL_URL" }),
          kind === "postgres"
            ? "The variable holding the connection URL. Empty: DATABASE_URL, then the standard PG* variables (PGHOST, PGUSER, PGPASSWORD…)."
            : "The variable holding the connection URL. Empty: the MYSQL_* variables (MYSQL_HOST, MYSQL_USER, MYSQL_PWD…)."));
      break;
    }
    case "sqlite":
      form.append(
        field("Database file", h("div", { class: "wz-path-row" }, t("path", { placeholder: "data/app.sqlite", "data-key": "path", class: "wz-path" }), browse("path", true))),
        ...tableOrQuery());
      break;
    default:
      form.append(field("Plugin", t("plugin")));
  }
  form.append(h("div", { class: "wz-form-actions" }, connect));
  return form;
}

// --------------------------------------------------------------------------- folder / file picker

export function folderPicker(api, { start = "", files = false, title = "Choose a folder" } = {}) {
  return new Promise((resolve) => {
    let picked = null;
    let current = null;
    const pathInput = h("input", { type: "text", class: "wz-path", value: start, spellcheck: "false", "aria-label": "Folder path" });
    const list = h("div", { class: "wz-browse-list" });
    const chooseBtn = h("button", { type: "button", class: "wz-btn wz-btn-primary", hidden: files,
      onclick: () => { picked = current; d.close(); } }, "Use this folder");
    async function go(path) {
      clear(list, stateBox("loading", "Loading…"));
      try {
        const res = await api.get(`/api/setup/browse?${new URLSearchParams({ path: path || "", files: files ? "true" : "false" })}`);
        current = res.path;
        pathInput.value = res.path;
        const items = [];
        if (res.parent) items.push(h("li", {}, h("button", { type: "button", class: "wz-browse-item wz-browse-up", onclick: () => go(res.parent) }, "↑ ", h("span", {}, "Up one folder"))));
        for (const e of res.entries) {
          const icon = e.kind === "file" ? "▤" : e.kind === "project" ? "◧" : "▸";
          items.push(h("li", {}, h("button", {
            type: "button", class: `wz-browse-item wz-browse-${e.kind}`,
            onclick: () => { if (e.kind === "file") { picked = e.path; d.close(); } else go(e.path); },
          }, h("span", { "aria-hidden": "true" }, icon + " "), e.name,
          e.kind === "project" ? h("span", { class: "wz-tag" }, "project") : null,
          e.size !== undefined ? h("span", { class: "wz-muted" }, ` ${(e.size / 1024).toFixed(0)} KB`) : null)));
        }
        clear(list, items.length > (res.parent ? 1 : 0) ? h("ul", { class: "wz-browse" }, items)
          : [h("ul", { class: "wz-browse" }, items), h("p", { class: "wz-muted wz-browse-empty" }, files ? "No data files here." : "No folders here.")]);
        const first = list.querySelector("button");
        if (first && d.open && !d.contains(document.activeElement)) first.focus();
      } catch (err) {
        clear(list, errorBox(err, () => go(pathInput.value)));
      }
    }
    const d = dialog({
      title, wide: true,
      body: [
        h("form", { class: "wz-path-row", onsubmit: (e) => { e.preventDefault(); go(pathInput.value); } },
          pathInput, h("button", { type: "submit", class: "wz-btn" }, "Go")),
        list,
      ],
      actions: [h("button", { type: "button", class: "wz-btn", onclick: () => d.close() }, "Cancel"), chooseBtn],
    });
    d.addEventListener("close", () => { announce(picked ? `Chose ${picked}` : "Picker closed"); resolve(picked); });
    go(start);
  });
}
