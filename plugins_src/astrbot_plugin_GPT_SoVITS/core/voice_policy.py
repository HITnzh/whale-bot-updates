"""Provider-independent voice preferences and conservative speech eligibility."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path


_PREFERENCES = frozenset({"auto", "text", "voice"})
_HASH_KEY = re.compile(r"^[0-9a-f]{64}$")
_QUOTED = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`|“[^”\n]*”|‘[^’\n]*’|\"[^\"\n]*\"")
_CLAUSE = r"(?:^|[，,。.!！?？;；\n]|但是|不过|但)\s*"
_PREFIX = (
    r"(?:(?:请你?|麻烦你?|帮我|给我|直接|还是|这次|现在|先|"
    r"我想要?|我希望你?|你能不能|你可以|你能|能不能|可以)\s*){0,4}"
)
_TEXT_INTENT = re.compile(
    _CLAUSE + _PREFIX + r"(?:"
    r"(?:不要|别|不用|无需|不需要|禁止)(?:再|给我|用|使用|发|发送|回|回复|输出|生成|合成|转成|来|开启|说|读|朗读|\s){0,8}(?:语音|音频|朗读|念出来|读出来)|"
    r"(?:我(?:现在)?|现在|这会儿)?(?:不想|不方便|不能)听(?:语音|音频)|"
    r"(?:只|仅)(?:要|用|使用|发|发送|看|接受)?(?:文字|文本)|"
    r"(?:用|以|改用|换成|改成|发|发送)(?:纯)?(?:文字|文本)(?:来)?(?:回答|回复|回我|说|输出|给我|就好|就行|即可|吧|(?=$|[，,。.!！?？;；\n]))|"
    r"(?:文字|文本)(?:回答|回复)|"
    r"打字(?:回答|回复|回我|说|给我|就好|即可|吧)?"
    r")",
    re.IGNORECASE,
)
_VOICE_INTENT = re.compile(
    _CLAUSE + _PREFIX + r"(?:"
    r"(?:用|以|通过)语音(?:来)?(?:回复|回答|回我|跟我说|说|讲|告诉我|念|读|播报)|"
    r"(?:发|发送|来)(?:一条|一段|个|段|条)?语音(?:回复|回答|给我)?|"
    r"(?:读|念)给我听|朗读一下|(?:把.{1,40})(?:读出来|念出来)"
    r")",
    re.IGNORECASE,
)
_ENGLISH_TEXT = re.compile(
    r"(?:^|[.!?;\n])\s*(?:please\s+)?(?:text only\b|(?:reply|respond)(?:\s+to me)?\s+(?:in|with)\s+text\b|no\s+(?:voice|audio)\b|(?:do not|don't)\s+(?:send|use)\s+(?:voice|audio)\b)",
    re.IGNORECASE,
)
_ENGLISH_VOICE = re.compile(
    r"(?:^|[.!?;\n])\s*(?:please\s+)?(?:(?:reply|respond)(?:\s+to me)?\s+(?:in|with)\s+(?:voice|audio)\b|send me\s+(?:a\s+)?voice\b|read (?:this|it) aloud\b)",
    re.IGNORECASE,
)
_URL = re.compile(r"(?:https?|ftp)://|\bwww\.|\bmailto:", re.IGNORECASE)
_CODE = re.compile(
    r"```|~~~|`[^`\n]+`|(?m:^\s*(?:def\s+\w+\s*\(|class\s+\w+\s*[:(]|from\s+\S+\s+import\s|import\s+\w|function\s+\w+\s*\(|(?:const|let|var)\s+\w+\s*=))"
)
_TABLE_SEPARATOR = re.compile(r"(?m)^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")


def voice_intent(user_text: str) -> str | None:
    """Recognize explicit delivery requests, giving text requests precedence.

    This does not persist a preference. Quoted examples and ordinary discussion
    of voices or speech APIs are not treated as delivery instructions.
    """
    text = unicodedata.normalize("NFKC", str(user_text or ""))
    text = _QUOTED.sub("", text).strip()
    if _TEXT_INTENT.search(text) or _ENGLISH_TEXT.search(text):
        return "text"
    match = _VOICE_INTENT.search(text)
    if match:
        # A question about the wording is not a request to speak it.
        if not text[match.end():].lstrip().startswith(("是什么意思", "是什么", "怎么", "的功能")):
            return "voice"
    if _ENGLISH_VOICE.search(text):
        return "voice"
    return None


def speech_block_reason(
    user_text: str,
    spoken_text: str,
    preference: str,
    max_chars: int,
) -> str | None:
    """Return a stable reason code, or None when text is eligible for speech."""
    if voice_intent(user_text) == "text":
        return "text_requested"
    if str(preference or "").strip().casefold() == "text":
        return "text_preference"
    text = str(spoken_text or "").strip()
    if not text:
        return "empty"
    try:
        limit = int(max_chars)
    except (TypeError, ValueError, OverflowError):
        return "invalid_limit"
    if isinstance(max_chars, bool) or limit <= 0 or limit != max_chars:
        return "invalid_limit"
    if len(text) > limit:
        return "too_long"
    checked = unicodedata.normalize("NFKC", text)
    if _CODE.search(checked):
        return "contains_code"
    if _URL.search(checked) or re.search(r"\[[^\]\n]+\]\([^\)\n]+\)", checked):
        return "contains_url"
    table_lines = [line.strip() for line in checked.splitlines()]
    if _TABLE_SEPARATOR.search(checked) or sum(
        line.startswith("|") and line.endswith("|") and line.count("|") >= 2
        for line in table_lines
    ) >= 2:
        return "contains_table"
    return None


class VoicePolicy:
    """Persist per-person preferences; keep successful-send cooldowns in memory."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.monotonic):
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.RLock()
        self._preferences = self._load()
        self._sent: dict[str, float] = {}

    @staticmethod
    def _scope(umo: str, sender_id: str | None = None) -> str | None:
        values = [str(umo or "").strip()]
        if sender_id is not None:
            values.append(str(sender_id or "").strip())
        if not all(values):
            return None
        encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _load(self) -> dict[str, str]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                return {}
            preferences = payload.get("preferences")
            if not isinstance(preferences, dict):
                return {}
            return {
                key: value for key, value in preferences.items()
                if isinstance(key, str) and _HASH_KEY.fullmatch(key)
                and isinstance(value, str) and value in _PREFERENCES
            }
        except (OSError, ValueError, UnicodeError):
            return {}

    def preference(self, umo: str, sender_id: str) -> str:
        if not str(sender_id or "").strip():
            return "auto"
        key = self._scope(umo, sender_id)
        with self._lock:
            return self._preferences.get(key, "auto") if key else "auto"

    def set_preference(self, umo: str, sender_id: str, value: str) -> None:
        key = self._scope(umo, sender_id) if str(sender_id or "").strip() else None
        if key is None:
            raise ValueError("Voice preferences require both session and sender identity")
        value = str(value or "").strip().casefold()
        if value not in _PREFERENCES:
            raise ValueError("Voice preference must be auto, text, or voice")
        with self._lock:
            updated = dict(self._preferences)
            updated[key] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent,
            )
            temporary_path = Path(temporary)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump({"version": 1, "preferences": updated}, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_path, self.path)
                self._preferences = updated
            finally:
                temporary_path.unlink(missing_ok=True)

    def cooldown_remaining(self, umo: str, seconds: float) -> float:
        key = self._scope(umo)
        duration = float(seconds)
        if key is None or not math.isfinite(duration) or duration <= 0:
            return 0.0
        with self._lock:
            sent = self._sent.get(key)
            if sent is None:
                return 0.0
            elapsed = max(0.0, float(self._clock()) - sent)
            return max(0.0, duration - elapsed)

    def mark_sent(self, umo: str) -> None:
        key = self._scope(umo)
        if key is None:
            return
        with self._lock:
            self._sent[key] = float(self._clock())
