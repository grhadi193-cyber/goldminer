"use client";

import React, { useMemo, useState } from "react";
import { Chip, DistributionLegend, KeyValue, Panel, Ring, StackedBar } from "./Bits";
import type { Bundle, Decision } from "@/lib/types";
import { int, num, stamp, tehran } from "@/lib/format";

interface Answer {
  type: string;
  value: string | number;
  confidence: number;
  distribution: Record<string, number>;
}

const QUESTION_ORDER = ["day_direction", "final_action", "signal_strength", "credible_contrading_news", "coordinated_pumping"];

const CHOICE_ORDER: Record<string, string[]> = {
  day_direction: ["up", "down", "flat"],
  final_action: ["buy", "wait", "avoid"],
  signal_strength: ["1", "2", "3", "4", "5"],
};

const CHOICE_COLORS: Record<string, string[]> = {
  day_direction: ["#34D399", "#F87171", "#8FA0B8"],
  final_action: ["#34D399", "#F59E0B", "#F87171"],
  signal_strength: ["#1E2635", "#3A465C", "#F59E0B", "#FBBF24", "#34D399"],
};

function QuestionCard({ name, answer, question }: { name: string; answer: Answer; question?: { type?: string; instructions?: string } }) {
  const dist = answer.distribution ?? {};
  const choice = answer.type === "choice" || answer.type === "score";
  return (
    <div className="rounded border border-line bg-panel2 px-3 py-2.5">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-[12px] font-semibold text-ink">{name}</div>
          <div className="text-2xs text-dim">
            {answer.type} · answer <span className="text-ink">{String(answer.value)}</span> · share{" "}
            {num(answer.confidence, 3)}
          </div>
        </div>
      </div>
      {choice && Object.keys(dist).length > 0 && (
        <>
          <div className="mt-2">
            <StackedBar distribution={dist} order={CHOICE_ORDER[name]} colors={CHOICE_COLORS[name]} />
          </div>
          <DistributionLegend distribution={dist} order={CHOICE_ORDER[name]} colors={CHOICE_COLORS[name]} />
        </>
      )}
      {!choice && (
        <div className="mt-2 text-2xs text-dim">
          probability of &quot;yes&quot; from the model&apos;s token distribution — a continuous answer, no threshold applied here.
        </div>
      )}
      {question?.instructions && <p className="mt-2 text-2xs text-dim">{question.instructions}</p>}
    </div>
  );
}

function VerdictPanel({ decision }: { decision: Decision }) {
  const verdict = decision.verdict;
  const tone = verdict?.action === "BUY" ? "ok" : verdict?.action === "AVOID" ? "bad" : "warn";
  return (
    <Panel
      title="Deterministic gate verdict"
      subtitle="computed in code from explicit checks — Jev's answer is only an input"
      right={
        <>
          <Chip tone={tone as never}>{verdict?.action ?? "not stored"}</Chip>
          <Chip tone={decision.split === "outofsample" ? "bad" : decision.split === "discovery" ? "info" : "default"}>
            split: {decision.split}
          </Chip>
        </>
      }
    >
      {verdict ? (
        <>
          <p className="mb-3 text-[12px] text-ink">{verdict.reason}</p>
          <table className="w-full text-left text-2xs">
            <thead className="text-dim">
              <tr>
                <th className="py-1 font-medium">check</th>
                <th className="py-1 font-medium">result</th>
                <th className="py-1 font-medium">detail</th>
              </tr>
            </thead>
            <tbody>
              {verdict.checks.map((c) => (
                <tr key={c.name} className="border-t border-line/60">
                  <td className="py-1 pr-3 text-ink">
                    {c.name}
                    {c.hard_veto && <span className="ml-1 text-2xs text-dim">(hard veto)</span>}
                  </td>
                  <td className="py-1 pr-3">
                    <span className={c.passed ? "text-green" : "text-red"}>{c.passed ? "pass" : "block"}</span>
                  </td>
                  <td className="py-1 text-dim">{c.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : (
        <p className="text-2xs text-dim">
          This decision predates the verdict column (migration 0005). Re-run the replay/backtest to store it — the UI never
          recomputes a verdict on its own.
        </p>
      )}
    </Panel>
  );
}

export function DecisionView({ bundle }: { bundle: Bundle }) {
  const ordered = useMemo(
    () => [...bundle.decisions.items].sort((a, b) => (a.decision_ts < b.decision_ts ? -1 : 1)),
    [bundle.decisions.items],
  );
  const [index, setIndex] = useState(Math.max(0, ordered.length - 1));
  const decision = ordered[index];

  if (!decision) {
    return (
      <Panel title="Jev decision panel">
        <p className="text-2xs text-dim">
          No decisions exported yet. Run <code className="text-ink">python -m worker replay day --date YYYY-MM-DD</code> or a
          backtest, then <code className="text-ink">python -m worker ui export</code>.
        </p>
      </Panel>
    );
  }

  const answers = (decision.answers ?? {}) as Record<string, Answer>;
  const questions = (decision.questions ?? {}) as Record<string, { type?: string; instructions?: string }>;
  const state = decision.state as {
    market?: Record<string, number>;
    history?: Record<string, number>;
    signals?: Record<string, unknown>;
    day?: Record<string, string>;
  };
  const names = QUESTION_ORDER.filter((n) => answers[n]).concat(Object.keys(answers).filter((n) => !QUESTION_ORDER.includes(n)));

  return (
    <>
      <Panel
        title="Replay slider"
        subtitle="every snapshot is the SAME anti-leakage state builder the backtest used — sliding never reveals a message published after the decision instant"
        right={
          <>
            <Chip tone={decision.model === "mock-jev-1.13.0" ? "warn" : "default"} title="Mock transport answers are NOT Jev's.">
              {decision.model ?? "unknown model"}
            </Chip>
            {decision.from_cache && <Chip tone="info">cached</Chip>}
          </>
        }
      >
        <input
          type="range"
          min={0}
          max={ordered.length - 1}
          value={index}
          onChange={(e) => setIndex(Number(e.target.value))}
          className="w-full accent-cyan"
        />
        <div className="mt-1 flex flex-wrap items-center justify-between gap-2 text-2xs text-dim">
          <span>
            {decision.day} · checkpoint {decision.checkpoint} · {stamp(decision.decision_ts)} UTC ({tehran(decision.decision_ts)}{" "}
            Tehran)
          </span>
          <span>
            decision {index + 1} / {ordered.length} · state_hash <code className="text-ink">{decision.state_hash.slice(0, 16)}…</code>
          </span>
        </div>
      </Panel>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[320px_1fr]">
        <Panel title="Confidence" subtitle="share of the model's answer distribution behind its own pick">
          <div className="flex items-center gap-4">
            <Ring value={decision.confidence} />
            <div className="text-2xs text-dim">
              <div>
                gate needs <span className="text-ink">≥ 0.60</span>
              </div>
              <div className="mt-1">
                day pnl so far <span className="text-ink">{num(decision.day_pnl_bps, 1)} bps</span>
              </div>
              <div className="mt-1">
                checkpoint <span className="text-ink">{decision.checkpoint}</span>
              </div>
            </div>
          </div>
          <p className="mt-3 text-2xs text-dim">
            This is NOT a calibrated probability yet. The calibration view shows whether &quot;0.70&quot; has meant 70% in this
            window; until it does, treat the ring as a ranking, not a risk number.
          </p>
        </Panel>

        <Panel title="State the model saw" subtitle="numbers computed by code, unit-tagged, no raw text except the snippets it is allowed">
          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            <div>
              <div className="mb-1 text-2xs uppercase tracking-wider text-dim">market</div>
              <KeyValue items={Object.entries(state.market ?? {}).map(([k, v]) => [k, num(Number(v), 2)])} />
            </div>
            <div>
              <div className="mb-1 text-2xs uppercase tracking-wider text-dim">history</div>
              <KeyValue items={Object.entries(state.history ?? {}).map(([k, v]) => [k, num(Number(v), 2)])} />
            </div>
            <div>
              <div className="mb-1 text-2xs uppercase tracking-wider text-dim">signal counts</div>
              <KeyValue
                items={[
                  ["messages last 3h", String((state.signals?.n_messages_last_3h as number) ?? "—")],
                  ["extracted last 3h", String((state.signals?.n_extracted_last_3h as number) ?? "—")],
                  ["channels active", String((state.signals?.n_channels_active as number) ?? "—")],
                  ["mean intensity", num(Number(state.signals?.mean_intensity ?? 0), 2)],
                  ["high intensity", String((state.signals?.high_intensity_count as number) ?? "—")],
                  ["kind counts", JSON.stringify(state.signals?.kind_counts ?? {})],
                ]}
              />
            </div>
          </div>
          {Array.isArray(state.signals?.recent_signal_snippets) && (
            <div className="mt-3">
              <div className="text-2xs uppercase tracking-wider text-dim">snippets passed to the model</div>
              <ul className="mt-1 space-y-0.5 text-2xs text-dim">
                {(state.signals?.recent_signal_snippets as string[]).slice(0, 6).map((s, i) => (
                  <li key={i} className="truncate">
                    {s}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </Panel>
      </div>

      <Panel title="Fixed question panel" subtitle="the same question tree every day — no free-form prompting (rule R3)">
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          {names.map((name) => (
            <QuestionCard key={name} name={name} answer={answers[name]} question={questions[name]} />
          ))}
        </div>
      </Panel>

      <VerdictPanel decision={decision} />

      <Panel title="Decision ledger" subtitle="one row per stored decision, newest first" bodyClass="p-0">
        <div className="max-h-[40vh] overflow-auto">
          <table className="w-full text-left text-2xs">
            <thead className="sticky top-0 bg-panel text-dim">
              <tr>
                <th className="px-3 py-2 font-medium">day</th>
                <th className="px-3 py-2 font-medium">checkpoint</th>
                <th className="px-3 py-2 font-medium">direction</th>
                <th className="px-3 py-2 font-medium">action</th>
                <th className="px-3 py-2 font-medium">confidence</th>
                <th className="px-3 py-2 font-medium">verdict</th>
                <th className="px-3 py-2 font-medium">split</th>
              </tr>
            </thead>
            <tbody>
              {ordered
                .slice()
                .reverse()
                .map((d, i) => {
                  const a = (d.answers ?? {}) as Record<string, Answer>;
                  return (
                    <tr
                      key={d.decision_id}
                      className={`cursor-pointer border-t border-line/60 hover:bg-panel2 ${index === ordered.length - 1 - i ? "bg-panel2" : ""}`}
                      onClick={() => setIndex(ordered.length - 1 - i)}
                    >
                      <td className="px-3 py-1.5 text-ink">{d.day}</td>
                      <td className="px-3 py-1.5 text-dim">{d.checkpoint}</td>
                      <td className="px-3 py-1.5 text-dim">{String(a.day_direction?.value ?? "—")}</td>
                      <td className="px-3 py-1.5 text-dim">{String(a.final_action?.value ?? "—")}</td>
                      <td className="px-3 py-1.5 tabular text-dim">{num(d.confidence, 3)}</td>
                      <td className="px-3 py-1.5">
                        <span className={d.verdict?.action === "BUY" ? "text-green" : d.verdict?.action === "AVOID" ? "text-red" : "text-amber"}>
                          {d.verdict?.action ?? "—"}
                        </span>
                      </td>
                      <td className="px-3 py-1.5 text-dim">{d.split}</td>
                    </tr>
                  );
                })}
            </tbody>
          </table>
        </div>
        <p className="border-t border-line px-3 py-2 text-2xs text-dim">
          {int(ordered.length)} decisions exported. Out-of-sample rows are shown with a red split tag on purpose: they are one-shot
          evidence and must never be silently mixed into discovery numbers.
        </p>
      </Panel>
    </>
  );
}
