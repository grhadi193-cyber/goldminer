// Display helpers. Every number shown in the UI is formatted here and nowhere else, so
// "English labels, Latin digits, no Persian numerals" (spec §C) holds everywhere at once.

const LATIN = /[٠-٩۰-۹]/g;
const PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹";
const ARABIC_DIGITS = "٠١٢٣٤٥٦٧٨٩";

/** Defensive: any Persian/Arabic digit that sneaks in from scraped text becomes Latin. */
export function latinDigits(input: string): string {
  return input.replace(LATIN, (d) => {
    const p = PERSIAN_DIGITS.indexOf(d);
    if (p >= 0) return String(p);
    return String(ARABIC_DIGITS.indexOf(d));
  });
}

export function num(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function int(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return Math.round(value).toLocaleString("en-US");
}

export function pct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${value.toFixed(digits)}%`;
}

export function bps(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(digits)}`;
}

export function signed(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(digits)}`;
}

export function stamp(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toISOString().replace("T", " ").slice(0, 16) + "Z";
}

export function timeOf(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toISOString().slice(11, 16) + "Z";
}

export function ageLabel(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined) return "no data";
  if (minutes < 1) return `${Math.round(minutes * 60)}s ago`;
  if (minutes < 90) return `${minutes.toFixed(0)}m ago`;
  return `${(minutes / 60).toFixed(1)}h ago`;
}

/** Tehran wall clock (the trader's clock), rendered from a UTC instant. */
export function tehran(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const d = typeof value === "number" ? new Date(value) : new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Tehran",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(d);
}

export function signColor(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "text-dim";
  if (value > 0) return "text-green";
  if (value < 0) return "text-red";
  return "text-dim";
}

/** Stable per-channel colour (the timeline uses one hue per channel). */
const CHANNEL_COLORS = [
  "#22D3EE", "#34D399", "#A78BFA", "#F59E0B", "#F472B6",
  "#60A5FA", "#FBBF24", "#2DD4BF", "#FB7185", "#C084FC",
];

export function channelColor(username: string): string {
  let h = 0;
  for (let i = 0; i < username.length; i += 1) h = (h * 31 + username.charCodeAt(i)) % 100003;
  return CHANNEL_COLORS[h % CHANNEL_COLORS.length];
}

/** Spec §C marker shapes: ◆ news, ◇ rumor, ● opinion, ○ price_quote. */
export function kindGlyph(kind: string | null | undefined): string {
  switch (kind) {
    case "news":
      return "◆";
    case "rumor":
      return "◇";
    case "opinion":
      return "●";
    case "price_quote":
      return "○";
    default:
      return "·";
  }
}

export function labelChip(label: string): { text: string; cls: string } {
  switch (label) {
    case "lead":
      return { text: "lead", cls: "chip chip-ok" };
    case "lag":
      return { text: "lag", cls: "chip chip-info" };
    case "noise":
      return { text: "noise", cls: "chip" };
    default:
      return { text: "insufficient", cls: "chip chip-warn" };
  }
}
