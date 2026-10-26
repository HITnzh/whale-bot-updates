"""Self address preferences — separate from admin identity / permissions."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# scope:
#   "private"           — DM only
#   "group:<group_id>"  — one group
#   "global"            — all contexts for this user (lowest priority than more specific)


@dataclass
class AddressPref:
    address: str
    subject_id: str
    scope: str
    valid_from: float
    valid_until: float | None  # None = long-term
    source: str = "self"  # self | admin | system
    temporary: bool = False

    def is_active(self, now: float | None = None, scope: str | None = None) -> bool:
        now = float(now if now is not None else time.time())
        if now < self.valid_from:
            return False
        if self.valid_until is not None and now >= self.valid_until:
            return False
        if scope is not None and not scope_matches(self.scope, scope):
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "subject_id": self.subject_id,
            "scope": self.scope,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "source": self.source,
            "temporary": self.temporary,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AddressPref":
        return cls(
            address=str(d.get("address") or ""),
            subject_id=str(d.get("subject_id") or ""),
            scope=str(d.get("scope") or "global"),
            valid_from=float(d.get("valid_from") or 0),
            valid_until=(
                None
                if d.get("valid_until") in (None, "", 0)
                else float(d["valid_until"])
            ),
            source=str(d.get("source") or "self"),
            temporary=bool(d.get("temporary")),
        )


def scope_matches(pref_scope: str, current_scope: str) -> bool:
    if pref_scope == "global":
        return True
    return pref_scope == current_scope


def scope_specificity(scope: str) -> int:
    if scope.startswith("group:"):
        return 3
    if scope == "private":
        return 2
    return 1  # global


def current_scope(is_private: bool, group_id: str | None) -> str:
    if is_private:
        return "private"
    gid = str(group_id or "").strip()
    return f"group:{gid}" if gid else "global"


def end_of_shanghai_day(now: float | None = None) -> float:
    """Unix ts of next Asia/Shanghai midnight after `now`."""
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=8))
    now = float(now if now is not None else time.time())
    dt = datetime.fromtimestamp(now, tz)
    nxt = (dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.timestamp()


class AddressPrefStore:
    """JSON file: { "platform:uid": [AddressPref dict, ...] }"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict[str, list[dict[str, Any]]] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self._data = {}
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                raw = json.load(f) or {}
            self._data = {str(k): list(v or []) for k, v in raw.items()}
        except Exception:
            self._data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def list_prefs(self, key: str) -> list[AddressPref]:
        return [AddressPref.from_dict(d) for d in self._data.get(key, [])]

    def set_pref(self, key: str, pref: AddressPref) -> None:
        """Replace same-scope prefs; keep others. Temp overrides long-term same scope."""
        items = self.list_prefs(key)
        kept: list[AddressPref] = []
        for p in items:
            if p.scope == pref.scope and p.temporary == pref.temporary:
                continue
            # New temp replaces long-term for same scope while active — keep long-term
            # but resolve() prefers temp.
            if p.scope == pref.scope and pref.temporary and not p.temporary:
                kept.append(p)
                continue
            if p.scope == pref.scope and not pref.temporary and p.temporary:
                # Setting long-term does not wipe current temp
                kept.append(p)
                continue
            if p.scope == pref.scope:
                continue
            kept.append(p)
        kept.append(pref)
        self._data[key] = [p.to_dict() for p in kept]
        self.save()

    def clear_scope(self, key: str, scope: str, *, temporary_only: bool = False) -> int:
        items = self.list_prefs(key)
        kept = []
        removed = 0
        for p in items:
            if p.scope == scope and (not temporary_only or p.temporary):
                removed += 1
                continue
            kept.append(p)
        self._data[key] = [p.to_dict() for p in kept]
        if removed:
            self.save()
        return removed

    def resolve(
        self,
        key: str,
        scope: str,
        *,
        now: float | None = None,
    ) -> AddressPref | None:
        now = float(now if now is not None else time.time())
        candidates = [
            p
            for p in self.list_prefs(key)
            if p.is_active(now=now, scope=scope) and p.address.strip()
        ]
        if not candidates:
            return None

        def rank(p: AddressPref) -> tuple:
            # temp > long-term; more specific scope first
            return (
                0 if p.temporary else 1,
                -scope_specificity(p.scope),
                -(p.valid_from or 0),
            )

        candidates.sort(key=rank)
        return candidates[0]


def parse_address_request(text: str) -> dict[str, Any] | None:
    """Heuristic NL parse for self address changes. Returns None if not a request."""
    import re

    t = (text or "").strip()
    if not t:
        return None

    # Temporary: 今天别叫X / 今天叫我Y / 先别叫陛下
    m = re.search(
        r"(今天|今晚|暂时|先)(?:不要|别|别再)?叫(?:我)?(?P<old>[^\s，,。！!？?]{1,12})",
        t,
    )
    if m and ("别" in t or "不要" in t):
        return {
            "action": "temp_avoid",
            "avoid": m.group("old"),
            "temporary": True,
        }

    m = re.search(
        r"(今天|今晚|暂时|先)(?:请)?叫我(?P<name>[^\s，,。！!？?]{1,12})",
        t,
    )
    if m:
        return {
            "action": "set",
            "address": m.group("name"),
            "temporary": True,
        }

    m = re.search(
        r"(以后|从今|往后|请|麻烦)?(?:都)?叫我(?P<name>[^\s，,。！!？?]{1,12})",
        t,
    )
    if m:
        name = m.group("name")
        if name in ("主人", "陛下") and "叫我主人" in t:
            # May be master-ask; still a set request for self address, not permission
            return {"action": "set", "address": name, "temporary": False}
        return {"action": "set", "address": name, "temporary": False}

    m = re.search(r"(别再叫|不要叫|别叫)(?:我)?(?P<name>[^\s，,。！!？?]{1,12})", t)
    if m:
        return {
            "action": "avoid",
            "avoid": m.group("name"),
            "temporary": "今天" in t or "今晚" in t or "暂时" in t,
        }

    return None
