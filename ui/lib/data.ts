"use client";

import type { Bundle, TimelineDay } from "./types";
import { fetchMember } from "./api";

/**
 * The UI is a STATIC export: there is no Next server at runtime, so the bundle is read by the
 * browser from `/data/*.json`, which the worker serves (same origin as `/api/*`). That keeps the
 * deployment to one service on Render and keeps the browser out of the decision path — the files
 * are exactly the ones `python -m worker ui export` writes.
 */

export async function loadBundle(): Promise<Bundle | null> {
  const meta = await fetchMember<Bundle["meta"]>("meta.json");
  if (!meta) return null;
  const [live, leaderboard, decisions, timelineIndex, coverage, hypotheses, runConfig, backtest] =
    await Promise.all([
      fetchMember<Bundle["live"]>("live.json"),
      fetchMember<Bundle["leaderboard"]>("leaderboard.json"),
      fetchMember<Bundle["decisions"]>("decisions.json"),
      fetchMember<Bundle["timelineIndex"]>("timeline_index.json"),
      fetchMember<Bundle["coverage"]>("coverage.json"),
      fetchMember<Bundle["hypotheses"]>("hypotheses.json"),
      fetchMember<Bundle["runConfig"]>("run_config.json"),
      fetchMember<Bundle["backtest"]>("backtest.json"),
    ]);
  return {
    meta,
    live: live ?? {
      as_of: meta.generated_at,
      stale_after_minutes: 3,
      mode: "paper",
      tiles: [],
      open_position: null,
      last_equity: null,
    },
    leaderboard: leaderboard ?? {
      asof_date: null,
      method_version: meta.method_version,
      family: null,
      cost_model: null,
      channels: [],
    },
    decisions: decisions ?? { split: meta.split, items: [] },
    timelineIndex: timelineIndex ?? { days: [] },
    coverage: coverage ?? { rows: [], acceptance: [] },
    hypotheses: hypotheses ?? { items: [] },
    runConfig: runConfig ?? { settings: {}, run_config: [] },
    backtest: backtest ?? null,
  };
}

/** Polled every 30s by the live tiles: the price tiles go amber after 3 minutes of silence. */
export async function loadLive(): Promise<Bundle["live"] | null> {
  return fetchMember<Bundle["live"]>("live.json");
}

export async function loadTimelineDay(day: string): Promise<TimelineDay | null> {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return null;
  return fetchMember<TimelineDay>(`timeline/${day}.json`);
}
