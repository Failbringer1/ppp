"""Extract ad-manager contacts from a channel description or pinned post."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .text import TME_RE, USERNAME_RE

AD_CONTEXT = re.compile(
    r"(реклам|reklam|рекл\.|ads\b|\bad\b|по вопросам|сотруднич|менеджер|manager|admin|админ|"
    r"murojaat|aloqa|bog['ʻ’]?lanish|hamkorlik|homiylik|taklif|прайс|price|narx|связь|contact|"
    r"мурожаат|алоқа|реклама бўйича|ҳамкорлик)",
    re.IGNORECASE,
)
PHONE_RE = re.compile(r"(?<!\d)(?:\+?998|8)[\s\-()]*(\d{2})[\s\-()]*(\d{3})[\s\-]*(\d{2})[\s\-]*(\d{2})(?!\d)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


@dataclass
class FoundContact:
    kind: str       # username | invite | phone | email
    value: str      # normalized
    ad_context: bool
    position: int

    @property
    def priority(self) -> int:
        base = {"username": 3, "invite": 1, "phone": 2, "email": 1}[self.kind]
        return base + (5 if self.ad_context else 0)


def _context(text: str, start: int, end: int) -> bool:
    """Ad keywords on the contact's own line, or on the line above when the
    contact sits alone on its line ("Реклама:\n@manager")."""
    ls = text.rfind("\n", 0, start) + 1
    le = text.find("\n", end)
    line = text[ls: le if le != -1 else len(text)]
    if AD_CONTEXT.search(line):
        return True
    if len(line.strip()) - (end - start) <= 6 and ls > 0:
        prev = text[text.rfind("\n", 0, ls - 1) + 1: ls - 1]
        return bool(AD_CONTEXT.search(prev))
    return False


def extract_contacts(text: str, own_username: str | None = None) -> list[FoundContact]:
    text = text or ""
    own = (own_username or "").lower()
    found: dict[tuple[str, str], FoundContact] = {}

    def add(kind: str, value: str, start: int, end: int) -> None:
        key = (kind, value)
        ctx = _context(text, start, end)
        if key in found:
            found[key].ad_context = found[key].ad_context or ctx
        else:
            found[key] = FoundContact(kind, value, ctx, start)

    for m in TME_RE.finditer(text):
        path = m.group(1)
        if path.startswith("+") or path.lower().startswith("joinchat/"):
            add("invite", path.split("/")[-1].lstrip("+"), m.start(), m.end())
        elif path.lower() != own:
            add("username", path.lower(), m.start(), m.end())
    for m in USERNAME_RE.finditer(text):
        u = m.group(1).lower()
        if u != own:
            add("username", u, m.start(), m.end())
    for m in EMAIL_RE.finditer(text):
        add("email", m.group(0).lower(), m.start(), m.end())
    for m in PHONE_RE.finditer(text):
        add("phone", "+998" + "".join(m.groups()), m.start(), m.end())

    # e-mails also match the @username regex on their domain part; drop those
    emails = [c.value for c in found.values() if c.kind == "email"]
    for key in list(found):
        if key[0] == "username" and any("@" + key[1] in e for e in emails):
            del found[key]
    return sorted(found.values(), key=lambda c: (-c.priority, c.position))
