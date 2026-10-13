"use client";

import React, { useEffect, useState } from "react";
import { Chip, KeyValue, Panel } from "./Bits";
import type { Bundle } from "@/lib/types";
import { num, stamp } from "@/lib/format";

interface ChangeRequest {
  key: string;
  value: string;
}

/**
 * Admin settings (spec §C). Writes NEVER happen in the browser: the form posts a change request to
 * `/api/admin`, which validates an env token and either forwards it to the worker bridge
 * (WORKER_ADMIN_URL) or refuses. All writes are audit-logged by the worker, which is the only
 * process allowed to touch `channels` / `run_config`.
 */
export function AdminView({ bundle }: { bundle: Bundle }) {
  const [token, setToken] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [req, setReq] = useState<ChangeRequest>({ key: "", value: "" });

  useEffect(() => {
    const saved = window.localStorage.getItem("mishkah.adminToken");
    if (saved) setToken(saved);
  }, []);

  const submit = async () => {
    setPending(true);
    setStatus(null);
    window.localStorage.setItem("mishkah.adminToken", token);
    try {
      const res = await fetch("/api/admin", {
        method: "POST",
        headers: { "content-type": "application/json", "x-admin-token": token },
        body: JSON.stringify({ key: req.key, value: req.value, source: "ui" }),
      });
      const body = await res.json().catch(() => ({}));
      setStatus(`${res.status} — ${body.message ?? body.error ?? "no message"}`);
    } catch (err) {
      setStatus(`request failed: ${String(err)}`);
    } finally {
      setPending(false);
    }
  };

  const settings = (bundle.runConfig.settings ?? {}) as Record<string, unknown>;
  const gates = (settings.gates ?? {}) as Record<string, unknown>;
  const jev = (settings.jevs ?? {}) as Record<string, unknown>;
  const costs = (settings.costs ?? {}) as Record<string, unknown>;

  return (
    <>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Panel title="Runtime settings" subtitle="read from the worker environment — secrets are never exported">
          <KeyValue
            items={[
              ["session (Tehran)", `${String(settings.open_at_local ?? "—")} → ${String(settings.flat_at_local ?? "—")}`],
              ["checkpoints", String(settings.checkpoints_local ?? "—")],
              ["trading weekdays", String(settings.trading_weekdays ?? "—")],
              ["paper only", settings.paper_only ? "true (no order code exists)" : "—"],
            ]}
          />
          <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
            <div>
              <div className="mb-1 text-2xs uppercase tracking-wider text-dim">gate thresholds</div>
              <KeyValue items={Object.entries(gates).map(([k, v]) => [k, String(v)])} />
            </div>
            <div>
              <div className="mb-1 text-2xs uppercase tracking-wider text-dim">costs (bps)</div>
              <KeyValue
                items={Object.entries(costs).map(([k, v]) => {
                  const c = (v ?? {}) as { spread_bps?: number; slippage_bps?: number };
                  const rt =
                    c.spread_bps !== undefined && c.slippage_bps !== undefined
                      ? 2 * c.slippage_bps + c.spread_bps
                      : null;
                  return [
                    k,
                    `${num(c.spread_bps ?? 0, 0)}/${num(c.slippage_bps ?? 0, 0)} (round trip ${rt === null ? "—" : num(rt, 0)})`,
                  ] as [string, string];
                })}
              />
            </div>
          </div>
          <div className="mt-3">
            <div className="mb-1 text-2xs uppercase tracking-wider text-dim">interview model</div>
            <KeyValue
              items={[
                ["model", String(jev.model ?? "—")],
                ["host", String(jev.host ?? "—")],
                ["mock transport", jev.mock ? "yes — answers are NOT Jev's" : "no"],
              ]}
            />
          </div>
        </Panel>

        <Panel
          title="Change request"
          subtitle="queued through /api/admin; the worker performs the write and the audit entry"
          right={<Chip tone="warn">env-token gated</Chip>}
        >
          <label className="block text-2xs text-dim">
            admin token
            <input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="UI_ADMIN_TOKEN"
              className="mt-1 w-full rounded border border-line bg-panel2 px-2 py-1 text-[12px] text-ink"
            />
          </label>
          <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
            <label className="block text-2xs text-dim">
              run_config key
              <input
                value={req.key}
                onChange={(e) => setReq({ ...req, key: e.target.value })}
                placeholder="e.g. channels.hojoom_tracker.active"
                className="mt-1 w-full rounded border border-line bg-panel2 px-2 py-1 text-[12px] text-ink"
              />
            </label>
            <label className="block text-2xs text-dim">
              value (JSON)
              <input
                value={req.value}
                onChange={(e) => setReq({ ...req, value: e.target.value })}
                placeholder='false'
                className="mt-1 w-full rounded border border-line bg-panel2 px-2 py-1 text-[12px] text-ink"
              />
            </label>
          </div>
          <button
            type="button"
            disabled={pending || !token || !req.key}
            onClick={submit}
            className="mt-3 rounded border border-line bg-panel2 px-3 py-1.5 text-2xs text-ink disabled:opacity-40"
          >
            {pending ? "sending…" : "submit change request"}
          </button>
          {status && <p className="mt-2 text-2xs text-amber">{status}</p>}
          <p className="mt-3 text-2xs text-dim">
            If the deployment has no <code className="text-ink">WORKER_ADMIN_URL</code> bridge configured, the route answers
            501 with that explanation rather than pretending the write happened. The worker is the only writer in this system —
            that is what keeps the audit log complete.
          </p>
        </Panel>
      </div>

      <Panel title="run_config table" subtitle="every frozen knob, who set it, when" bodyClass="p-0">
        <table className="w-full text-left text-2xs">
          <thead className="bg-panel text-dim">
            <tr>
              <th className="px-3 py-2 font-medium">key</th>
              <th className="px-3 py-2 font-medium">value</th>
              <th className="px-3 py-2 font-medium">set at</th>
              <th className="px-3 py-2 font-medium">set by</th>
            </tr>
          </thead>
          <tbody>
            {bundle.runConfig.run_config.map((r) => (
              <tr key={r.key} className="border-t border-line/60">
                <td className="px-3 py-1.5 text-ink">{r.key}</td>
                <td className="max-w-[60ch] truncate px-3 py-1.5 text-dim">{JSON.stringify(r.value_json)}</td>
                <td className="px-3 py-1.5 text-dim">{stamp(r.set_at)}</td>
                <td className="px-3 py-1.5 text-dim">{r.set_by}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
    </>
  );
}
