"""Text helpers: normalization, Telegram references, similarity."""
from __future__ import annotations

import hashlib
import re

# Telegram usernames: 5-32 chars, start with a letter.
USERNAME_RE = re.compile(r"(?<![\w@])@([A-Za-z][A-Za-z0-9_]{3,31})\b")
TME_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/"
    r"(\+[A-Za-z0-9_-]{8,}|joinchat/[A-Za-z0-9_-]{8,}|[A-Za-z][A-Za-z0-9_]{3,31})(?:/(\d+))?",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)

# t.me paths that are not channels
_TME_RESERVED = {"addstickers", "addemoji", "share", "proxy", "socks", "addtheme", "setlanguage", "iv", "boost", "c"}

_WS_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)


def normalize(text: str) -> str:
    """Lowercase, strip urls/mentions/punctuation, collapse whitespace."""
    if not text:
        return ""
    text = URL_RE.sub(" ", text)
    text = TME_RE.sub(" ", text)
    text = USERNAME_RE.sub(" ", text)
    text = text.lower().replace("ё", "е").replace("ʻ", "'").replace("‘", "'").replace("’", "'")
    text = _NON_WORD_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def text_hash(text: str) -> str:
    return hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()[:16]


def shingles(text: str, k: int = 3) -> set[str]:
    """Word k-shingles of the normalized text (character 5-grams for very short texts)."""
    words = normalize(text).split()
    if len(words) < k + 2:
        joined = " ".join(words)
        return {joined[i:i + 5] for i in range(max(1, len(joined) - 4))} if joined else set()
    return {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def extract_refs(text: str, urls: list[str] | None = None) -> dict[str, set[str]]:
    """Find Telegram channel references in a post.

    Returns {"usernames": {...}, "invites": {...}}; usernames are lowercased,
    invites are the hash part of t.me/+HASH or t.me/joinchat/HASH links.
    """
    usernames: set[str] = set()
    invites: set[str] = set()
    blobs = [text or ""] + list(urls or [])
    for blob in blobs:
        for m in TME_RE.finditer(blob):
            path = m.group(1)
            if path.startswith("+"):
                invites.add(path[1:])
            elif path.lower().startswith("joinchat/"):
                invites.add(path.split("/", 1)[1])
            elif path.lower() not in _TME_RESERVED:
                usernames.add(path.lower())
        for m in USERNAME_RE.finditer(blob):
            usernames.add(m.group(1).lower())
    return {"usernames": usernames, "invites": invites}


def parse_channel_ref(ref: str) -> str | None:
    """Turn '@name', 't.me/name', 'https://t.me/+hash' into 'name' or '+hash'."""
    ref = (ref or "").strip()
    if not ref:
        return None
    m = TME_RE.search(ref)
    if m:
        path = m.group(1)
        if path.lower().startswith("joinchat/"):
            return "+" + path.split("/", 1)[1]
        return path if path.startswith("+") else path.lower()
    ref = ref.lstrip("@")
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", ref):
        return ref.lower()
    return None
