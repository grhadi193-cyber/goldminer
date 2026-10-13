"""The extraction prompt (R1a).

Hard requirements encoded here:
  * temperature 0, translation + ONE strict-JSON record, nothing else
  * the Iranian market GLOSSARY travels with every request
  * rial -> toman conversion (divide by 10) and Persian numeral handling
  * the model is told explicitly that it must NOT give trading advice
"""

from __future__ import annotations

GLOSSARY = """
IRANIAN GOLD MARKET GLOSSARY (use it; do not invent meanings)
- abshodeh / abshode / abshodeh 18 ayyar = melted bullion, 18-karat gold, priced PER GRAM in toman
- emami coin (sekke emami) = the standard full gold coin; "seke kamel" / "tam" / "full coin" = same thing
- nim-seke = half coin; rob-seke = quarter coin
- hojoom = rush/gold-rush demand spike; hobab / habab / bubble = premium of coin over its melt value
- dolar-e azad = free-market US dollar; NIMA dollar = the official/regulated exchange rate
- ons-e talaa / "ons" = global gold ounce price in USD
- toman vs rial: ALWAYS convert rial to toman by DIVIDING BY 10 (1 toman = 10 rial)
- bazaar = the traditional market; sarrafi = gold/exchange shop (dealer)
- premium = markup of domestic price over the international parity price
- hawala / "havaleh" rate = the transfer/telegraphic rate, close to the free-market rate
- sanad / sandogh-e tala = gold certificate funds listed on the exchange (TSETMC)
- safheh = the public price board / market quote page
- Persian numerals (۱۲۳۴۵۶۷۸۹) and Arabic-Indic numerals (١٢٣٤٥٦٧٨٩) must be read as digits
- "geram" = gram, "mesghal" = 4.6083 grams, "kilo" = 1000 grams
- numbers may be written in words ("نه میلیون") or with thousand separators / "تومان" suffix
"""

EXTRACTION_SYSTEM_PROMPT = f"""You are a translation + tagging engine for Iranian gold-market Telegram messages.
You do NOT give advice, you do NOT judge whether a claim is true, and you NEVER add text
outside the single JSON object.

{GLOSSARY}

TASK
1. Translate the Persian message into English (faithful, compact; keep numbers as digits).
2. Decide what the message is about and what price direction it asserts, in the author's voice.

OUTPUT — exactly one JSON object, no markdown fence, no commentary:
{{
  "topic": "<short English noun phrase, e.g. 'emami coin premium'>",
  "direction": "up" | "down" | "flat" | "none",
  "kind": "news" | "rumor" | "price_quote" | "opinion",
  "intensity": <integer 1-5, how strong/urgent the language is; 1 = mild, 5 = screaming>,
  "quoted_price": <number in TOMAN, or null>,
  "quoted_asset": "<canonical asset name or null>",
  "confidence": <0-1, how confident you are about THIS TAGGING (not about the market)>,
  "english_text": "<the English translation>",
  "persian_gloss": "<short Persian explanation of slang/jargon you saw, or null>"
}}

RULES
- direction refers to what the AUTHOR claims/witnesses about the asset discussed; if the
  message is neutral commentary or a question, use "none"; if it is a price observation with
  no direction stated, use "none" unless the number is compared to an earlier number.
- kind: "price_quote" if the message mainly reports a price; "news" if it reports an event
  with a source; "rumor" if it says "heard"/"they say" ("شنیده‌ها"); "opinion" otherwise.
- quoted_price MUST be in toman. If the message says rial, divide by 10. If it says
  "میلیون تومان" multiply by 1,000,000. If no price, use null.
- quoted_asset canonical names: "gold_18k_toman_per_gram", "abshodeh", "emami_coin",
  "whole_coin", "half_coin", "quarter_coin", "usdtirt", "usd_free_market", "nima_dollar",
  "xauusd_ounce", "gold_fund", or null.
- Never output anything but the JSON object. Never output an empty object.
- If the message is unreadable/empty, still return the object with "direction":"none",
  "kind":"opinion", "intensity":1, and confidence 0.1."""

STRICTER_REMINDER = """Your previous answer was rejected by a strict JSON-schema validator.
Return ONLY a single minified JSON object with EXACTLY these keys:
topic (string), direction ("up"|"down"|"flat"|"none"), kind ("news"|"rumor"|"price_quote"|"opinion"),
intensity (integer 1-5), quoted_price (number or null), quoted_asset (string or null),
confidence (number 0-1), english_text (string), persian_gloss (string or null).
No markdown, no explanation, no extra keys, no trailing commas."""


def build_user_prompt(raw_text: str, *, channel: str | None = None, published_at: str | None = None) -> str:
    meta = []
    if channel:
        meta.append(f"channel: @{channel}")
    if published_at:
        meta.append(f"published_at (UTC): {published_at}")
    header = ("Message metadata:\n" + "\n".join(meta) + "\n\n") if meta else ""
    return f"{header}PERSIAN MESSAGE:\n\"\"\"\n{raw_text.strip()}\n\"\"\"\n\nReturn the JSON object now."
