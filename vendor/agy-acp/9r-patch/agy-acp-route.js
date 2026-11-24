import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

/**
 * Proxy for the local agy ACP bridge admin API.
 *
 * The bridge holds the Google OAuth login flow for Antigravity CLI profiles.
 * Its admin token stays server-side here so the dashboard never sees it and the
 * browser only ever talks to 9router's own origin.
 *
 *   GET  /api/agy-acp/profiles
 *   GET  /api/agy-acp/logins
 *   POST /api/agy-acp/login/start     {profile}
 *   POST /api/agy-acp/login/complete  {loginId, code}
 *   POST /api/agy-acp/test            {profile, model}
 *   POST /api/agy-acp/primary         {profile}
 */
const BRIDGE = process.env.AGY_BRIDGE_URL || "http://agy-acp:20130";
// Fail fast at REQUEST time: a predictable fallback token turns a
// misconfiguration into an authentication bypass on the bridge's
// account-management API. The compose file enforces the same with
// ${AGY_ADMIN_TOKEN:?}. Validated lazily because Next.js collects page data
// (and imports this module) at build time, where the env is intentionally
// absent.
function adminToken() {
  const token = process.env.AGY_ADMIN_TOKEN;
  if (!token) {
    throw new Error("AGY_ADMIN_TOKEN is required — refusing to proxy the bridge admin API without it");
  }
  return token;
}
// Keep the fetch well under the dashboard's patience: the accounts card polls
// every 15s, so a hung bridge must not hold Next.js requests open.
const BRIDGE_TIMEOUT_MS = 10_000;

const ROUTES = {
  profiles: { method: "GET", path: "/admin/profiles" },
  logins: { method: "GET", path: "/admin/logins" },
  health: { method: "GET", path: "/health" },
  "login/start": { method: "POST", path: "/admin/login/start" },
  "login/complete": { method: "POST", path: "/admin/login/complete" },
  test: { method: "POST", path: "/admin/test" },
  primary: { method: "POST", path: "/admin/primary" },
};

async function handle(request, params, method) {
  // Auth is handled by dashboardGuard for every /api/* route, exactly like
  // /api/providers — an extra session check here would 401 a dashboard that
  // runs with require-login off.
  const { path } = await params;
  const key = Array.isArray(path) ? path.join("/") : String(path || "");
  const route = ROUTES[key];
  if (!route || route.method !== method) {
    return NextResponse.json({ error: "Unknown bridge route" }, { status: 404 });
  }

  // Token travels in a header, never a query string: query parameters end up
  // in proxy and access logs; headers do not.
  let body;
  if (method === "POST") {
    const raw = await request.text();
    try {
      body = JSON.parse(raw);
    } catch {
      return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
    }
  }
  const init = {
    method,
    cache: "no-store",
    headers: { "X-Admin-Token": adminToken() },
    signal: AbortSignal.timeout(BRIDGE_TIMEOUT_MS),
  };
  if (method === "POST") {
    init.headers = { ...init.headers, "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }

  try {
    const res = await fetch(`${BRIDGE}${route.path}`, init);
    const text = await res.text();
    let data;
    try {
      data = JSON.parse(text);
    } catch {
      data = { error: text.slice(0, 300) };
    }
    return NextResponse.json(data, { status: res.ok ? 200 : res.status });
  } catch (error) {
    return NextResponse.json(
      { error: error.name === "TimeoutError"
          ? "Bridge timed out after 10s"
          : `Bridge unreachable: ${error.message}. Is agy-bridge.service running?` },
      { status: error.name === "TimeoutError" ? 504 : 502 },
    );
  }
}

export async function GET(request, { params }) {
  return handle(request, params, "GET");
}

export async function POST(request, { params }) {
  return handle(request, params, "POST");
}
