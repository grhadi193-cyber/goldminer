"use client";

import React, { useEffect, useMemo, useState } from "react";
import { Chip, KeyValue, Panel } from "./Bits";
import type { Bundle, TimelineDay, TimelineItem } from "@/lib/types";
import { channelColor, int, kindGlyph, latinDigits, num, tehran, timeOf } from "@/lib/format";

const W = 1000;
const H = 190;

function Story({ item, onClose }: { item: TimelineItem; onClose: () => void }) {
  return (
    <div className="panel">
      <header className="flex items-center justify-between gap-2 border-b border-line px-4 py-2.5">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span style={{ color: channelColor(item.username) }}>●</span>
            <span className="truncate text-[13px] font-semibold text-ink">{item.username}</span>
            <Chip>{item.kind ?? "unclassified"}</Chip>
            {item.direction && (
              <Chip tone={item.direction === "up" ? "ok" : item.direction === "down" ? "bad" : "default"}>
                {item.direction}
              </Chip>
            )}
            {item.is_edited && <Chip tone="warn" title={`version ${item.version}`}>edited</Chip>}
          </div>
          <div className="mt-0.5 text-2xs text-dim">
            published {timeOf(item.published_at)} UTC · {tehran(item.published_at)} Tehran · intensity{" "}
            {item.intensity ?? "—"}/5 · extraction {item.extraction_status}
          </div>
        </div>
        <button type="button" className="text-2xs text-dim hover:text-ink" onClick={onClose}>
          close ✕
        </button>
      </header>
      <div className="grid grid-cols-1 gap-4 px-4 py-3 lg:grid-cols-2">
        <div>
          <div className="mb-1 text-2xs uppercase tracking-wider text-dim">raw Persian (as published)</div>
          <p className="fa-text max-h-56 overflow-auto rounded border border-line bg-panel2 p-2 text-[12px] leading-6 text-ink">
            {latinDigits(item.persian_text ?? "")}
          </p>
        </div>
        <div>
          <div className="mb-1 text-2xs uppercase tracking-wider text-dim">strict-JSON extraction (cheap model, English)</div>
          <p className="max-h-56 overflow-auto rounded border border-line bg-panel2 p-2 text-[12px] leading-5 text-ink">
            {item.english_text ?? "no extraction for this message"}
          </p>
          {item.persian_gloss && <p className="fa-text mt-1 text-2xs text-dim">{latinDigits(item.persian_gloss)}</p>}
        </div>
      </div>
      <div className="border-t border-line px-4 py-2">
        <KeyValue
          items={[
            ["topic", item.topic ?? "—"],
            ["confidence", item.confidence !== null ? num(item.confidence, 3) : "—"],
            ["quoted", item.quoted_asset ? `${item.quoted_asset} @ ${item.quoted_price ?? "?"}` : "—"],
            ["message id", String(item.id)],
          ]}
        />
      </div>
    </div>
  );
}

export function TimelineView({ bundle }: { bundle: Bundle }) {
  const days = bundle.timelineIndex.days.map((d) => d.day);
  const [day, setDay] = useState(days[days.length - 1] ?? "");
  const [data, setData] = useState<TimelineDay | null>(null);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<TimelineItem | null>(null);
  const [channels, setChannels] = useState<Record<string, boolean>>({});

  useEffect(() => {
    if (!day) return;
    let alive = true;
    setLoading(true);
    fetch(`/data/timeline/${day}.json`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d: TimelineDay | null) => {
        if (alive) {
          setData(d);
          setSelected(null);
          setLoading(false);
        }
      })
      .catch(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [day]);

  const items = useMemo(() => {
    const all = data?.items ?? [];
    const names = Object.keys(channels);
    if (!names.length) return all;
    return all.filter((i) => channels[i.username]);
  }, [data, channels]);

  const path = data?.price_path;
  const geometry = useMemo(() => {
    const pts = path?.points ?? [];
    if (pts.length < 2) return null;
    const tMax = Math.max(...pts.map((p) => p.t), 480);
    const yMin = Math.min(...pts.map((p) => p.p));
    const yMax = Math.max(...pts.map((p) => p.p));
    const x = (t: number) => (t / tMax) * W;
    const y = (v: number) => H - 18 - ((v - yMin) / (yMax - yMin || 1)) * (H - 34);
    const d = pts.map((p, i) => `${i === 0 ? "M" : "L"}${x(p.t).toFixed(1)},${y(p.p).toFixed(1)}`).join(" ");
    return { d, x, y, yMin, yMax, tMax };
  }, [path]);

  const allChannels = useMemo(
    () => Array.from(new Set((data?.items ?? []).map((i) => i.username))).sort(),
    [data],
  );

  return (
    <>
      <Panel
        title="Day timeline"
        subtitle={
          path?.source
            ? `markers are messages, line is ${path.source}/${path.symbol}/${path.resolution} across the ${bundle.meta.tev?.open_local ?? "08:00"}–${bundle.meta.tev?.flat_local ?? "22:00"} Tehran session`
            : "markers are messages; no intraday series for this day"
        }
        right={
          <>
            <Chip>{int(items.length)} signals</Chip>
            {data?.truncated && <Chip tone="warn">truncated</Chip>}
            <select
              className="rounded border border-line bg-panel2 px-2 py-1 text-2xs text-ink"
              value={day}
              onChange={(e) => setDay(e.target.value)}
            >
              {days
                .slice()
                .reverse()
                .map((d) => (
                  <option key={d} value={d}>
                    {d}
                  </option>
                ))}
            </select>
          </>
        }
        bodyClass="px-2 py-3"
      >
        {geometry ? (
          <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ height: H }}>
            <line x1={0} x2={W} y1={H - 18} y2={H - 18} stroke="#1E2635" />
            {[0, 120, 240, 360, 480, 600, 720, 840].map((t) => (
              <g key={t}>
                <line x1={geometry.x(t)} x2={geometry.x(t)} y1={10} y2={H - 18} stroke="#151B26" />
                <text x={geometry.x(t) + 3} y={H - 5} fill="#8FA0B8" fontSize={10}>
                  {String(Math.floor(t / 60)).padStart(2, "0")}:{String(t % 60).padStart(2, "0")}
                </text>
              </g>
            ))}
            <path d={geometry.d} fill="none" stroke="#3A465C" strokeWidth={1.3} />
            {/* direction above/below the axis: up = above, down = below, everything else on it */}
            {items
              .filter((i) => i.direction === "up" || i.direction === "down")
              .map((i) => {
                const p = (() => {
                  const points = path?.points ?? [];
                  if (!points.length) return null;
                  // `i.t` is minutes since the session open, computed by the worker with zoneinfo.
                  const t = i.t;
                  const nearest = points.reduce(
                    (best, cur) => (Math.abs(cur.t - t) < Math.abs(best.t - t) ? cur : best),
                    points[0],
                  );
                  return { cx: geometry.x(Math.max(0, Math.min(geometry.tMax, t))), cy: geometry.y(nearest.p) };
                })();
                if (!p) return null;
                const up = i.direction === "up";
                const cy = up ? Math.max(12, p.cy - 22) : Math.min(H - 24, p.cy + 22);
                return (
                  <g key={i.id} onClick={() => setSelected(i)} className="cursor-pointer">
                    <line x1={p.cx} x2={p.cx} y1={p.cy} y2={cy} stroke={channelColor(i.username)} strokeWidth={0.7} opacity={0.5} />
                    <text x={p.cx} y={cy} textAnchor="middle" fill={channelColor(i.username)} fontSize={11}>
                      {kindGlyph(i.kind)}
                    </text>
                  </g>
                );
              })}
            <text x={6} y={14} fill="#8FA0B8" fontSize={10}>
              {num(geometry.yMax, 0)}
            </text>
            <text x={6} y={H - 22} fill="#8FA0B8" fontSize={10}>
              {num(geometry.yMin, 0)}
            </text>
          </svg>
        ) : (
          <p className="px-3 py-6 text-center text-2xs text-dim">
            {loading
              ? `loading ${day}…`
              : `No intraday price series is available for ${day}. In demo mode only daily bars exist, so the timeline would have nothing honest to draw.`}
          </p>
        )}
      </Panel>

      <div className="flex flex-wrap items-center gap-2">
        {allChannels.map((name) => {
          const on = !Object.keys(channels).length || channels[name];
          return (
            <button
              key={name}
              type="button"
              className="chip"
              style={{ borderColor: on ? channelColor(name) : "#1E2635", color: on ? channelColor(name) : "#8FA0B8" }}
              onClick={() =>
                setChannels((prev) => {
                  const next = { ...prev };
                  if (!Object.keys(prev).length) {
                    allChannels.forEach((n) => (next[n] = n === name));
                  } else {
                    next[name] = !prev[name];
                  }
                  return next;
                })
              }
            >
              {name}
            </button>
          );
        })}
        {Object.keys(channels).length > 0 && (
          <button type="button" className="chip" onClick={() => setChannels({})}>
            show all
          </button>
        )}
      </div>

      {selected && <Story item={selected} onClose={() => setSelected(null)} />}

      <Panel title="Signals of the day" subtitle="click a row for the raw Persian text and its extraction" bodyClass="p-0">
        <div className="max-h-[46vh] overflow-auto">
          <table className="w-full text-left text-2xs">
            <thead className="sticky top-0 bg-panel text-dim">
              <tr>
                <th className="px-3 py-2 font-medium">time (UTC)</th>
                <th className="px-3 py-2 font-medium">tehran</th>
                <th className="px-3 py-2 font-medium">channel</th>
                <th className="px-3 py-2 font-medium">kind</th>
                <th className="px-3 py-2 font-medium">dir</th>
                <th className="px-3 py-2 font-medium">int</th>
                <th className="px-3 py-2 font-medium">english extraction</th>
              </tr>
            </thead>
            <tbody>
              {items
                .slice()
                .reverse()
                .map((i) => (
                  <tr
                    key={i.id}
                    className="cursor-pointer border-t border-line/60 hover:bg-panel2"
                    onClick={() => setSelected(i)}
                  >
                    <td className="whitespace-nowrap px-3 py-1.5 tabular text-dim">{timeOf(i.published_at)}</td>
                    <td className="whitespace-nowrap px-3 py-1.5 tabular text-dim">{tehran(i.published_at)}</td>
                    <td className="whitespace-nowrap px-3 py-1.5" style={{ color: channelColor(i.username) }}>
                      {i.username}
                    </td>
                    <td className="whitespace-nowrap px-3 py-1.5 text-dim">{i.kind ?? "—"}</td>
                    <td className="whitespace-nowrap px-3 py-1.5 text-dim">{i.direction ?? "—"}</td>
                    <td className="whitespace-nowrap px-3 py-1.5 tabular text-dim">{i.intensity ?? "—"}</td>
                    <td className="max-w-[52ch] truncate px-3 py-1.5 text-ink">{i.english_text ?? "—"}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </>
  );
}
