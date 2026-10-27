"""Price extraction from admin replies, currency conversion, CPM."""
from __future__ import annotations

import re
from dataclasses import dataclass

AMOUNT_RE = re.compile(
    r"(?P<pre>\$)?\s*"
    r"(?P<num>\d{1,3}(?:[ \u00a0.,]\d{3})+(?![\d/])|\d+(?:[.,]\d{1,2})?(?![\d/]))"
    r"\s*(?P<mult>k\b|к\b|тыс\w*\.?|ming\b|минг\b|млн\b|mln\b|million\w*|миллион\w*)?"
    r"\s*(?P<cur>\$|usd\b|долл\w*|dollar\w*|у\.?\s?е\.?|сум\w*|so['ʻ’`]?m\b|sum\b|сўм\w*|uzs\b|руб\w*|₽|rub\b)?",
    re.IGNORECASE,
)
NOT_PRICE_AFTER = re.compile(r"^\W{0,3}(подпис|obunachi|просмотр|ko['ʻ’]?rish|views|охват|человек|kishi|мин|daq|час|soat|ч\b|h\b|:\d)", re.I)
FORMAT_RE = re.compile(r"(?<![\d/])([1-9])\s*/\s*(24|48|72|96|168)(?![\d])")
FOREVER_RE = re.compile(r"(навсегда|без удаления|не удаля|вечн|abadiy|doimiy|o['ʻ’]?chirilmaydi|o['ʻ’]?chirmasdan|forever)", re.I)
HOURS_RE = re.compile(r"(24|48|72)\s*(?:час|ч\b|soat|соат|h\b)", re.I)

MULT = {"k": 1e3, "к": 1e3, "тыс": 1e3, "ming": 1e3, "минг": 1e3, "млн": 1e6, "mln": 1e6, "million": 1e6, "миллион": 1e6}


@dataclass
class PriceOption:
    amount: float
    currency: str | None  # UZS | USD | RUB | None (unknown)
    fmt: str | None       # "1/24", "1/48", "forever", ...
    line: str


def _currency(cur: str | None, pre: str | None) -> str | None:
    if pre:
        return "USD"
    if not cur:
        return None
    c = cur.lower()
    if c.startswith(("$", "usd", "долл", "dollar", "у")):
        return "USD"
    if c.startswith(("руб", "₽", "rub")):
        return "RUB"
    return "UZS"


def _mult(m: str | None) -> float:
    if not m:
        return 1.0
    m = m.lower().rstrip(".")
    for k, v in MULT.items():
        if m.startswith(k):
            return v
    return 1.0


def detect_format(text: str) -> str | None:
    m = FORMAT_RE.search(text)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    if FOREVER_RE.search(text):
        return "forever"
    m = HOURS_RE.search(text)
    if m:
        return f"1/{m.group(1)}"
    return None


def _formats_in(line: str) -> list[tuple[int, str]]:
    found = [(m.start(), f"{m.group(1)}/{m.group(2)}") for m in FORMAT_RE.finditer(line)]
    found += [(m.start(), "forever") for m in FOREVER_RE.finditer(line)]
    found += [(m.start(), f"1/{m.group(1)}") for m in HOURS_RE.finditer(line)]
    return found


_SEP_RE = re.compile(r"[;|]|,\s|\s[—–-]{1,2}\s(?=\d+\s*/)")


def _nearest_format(line: str, formats: list[tuple[int, str]], pos: int) -> str | None:
    """Format from the same comma/semicolon-separated chunk, else the nearest one
    (a format written before the price wins a tie: "1/24 — 250k")."""
    if not formats:
        return None
    cuts = [0] + [m.end() for m in _SEP_RE.finditer(line)] + [len(line) + 1]
    lo = max(c for c in cuts if c <= pos)
    hi = min(c for c in cuts if c > pos)
    same = [f for f in formats if lo <= f[0] < hi]
    pool = same or formats
    return min(pool, key=lambda f: abs(f[0] - pos) + (0 if f[0] < pos else 3))[1]


def parse_prices(text: str) -> list[PriceOption]:
    """Find price mentions line by line; each gets the nearest ad format on its line
    ("1/24 — 250k, 1/48 — 350k" -> two options with their own formats)."""
    out: list[PriceOption] = []
    default_fmt = detect_format(text or "")
    for line in (text or "").splitlines():
        formats = _formats_in(line)
        for m in AMOUNT_RE.finditer(line):
            raw = m.group("num")
            if not raw:
                continue
            tail = line[m.end():m.end() + 20]
            if NOT_PRICE_AFTER.search(tail):
                continue
            before = line[max(0, m.start("num") - 1):m.start("num") + 1]
            if "/" in before or "+" in before:
                continue  # format "1/24" or phone
            digits = re.sub(r"[ \u00a0]", "", raw)
            if re.fullmatch(r"\d{1,3}([.,]\d{3})+", digits):
                num = float(re.sub(r"[.,]", "", digits))
            else:
                num = float(digits.replace(",", "."))
            amount = num * _mult(m.group("mult"))
            cur = _currency(m.group("cur"), m.group("pre"))
            if cur is None:
                if m.group("mult") and amount >= 1000:
                    cur = "UZS"
                elif amount >= 10_000:
                    cur = "UZS"
                else:
                    continue  # bare small number: a date, hour or count, not a price
            if cur == "UZS" and amount < 1000:
                continue
            fmt = _nearest_format(line, formats, m.start("num")) or default_fmt
            out.append(PriceOption(amount=amount, currency=cur, fmt=fmt, line=line.strip()))
    return out


def to_uzs(amount: float | None, currency: str | None, usd_uzs: float, rub_uzs: float) -> float | None:
    if amount is None:
        return None
    cur = (currency or "UZS").upper()
    if cur == "USD":
        return amount * usd_uzs
    if cur == "RUB":
        return amount * rub_uzs
    return amount


def cpm(price_uzs: float | None, views: float | None) -> float | None:
    if not price_uzs or not views:
        return None
    return price_uzs / views * 1000


def recommend(cpm_value: float | None, target_cpm: float) -> str:
    if cpm_value is None:
        return "unknown"
    if cpm_value <= target_cpm:
        return "buy"
    if cpm_value <= target_cpm * 1.6:
        return "negotiate"
    return "skip"


REC_TEXT = {"buy": "Брать", "negotiate": "Торговаться", "skip": "Дорого — пропустить", "unknown": "Нет данных"}
