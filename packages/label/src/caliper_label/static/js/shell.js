// caliper label: app shell (CONTRACT, see packages/label/CONTRACT.md).
//
// Hash router: "#/<route>/<arg>/<arg>?k=v" dynamically imports
// /static/js/<route>/index.js and calls its `mount(el, ctx)`. A module may
// return an `unmount()` function; it is called before the next route mounts.
// Adding a screen = adding a folder. Nobody needs to edit this file.

import { api } from "./common/api.js";

const ROUTES = new Set(["start", "wizard", "settings", "label", "dashboard", "adjudicate", "export"]);

let current = null;

function parse() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, query = ""] = raw.split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  return { route: parts[0] || "", args: parts.slice(1), query: new URLSearchParams(query) };
}

export function go(route, ...args) {
  location.hash = "#/" + [route, ...args].map(encodeURIComponent).join("/");
}

async function render() {
  const el = document.getElementById("view");
  let { route, args, query } = parse();
  if (!ROUTES.has(route)) {
    const status = await api.get("/api/status").catch(() => null);
    route = status && status.project ? "label" : "start";
    history.replaceState(null, "", "#/" + route);
  }
  if (current) {
    try { current(); } catch (e) { console.error(e); }
    current = null;
  }
  el.replaceChildren();
  el.setAttribute("aria-busy", "true");
  document.body.dataset.route = route;
  try {
    const mod = await import(`/static/js/${route}/index.js`);
    const out = await mod.mount(el, { api, go, args, query });
    current = typeof out === "function" ? out : null;
  } catch (err) {
    console.error(err);
    el.innerHTML = "";
    const box = document.createElement("div");
    box.className = "state state-error";
    box.textContent = `Could not open this screen (${route}). ${err.message || err}`;
    el.append(box);
  } finally {
    el.removeAttribute("aria-busy");
  }
}

window.addEventListener("hashchange", render);
render();
