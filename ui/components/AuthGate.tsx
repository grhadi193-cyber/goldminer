"use client";

import React, { useState } from "react";
import { Panel, Chip } from "./Bits";
import { ApiError, authLogin, authSetup, type AuthStatus } from "@/lib/api";

/**
 * The panel door (spec §C, operator request): one password, set by the operator on first visit,
 * hashed with scrypt and stored by the worker — never in the browser, never in plaintext.
 *
 * The UI cannot bypass this: every data member and every admin route is served by the worker,
 * which refuses without a session cookie. This component only decides what to *show*.
 */
export function AuthGate({
  status,
  onUnlocked,
}: {
  status: AuthStatus;
  onUnlocked: (status: AuthStatus) => void;
}) {
  const setupMode = !status.password_set;
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    if (setupMode && password !== confirm) {
      setError("The two passwords do not match.");
      return;
    }
    if (password.length < 10) {
      setError("Use at least 10 characters — this password guards the whole panel and every API key in it.");
      return;
    }
    setBusy(true);
    try {
      const next = setupMode ? await authSetup(password) : await authLogin(password);
      if (next) onUnlocked({ password_set: true, authenticated: true, session_minutes: status.session_minutes });
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.status === 429 ? "Too many attempts. Wait a few minutes and try again." : err.message);
      } else {
        setError(String(err));
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center p-6">
      <div className="mb-4">
        <h1 className="text-base font-semibold tracking-wide text-ink">Mishkah · Mission Control</h1>
        <p className="mt-1 text-2xs text-dim">
          Intraday research + paper-trading monitor for Iranian physical gold.
        </p>
      </div>

      <Panel
        title={setupMode ? "Set the panel password" : "Sign in"}
        subtitle={
          setupMode
            ? "First run: choose the password that protects this panel."
            : "The panel is locked. Enter the password you set for this deployment."
        }
        right={setupMode ? <Chip tone="warn">first run</Chip> : <Chip tone="info">locked</Chip>}
      >
        <form onSubmit={submit} className="space-y-3">
          <label className="block text-2xs text-dim">
            password
            <input
              type="password"
              autoFocus
              autoComplete={setupMode ? "new-password" : "current-password"}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1 w-full rounded border border-line bg-panel2 px-2 py-1.5 text-[13px] text-ink"
              placeholder={setupMode ? "at least 10 characters" : "panel password"}
            />
          </label>
          {setupMode && (
            <label className="block text-2xs text-dim">
              repeat password
              <input
                type="password"
                autoComplete="new-password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                className="mt-1 w-full rounded border border-line bg-panel2 px-2 py-1.5 text-[13px] text-ink"
              />
            </label>
          )}
          <button
            type="submit"
            disabled={busy || password.length === 0}
            className="w-full rounded border border-line bg-panel2 px-3 py-2 text-[12px] text-ink transition-colors hover:border-cyan hover:text-cyan disabled:opacity-40"
          >
            {busy ? "working…" : setupMode ? "set password and enter" : "enter"}
          </button>
          {error && <p className="text-2xs text-amber">{error}</p>}
        </form>

        <div className="mt-4 space-y-1 border-t border-line pt-3 text-2xs text-dim">
          <p>
            The password is stored as a scrypt hash by the worker. API keys you enter in Settings are
            AES-256-GCM encrypted under <code className="text-ink">MISHKAH_MASTER_KEY</code> and are never sent
            back to this browser — the panel only ever shows a fingerprint.
          </p>
          <p>
            Sessions last {Math.round(status.session_minutes / 60)}h by default and live in an HttpOnly cookie.
          </p>
        </div>
      </Panel>

      <p className="mt-4 text-2xs text-dim">
        Phase 1 is research + paper trading only. There is no code path in this repository that places a real order, and
        every number shown is computed by the Python worker — never by this browser.
      </p>
    </main>
  );
}
