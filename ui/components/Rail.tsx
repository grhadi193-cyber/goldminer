"use client";

import React from "react";

export type ViewKey =
  | "live"
  | "timeline"
  | "leaderboard"
  | "decision"
  | "backtest"
  | "coverage"
  | "hypotheses"
  | "admin";

const ICONS: Record<ViewKey, React.ReactNode> = {
  live: (
    <path d="M2 12h4l2.5-6 3 12 2.5-8 2 2h6" />
  ),
  timeline: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3.5 2" />
    </>
  ),
  leaderboard: (
    <>
      <path d="M4 20V10M10 20V4M16 20v-7M22 20H2" />
    </>
  ),
  decision: (
    <>
      <path d="M12 3v18M5 7h14M7 7l-3 6a3 3 0 0 0 6 0zM17 7l-3 6a3 3 0 0 0 6 0z" />
    </>
  ),
  backtest: (
    <path d="M3 17l5-6 4 3 5-8 4 4" />
  ),
  coverage: (
    <>
      <ellipse cx="12" cy="6" rx="8" ry="3" />
      <path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3" />
    </>
  ),
  hypotheses: (
    <>
      <path d="M9 18h6M10 21h4M12 3a6 6 0 0 1 3.5 10.9c-.6.5-.5 1.1-.5 2.1h-6c0-1 .1-1.6-.5-2.1A6 6 0 0 1 12 3z" />
    </>
  ),
  admin: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2 2 2 0 1 1-4 0 1.7 1.7 0 0 0-2.9-1.2l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.7 1.7 0 0 0 4.6 15a2 2 0 1 1 0-4 1.7 1.7 0 0 0 1.2-2.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 11.5 4a2 2 0 1 1 4 0 1.7 1.7 0 0 0 2.9 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1A1.7 1.7 0 0 0 19.4 11a2 2 0 1 1 0 4z" />
    </>
  ),
};

const LABELS: Record<ViewKey, string> = {
  live: "Live",
  timeline: "Timeline",
  leaderboard: "Leaderboard",
  decision: "Jev decision",
  backtest: "Backtest",
  coverage: "Coverage",
  hypotheses: "Hypotheses",
  admin: "Admin",
};

export function Rail({ active, onChange }: { active: ViewKey; onChange: (v: ViewKey) => void }) {
  return (
    <nav className="flex h-full w-[64px] shrink-0 flex-col items-center gap-1 border-r border-line bg-panel2 py-3">
      <div className="mb-2 grid h-9 w-9 place-items-center rounded-lg border border-line text-[13px] font-bold text-cyan">
        م
      </div>
      {(Object.keys(ICONS) as ViewKey[]).map((key) => (
        <button
          key={key}
          type="button"
          className="rail-btn"
          data-active={active === key}
          title={LABELS[key]}
          aria-label={LABELS[key]}
          onClick={() => onChange(key)}
        >
          <svg width={20} height={20} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round">
            {ICONS[key]}
          </svg>
        </button>
      ))}
      <div className="mt-auto text-[9px] uppercase tracking-widest text-dim [writing-mode:vertical-rl]">paper only</div>
    </nav>
  );
}

export { LABELS };
