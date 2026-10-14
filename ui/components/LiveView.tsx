"use client";

import React from "react";
import { Chip, KeyValue, Panel, Stat } from "./Bits";
import type { Bundle, Tile } from "@/lib/types";
import { ageLabel, int, num, stamp, tehran } from "@/lib/format";

function TileCard({ tile }: { tile: Tile }) {
  const tone = tile.status === "ok" ? "text-ink" : tile.status === "stale" ? "text-amber" : "text-dim";
  const chip = tile.status === "ok" ? "ok" : tile.status === "stale" ? "warn" : "default";
  return (
    <div className="panel px-4 py-3">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="text-2xs uppercase tracking-wider text-dim">{tile.label}</div>
          <div className={`mt-1 text-xl font-semibold tabular ${tone}`}>
            {tile.price === null ? "no data" : num(tile.price, 0)}
          </div>
          <div className="text-2xs text-dim">{tile.unit}</div>
        </div>
        <Chip tone={chip as never} title={tile.ts ? `bar ${stamp(tile.ts)}` : undefined}>
          {tile.status === "ok" ? ageLabel(tile.age_minutes) : tile.status}
        </Chip>
      </div>
      <div className="mt-2 flex items-center justify-between text-2xs text-dim">
        <span>
          {tile.source ?? "—"} · {tile.resolution ?? "—"}
        </span>
        {tile.ts && <span title="Tehran time">{tehran(tile.ts)} Tehran</span>}
      </div>
    </div>
  );
}

export function LiveView({ bundle }: { bundle: Bundle }) {
  const { live, meta, leaderboard } = bundle;
  const stale = live.tiles.filter((t) => t.status !== "ok").length;
  const leads = leaderboard.channels.filter((c) => c.label === "lead");
  const laggers = leaderboard.channels.filter((c) => c.label === "lag");

  return (
    <>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {live.tiles.map((tile) => (
          <TileCard key={tile.key} tile={tile} />
        ))}
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Panel
          title="Freshness"
          subtitle={`tiles turn amber past ${live.stale_after_minutes} minutes of no new bar`}
          right={<Chip tone={stale ? "warn" : "ok"}>{stale ? `${stale} stale` : "all fresh"}</Chip>}
        >
          <KeyValue
            items={[
              ["bundle as-of", stamp(live.as_of)],
              ["tehran clock", `${tehran(live.as_of)}`],
              ["series tracked", int(meta.series.length)],
              ["demo mode", meta.demo ? "yes — synthetic" : "no"],
            ]}
          />
          <p className="mt-3 text-2xs text-dim">
            The amber state is the point: a tile older than three minutes is not tradeable information, and the UI says so
            instead of showing a number that looks live.
          </p>
        </Panel>

        <Panel title="Paper book" subtitle="Phase 1 never places a real order (PAPER_ONLY=true)">
          {live.open_position ? (
            <KeyValue
              items={Object.entries(live.open_position).map(([k, v]) => [k, String(v)] as [string, React.ReactNode])}
            />
          ) : (
            <p className="text-2xs text-dim">No open paper position right now.</p>
          )}
          <div className="mt-3">
            <Stat
              label="last paper equity"
              value={live.last_equity ? num(Number(live.last_equity.equity_toman), 0) : "—"}
              sub={live.last_equity ? `toman · ${stamp(String(live.last_equity.ts))}` : "no equity row yet"}
            />
          </div>
        </Panel>

        <Panel title="What is speaking right now" subtitle={`leaderboard as-of ${leaderboard.asof_date ?? "—"}`}>
          <div className="space-y-3">
            <div>
              <div className="text-2xs uppercase tracking-wider text-dim">leads</div>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {leads.length ? (
                  leads.map((c) => (
                    <Chip key={c.username} tone="ok" title={c.label_reason ?? undefined}>
                      {c.username} {c.lead_lag_minutes ?? ""}m
                    </Chip>
                  ))
                ) : (
                  <span className="text-2xs text-dim">none</span>
                )}
              </div>
            </div>
            <div>
              <div className="text-2xs uppercase tracking-wider text-dim">lags</div>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {laggers.length ? (
                  laggers.map((c) => (
                    <Chip key={c.username} tone="info">
                      {c.username} +{c.lead_lag_minutes ?? "?"}m
                    </Chip>
                  ))
                ) : (
                  <span className="text-2xs text-dim">none</span>
                )}
              </div>
            </div>
          </div>
          <p className="mt-3 text-2xs text-dim">
            Labels are net-of-cost, FDR-corrected at q={leaderboard.family?.q_threshold ?? 0.1} over{" "}
            {int(leaderboard.family?.size ?? 0)} pre-registered tests ({int(leaderboard.family?.significant ?? 0)} significant).
          </p>
        </Panel>
      </div>
    </>
  );
}
