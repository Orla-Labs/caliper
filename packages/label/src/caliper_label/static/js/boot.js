// caliper label: page boot (dev 3). Runs before the shell.
//
// 1. Team mode: a `?token=...` link stores the token (same localStorage key as
//    common/api.js) and drops it from the address bar, so it is not left in
//    history, bookmarks or screen shares. The server also sets an HttpOnly cookie
//    on the first authenticated request, so <img> tags work.
// 2. If the server needs a token and we have none (or a wrong one), show the
//    token gate instead of the app.
// 3. App bar: project name, nav (only when a project is open), active link.
// 4. Load the shell router.

import { api } from "./common/api.js";
import { el } from "./common/dom.js";

const TOKEN_KEY = "caliper-label-token";

function takeTokenFromUrl() {
  const url = new URL(location.href);
  const t = url.searchParams.get("token");
  if (!t) return;
  try { localStorage.setItem(TOKEN_KEY, t); } catch { /* private mode: the cookie below still works */ }
  // One request with ?token= lets the server set its HttpOnly cookie (images, downloads).
  const cookie = fetch(`/api/status?token=${encodeURIComponent(t)}`, { credentials: "same-origin" }).catch(() => null);
  url.searchParams.delete("token");
  history.replaceState(null, "", url.pathname + (url.search ? url.search : "") + url.hash);
  return cookie;
}

function gate(status, wrong) {
  const view = document.getElementById("view");
  const input = el("input", { id: "token-input", type: "password", autocomplete: "off", required: true, "aria-describedby": "token-help" });
  const form = el("form", { class: "state token-gate" },
    el("div", { class: "state-icon", "aria-hidden": "true", text: "🔒" }),
    el("h2", { text: "Access token needed" }),
    el("p", { id: "token-help", text: wrong
      ? "That token was not accepted. Paste the token from the link the person running the server shared."
      : "This labelling server is in team mode. Open the link you were given (it ends in ?token=…), or paste the token here." }),
    el("label", { for: "token-input", class: "visually-hidden", text: "Access token" }),
    input,
    el("div", { class: "state-actions", style: "margin-top: 12px" }, el("button", { type: "submit", class: "btn btn-primary", text: "Continue" })),
  );
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    try { localStorage.setItem(TOKEN_KEY, input.value.trim()); } catch { /* ignore */ }
    location.reload();
  });
  view.replaceChildren(form);
  input.focus();
}

function syncNav(status) {
  const nav = document.getElementById("appnav");
  const name = document.getElementById("project-name");
  const project = status && status.project;
  nav.hidden = !project;
  name.textContent = project ? project.name : "";
  for (const a of nav.querySelectorAll("[data-project-only]")) a.hidden = !(status && status.mode === "project");
  if (project) document.title = `${project.name} · caliper label`;
}

function markActive() {
  const route = (location.hash.replace(/^#\/?/, "").split(/[/?]/)[0]) || "";
  for (const a of document.querySelectorAll("#appnav a")) {
    if (a.dataset.route === route) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
}

export async function refreshAppBar() {
  const status = await api.get("/api/status").catch(() => null);
  syncNav(status);
  return status;
}

async function boot() {
  await takeTokenFromUrl();
  let status = null;
  try {
    status = await api.get("/api/status");
  } catch (err) {
    console.error(err);
  }
  if (status && status.auth === "token" && !status.authed) {
    let had = null;
    try { had = localStorage.getItem(TOKEN_KEY); } catch { /* ignore */ }
    gate(status, !!had);
    return;
  }
  syncNav(status);
  window.addEventListener("hashchange", markActive);
  window.addEventListener("caliper:project-changed", () => { refreshAppBar(); });
  markActive();
  await import("./shell.js");
  markActive();
}

boot();
