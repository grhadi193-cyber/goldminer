"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { MissionControl } from "@/components/MissionControl";
import { AuthGate } from "@/components/AuthGate";
import { Panel } from "@/components/Bits";
import { authStatus, type AuthStatus } from "@/lib/api";
import { loadBundle, loadLive } from "@/lib/data";
import type { Bundle } from "@/lib/types";

const FULL_REFRESH_MS = 120_000; // decisions, leaderboard, coverage…
const LIVE_REFRESH_MS = 30_000; // tiles only, so the panel stays cheap to poll

export default function Page() {
  const [session, setSession] = useState<AuthStatus | null>(null);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [bundle, setBundle] = useState<Bundle | null>(null);
  const [bundleError, setBundleError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const refreshing = useRef(false);

  useEffect(() => {
    authStatus()
      .then(setSession)
      .catch((err: unknown) => setSessionError(String(err)));
  }, []);

  const refresh = useCallback(async () => {
    if (refreshing.current) return;
    refreshing.current = true;
    setLoading(true);
    try {
      const next = await loadBundle();
      if (next) {
        setBundle(next);
        setBundleError(null);
      } else {
        setBundleError(
          "No bundle at /data. The worker serves it: run `python -m worker ui export` (or open the worker root and follow the instructions there).",
        );
      }
    } finally {
      refreshing.current = false;
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!session?.authenticated) return;
    void refresh();
    const timer = window.setInterval(() => void refresh(), FULL_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [session?.authenticated, refresh]);

  useEffect(() => {
    if (!session?.authenticated) return;
    const timer = window.setInterval(async () => {
      const live = await loadLive();
      if (live) setBundle((current) => (current ? { ...current, live } : current));
    }, LIVE_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [session?.authenticated]);

  if (sessionError) {
    return (
      <main className="mx-auto max-w-2xl p-10">
        <Panel title="Worker unavailable" subtitle="the panel could not reach the API on this origin">
          <p className="text-2xs text-amber">{sessionError}</p>
          <p className="mt-2 text-2xs text-dim">
            The Mission Control UI is a static export; all data and every write come from the worker. On a Render
            deployment the worker serves this page itself, so this only happens if the service is still starting or the
            database URL is wrong.
          </p>
        </Panel>
      </main>
    );
  }

  if (!session) {
    return (
      <main className="mx-auto max-w-md p-10 text-2xs text-dim">connecting to the worker…</main>
    );
  }

  if (!session.authenticated) {
    return <AuthGate status={session} onUnlocked={setSession} />;
  }

  if (!bundle) {
    return (
      <main className="mx-auto max-w-2xl p-10">
        <Panel
          title="Mission Control"
          subtitle="the UI renders only what the worker computed"
          right={
            <button
              type="button"
              onClick={() => void refresh()}
              className="rounded border border-line bg-panel2 px-2.5 py-1 text-2xs text-ink"
            >
              {loading ? "loading…" : "reload"}
            </button>
          }
        >
          {bundleError ? (
            <>
              <p className="text-2xs text-amber">{bundleError}</p>
              <pre className="mt-3 overflow-x-auto rounded border border-line bg-panel2 p-3 text-2xs text-cyan">
{`# from the repository root
.venv/bin/python -m worker demo seed                    # synthetic dataset, if the DB is empty
.venv/bin/python -m worker backtest run --split discovery
.venv/bin/python -m worker report build --split discovery
.venv/bin/python -m worker ui export --split discovery --days 45 --clean`}
              </pre>
            </>
          ) : (
            <p className="text-2xs text-dim">loading the bundle…</p>
          )}
        </Panel>
      </main>
    );
  }

  return <MissionControl bundle={bundle} session={session} />;
}
