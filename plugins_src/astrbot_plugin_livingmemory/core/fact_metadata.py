"""Per-fact subject / visibility / supersede / time helpers (offline-testable)."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

SHANGHAI = timezone(timedelta(hours=8))

VIS_PRIVATE = "private"
VIS_GROUP = "group"  # + source_session
VIS_SHARED = "shared"  # explicitly allowed to share beyond source

EVENT_PENDING = "pending"
EVENT_DONE = "done"
EVENT_CANCELLED = "cancelled"
EVENT_RESCHEDULED = "rescheduled"


@dataclass
class StructuredFact:
    content: str
    subject_id: str | None = None  # stable platform id; never nickname-only merge
    predicate: str | None = None
    value: str | None = None
    visibility: str = VIS_PRIVATE
    source_session: str | None = None
    source_message_ids: list[str] = field(default_factory=list)
    source_time: float | None = None
    valid_from: float | None = None
    valid_until: float | None = None
    supersedes_atom_id: int | None = None
    event_start: float | None = None
    event_end: float | None = None
    time_precision: str | None = None  # day | hour | range | fuzzy
    event_state: str | None = None
    event_version: int = 1
    followup_disabled: bool = False
    provenance: str = "user_stated"  # user_stated | third_party | joke | hypothesis | bot

    def to_metadata(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "predicate": self.predicate,
            "value": self.value,
            "visibility": self.visibility,
            "source_session": self.source_session,
            "source_message_ids": list(self.source_message_ids),
            "source_time": self.source_time,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "supersedes_atom_id": self.supersedes_atom_id,
            "event_start": self.event_start,
            "event_end": self.event_end,
            "time_precision": self.time_precision,
            "event_state": self.event_state,
            "event_version": self.event_version,
            "followup_disabled": self.followup_disabled,
            "provenance": self.provenance,
        }


def normalize_fact_item(
    item: Any,
    *,
    default_subject: str | None = None,
    default_visibility: str = VIS_PRIVATE,
    source_session: str | None = None,
    source_time: float | None = None,
) -> StructuredFact | None:
    """Accept legacy str key_facts or dict structured facts."""
    if item is None:
        return None
    if isinstance(item, str):
        content = item.strip()
        if not content:
            return None
        return StructuredFact(
            content=content,
            subject_id=None,  # unknown — do not invent
            visibility=default_visibility,
            source_session=source_session,
            source_time=source_time,
            provenance="user_stated",
        )
    if not isinstance(item, dict):
        return None
    content = str(item.get("content") or item.get("fact") or item.get("text") or "").strip()
    if not content:
        return None
    sub = item.get("subject_id")
    if sub is not None:
        sub = str(sub).strip() or None
    # Never treat bare nickname as subject_id
    if sub and not _looks_like_stable_id(sub):
        sub = None
    if sub is None and default_subject and _looks_like_stable_id(default_subject):
        # Only apply default when extraction explicitly said subject=speaker
        if str(item.get("subject_is_speaker") or "").lower() in ("1", "true", "yes"):
            sub = str(default_subject)

    vis = str(item.get("visibility") or default_visibility).lower()
    if vis not in (VIS_PRIVATE, VIS_GROUP, VIS_SHARED):
        vis = default_visibility

    return StructuredFact(
        content=content,
        subject_id=sub,
        predicate=(str(item["predicate"]).strip() if item.get("predicate") else None),
        value=(str(item["value"]).strip() if item.get("value") else None),
        visibility=vis,
        source_session=str(item.get("source_session") or source_session or "") or None,
        source_message_ids=[str(x) for x in (item.get("source_message_ids") or []) if x],
        source_time=_as_float(item.get("source_time"), source_time),
        valid_from=_as_float(item.get("valid_from"), None),
        valid_until=_as_float(item.get("valid_until"), None),
        supersedes_atom_id=_as_int(item.get("supersedes_atom_id")),
        event_start=_as_float(item.get("event_start"), None),
        event_end=_as_float(item.get("event_end"), None),
        time_precision=(str(item["time_precision"]) if item.get("time_precision") else None),
        event_state=(str(item["event_state"]) if item.get("event_state") else None),
        event_version=int(item.get("event_version") or 1),
        followup_disabled=bool(item.get("followup_disabled")),
        provenance=str(item.get("provenance") or "user_stated"),
    )


def _looks_like_stable_id(s: str) -> bool:
    s = (s or "").strip()
    if not s:
        return False
    # Numeric QQ / long openid / platform:id
    if re.fullmatch(r"\d{5,}", s):
        return True
    if ":" in s and len(s) >= 8:
        return True
    if re.fullmatch(r"[0-9A-Fa-f]{16,}", s):
        return True
    return False


def _as_float(v: Any, default: float | None) -> float | None:
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _as_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_relative_range(
    text: str,
    *,
    source_time: float,
) -> tuple[float | None, float | None, str | None]:
    """Parse 明天/周五下午 etc. relative to source_time in Asia/Shanghai.

    Fuzzy words like 下次/最近 → (None, None, 'fuzzy') — do not invent precision.
    """
    t = text or ""
    if re.search(r"(下次|最近|改天|有空|回头|过几天)", t):
        return None, None, "fuzzy"

    base = datetime.fromtimestamp(source_time, SHANGHAI)
    day = None
    if "今天" in t:
        day = base
    elif "明天" in t:
        day = base + timedelta(days=1)
    elif "后天" in t:
        day = base + timedelta(days=2)
    else:
        weekday_map = {
            "周一": 0, "星期二": 1, "周二": 1, "周三": 2, "周四": 3,
            "周五": 4, "星期六": 5, "周六": 5, "周日": 6, "周天": 6,
            "星期一": 0, "星期三": 2, "星期四": 3, "星期五": 4, "星期日": 6,
        }
        for k, wd in weekday_map.items():
            if k in t:
                delta = (wd - base.weekday()) % 7
                if delta == 0 and "这" not in t:
                    delta = 7
                day = base + timedelta(days=delta)
                break

    if day is None:
        return None, None, None

    # Time-of-day windows
    if "上午" in t:
        start = day.replace(hour=9, minute=0, second=0, microsecond=0)
        end = day.replace(hour=12, minute=0, second=0, microsecond=0)
        return start.timestamp(), end.timestamp(), "range"
    if "下午" in t:
        start = day.replace(hour=13, minute=0, second=0, microsecond=0)
        end = day.replace(hour=18, minute=0, second=0, microsecond=0)
        return start.timestamp(), end.timestamp(), "range"
    if "晚上" in t or "今晚" in t:
        start = day.replace(hour=18, minute=0, second=0, microsecond=0)
        end = day.replace(hour=23, minute=0, second=0, microsecond=0)
        return start.timestamp(), end.timestamp(), "range"

    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = day.replace(hour=23, minute=59, second=59, microsecond=0)
    return start.timestamp(), end.timestamp(), "day"


def atom_meta(atom_or_meta: Any) -> dict[str, Any]:
    if isinstance(atom_or_meta, dict):
        return dict(atom_or_meta)
    md = getattr(atom_or_meta, "metadata", None)
    return dict(md) if isinstance(md, dict) else {}


def is_atom_visible(
    meta: dict[str, Any],
    *,
    recall_session_id: str | None,
    is_private_recall: bool,
    now: float | None = None,
) -> bool:
    """Visibility + validity gate before ranking."""
    now = float(now if now is not None else time.time())
    status = str(meta.get("status") or "active").lower()
    if status in ("superseded", "forgotten", "expired"):
        return False
    # Also honor AtomStatus in top-level if present via caller
    vf = meta.get("valid_from")
    vu = meta.get("valid_until")
    if vf is not None and now < float(vf):
        return False
    if vu is not None and now >= float(vu):
        return False

    vis = str(meta.get("visibility") or VIS_GROUP).lower()
    src = str(meta.get("source_session") or meta.get("session_id") or "")
    if vis == VIS_SHARED:
        return True
    if vis == VIS_PRIVATE:
        # private facts only in matching private session
        if not is_private_recall:
            return False
        if src and recall_session_id and src != recall_session_id:
            return False
        return True
    # group: only same session
    if recall_session_id and src and src != recall_session_id:
        return False
    return True


def filter_atoms_for_recall(
    atoms: list[Any],
    *,
    recall_session_id: str | None,
    is_private_recall: bool,
    subject_id: str | None = None,
    now: float | None = None,
    limit: int = 5,
) -> list[Any]:
    now = float(now if now is not None else time.time())
    out = []
    for atom in atoms:
        meta = atom_meta(atom)
        # Merge atom.status
        st = getattr(atom, "status", None)
        if st is not None:
            meta = {**meta, "status": getattr(st, "value", st)}
        if meta.get("session_id") is None and getattr(atom, "session_id", None):
            meta = {**meta, "session_id": atom.session_id}
        if not is_atom_visible(
            meta,
            recall_session_id=recall_session_id,
            is_private_recall=is_private_recall,
            now=now,
        ):
            continue
        if subject_id and meta.get("subject_id") and str(meta["subject_id"]) != str(subject_id):
            # When filtering for a specific person, skip other subjects
            continue
        # Skip bot provenance as user facts
        if str(meta.get("provenance") or "") == "bot":
            continue
        out.append(atom)
        if len(out) >= limit:
            break
    return out


def should_supersede(old_meta: dict[str, Any], new_fact: StructuredFact) -> bool:
    if not new_fact.subject_id or not new_fact.predicate:
        return False
    if str(old_meta.get("subject_id") or "") != str(new_fact.subject_id):
        return False
    if str(old_meta.get("predicate") or "") != str(new_fact.predicate):
        return False
    # Same visibility/source family
    if str(old_meta.get("visibility") or "") != str(new_fact.visibility):
        return False
    return True


def merge_structured_into_key_facts(
    key_facts: list[Any],
    structured_facts: list[Any] | None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Keep legacy key_facts: list[str] while also returning structured list."""
    structured: list[StructuredFact] = []
    if structured_facts:
        for item in structured_facts:
            sf = normalize_fact_item(item)
            if sf:
                structured.append(sf)
    legacy_strs: list[str] = []
    for item in key_facts or []:
        if isinstance(item, str) and item.strip():
            legacy_strs.append(item.strip())
            if not structured_facts:
                sf = normalize_fact_item(item)
                if sf:
                    structured.append(sf)
        elif isinstance(item, dict):
            sf = normalize_fact_item(item)
            if sf:
                structured.append(sf)
                legacy_strs.append(sf.content)
    # Prefer structured contents for legacy list
    if structured and not legacy_strs:
        legacy_strs = [s.content for s in structured]
    return legacy_strs[:10], [s.to_metadata() | {"content": s.content} for s in structured[:10]]
