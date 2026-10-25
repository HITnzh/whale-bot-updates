"""Parse parenthetical tone hints for /说 and strip them from spoken text."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .entry import EmotionEntry, EntryManager

# Leading paren forms: （…） (…) 【…】 〔…〕
_LEADING_PAREN_RE = re.compile(
    r"^\s*([（(【〔])"
    r"(.*?)"
    r"([）)】〕])"
    r"\s*"
)

# Soft-clean common Chinese instruction wrappers around an emotion word.
_WRAPPER_RE = re.compile(
    r"^(?:请|麻烦)?(?:用|以|换成|改成|使用)?"
    r"(?P<core>.+?)"
    r"(?:的?(?:语气|口吻|腔调|声调|声音|情绪|情感))?"
    r"(?:来说|来说话|说|念|读)?$"
)


def extract_tone_hint(text: str) -> tuple[str | None, str]:
    """
    If text starts with a parenthetical, return (hint, remainder).
    Otherwise return (None, original text stripped of outer whitespace only when
    a hint was found; without a hint, return text unchanged aside from no strip).
    """
    if not text:
        return None, text

    m = _LEADING_PAREN_RE.match(text)
    if not m:
        return None, text

    open_ch, inner, close_ch = m.group(1), m.group(2), m.group(3)
    pairs = {"（": "）", "(": ")", "【": "】", "〔": "〕"}
    if pairs.get(open_ch) != close_ch:
        return None, text

    hint = (inner or "").strip()
    remainder = text[m.end() :]
    # Keep empty remainder as "" so caller can decide; strip only ends of speak text.
    speak = remainder.strip() if remainder is not None else ""
    if not hint:
        # Empty paren: still strip it so it is not spoken.
        return None, speak
    return hint, speak


def simplify_tone_hint(hint: str) -> list[str]:
    """
    Produce candidate strings to match against entry names/keywords.
    Order: full hint, wrapper-stripped core, then individual tokens.
    """
    hint = (hint or "").strip()
    if not hint:
        return []

    candidates: list[str] = []
    seen: set[str] = set()

    def add(s: str) -> None:
        s = s.strip()
        if not s or s in seen:
            return
        seen.add(s)
        candidates.append(s)

    add(hint)

    m = _WRAPPER_RE.match(hint)
    if m:
        add(m.group("core"))

    # Also split on common separators / whitespace.
    for part in re.split(r"[\s,，、/|]+", hint):
        add(part)
        m2 = _WRAPPER_RE.match(part)
        if m2:
            add(m2.group("core"))

    return candidates


def match_tone_to_entry(
    entry_mgr: "EntryManager", hint: str
) -> "EmotionEntry | None":
    """
    Match a tone hint against emotion entries.

    Priority:
    1. Exact entry name equals a candidate
    2. Entry name appears inside a candidate (or vice versa for short names)
    3. Any entry keyword appears in a candidate / candidate equals keyword
    4. Fallback: EntryManager.match_entry(hint) keyword-in-message scan
    """
    if not hint:
        return None

    candidates = simplify_tone_hint(hint)
    entries = list(entry_mgr.entries)

    # 1) Exact name
    for cand in candidates:
        entry = entry_mgr.get_entry(cand)
        if entry is not None:
            return entry

    # 2) Name contained in candidate (e.g. "用开心的语气说" → name 开心)
    # Prefer longer names first to reduce accidental short hits.
    by_name_len = sorted(entries, key=lambda e: len(e.name or ""), reverse=True)
    for cand in candidates:
        for entry in by_name_len:
            name = entry.name or ""
            if not name:
                continue
            if name in cand or cand == name:
                return entry

    # 3) Keyword match against candidates (keyword in cand or cand == keyword)
    # Longer keywords first.
    scored: list[tuple[int, object]] = []
    for entry in entries:
        for kw in entry.keywords or []:
            if not kw:
                continue
            for cand in candidates:
                if kw == cand or kw in cand or (len(cand) >= 2 and cand in kw):
                    scored.append((len(kw), entry))
                    break
    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]  # type: ignore[return-value]

    # 4) Original keyword-in-message behaviour on the raw hint
    return entry_mgr.match_entry(hint)
