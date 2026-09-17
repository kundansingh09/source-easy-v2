// Thin client over the FastAPI backend. All filter state lives in App.jsx;
// this module only knows how to turn it into a query string.

const BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

function qs(params) {
  const sp = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === "") continue;
    if (Array.isArray(value)) {
      // FastAPI reads repeated keys as a list: ?country=Japan&country=Taiwan
      value.forEach((v) => sp.append(key, v));
    } else {
      sp.append(key, value);
    }
  }
  return sp.toString();
}

async function get(path, params) {
  const url = `${BASE}${path}${params ? `?${qs(params)}` : ""}`;
  let res;
  try {
    res = await fetch(url);
  } catch {
    // Distinguish "server unreachable" from "server said no" - on a cold
    // Render instance the first request can fail outright while the
    // service wakes, and that needs a different message than a 500.
    throw new Error("Can't reach the API. If it's on a free/idle instance, it may still be waking up.");
  }
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return res.json();
}

export const api = {
  health: () => get("/api/health"),
  taxonomy: () => get("/api/taxonomy"),
  facets: (f) => get("/api/facets", {
    expo: f.expo, country: f.countries, l1: f.l1, l2: f.l2,
  }),
  search: (f) => get("/api/search", {
    q: f.query, expo: f.expo, country: f.countries, l1: f.l1, l2: f.l2,
    limit: f.limit, offset: f.offset,
    rerank: f.rerank ? "true" : "false",
    alpha: f.alpha, fusion: f.fusion,
  }),
};
