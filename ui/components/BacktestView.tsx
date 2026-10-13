"use client";

import React from "react";
import { Chip, KeyValue, LineChart, Panel, Stat } from "./Bits";
import type { Bundle } from "@/lib/types";
import { bps, int, num, pct } from "@/lib/format";

export function BacktestView({ bundle }: { bundle: Bundle }) {
  const bt = bundle.backtest;
  if (!bt) {
    return (
      <Panel title="Backtest">
        <p className="text-2xs text-dim">
          No artifact yet. Run <code className="text-ink">python -m worker backtest run --split discovery</code> then{" "}
          <code className="text-ink">python -m worker ui export</code>.
        </p>
      </Panel>
    );
  }
  const equity = bt.equity ?? [];
  const unmeasured = equity.reduce((a, r) => a + (r.unmeasured_marks ?? 0), 0);
  const trades = equity.reduce((a, r) => a + r.trades, 0);
  const last = equity[equity.length - 1];
  const first = equity[0];
  const totalReturn = last && first ? (last.equity / (first.equity / (1 + first.net_bps / 1e4)) - 1) * 100 : null;
  const cal = bt.calibration;
  const summary = bt.summary ?? {};
  const costs = (bt.config as { costs?: Record<string, { spread_bps: number; slippage_bps: number }> })?.costs ?? {};
  const gold = costs.gold;
  const goldOneSided = gold ? gold.spread_bps + gold.slippage_bps : null;
  const goldRoundTrip = goldOneSided === null ? null : goldOneSided + gold.slippage_bps;

  return (
    <>
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
        <Panel title="Paper equity">
          <Stat label="total return" value={totalReturn === null ? "—" : `${num(totalReturn, 2)}%`} tone={totalReturn && totalReturn > 0 ? "ok" : "bad"} sub="paper book, not a tradable result" />
        </Panel>
        <Panel title="Days">
          <Stat label="traded days" value={int(equity.length)} sub={`of ${int(Number(summary.days ?? equity.length))} in the split`} />
        </Panel>
        <Panel title="Trades">
          <Stat label="round trips" value={int(trades)} sub={unmeasured ? `${int(unmeasured)} marked on a single bar` : "each leg marked by a real series"} tone={unmeasured ? "warn" : "default"} />
        </Panel>
        <Panel title="Calibration">
          <Stat label="Brier" value={num(cal?.brier, 4)} sub={`ECE ${num(cal?.ece, 4)} · n=${int(cal?.n ?? 0)}`} />
        </Panel>
        <Panel title="Costs">
          <Stat
            label="gold one-sided"
            value={goldOneSided === null ? "—" : `${num(goldOneSided, 0)} bps`}
            sub={goldRoundTrip === null ? "no cost model in artifact" : `round trip ${num(goldRoundTrip, 0)} bps`}
          />
        </Panel>
      </div>

      <Panel
        title="Equity curve (log scale) and drawdown"
        subtitle={`run ${bt.run_id} · ${int(equity.length)} days`}
        right={unmeasured ? <Chip tone="warn" title="Entry and exit read the same bar, so the mid move is exactly zero by construction: the curve is measuring cost, not price.">mechanically unmeasured marks</Chip> : <Chip tone="ok">marked</Chip>}
        bodyClass="px-2 py-3"
      >
        <LineChart
          points={equity.map((r) => ({ x: 0, y: r.equity }))}
          height={230}
          log
          stroke={totalReturn && totalReturn > 0 ? "#34D399" : "#F87171"}
          area
          yFormat={(v) => num(v / 1e9, 3) + "B"}
        />
        <div className="mt-2">
          <LineChart
            points={equity.map((r) => ({ x: 0, y: r.drawdown }))}
            height={110}
            stroke="#F59E0B"
            area
            zeroLine
            yFormat={(v) => `${num(v, 1)}%`}
          />
        </div>
      </Panel>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Panel title="Jev vs baselines" subtitle="day-level calls; costs charged on every call that implies a position" bodyClass="p-0">
          <table className="w-full text-left text-2xs">
            <thead className="bg-panel text-dim">
              <tr>
                <th className="px-3 py-2 font-medium">model</th>
                <th className="px-3 py-2 font-medium">calls</th>
                <th className="px-3 py-2 font-medium">coverage</th>
                <th className="px-3 py-2 font-medium">accuracy</th>
                <th className="px-3 py-2 font-medium">gross bps</th>
                <th className="px-3 py-2 font-medium">net bps</th>
              </tr>
            </thead>
            <tbody>
              {bt.baselines.map((b) => (
                <tr key={b.name} className={`border-t border-line/60 ${b.name.startsWith("Jev") ? "bg-panel2" : ""}`}>
                  <td className="px-3 py-2 text-ink">{b.name}</td>
                  <td className="px-3 py-2 tabular text-dim">{int(b.n_calls)}</td>
                  <td className="px-3 py-2 tabular text-dim">{num(b.coverage, 3)}</td>
                  <td className="px-3 py-2 tabular text-dim">{b.directional_accuracy === null ? "—" : num(b.directional_accuracy, 4)}</td>
                  <td className={`px-3 py-2 tabular ${b.mean_signed_bps_gross > 0 ? "text-green" : "text-red"}`}>{bps(b.mean_signed_bps_gross)}</td>
                  <td className={`px-3 py-2 tabular ${b.mean_signed_bps_net > 0 ? "text-green" : "text-red"}`}>{bps(b.mean_signed_bps_net)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="border-t border-line px-3 py-2 text-2xs text-dim">
            Baselines exist to embarrass the expensive model. B2 (USDTIRT morning momentum) needs no LLM at all — if Jev cannot
            beat it net of costs, the honest conclusion is that the LLM is not adding tradeable information.
          </p>
        </Panel>

        <Panel title="Calibration reliability" subtitle={`${int(cal?.n ?? 0)} day-level calls · Brier ${num(cal?.brier, 4)} · ECE ${num(cal?.ece, 4)}`} bodyClass="px-2 py-3">
          <svg viewBox="0 0 320 200" className="w-full" style={{ height: 210 }}>
            <line x1="40" y1="170" x2="310" y2="170" stroke="#1E2635" />
            <line x1="40" y1="10" x2="40" y2="170" stroke="#1E2635" />
            <line x1="40" y1="170" x2="310" y2="10" stroke="#3A465C" strokeDasharray="4 4" />
            {[0, 0.25, 0.5, 0.75, 1].map((t) => (
              <text key={t} x={40 + t * 270 - 6} y={185} fill="#8FA0B8" fontSize={9}>
                {t.toFixed(2)}
              </text>
            ))}
            <text x="4" y="16" fill="#8FA0B8" fontSize={9}>
              1.0
            </text>
            <text x="4" y="170" fill="#8FA0B8" fontSize={9}>
              0.0
            </text>
            {(cal?.reliability ?? [])
              .filter((r) => r.n > 0)
              .map((r) => (
                <g key={r.bin_low}>
                  <circle cx={40 + r.mean_confidence * 270} cy={170 - r.empirical_frequency * 160} r={Math.min(9, 3 + r.n / 4)} fill="#22D3EE" opacity={0.85} />
                  <text x={40 + r.mean_confidence * 270 + 8} y={170 - r.empirical_frequency * 160 + 3} fill="#8FA0B8" fontSize={8}>
                    n={r.n}
                  </text>
                </g>
              ))}
          </svg>
          <p className="mt-1 text-2xs text-dim">
            Points above the diagonal mean the model is under-confident in this window; below means over-confident. Bubble size is
            the number of calls in the bin.
          </p>
        </Panel>
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Panel title="Confidence sweep" subtitle="if we only traded above a confidence threshold, would precision survive?" bodyClass="p-0">
          <table className="w-full text-left text-2xs">
            <thead className="bg-panel text-dim">
              <tr>
                <th className="px-3 py-2 font-medium">gate</th>
                <th className="px-3 py-2 font-medium">coverage</th>
                <th className="px-3 py-2 font-medium">n trades</th>
                <th className="px-3 py-2 font-medium">precision</th>
                <th className="px-3 py-2 font-medium">base rate up</th>
              </tr>
            </thead>
            <tbody>
              {(cal?.sweep ?? []).map((s) => (
                <tr key={s.threshold} className="border-t border-line/60">
                  <td className="px-3 py-1.5 tabular text-ink">≥ {num(s.threshold, 2)}</td>
                  <td className="px-3 py-1.5 tabular text-dim">{num(s.coverage, 3)}</td>
                  <td className="px-3 py-1.5 tabular text-dim">{int(s.n_trades)}</td>
                  <td className="px-3 py-1.5 tabular text-dim">{s.precision === null ? "—" : num(s.precision, 4)}</td>
                  <td className="px-3 py-1.5 tabular text-dim">{s.hit_rate_up_days === null ? "—" : num(s.hit_rate_up_days, 4)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>

        <Panel title="Run configuration" subtitle="what produced the curve above — frozen, in the artifact">
          <KeyValue
            items={[
              ["run id", String(bt.run_id)],
              ["split", String((bt.config as { mode?: string })?.mode ?? "—")],
              ["first day", String((bt.config as { first_day?: string })?.first_day ?? "—")],
              ["last day", String((bt.config as { last_day?: string })?.last_day ?? "—")],
              ["checkpoints", JSON.stringify((bt.config as { checkpoints?: string[] })?.checkpoints ?? [])],
              ["flat band", `${num(Number((bt.config as { flat_band_bps?: number })?.flat_band_bps ?? 0), 1)} bps`],
              ["day records", int(bt.n_day_records)],
              ["days covered", int(bt.scoring?.days_covered ?? 0)],
              ["days missing", int(bt.scoring?.days_missing ?? 0)],
            ]}
          />
          <p className="mt-3 text-2xs text-dim">
            Day-level accuracy on a window this short carries a 95% interval of roughly ±15 percentage points. Anything that
            looks like an edge here is a hypothesis, not a result — the message-level leaderboard has far more power than the
            daily call.
          </p>
          {cal && cal.n > 0 && (
            <p className="mt-2 text-2xs text-amber">
              {pct(100 * (cal.n / Math.max(1, Number(summary.days ?? cal.n))), 0)} of split days produced a usable day record.
            </p>
          )}
        </Panel>
      </div>
    </>
  );
}
