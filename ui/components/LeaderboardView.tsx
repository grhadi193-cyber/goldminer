"use client";

import React, { useMemo, useState } from "react";
import { Chip, CiBar, KeyValue, Panel } from "./Bits";
import type { Bundle, LeaderboardRow } from "@/lib/types";
import { bps, int, labelChip, num, pct } from "@/lib/format";

const HORIZONS = ["30m", "1h", "3h", "eod"] as const;

function Drilldown({ row, costBps }: { row: LeaderboardRow; costBps: number | null }) {
  const ci = row.ci_95.eod ?? null;
  return (
    <div className="mt-2 grid grid-cols-1 gap-4 rounded border border-line bg-panel2 px-3 py-3 lg:grid-cols-3">
      <div>
        <div className="text-2xs uppercase tracking-wider text-dim">why this label</div>
        <p className="mt-1 text-[12px] text-ink">{row.label_reason ?? "—"}</p>
        <p className="mt-2 text-2xs text-dim">
          Label rule: a lead/lag needs ≥20 events and a significant lead/lag peak inside ±60m (lag = signal minute − return
          minute, so negative minutes mean the channel speaks first). Everything else is noise; too few events is
          &quot;insufficient&quot;.
        </p>
      </div>
      <div>
        <div className="text-2xs uppercase tracking-wider text-dim">net lifts by horizon (bps)</div>
        <div className="mt-1">
          <KeyValue
            items={HORIZONS.map(
              (h) =>
                [
                  h,
                  <span key={h} className={Number(row.lifts_net_bps[h]) > 0 ? "text-green" : "text-red"}>
                    {bps(row.lifts_net_bps[h])} {row.q_values[h] !== undefined && row.q_values[h] !== null ? `(q=${num(row.q_values[h], 3)})` : ""}
                  </span>,
                ] as [string, React.ReactNode],
            )}
          />
        </div>
        <p className="mt-2 text-2xs text-dim">
          CI at EOD: {ci ? `[${bps(ci[0])}, ${bps(ci[1])}]` : "—"} bps · one-sided cost {costBps ?? "—"} bps. A lift whose
          interval does not clear the cost line is not a tradeable edge, whatever its p-value.
        </p>
      </div>
      <div>
        <div className="text-2xs uppercase tracking-wider text-dim">measurement + behaviour</div>
        <div className="mt-2">
          <KeyValue
            items={[
              ["events", int(row.n_events)],
              ["measured against", Object.entries(row.measures).map(([k, v]) => `${k} (${v})`).join(", ") || "—"],
              ["pumper flag", row.pumper_flag ? "YES" : "no"],
              ["quoted bias", row.quoted_bias_pct !== null ? pct(row.quoted_bias_pct * 100, 1) : "—"],
              ["burst share", row.burst_share !== null ? num(row.burst_share, 3) : "—"],
              ["method", row.method_version],
            ]}
          />
        </div>
      </div>
    </div>
  );
}

export function LeaderboardView({ bundle }: { bundle: Bundle }) {
  const { leaderboard } = bundle;
  const [open, setOpen] = useState<string | null>(null);
  const costBps = leaderboard.cost_model?.signal_cost_bps ?? null;

  const { min, max } = useMemo(() => {
    const vals: number[] = [];
    leaderboard.channels.forEach((c) => {
      Object.values(c.lifts_net_bps).forEach((v) => v !== null && vals.push(v as number));
      Object.values(c.ci_95).forEach((c2) => c2 && vals.push(c2[0], c2[1]));
    });
    const lo = Math.min(-60, ...(vals.length ? vals : [-60]));
    const hi = Math.max(10, ...(vals.length ? vals : [10]));
    return { min: lo - 5, max: hi + 5 };
  }, [leaderboard.channels]);

  return (
    <>
      <Panel
        title="Channel leaderboard"
        subtitle={`as-of ${leaderboard.asof_date ?? "—"} · net of costs · FDR-corrected family ${
          leaderboard.family?.name ?? "—"
        } (q = ${leaderboard.family?.q_threshold ?? "—"}, ${int(leaderboard.family?.size ?? 0)} tests, ${int(
          leaderboard.family?.significant ?? 0,
        )} significant)`}
        right={
          <>
            <Chip tone="info" title="Every row's lift is multiplied by −1 for down signals, then averaged.">
              {`cost ${costBps ?? "—"} bps one-sided`}
            </Chip>
            <Chip tone="ok" title="Benjamini–Hochberg across the whole pre-registered family.">
              multiple-testing corrected: yes
            </Chip>
          </>
        }
        bodyClass="p-0"
      >
        <div className="overflow-x-auto">
          <table className="w-full min-w-[980px] text-left text-2xs">
            <thead className="bg-panel text-dim">
              <tr>
                <th className="px-3 py-2 font-medium">channel</th>
                <th className="px-3 py-2 font-medium">label</th>
                <th className="px-3 py-2 font-medium">lead/lag</th>
                <th className="px-3 py-2 font-medium">events</th>
                <th className="px-3 py-2 font-medium">net 30m</th>
                <th className="px-3 py-2 font-medium">net 1h</th>
                <th className="px-3 py-2 font-medium">net 3h</th>
                <th className="px-3 py-2 font-medium">net EOD</th>
                <th className="px-3 py-2 font-medium">95% CI (EOD) vs 0 and cost line</th>
                <th className="px-3 py-2 font-medium">q</th>
              </tr>
            </thead>
            <tbody>
              {leaderboard.channels.map((c) => {
                const chip = labelChip(c.label);
                const isOpen = open === c.username;
                return (
                  <React.Fragment key={c.username}>
                    <tr
                      className="cursor-pointer border-t border-line/60 hover:bg-panel2"
                      onClick={() => setOpen(isOpen ? null : c.username)}
                    >
                      <td className="whitespace-nowrap px-3 py-2">
                        <span className="text-ink">{c.username}</span>
                        {!c.active && <span className="ml-2 text-dim">(inactive)</span>}
                        {c.pumper_flag && (
                          <span className="ml-2">
                            <Chip tone="bad" title="Quotes sit ≥1% away from the real price on ≥10 quotes.">
                              pumper
                            </Chip>
                          </span>
                        )}
                      </td>
                      <td className="whitespace-nowrap px-3 py-2">
                        <span className={chip.cls}>{chip.text}</span>
                      </td>
                      <td className="whitespace-nowrap px-3 py-2 tabular text-dim">
                        {c.lead_lag_minutes === null ? "—" : `${c.lead_lag_minutes > 0 ? "+" : ""}${c.lead_lag_minutes}m`}
                      </td>
                      <td className="whitespace-nowrap px-3 py-2 tabular text-dim">{int(c.n_events)}</td>
                      {HORIZONS.map((h) => {
                        const v = c.lifts_net_bps[h];
                        return (
                          <td key={h} className={`whitespace-nowrap px-3 py-2 tabular ${v === null ? "text-dim" : v > 0 ? "text-green" : "text-red"}`}>
                            {bps(v)}
                          </td>
                        );
                      })}
                      <td className="px-2 py-1">
                        <CiBar value={c.lifts_net_bps.eod} ci={c.ci_95.eod ?? null} min={min} max={max} costBps={costBps} />
                      </td>
                      <td className="whitespace-nowrap px-3 py-2 tabular text-dim">{num(c.q_values.eod, 4)}</td>
                    </tr>
                    {isOpen && (
                      <tr className="border-t border-line bg-panel2/40">
                        <td colSpan={10} className="px-3 pb-3">
                          <Drilldown row={c} costBps={costBps} />
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title="How to read this" subtitle="the numbers are honest, the sample is small">
        <ul className="list-disc space-y-1 pl-5 text-2xs text-dim">
          <li>
            A <span className="text-ink">lead</span> label means the channel statistically moves before the price, at an
            FDR-corrected q ≤ 0.10 with ≥20 events. It is a statement about this window, not a promise about next week.
          </li>
          <li>
            Lifts are <span className="text-ink">net of the cost of trading the asset the channel talks about</span> (gold 30/15
            bps, coin 50/25). Gross lifts are in the report; they are context, not a result.
          </li>
          <li>
            A wide CI crossing zero is the honest answer for most channels. The bootstrap resamples <span className="text-ink">days</span>,
            not messages, so a channel that fires 400 times on one afternoon does not get an inflated sample size.
          </li>
          <li>
            The cost line on each bar is the one-sided cost. Bars entirely to the left of it are channels that cannot pay for
            their own spread.
          </li>
        </ul>
      </Panel>
    </>
  );
}
