"use client";

import React from "react";
import { Chip, Panel } from "./Bits";
import type { Bundle } from "@/lib/types";
import { num, stamp } from "@/lib/format";

const STATUS_TONE: Record<string, "ok" | "bad" | "warn" | "info" | "default"> = {
  confirmed: "ok",
  rejected: "bad",
  tested: "info",
  proposed: "warn",
};

export function HypothesesView({ bundle }: { bundle: Bundle }) {
  const rows = bundle.hypotheses.items;
  return (
    <>
      <Panel
        title="Hypothesis registry"
        subtitle="the strong model proposes, code decides — a hypothesis with no codified test stays 'proposed' forever"
        right={<Chip tone="info">{rows.length} rows</Chip>}
        bodyClass="p-0"
      >
        {rows.length === 0 ? (
          <p className="px-4 py-4 text-2xs text-dim">
            Nothing here yet. With <code className="text-ink">EXPLAINER_MODEL</code> + <code className="text-ink">OPENROUTER_API_KEY</code>{" "}
            set, <code className="text-ink">python -m worker report build</code> asks the explainer for falsifiable hypotheses and
            files them here; <code className="text-ink">python -m worker hypothesis test-all</code> then runs the coded tests.
          </p>
        ) : (
          <table className="w-full text-left text-2xs">
            <thead className="bg-panel text-dim">
              <tr>
                <th className="px-3 py-2 font-medium">id</th>
                <th className="px-3 py-2 font-medium">hypothesis</th>
                <th className="px-3 py-2 font-medium">status</th>
                <th className="px-3 py-2 font-medium">proposed by</th>
                <th className="px-3 py-2 font-medium">coded test</th>
                <th className="px-3 py-2 font-medium">proposed</th>
                <th className="px-3 py-2 font-medium">tested</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((h) => (
                <tr key={h.id} className="border-t border-line/60">
                  <td className="px-3 py-2 tabular text-dim">{h.id}</td>
                  <td className="max-w-[60ch] px-3 py-2 text-ink">{h.text}</td>
                  <td className="px-3 py-2">
                    <Chip tone={STATUS_TONE[h.status] ?? "default"}>{h.status}</Chip>
                  </td>
                  <td className="px-3 py-2 text-dim">{h.proposed_by_model ?? "—"}</td>
                  <td className="px-3 py-2 text-dim">{h.test_method ?? "unmapped"}</td>
                  <td className="px-3 py-2 text-dim">{stamp(h.proposed_at)}</td>
                  <td className="px-3 py-2 text-dim">{h.tested_at ? stamp(h.tested_at) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      <Panel title="Coded tests available" subtitle="the only way a hypothesis can ever be called confirmed or rejected">
        <ul className="space-y-1 text-2xs text-dim">
          <li>
            <span className="text-ink">channel_leads</span> — 95% block-bootstrap CI of a channel&apos;s signed 30m lift excludes zero.
          </li>
          <li>
            <span className="text-ink">cost_breakeven</span> — the CI&apos;s lower bound clears the one-sided cost of the traded asset.
          </li>
          <li>
            <span className="text-ink">agreement_scaling</span> — net lift at agreement level k, per k = 2..5.
          </li>
          <li>
            <span className="text-ink">pumper_flag</span> — flagged channels&apos; mean lift vs clean channels&apos;.
          </li>
          <li>
            <span className="text-ink">hour_of_day</span> — net lift per Tehran session bucket (open / midday / late).
          </li>
          <li>
            <span className="text-ink">ounce_vs_dollar</span> — does the morning direction of XAUUSD or USDTIRT predict the day?
          </li>
        </ul>
        <p className="mt-2 text-2xs text-dim">
          Verdict rule is fixed before the test runs: confirmed when the CI excludes zero in the stated direction, rejected when it
          excludes zero in the opposite one, inconclusive when it straddles — and a straddling CI is never spun as support.
        </p>
      </Panel>
    </>
  );
}
