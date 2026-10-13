"""Synthetic Persian channel stream (DEMO ONLY).

Behavioural archetypes are baked in so the statistics module has something true to find:

  * `hojoom_tracker`      — genuinely LEADS the domestic market (direction drawn from the
                            *future* 45-minute USD move)
  * `tala_bazaar_news`    — news channel correlated with the ounce, sometimes leading
  * `coin_watch`          — LAGS: direction repeated from the *trailing* 60 minutes
  * `tabloid_gold`        — PUMPER: near-always up, quotes 1.5–3.5% above the real price,
                            bursts of messages within minutes
  * `ons_analyst`, `nim_seke_channel`, `sanad_funds` — noise / weak
  * `silent_archive`      — fewer than 20 events (drives the `insufficient` label)
  * `moteghafer_old`      — inactive channel, kept for the UI toggle test

Persian numerals are used in roughly a third of the messages on purpose: the extraction
prompt must normalise them and divide rial by 10.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from ..timez import TEHRAN, local_ts
from .market import DemoMarket

PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"

CHANNELS: list[dict] = [
    {"username": "hojoom_tracker", "title": "ردیاب هجوم بازار", "tier": "candidate_lead", "active": True},
    {"username": "tala_bazaar_news", "title": "اخبار بازار طلا", "tier": "news", "active": True},
    {"username": "coin_watch", "title": "دیده‌بان سکه", "tier": "candidate_lag", "active": True},
    {"username": "tabloid_gold", "title": "طلا ۲۴", "tier": "suspected_pump", "active": True},
    {"username": "ons_analyst", "title": "تحلیل انس", "tier": "opinion", "active": True},
    {"username": "nim_seke_channel", "title": "نیم‌سکه و ربع", "tier": "opinion", "active": True},
    {"username": "sanad_funds", "title": "صندوق‌های طلا", "tier": "funds", "active": True},
    {"username": "silent_archive", "title": "آرشیو کم‌فعالیت", "tier": "low_activity", "active": True},
    {"username": "moteghafer_old", "title": "کانال قدیمی غیرفعال", "tier": "inactive", "active": False},
]

UP_TEXTS = [
    "فوری: انس طلا با جهش به بالای {ounce} دلار رسید، بازار جهانی سبز است",
    "آبشده ۱۸ عیار الان در بازار {price} تومان معامله می‌شود، تقاضا بالا رفته",
    "شنیده‌ها از صرافی‌ها: تقاضای خرید ارز زیاد شده، دلار آزاد دارد به سمت {usd} حرکت می‌کند",
    "سکه امامی در حال رشد است، حباب هم بزرگ‌تر شده. نیم‌سکه و ربع‌سکه هم گران شدند",
    "طلای جهانی مثبت است و انتظار داریم بازار داخلی هم امروز بالا برود",
    "دلار نیما ثابت است ولی حواله آزاد گرم شده؛ صندوق‌های طلا (سند) هم مثبت شدند",
]
DOWN_TEXTS = [
    "فشار فروش در بازار زیاد شده، آبشده دارد پایین می‌آید",
    "انس جهانی منفی شد، احتمالاً بازار داخلی هم اصلاح می‌کند",
    "حباب سکه در حال تخلیه است؛ بازار فروشنده است، سکه کامل ارزان شد",
    "شنیده‌ها از صرافی: خریدار نیست، قیمت دلار آزاد کمی افتاد",
    "بازار امروز سنگین است و صف فروش در صندوق‌های طلا داریم",
]
FLAT_TEXTS = [
    "بازار امروز بی‌رمق است، احتمالاً رنج می‌مانیم",
    "معاملات کم‌حجم است؛ صفحه بازار تغییری ندارد",
    "آبشده تقریباً ثابت است، فاصله خرید و فروش زیاد شده",
]
RUMOR_TEXTS = [
    "شنیده‌ایم تا ساعتی دیگر خبری درباره نرخ ارز منتشر می‌شود، آماده باشید",
    "شنیده‌ها: یک صرافی بزرگ دارد ذخیره طلا جمع می‌کند",
]
PUMP_TEXTS = [
    "سکه امامی فردا {coin} تومان! عجله کنید بازار می‌ترکد",
    "فرصت را از دست ندهید، آبشده به {price} می‌رسد. خرید را جدی بگیرید",
    "همین حالا بخرید؛ حباب سکه با این قیمت‌ها تازه شروع شده",
]
OPINION_TEXTS = [
    "به نظرم امروز بازار رنج می‌زند و بهتر است صبر کنیم",
    "تحلیل شخصی: اگر انس بالای {ounce} بماند، طلا داخلی رشد می‌کند",
]

PRICE_TEMPLATE_TOMAN = "آبشده ۱۸ عیار: {price} تومان — صرافی {shop}"
PRICE_TEMPLATE_RIAL = "قیمت لحظه‌ای سکه امامی: {price} ریال"


@dataclass
class GeneratedMessage:
    username: str
    telegram_msg_id: int
    published_at: datetime
    ingested_at: datetime
    raw_text: str
    version: int = 1
    is_edited: bool = False
    edited_at: datetime | None = None


def _fmt_digits(value: str, use_persian: random.Random) -> str:
    if use_persian.random() < 0.35:
        return "".join(PERSIAN_DIGITS[int(c)] if c.isdigit() else c for c in value)
    return value


def _group(n: float, use_persian: random.Random) -> str:
    return _fmt_digits(f"{int(n):,}", use_persian)


def generate(market: DemoMarket, *, seed: int | None = None, max_days: int | None = None) -> list[GeneratedMessage]:
    rng = random.Random((seed if seed is not None else market.seed) + 991)
    out: list[GeneratedMessage] = []
    counter: dict[str, int] = {c["username"]: 1000 for c in CHANNELS}
    days = market.trading_days[-max_days:] if max_days else market.trading_days

    for day in days:
        idx = market.trading_days.index(day)
        prev_gold = market.gold_close[market.trading_days[max(0, idx - 1)]]
        for slug, plan in _daily_plan(market, day).items():
            for minute, payload in plan:
                counter[slug] += 1
                text = _render(slug, payload, market, day, minute, prev_gold, rng)
                published = local_ts(day, datetime.min.time().replace(hour=8), TEHRAN) + timedelta(minutes=minute)
                # Backfill latency: our worker saw the message minutes-to-hours later, but the
                # event time stays Telegram's own date (R3).
                latency = timedelta(minutes=rng.randint(1, 25), seconds=rng.randint(0, 59))
                out.append(
                    GeneratedMessage(
                        username=slug,
                        telegram_msg_id=counter[slug],
                        published_at=published,
                        ingested_at=published + latency,
                        raw_text=text,
                    )
                )
    out.sort(key=lambda m: m.published_at)
    return out


def generate_edits(messages: list[GeneratedMessage], *, share: float = 0.02, seed: int | None = None) -> list[GeneratedMessage]:
    """A few messages get edited later — the edit must NOT rewrite event time (T2/T6)."""
    rng = random.Random((seed if seed is not None else 20260401) + 17)
    edits: list[GeneratedMessage] = []
    for msg in messages:
        if rng.random() >= share:
            continue
        edited_at = msg.published_at + timedelta(minutes=rng.randint(20, 240))
        edits.append(
            GeneratedMessage(
                username=msg.username,
                telegram_msg_id=msg.telegram_msg_id,
                published_at=msg.published_at,  # unchanged!
                ingested_at=edited_at + timedelta(seconds=30),
                raw_text=msg.raw_text + "\n\n— ویرایش: قیمت به‌روز شد",
                version=msg.version + 1,
                is_edited=True,
                edited_at=edited_at,
            )
        )
    return edits


def _daily_plan(market: DemoMarket, day: date) -> dict[str, list[tuple[int, dict]]]:
    """Where each channel "speaks" during the session, and with what bias."""
    import math

    rng = random.Random(hash((market.seed, day.toordinal())) & 0xFFFFFFFF)
    plan: dict[str, list[tuple[int, dict]]] = {c["username"]: [] for c in CHANNELS}

    # --- hojoom_tracker: LEADS. Direction from the future 45m USD return.
    for _ in range(rng.randint(10, 18)):
        minute = rng.randint(30, 800)
        fwd = market.usd_forward_return(day, minute, 30)
        edge = min(0.95, max(0.05, 0.5 + 45.0 * fwd + rng.gauss(0, 0.16)))
        direction = "up" if rng.random() < edge else "down"
        plan["hojoom_tracker"].append(
            (minute, {"direction": direction, "kind": "price_quote", "intensity": rng.choice([3, 4, 4, 5])})
        )

    # --- tala_bazaar_news: reacts to the ounce path around the session.
    for _ in range(rng.randint(6, 12)):
        minute = rng.randint(0, 830)
        hour = min(23, (minute // 60) + 3)
        ounce_move = (market.ox_hourly[day][hour] / market.ox_opens[day]) - 1.0
        edge = min(0.9, max(0.1, 0.5 + 55.0 * ounce_move + rng.gauss(0, 0.18)))
        direction = "up" if rng.random() < edge else "down"
        plan["tala_bazaar_news"].append((minute, {"direction": direction, "kind": "news", "intensity": rng.choice([3, 4, 5])}))

    # --- coin_watch: LAGS. Direction from the trailing 60m USD return.
    for _ in range(rng.randint(8, 16)):
        minute = rng.randint(60, 838)
        trail = market.usd_trailing_return(day, minute, 60)
        edge = min(0.9, max(0.1, 0.5 + 18.0 * trail + rng.gauss(0, 0.12)))
        direction = "up" if rng.random() < edge else "down"
        plan["coin_watch"].append((minute, {"direction": direction, "kind": "opinion", "intensity": rng.choice([2, 3, 4])}))

    # --- tabloid_gold: PUMPER. Bursts, almost always up, quotes above real price.
    for _ in range(rng.randint(2, 4)):
        base = rng.randint(60, 780)
        for burst in range(rng.randint(2, 4)):
            minute = min(839, base + burst * rng.randint(3, 12))
            plan["tabloid_gold"].append(
                (minute, {"direction": "up", "kind": "price_quote", "intensity": 5, "quote_bias": rng.uniform(0.015, 0.035)})
            )

    # --- ons_analyst / nim_seke / sanad_funds: noise-ish chatter.
    for slug, count in (("ons_analyst", (4, 10)), ("nim_seke_channel", (3, 8)), ("sanad_funds", (2, 6))):
        for _ in range(rng.randint(*count)):
            minute = rng.randint(30, 830)
            direction = rng.choice(["up", "down", "flat", "none"])
            kind = rng.choice(["opinion", "news", "rumor", "opinion"])
            plan[slug].append((minute, {"direction": direction, "kind": kind, "intensity": rng.randint(1, 3)}))

    # --- silent_archive: rare.
    if rng.random() < 0.055:
        plan["silent_archive"].append((rng.randint(60, 800), {"direction": rng.choice(["up", "down"]),
                                                              "kind": "opinion", "intensity": 2}))
    # --- inactive channel still "spoke" historically.
    if rng.random() < 0.25:
        plan["moteghafer_old"].append((rng.randint(60, 800), {"direction": rng.choice(["up", "down"]),
                                                              "kind": "rumor", "intensity": 2}))
    _ = math
    return plan


def _render(
    slug: str, payload: dict, market: DemoMarket, day: date, minute: int, prev_gold: float, rng: random.Random
) -> str:
    direction = payload.get("direction", "none")
    kind = payload.get("kind", "opinion")
    real = market.real_gold_at(day, min(minute, 839))
    quote_bias = payload.get("quote_bias", rng.gauss(0.0, 0.004))
    quoted = round(real * (1 + quote_bias) / 1000.0) * 1000.0
    usd = market.usd_paths[day][min(minute, 839)] * (1 + rng.gauss(0, 0.003))
    ounce = market.ox_hourly[day][min(23, (minute // 60) + 3)]

    pool = {
        "up": UP_TEXTS,
        "down": DOWN_TEXTS,
        "flat": FLAT_TEXTS,
        "none": OPINION_TEXTS,
    }[direction]
    template = rng.choice(pool)
    if kind == "rumor":
        template = rng.choice(RUMOR_TEXTS)
    if slug == "tabloid_gold":
        template = rng.choice(PUMP_TEXTS)
    text = template.format(
        price=_group(quoted, rng),
        coin=_group(quoted * 9.4, rng),
        usd=_group(usd, rng),
        ounce=f"{ounce:,.0f}",
        shop=rng.choice(["میدان", "فردوسی", "ولیعصر", "بازار"]),
    )
    if kind == "price_quote" and slug in ("coin_watch", "tabloid_gold", "hojoom_tracker"):
        text = f"{text} — {PRICE_TEMPLATE_TOMAN.format(price=_group(quoted, rng), shop='میدان')}"
    if slug == "sanad_funds" and rng.random() < 0.4:
        text = "صندوق طلا (سند) امروز مثبت است؛ NAV و حباب صندوق‌ها را چک کنید"
    if slug == "nim_seke_channel" and rng.random() < 0.5:
        text = f"نیم‌سکه {_group(quoted * 5.0, rng)} و ربع‌سکه {_group(quoted * 2.7, rng)} تومان معامله شد"
    return f"[demo] {text}"
