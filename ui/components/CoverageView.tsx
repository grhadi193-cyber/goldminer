"use client";

import React from "react";
import { Chip, Panel } from "./Bits";
import type { Bundle } from "@/lib/types";
import { int, num, stamp } from "@/lib/format";

export function CoverageView({ bundle }: { bundle: Bundle }) {
  const { coverage, meta } = bundle;
  const failed = coverage.acceptance.filter((a) => !a.passed);

  return (
    <>
      <Panel
        title="Coverage acceptance (M1)"
        subtitle="the gate that decides whether history is complete enough to research on"
        right={<Chip tone={failed.length ? "bad" : "ok"}>{failed.length ? `${failed.length} failing` : "all passing"}</Chip>}
        bodyClass="p-0"
      >
        <table className="w-full text-left text-2xs">
          <thead className="bg-panel text-dim">
            <tr>
              <th className="px-3 py-2 font-medium">check</th>
              <th className="px-3 py-2 font-medium">result</th>
              <th className="px-3 py-2 font-medium">threshold</th>
              <th className="px-3 py-2 font-medium">detail</th>
            </tr>
          </thead>
          <tbody>
            {coverage.acceptance.map((a) => (
              <tr key={a.name} className="border-t border-line/60">
                <td className="px-3 py-2 text-ink">{a.name}</td>
                <td className="px-3 py-2">
                  <span className={a.passed ? "text-green" : "text-red"}>{a.passed ? "PASS" : "FAIL"}</span>
                </td>
                <td className="px-3 py-2 text-dim">{a.threshold}</td>
                <td className="px-3 py-2 text-dim">{a.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      <Panel
        title="Bars per series per month"
        subtitle="bar counts, expected counts and gaps — no interpolation, no forward fill, nothing hidden"
        right={<Chip tone="info">{int(meta.series.length)} series tracked</Chip>}
        bodyClass="p-0"
      >
        <div className="max-h-[52vh] overflow-auto">
          <table className="w-full text-left text-2xs">
            <thead className="sticky top-0 bg-panel text-dim">
              <tr>
                <th className="px-3 py-2 font-medium">source</th>
                <th className="px-3 py-2 font-medium">symbol</th>
                <th className="px-3 py-2 font-medium">res</th>
                <th className="px-3 py-2 font-medium">month</th>
                <th className="px-3 py-2 font-medium">bars</th>
                <th className="px-3 py-2 font-medium">expected</th>
                <th className="px-3 py-2 font-medium">coverage</th>
                <th className="px-3 py-2 font-medium">gaps</th>
                <th className="px-3 py-2 font-medium">gap minutes</th>
              </tr>
            </thead>
            <tbody>
              {coverage.rows.map((r) => (
                <tr key={`${r.source}-${r.symbol}-${r.resolution}-${r.month}`} className="border-t border-line/60">
                  <td className="px-3 py-1.5 text-dim">{r.source}</td>
                  <td className="px-3 py-1.5 text-ink">{r.symbol}</td>
                  <td className="px-3 py-1.5 text-dim">{r.resolution}</td>
                  <td className="px-3 py-1.5 text-dim">{r.month}</td>
                  <td className="px-3 py-1.5 tabular text-dim">{int(r.n_bars)}</td>
                  <td className="px-3 py-1.5 tabular text-dim">{int(r.expected_bars)}</td>
                  <td className={`px-3 py-1.5 tabular ${r.coverage_pct >= 95 ? "text-green" : r.coverage_pct >= 80 ? "text-amber" : "text-red"}`}>
                    {num(r.coverage_pct, 1)}%
                  </td>
                  <td className="px-3 py-1.5 tabular text-dim">{int(r.n_gaps)}</td>
                  <td className="px-3 py-1.5 tabular text-dim">{num(r.gap_minutes, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title="Series last seen" subtitle="staleness of every tracked series in the database">
        <div className="grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-3">
          {meta.series.map((s) => (
            <div key={`${s.source}-${s.symbol}-${s.resolution}`} className="rounded border border-line bg-panel2 px-3 py-2">
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-[12px] text-ink">
                  {s.source} · {s.symbol} · {s.resolution}
                </span>
                <Chip tone={s.source === "synthetic" ? "warn" : "info"}>{s.source === "synthetic" ? "demo" : "real"}</Chip>
              </div>
              <div className="mt-1 text-2xs text-dim">
                {int(s.n_bars)} bars · last {stamp(s.last_ts)}
              </div>
            </div>
          ))}
        </div>
      </Panel>
    </>
  );
}
