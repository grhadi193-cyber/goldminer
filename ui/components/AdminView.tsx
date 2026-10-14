"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Chip, KeyValue, Panel } from "./Bits";
import type { Bundle } from "@/lib/types";
import { num, stamp } from "@/lib/format";
import {
  ApiError,
  authChangePassword,
  authLogout,
  clearIntegration,
  integrations,
  runConfigSet,
  setIntegration,
  testGroup,
  type IntegrationField,
  type IntegrationGroup,
  type AuthStatus,
  type IntegrationsPayload,
  type TestResult,
} from "@/lib/api";

/**
 * Admin settings (spec §C + operator request). Every write goes to the WORKER, which is the only
 * process allowed to touch the database, and every write is audit-logged. Two rules the UI keeps:
 *
 *  1. a secret is write-only — the panel shows a fingerprint and never the value;
 *  2. the environment is never overwritten — a panel value is an override on top of it, and
 *     clearing the override falls back to the environment instead of disappearing.
 */

function sourceChip(field: IntegrationField) {
  if (field.source === "panel") return <Chip tone="ok" title={field.updated_by ? `set by ${field.updated_by}` : undefined}>panel</Chip>;
  if (field.source === "env") return <Chip tone="info" title="value comes from the process environment">env</Chip>;
  return <Chip tone="default">unset</Chip>;
}

function FieldRow({
  field,
  onSaved,
}: {
  field: IntegrationField;
  onSaved: (message: string) => void;
}) {
  const stored = field.source === "panel";
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await setIntegration(field.name, draft);
      setDraft("");
      onSaved(`${field.name} saved — it takes effect on the next worker call.`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const clear = async () => {
    setBusy(true);
    setError(null);
    try {
      await clearIntegration(field.name);
      onSaved(`${field.name} override cleared — the environment/default value applies again.`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const input = () => {
    const cls =
      "mt-1 w-full rounded border border-line bg-panel2 px-2 py-1.5 text-[12px] text-ink placeholder:text-dim";
    if (field.kind === "select") {
      return (
        <select value={draft} onChange={(e) => setDraft(e.target.value)} className={cls}>
          {["", ...field.options.filter((option) => option !== "")].map((option) => (
            <option key={option || "__empty"} value={option}>
              {option === "" ? "preset / environment (no override)" : option}
            </option>
          ))}
        </select>
      );
    }
    if (field.kind === "bool") {
      return (
        <select value={draft} onChange={(e) => setDraft(e.target.value)} className={cls}>
          <option value="">preset / environment (no override)</option>
          <option value="1">1 — enabled</option>
          <option value="0">0 — disabled</option>
        </select>
      );
    }
    return (
      <input
        type={field.secret ? "password" : field.kind === "number" ? "number" : "text"}
        value={draft}
        autoComplete="off"
        spellCheck={false}
        onChange={(e) => setDraft(e.target.value)}
        placeholder={
          field.secret
            ? stored
              ? `stored: ${field.fingerprint ?? "set"} — paste a new value to rotate`
              : "paste the key (stored encrypted)"
            : field.value !== null && field.value !== undefined && field.value !== ""
              ? `current: ${String(field.value)}`
              : "empty"
        }
        className={cls}
      />
    );
  };

  return (
    <div className="border-t border-line/60 py-3 first:border-t-0">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[12px] font-medium text-ink">{field.label}</span>
        <code className="text-2xs text-dim">{field.name}</code>
        {sourceChip(field)}
        {field.secret && stored && (
          <span className="text-2xs text-dim" title="hash + last four characters of the stored value">
            {field.fingerprint ?? "stored"}
          </span>
        )}
        {!field.secret && field.effective !== null && field.effective !== undefined && field.effective !== "" && (
          <span className="text-2xs text-dim">
            effective: <span className="text-cyan">{String(field.effective)}</span>
          </span>
        )}
        {field.updated_at && <span className="text-2xs text-dim">· {stamp(field.updated_at)}</span>}
      </div>
      <p className="mt-1 max-w-[90ch] text-2xs text-dim">{field.help}</p>
      <div className="mt-2 flex flex-wrap items-start gap-2">
        <div className="min-w-[260px] flex-1">{input()}</div>
        <button
          type="button"
          onClick={save}
          disabled={busy || draft === ""}
          className="rounded border border-line bg-panel2 px-2.5 py-1.5 text-2xs text-ink disabled:opacity-40 hover:border-cyan hover:text-cyan"
        >
          {field.secret && stored ? "rotate" : "save"}
        </button>
        {stored && (
          <button
            type="button"
            onClick={clear}
            disabled={busy}
            className="rounded border border-line bg-panel2 px-2.5 py-1.5 text-2xs text-amber disabled:opacity-40"
          >
            clear override
          </button>
        )}
      </div>
      {error && <p className="mt-1 text-2xs text-amber">{error}</p>}
    </div>
  );
}

export function AdminView({ bundle, session }: { bundle: Bundle; session?: AuthStatus | null }) {
  const [payload, setPayload] = useState<IntegrationsPayload | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [active, setActive] = useState<string>("jev");
  const [note, setNote] = useState<string | null>(null);
  const [test, setTest] = useState<TestResult | null>(null);
  const [testing, setTesting] = useState(false);
  const [password, setPassword] = useState({ current: "", next: "" });
  const [passwordNote, setPasswordNote] = useState<string | null>(null);
  const [rc, setRc] = useState({ key: "", value: "" });
  const [rcNote, setRcNote] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      setPayload(await integrations());
      setLoadError(null);
    } catch (err) {
      setLoadError(err instanceof ApiError ? `${err.status} — ${err.message}` : String(err));
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const group: IntegrationGroup | undefined = useMemo(
    () => payload?.groups.find((g) => g.key === active),
    [payload, active],
  );

  const settings = (bundle.runConfig.settings ?? {}) as Record<string, unknown>;
  const gates = (settings.gates ?? {}) as Record<string, unknown>;
  const jev = (settings.jevs ?? {}) as Record<string, unknown>;
  const costs = (settings.costs ?? {}) as Record<string, unknown>;

  const runTest = async () => {
    if (!group) return;
    setTesting(true);
    setTest(null);
    try {
      setTest(await testGroup(group.test));
    } catch (err) {
      setTest({ ok: false, detail: err instanceof ApiError ? `${err.status} — ${err.message}` : String(err) });
    } finally {
      setTesting(false);
    }
  };

  const changePassword = async () => {
    setPasswordNote(null);
    try {
      await authChangePassword(password.current, password.next);
      setPassword({ current: "", next: "" });
      setPasswordNote("password changed — it applies to the next login.");
    } catch (err) {
      setPasswordNote(err instanceof ApiError ? err.message : String(err));
    }
  };

  const submitRunConfig = async () => {
    setRcNote(null);
    let parsed: unknown = rc.value;
    try {
      parsed = JSON.parse(rc.value);
    } catch {
      parsed = rc.value;
    }
    try {
      await runConfigSet(rc.key, parsed);
      setRcNote(`${rc.key} written through the worker (audit-logged).`);
      setRc({ key: "", value: "" });
    } catch (err) {
      setRcNote(err instanceof ApiError ? err.message : String(err));
    }
  };

  if (loadError) {
    return (
      <Panel title="Settings" subtitle="the worker did not answer">
        <p className="text-2xs text-amber">{loadError}</p>
        <p className="mt-2 text-2xs text-dim">
          Settings live on the worker, so this view requires a live API on the same origin. Log in again if your session
          expired, then reload.
        </p>
        <button
          type="button"
          onClick={() => void reload()}
          className="mt-3 rounded border border-line bg-panel2 px-3 py-1.5 text-2xs text-ink"
        >
          retry
        </button>
      </Panel>
    );
  }

  return (
    <>
      {note && <p className="mb-3 text-2xs text-green">{note}</p>}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[260px_1fr]">
        <Panel title="Integrations" subtitle="what the worker may talk to" bodyClass="p-2">
          <ul className="space-y-1">
            {(payload?.groups ?? []).map((g) => (
              <li key={g.key}>
                <button
                  type="button"
                  onClick={() => {
                    setActive(g.key);
                    setTest(null);
                  }}
                  className={`w-full rounded px-2 py-2 text-left text-2xs transition-colors ${
                    g.key === active ? "bg-panel2 text-cyan" : "text-ink hover:bg-panel2"
                  }`}
                >
                  <span className="block text-[12px]">{g.title}</span>
                  <span className="mt-0.5 block text-dim">
                    {g.fields.filter((f) => f.source === "panel").length}/{g.fields.length} set in panel
                  </span>
                </button>
              </li>
            ))}
          </ul>
          <div className="mt-2 border-t border-line px-2 py-2 text-2xs text-dim">
            worker {payload?.worker.version ?? "—"} · demo mode {payload?.worker.demo ? "on (no network calls)" : "off"}
          </div>
        </Panel>

        <div className="space-y-4">
          {group && (
            <Panel
              title={group.title}
              subtitle={group.subtitle}
              right={
                <>
                  <Chip tone={test?.ok === false ? "bad" : test?.ok ? "ok" : "default"}>
                    {test ? (test.ok ? "test passed" : "test failed") : "not tested"}
                  </Chip>
                  <button
                    type="button"
                    onClick={runTest}
                    disabled={testing}
                    className="rounded border border-line bg-panel2 px-2.5 py-1 text-2xs text-ink disabled:opacity-40 hover:border-cyan hover:text-cyan"
                  >
                    {testing ? "testing…" : "test this group"}
                  </button>
                </>
              }
            >
              {test && (
                <div className="mb-3 rounded border border-line bg-panel2 px-3 py-2 text-2xs">
                  <span className={test.ok ? "text-green" : "text-amber"}>{test.detail ?? (test.ok ? "ok" : "failed")}</span>
                  {test.model && <span className="ml-2 text-dim">model {test.model}</span>}
                  {test.endpoint && <span className="ml-2 text-dim">{test.endpoint}</span>}
                  {test.warning && <p className="mt-1 text-amber">{test.warning}</p>}
                </div>
              )}
              {group.fields.map((field) => (
                <FieldRow key={field.name} field={field} onSaved={(message) => {
                  setNote(message);
                  void reload();
                }} />
              ))}
            </Panel>
          )}

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <Panel title="Session" subtitle="this browser's access to the panel">
              <KeyValue
                items={[
                  ["worker version", payload?.worker.version ?? "—"],
                  ["session length", session ? `${session.session_minutes} min` : "—"],
                  ["paper only", settings.paper_only ? "true — no order code exists" : "—"],
                  ["demo mode", bundle.meta.demo ? "on — synthetic data" : "off — real sources"],
                ]}
              />
              <div className="mt-3 grid grid-cols-1 gap-2 md:grid-cols-2">
                <input
                  type="password"
                  autoComplete="current-password"
                  placeholder="current password"
                  value={password.current}
                  onChange={(e) => setPassword({ ...password, current: e.target.value })}
                  className="rounded border border-line bg-panel2 px-2 py-1.5 text-[12px] text-ink"
                />
                <input
                  type="password"
                  autoComplete="new-password"
                  placeholder="new password (≥10 chars)"
                  value={password.next}
                  onChange={(e) => setPassword({ ...password, next: e.target.value })}
                  className="rounded border border-line bg-panel2 px-2 py-1.5 text-[12px] text-ink"
                />
              </div>
              <div className="mt-2 flex gap-2">
                <button
                  type="button"
                  onClick={changePassword}
                  disabled={password.current.length === 0 || password.next.length < 10}
                  className="rounded border border-line bg-panel2 px-2.5 py-1.5 text-2xs text-ink disabled:opacity-40"
                >
                  change password
                </button>
                <button
                  type="button"
                  onClick={async () => {
                    await authLogout().catch(() => undefined);
                    window.location.reload();
                  }}
                  className="rounded border border-line bg-panel2 px-2.5 py-1.5 text-2xs text-amber"
                >
                  log out
                </button>
              </div>
              {passwordNote && <p className="mt-2 text-2xs text-amber">{passwordNote}</p>}
            </Panel>

            <Panel title="Frozen runtime knobs" subtitle="read from the worker environment — secrets are never exported">
              <KeyValue
                items={[
                  ["session (Tehran)", `${String(settings.open_at_local ?? "—")} → ${String(settings.flat_at_local ?? "—")}`],
                  ["checkpoints", String(settings.checkpoints_local ?? "—")],
                  ["trading weekdays", String(settings.trading_weekdays ?? "—")],
                  ["interview model", `${String(jev.model ?? "—")} @ ${String(jev.host ?? "—")}`],
                  ["mock transport", jev.mock ? "yes — answers are NOT Jev's" : "no"],
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
            </Panel>
          </div>

          <Panel
            title="Freeze a run_config value"
            subtitle="the worker writes it and records who set it — this is why the panel never touches the database"
          >
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              <label className="block text-2xs text-dim">
                key
                <input
                  value={rc.key}
                  onChange={(e) => setRc({ ...rc, key: e.target.value })}
                  placeholder="e.g. channels.hojoom_tracker.active"
                  className="mt-1 w-full rounded border border-line bg-panel2 px-2 py-1.5 text-[12px] text-ink"
                />
              </label>
              <label className="block text-2xs text-dim">
                value (JSON or plain text)
                <input
                  value={rc.value}
                  onChange={(e) => setRc({ ...rc, value: e.target.value })}
                  placeholder="false"
                  className="mt-1 w-full rounded border border-line bg-panel2 px-2 py-1.5 text-[12px] text-ink"
                />
              </label>
            </div>
            <button
              type="button"
              onClick={submitRunConfig}
              disabled={rc.key.length === 0}
              className="mt-3 rounded border border-line bg-panel2 px-3 py-1.5 text-2xs text-ink disabled:opacity-40"
            >
              write through the worker
            </button>
            {rcNote && <p className="mt-2 text-2xs text-amber">{rcNote}</p>}
          </Panel>

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
        </div>
      </div>
    </>
  );
}
