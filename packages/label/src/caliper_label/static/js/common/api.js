// Fetch wrapper (CONTRACT). Adds the team-mode token, parses JSON, and turns
// error responses into Error objects carrying the server's `detail`/`hint`.
//
// Server error shape (all /api routes): {"detail": str, "hint": str | null}
// `hint` is a human fix, e.g. "set AWS_PROFILE or run `aws sso login`".

const TOKEN_KEY = "caliper-label-token";

function token() {
  try {
    const fromUrl = new URLSearchParams(location.search).get("token");
    if (fromUrl) localStorage.setItem(TOKEN_KEY, fromUrl);
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

async function request(method, url, body) {
  const headers = { Accept: "application/json" };
  const t = token();
  if (t) headers.Authorization = `Bearer ${t}`;
  const init = { method, headers };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const res = await fetch(url, init);
  const isJson = (res.headers.get("content-type") || "").includes("application/json");
  const data = isJson ? await res.json() : await res.text();
  if (!res.ok) {
    const err = new Error((data && data.detail) || res.statusText);
    err.status = res.status;
    err.hint = data && data.hint;
    throw err;
  }
  return data;
}

export const api = {
  get: (url) => request("GET", url),
  post: (url, body) => request("POST", url, body),
  put: (url, body) => request("PUT", url, body),
  del: (url) => request("DELETE", url),
};
