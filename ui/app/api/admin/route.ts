import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

/**
 * Admin change requests (spec §C). This route NEVER writes to Postgres: the worker is the only
 * process allowed to touch `channels` / `run_config`, and every write it makes is audit-logged.
 *
 * - token must equal UI_ADMIN_TOKEN (env, server-side only)
 * - if WORKER_ADMIN_URL is configured, the request is forwarded to the worker bridge and the
 *   worker's answer (including its audit reference) is returned
 * - otherwise the route answers 501 and says so, instead of pretending a change took effect
 */
export async function POST(request: Request) {
  const expected = process.env.UI_ADMIN_TOKEN;
  if (!expected) {
    return NextResponse.json(
      { error: "admin_disabled", message: "UI_ADMIN_TOKEN is not configured on the server; admin writes are disabled." },
      { status: 503 },
    );
  }
  const provided = request.headers.get("x-admin-token") ?? "";
  if (provided !== expected) {
    return NextResponse.json({ error: "unauthorized", message: "token rejected" }, { status: 401 });
  }

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "bad_request", message: "body must be JSON" }, { status: 400 });
  }
  const { key, value } = (body ?? {}) as { key?: string; value?: unknown };
  if (!key || typeof key !== "string") {
    return NextResponse.json({ error: "bad_request", message: "key is required" }, { status: 400 });
  }
  let parsed: unknown = value;
  if (typeof value === "string") {
    try {
      parsed = JSON.parse(value);
    } catch {
      parsed = value;
    }
  }

  const bridge = process.env.WORKER_ADMIN_URL;
  if (!bridge) {
    return NextResponse.json(
      {
        error: "no_bridge",
        message:
          "WORKER_ADMIN_URL is not configured, so this deployment has no path to the worker. Run the change on the worker host " +
          "(`python -m worker config set <key> <json>`) — that is the only writer, and the only place the audit log can stay complete.",
      },
      { status: 501 },
    );
  }

  const res = await fetch(`${bridge.replace(/\/$/, "")}/admin/run_config`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-admin-token": expected },
    body: JSON.stringify({ key, value: parsed, requested_from: "ui" }),
    cache: "no-store",
  });
  const text = await res.text();
  return new NextResponse(text, { status: res.status, headers: { "content-type": "application/json" } });
}
