"""Parsing TGStat HTML pages (ratings / catalog lists and channel stat pages).

Markup on tgstat changes from time to time, so the parsers avoid CSS classes:
channels are found by their link format (/channel/@name) and numbers by the
words next to them ("подписчиков", "средний охват", "ERR", ...).
"""
from __future__ import annotations

import html as htmllib
import re
from dataclasses import asdict, dataclass, field

CHANNEL_HREF_RE = re.compile(
    r"""href=["'](?:https?://(?:[a-z]{2}\.)?tgstat\.(?:ru|com))?(?:/[a-z]{2})?/channel/"""
    r"""(@[A-Za-z0-9_]{4,32}|[A-Za-z0-9_-]{10,64})(?:/[a-z]+)?/?["']""",
    re.IGNORECASE,
)
ANCHOR_RE = re.compile(r"<a\b[^>]*>(.*?)</a>", re.S | re.I)
_DROP_RE = re.compile(r"<(script|style|noscript|svg)\b.*?</\1>", re.S | re.I)
_BLOCK_RE = re.compile(r"<\s*(?:br|/?(?:div|p|li|ul|ol|tr|td|th|table|h[1-6]|section|article|header|footer)\b)[^>]*>",
                       re.I)
_TAG_RE = re.compile(r"<[^>]+>")
NUM_RE = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:[.,]\d+)?|\d{1,3}(?:,\d{3})+(?![\d.])|\d+(?:[.,]\d+)?)\s*"
    r"(k\b|к\b|тыс\.?|m\b|млн\.?|%)?",
    re.IGNORECASE,
)
CHALLENGE_MARKERS = ("Just a moment", "cf-chl", "challenge-platform", "Checking your browser",
                     "Attention Required! | Cloudflare")

LABELS = {
    "subscribers": r"подписчик|subscribers|obunachi|участник",
    "avg_reach": r"средний охват|охват 1 публикации|охват публикации|average post reach|post reach|o['ʻ’]?rtacha qamrov",
    "daily_reach": r"дневной охват|daily reach|kunlik qamrov",
    "err": r"\bERR\b|вовлеч[её]нност|engagement rate",
    "ci": r"индекс цитирования|citation index|\bИЦ\b",
    "mentions": r"упоминани|mentions|eslatm",
    "forwards": r"репост|пересыл|forwards|shares",
}
WARNING_RE = re.compile(r"(накрут|подозрени|scam|fake|мошенни|фейк)", re.I)


def is_challenge(page: str) -> bool:
    return any(m in page for m in CHALLENGE_MARKERS)


def to_text(page: str) -> str:
    page = _DROP_RE.sub(" ", page)
    page = _BLOCK_RE.sub("\n", page)
    page = _TAG_RE.sub(" ", page)
    page = htmllib.unescape(page)
    lines = [re.sub(r"[ \t\u00a0\u202f]+", " ", ln).strip() for ln in page.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def parse_number(s: str) -> float | None:
    m = NUM_RE.search(s or "")
    if not m:
        return None
    raw, suffix = m.group(1), (m.group(2) or "").lower().rstrip(".")
    raw = re.sub(r"[ \u00a0\u202f]", "", raw)
    raw = raw.replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+", raw) else raw.replace(",", ".")
    try:
        val = float(raw)
    except ValueError:
        return None
    if suffix in ("k", "к", "тыс"):
        val *= 1_000
    elif suffix in ("m", "млн"):
        val *= 1_000_000
    elif suffix == "%":
        val /= 100
    return val


def _pure_number(line: str) -> float | None:
    """Value of a line that is (almost) only a number, e.g. '12 345' or '4.5%'."""
    m = NUM_RE.search(line)
    if not m:
        return None
    rest = (line[:m.start()] + line[m.end():]).strip(" +-~≈")
    return parse_number(line) if len(rest) <= 2 else None


@dataclass
class ListEntry:
    ref: str                  # "@username" or private id
    title: str = ""
    subscribers: float | None = None


def parse_list(page: str) -> list[ListEntry]:
    """Channels of a ratings / catalog / search page in page order."""
    matches = list(CHANNEL_HREF_RE.finditer(page))
    entries: dict[str, ListEntry] = {}
    order: list[str] = []
    for i, m in enumerate(matches):
        ref = m.group(1)
        key = ref.lower()
        nxt = next((x.start() for x in matches[i + 1:] if x.group(1).lower() != key), len(page))
        seg = page[m.start(): min(nxt, m.start() + 6000)]
        e = entries.get(key)
        if e is None:
            e = entries[key] = ListEntry(ref=ref if not ref.startswith("@") else "@" + ref[1:].lower())
            order.append(key)
        if not e.title:
            for a in ANCHOR_RE.finditer(page[max(0, m.start() - 5): m.start() + 2000]):
                href = CHANNEL_HREF_RE.search(a.group(0))
                if href and href.group(1).lower() == key:
                    # anchor text may also hold a rank number and the category: take the first wordy line
                    words = [ln for ln in to_text(a.group(1)).splitlines()
                             if re.search(r"[^\W\d_]", ln) and not ln.startswith("@")]
                    if words:
                        e.title = words[0][:200]
                        break
        if e.subscribers is None:
            lines = to_text(seg).splitlines()
            rx = re.compile(LABELS["subscribers"], re.I)
            for i, line in enumerate(lines):
                if rx.search(line) and len(line) < 60:
                    e.subscribers = _value_near(lines, i)
                    if e.subscribers is not None:
                        break
    return [entries[k] for k in order]


@dataclass
class ChannelStats:
    ref: str | None = None
    title: str = ""
    subscribers: float | None = None
    avg_reach: float | None = None
    daily_reach: float | None = None
    err: float | None = None
    ci: float | None = None
    mentions: float | None = None
    forwards: float | None = None
    category: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)

    @property
    def found(self) -> int:
        return sum(v is not None for v in (self.subscribers, self.avg_reach, self.err, self.ci))


def _value_near(lines: list[str], idx: int, percent: bool = False) -> float | None:
    """Number for the label on lines[idx]: a number-only line above or below, else on the same line."""
    def ok(v: float | None, line: str) -> bool:
        return v is not None and (not percent or "%" in line)

    for j in (idx - 1, idx + 1):
        if 0 <= j < len(lines):
            v = _pure_number(lines[j])
            if ok(v, lines[j]):
                return v
    line = lines[idx]
    v = parse_number(line)
    if ok(v, line):
        return v
    if percent:  # ERR is sometimes shown without '%'
        for j in (idx - 1, idx + 1):
            if 0 <= j < len(lines) and _pure_number(lines[j]) is not None:
                v = _pure_number(lines[j])
                return v / 100 if v and v > 1 else v
    return None


def parse_channel_page(page: str) -> ChannelStats:
    st = ChannelStats()
    t = re.search(r"<title>(.*?)</title>", page, re.S | re.I)
    if t:
        title = htmllib.unescape(t.group(1)).strip()
        st.title = re.split(r"\s+[—–]\s+", title)[0].strip()[:200]
        um = re.search(r"@([A-Za-z0-9_]{4,32})", title)
        if um:
            st.ref = "@" + um.group(1).lower()
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S | re.I)
    if h1 and to_text(h1.group(1)):
        st.title = to_text(h1.group(1)).splitlines()[0][:200]
    if st.ref is None:
        m = CHANNEL_HREF_RE.search(page)
        if m:
            st.ref = m.group(1).lower() if m.group(1).startswith("@") else m.group(1)
    lines = to_text(page).splitlines()
    for key, pattern in LABELS.items():
        rx = re.compile(pattern, re.I)
        for i, line in enumerate(lines):
            if rx.search(line) and len(line) < 80:
                v = _value_near(lines, i, percent=(key == "err"))
                if v is not None:
                    setattr(st, key, v)
                    break
    for i, line in enumerate(lines):
        if re.fullmatch(r"(категория|category|kategoriya):?", line, re.I) and i + 1 < len(lines):
            st.category = lines[i + 1][:64]
            break
        m = re.match(r"(?:категория|category|kategoriya):\s*(.+)", line, re.I)
        if m:
            st.category = m.group(1)[:64]
            break
    st.warnings = [ln[:200] for ln in lines if WARNING_RE.search(ln) and len(ln) < 200][:3]
    return st


# Category slugs of the TGStat catalog (the same on all regional sites).
CATEGORIES = [
    ("", "все категории"), ("news", "Новости и СМИ"), ("blogs", "Блоги"), ("entertainment", "Юмор и развлечения"),
    ("video", "Видео и фильмы"), ("music", "Музыка"), ("sport", "Спорт"), ("education", "Образование"),
    ("career", "Карьера"), ("business", "Бизнес и стартапы"), ("economics", "Экономика"), ("crypto", "Криптовалюты"),
    ("tech", "Технологии"), ("marketing", "Маркетинг, PR, реклама"), ("psychology", "Психология"),
    ("health", "Здоровье и фитнес"), ("medicine", "Медицина"), ("fashion", "Мода и красота"), ("food", "Еда и кулинария"),
    ("family", "Семья и дети"), ("travels", "Путешествия"), ("religion", "Религия"), ("sales", "Продажи"),
    ("transport", "Транспорт"), ("games", "Игры"), ("apps", "Софт и приложения"), ("quotes", "Цитаты"),
    ("edutainment", "Познавательное"), ("other", "Другое"),
]
