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
const BRIDGE = process.env.AGY_BRIDGE_URL || "http://127.0.0.1:20130";
const TOKEN = process.env.AGY_ADMIN_TOKEN || "CHANGE_ME";

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

  const url = `${BRIDGE}${route.path}?t=${encodeURIComponent(TOKEN)}`;
  const init = { method, cache: "no-store" };
  if (method === "POST") {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(await request.json().catch(() => ({})));
  }

  try {
    const res = await fetch(url, init);
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
      { error: `Bridge unreachable: ${error.message}. Is agy-bridge.service running?` },
      { status: 502 },
    );
  }
}

export async function GET(request, { params }) {
  return handle(request, params, "GET");
}

export async function POST(request, { params }) {
  return handle(request, params, "POST");
}
