"use client";

import React from "react";
import { num } from "@/lib/format";

export function Panel({
  title,
  subtitle,
  right,
  children,
  className = "",
  bodyClass = "",
}: {
  title?: React.ReactNode;
  subtitle?: React.ReactNode;
  right?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  bodyClass?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      {(title || right) && (
        <header className="flex items-start justify-between gap-3 border-b border-line px-4 py-2.5">
          <div>
            <h2 className="text-[13px] font-semibold tracking-wide text-ink">{title}</h2>
            {subtitle && <p className="mt-0.5 text-2xs text-dim">{subtitle}</p>}
          </div>
          {right && <div className="flex shrink-0 items-center gap-2">{right}</div>}
        </header>
      )}
      <div className={`px-4 py-3 ${bodyClass}`}>{children}</div>
    </section>
  );
}

export function Chip({
  children,
  tone = "default",
  title,
}: {
  children: React.ReactNode;
  tone?: "default" | "ok" | "warn" | "bad" | "info";
  title?: string;
}) {
  const cls =
    tone === "ok" ? "chip chip-ok" : tone === "warn" ? "chip chip-warn" : tone === "bad" ? "chip chip-bad" : tone === "info" ? "chip chip-info" : "chip";
  return (
    <span className={cls} title={title}>
      {children}
    </span>
  );
}

export function Stat({
  label,
  value,
  sub,
  tone = "default",
}: {
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
  tone?: "default" | "ok" | "warn" | "bad" | "info";
}) {
  const color =
    tone === "ok" ? "text-green" : tone === "warn" ? "text-amber" : tone === "bad" ? "text-red" : tone === "info" ? "text-cyan" : "text-ink";
  return (
    <div className="min-w-0">
      <div className="text-2xs uppercase tracking-wider text-dim">{label}</div>
      <div className={`mt-1 truncate text-lg font-semibold tabular ${color}`}>{value}</div>
      {sub && <div className="mt-0.5 text-2xs text-dim">{sub}</div>}
    </div>
  );
}

/** Horizontal lift bar with the 95% CI drawn as a whisker across a zero line. */
export function CiBar({
  value,
  ci,
  min,
  max,
  costBps,
  height = 22,
}: {
  value: number | null;
  ci?: [number, number] | null;
  min: number;
  max: number;
  costBps?: number | null;
  height?: number;
}) {
  const span = max - min || 1;
  const x = (v: number) => ((v - min) / span) * 100;
  const zero = x(0);
  const color = value === null ? "#8FA0B8" : value > 0 ? "#34D399" : "#F87171";
  return (
    <svg viewBox={`0 0 100 ${height}`} preserveAspectRatio="none" className="h-[22px] w-full">
      <line x1={0} x2={100} y1={height / 2} y2={height / 2} stroke="#1E2635" strokeWidth={1} />
      <line x1={zero} x2={zero} y1={2} y2={height - 2} stroke="#3A465C" strokeWidth={1} />
      {costBps ? (
        <line x1={x(-costBps)} x2={x(-costBps)} y1={3} y2={height - 3} stroke="#F59E0B" strokeWidth={1} strokeDasharray="3 3" />
      ) : null}
      {ci ? (
        <>
          <line x1={x(ci[0])} x2={x(ci[1])} y1={height / 2} y2={height / 2} stroke={color} strokeWidth={2} opacity={0.55} />
          <line x1={x(ci[0])} x2={x(ci[0])} y1={6} y2={height - 6} stroke={color} strokeWidth={1} opacity={0.55} />
          <line x1={x(ci[1])} x2={x(ci[1])} y1={6} y2={height - 6} stroke={color} strokeWidth={1} opacity={0.55} />
        </>
      ) : null}
      {value !== null ? <circle cx={x(value)} cy={height / 2} r={3.5} fill={color} /> : null}
    </svg>
  );
}

/** Jev confidence ring. The number is a derived share of the answer distribution, not a promise. */
export function Ring({ value, size = 68 }: { value: number | null; size?: number }) {
  const v = value === null ? null : Math.max(0, Math.min(1, value));
  const r = size / 2 - 5;
  const c = 2 * Math.PI * r;
  const color = v === null ? "#8FA0B8" : v >= 0.6 ? "#34D399" : v >= 0.45 ? "#F59E0B" : "#F87171";
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="#1E2635" strokeWidth={6} />
      {v !== null && (
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={color}
          strokeWidth={6}
          strokeLinecap="round"
          strokeDasharray={`${c * v} ${c}`}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
        />
      )}
      <text x="50%" y="52%" textAnchor="middle" dominantBaseline="middle" fill="#E6EDF7" fontSize={size / 4} fontWeight={600}>
        {v === null ? "—" : v.toFixed(2)}
      </text>
    </svg>
  );
}

/** Stacked probability bar for a choice question (buy/wait/avoid, up/down/flat, 1..5). */
export function StackedBar({
  distribution,
  order,
  colors,
}: {
  distribution: Record<string, number>;
  order?: string[];
  colors?: string[];
}) {
  const keys = order ?? Object.keys(distribution);
  const palette = colors ?? ["#34D399", "#F59E0B", "#F87171", "#22D3EE", "#A78BFA"];
  return (
    <div className="flex h-3 w-full overflow-hidden rounded-sm border border-line">
      {keys.map((k, i) => {
        const p = distribution[k] ?? 0;
        if (p <= 0) return null;
        return (
          <div
            key={k}
            title={`${k}: ${(p * 100).toFixed(1)}%`}
            style={{ width: `${p * 100}%`, background: palette[i % palette.length] }}
            className="h-full"
          />
        );
      })}
    </div>
  );
}

/** Small probability-bar legend for the choice questions. */
export function DistributionLegend({
  distribution,
  order,
  colors,
}: {
  distribution: Record<string, number>;
  order?: string[];
  colors?: string[];
}) {
  const keys = order ?? Object.keys(distribution);
  const palette = colors ?? ["#34D399", "#F59E0B", "#F87171", "#22D3EE", "#A78BFA"];
  return (
    <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1">
      {keys.map((k, i) => (
        <span key={k} className="flex items-center gap-1 text-2xs text-dim">
          <span className="inline-block h-2 w-2 rounded-sm" style={{ background: palette[i % palette.length] }} />
          {k} {num((distribution[k] ?? 0) * 100, 1)}%
        </span>
      ))}
    </div>
  );
}

/** Line chart (log or linear y) with optional zero-axis shading for drawdowns. */
export function LineChart({
  points,
  height = 220,
  log = false,
  stroke = "#22D3EE",
  area = false,
  yFormat = (v: number) => num(v, 0),
  zeroLine = false,
}: {
  points: { x: number; y: number }[];
  height?: number;
  log?: boolean;
  stroke?: string;
  area?: boolean;
  yFormat?: (v: number) => string;
  zeroLine?: boolean;
}) {
  if (points.length < 2) return <div className="py-8 text-center text-2xs text-dim">not enough points</div>;
  const W = 1000;
  const H = height;
  const padL = 62;
  const padR = 12;
  const padT = 10;
  const padB = 22;
  const ys = points.map((p) => p.y);
  const yMin = Math.min(...ys);
  const yMax = Math.max(...ys);
  const tx = (i: number) => padL + (i / (points.length - 1)) * (W - padL - padR);
  const ty = (v: number) => {
    if (log) {
      const lo = Math.log10(Math.max(yMin, 1e-9));
      const hi = Math.log10(Math.max(yMax, 1e-9));
      const t = (Math.log10(Math.max(v, 1e-9)) - lo) / (hi - lo || 1);
      return H - padB - t * (H - padT - padB);
    }
    const t = (v - yMin) / (yMax - yMin || 1);
    return H - padB - t * (H - padT - padB);
  };
  const path = points.map((p, i) => `${i === 0 ? "M" : "L"}${tx(i).toFixed(2)},${ty(p.y).toFixed(2)}`).join(" ");
  const fill = `${path} L${tx(points.length - 1).toFixed(2)},${H - padB} L${tx(0).toFixed(2)},${H - padB} Z`;
  const ticks = [yMax, yMin + (yMax - yMin) / 2, yMin];
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" preserveAspectRatio="none" style={{ height }}>
      <line x1={padL} x2={W - padR} y1={H - padB} y2={H - padB} stroke="#1E2635" />
      {ticks.map((t, i) => (
        <g key={i}>
          <line x1={padL} x2={W - padR} y1={ty(t)} y2={ty(t)} stroke="#151B26" />
          <text x={padL - 6} y={ty(t) + 3} textAnchor="end" fill="#8FA0B8" fontSize={11}>
            {yFormat(t)}
          </text>
        </g>
      ))}
      {zeroLine && yMin <= 0 && yMax >= 0 ? (
        <line x1={padL} x2={W - padR} y1={ty(0)} y2={ty(0)} stroke="#3A465C" strokeDasharray="4 4" />
      ) : null}
      {area ? <path d={fill} fill={stroke} opacity={0.12} /> : null}
      <path d={path} fill="none" stroke={stroke} strokeWidth={1.6} />
    </svg>
  );
}

export function KeyValue({ items }: { items: [string, React.ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-2xs">
      {items.map(([k, v]) => (
        <React.Fragment key={k}>
          <dt className="text-dim">{k}</dt>
          <dd className="tabular text-ink">{v}</dd>
        </React.Fragment>
      ))}
    </dl>
  );
}
