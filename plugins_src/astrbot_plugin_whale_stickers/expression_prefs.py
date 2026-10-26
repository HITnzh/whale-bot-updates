"""Persistent expression mode preferences (text/voice) per identity key."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Literal

Mode = Literal["text", "voice", "text_sticker", "text_voice"]


class ExpressionPrefStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self._data = {}
            return
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8")) or {}
        except Exception:
            self._data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def set_mode(self, key: str, mode: Mode, *, scope: str = "global") -> None:
        self._data[key] = {
            "mode": mode,
            "scope": scope,
            "updated_at": int(time.time()),
        }
        self.save()

    def get_mode(self, key: str) -> Mode | None:
        row = self._data.get(key)
        if not row:
            return None
        m = row.get("mode")
        if m in ("text", "voice", "text_sticker", "text_voice"):
            return m  # type: ignore[return-value]
        return None
