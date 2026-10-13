import "server-only";
import { promises as fs } from "node:fs";
import path from "node:path";
import type { Bundle, TimelineDay } from "./types";

const DATA_DIR = path.join(process.cwd(), "public", "data");

async function readJson<T>(name: string): Promise<T | null> {
  try {
    const raw = await fs.readFile(path.join(DATA_DIR, name), "utf8");
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

/**
 * The whole bundle is read at request time from `public/data`, which the worker regenerates with
 * `python -m worker ui export`. Nothing here computes a number — the UI only renders what the
 * worker already decided, so the browser can never disagree with a report.
 */
export async function loadBundle(): Promise<Bundle | null> {
  const meta = await readJson<Bundle["meta"]>("meta.json");
  if (!meta) return null;
  const [live, leaderboard, decisions, timelineIndex, coverage, hypotheses, runConfig, backtest] =
    await Promise.all([
      readJson<Bundle["live"]>("live.json"),
      readJson<Bundle["leaderboard"]>("leaderboard.json"),
      readJson<Bundle["decisions"]>("decisions.json"),
      readJson<Bundle["timelineIndex"]>("timeline_index.json"),
      readJson<Bundle["coverage"]>("coverage.json"),
      readJson<Bundle["hypotheses"]>("hypotheses.json"),
      readJson<Bundle["runConfig"]>("run_config.json"),
      readJson<Bundle["backtest"]>("backtest.json"),
    ]);
  return {
    meta,
    live: live ?? { as_of: meta.generated_at, stale_after_minutes: 3, mode: "paper", tiles: [], open_position: null, last_equity: null },
    leaderboard: leaderboard ?? { asof_date: null, method_version: meta.method_version, family: null, cost_model: null, channels: [] },
    decisions: decisions ?? { split: meta.split, items: [] },
    timelineIndex: timelineIndex ?? { days: [] },
    coverage: coverage ?? { rows: [], acceptance: [] },
    hypotheses: hypotheses ?? { items: [] },
    runConfig: runConfig ?? { settings: {}, run_config: [] },
    backtest: backtest ?? null,
  };
}

export async function loadTimelineDay(day: string): Promise<TimelineDay | null> {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return null;
  return readJson<TimelineDay>(path.join("timeline", `${day}.json`));
}
