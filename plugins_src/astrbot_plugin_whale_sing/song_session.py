"""Per-user/session short-lived song revision state."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any


DEFAULT_TTL_SEC = 30 * 60


@dataclass
class SongSession:
    template_id: str
    theme: str
    recipient: str
    lyrics: str
    bpm: int
    version: int = 1
    mood: str = ""
    updated_at: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.updated_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SongSession":
        return cls(
            template_id=str(d.get("template_id") or ""),
            theme=str(d.get("theme") or ""),
            recipient=str(d.get("recipient") or ""),
            lyrics=str(d.get("lyrics") or ""),
            bpm=int(d.get("bpm") or 90),
            version=int(d.get("version") or 1),
            mood=str(d.get("mood") or ""),
            updated_at=float(d.get("updated_at") or time.time()),
        )


class SongSessionStore:
    def __init__(self, ttl_sec: float = DEFAULT_TTL_SEC):
        self.ttl_sec = ttl_sec
        self._data: dict[str, SongSession] = {}

    @staticmethod
    def key(session_id: str, user_id: str) -> str:
        return f"{session_id}::{user_id}"

    def get(self, session_id: str, user_id: str, *, now: float | None = None) -> SongSession | None:
        now = float(now if now is not None else time.time())
        k = self.key(session_id, user_id)
        ss = self._data.get(k)
        if not ss:
            return None
        if now - ss.updated_at > self.ttl_sec:
            self._data.pop(k, None)
            return None
        return ss

    def put(self, session_id: str, user_id: str, ss: SongSession) -> None:
        ss.touch()
        self._data[self.key(session_id, user_id)] = ss

    def clear(self, session_id: str, user_id: str) -> None:
        self._data.pop(self.key(session_id, user_id), None)
