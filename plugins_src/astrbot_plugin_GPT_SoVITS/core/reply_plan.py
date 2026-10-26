"""Shared reply_plan contract for text / voice / sticker expression."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Mode = Literal["text", "voice", "text_sticker", "text_voice"]
Emotion = Literal["calm", "happy", "playful", "comforting"]

EXTRA_KEY = "whale_reply_plan"

_MARKER_RE = re.compile(
    r"\s*[\[【［(（]\s*表情\s*[:：]\s*([^\]】］)）\n]{1,12})\s*[\]】］)）]\s*"
)
_OS_RE = re.compile(r"（内心OS[：:].*?）|\(内心OS[：:].*?\)")
_KAOMOJI_RE = re.compile(
    r"[（(]?[￣´｀]?[▽∀ωヘ皿﹏︿ŏ╥ー_¬←→←]*[）)]?"
    r"|[╮╭ノゞoＯ✧★☆💦✨🍚😏🥱]+"
)

# Preference predicates stored as memory-like facts elsewhere; here we just read dicts
PREF_TEXT = "reply_mode_text"
PREF_VOICE = "reply_mode_voice"


@dataclass
class ReplyPlan:
    mode: Mode = "text"
    emotion: Emotion = "calm"
    spoken_text: str = ""
    sticker_tag: str | None = None
    keep_transcript: bool = True
    source: str = "default"  # turn_override | preference | situational | default

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "ReplyPlan":
        if not d:
            return cls()
        mode = d.get("mode") or "text"
        if mode not in ("text", "voice", "text_sticker", "text_voice"):
            mode = "text"
        emotion = d.get("emotion") or "calm"
        if emotion not in ("calm", "happy", "playful", "comforting"):
            emotion = "calm"
        return cls(
            mode=mode,  # type: ignore[arg-type]
            emotion=emotion,  # type: ignore[arg-type]
            spoken_text=str(d.get("spoken_text") or ""),
            sticker_tag=(str(d["sticker_tag"]) if d.get("sticker_tag") else None),
            keep_transcript=bool(d.get("keep_transcript", True)),
            source=str(d.get("source") or "default"),
        )


def strip_for_speech(text: str) -> str:
    """Remove sticker markers, kaomoji/OS for TTS; keep user-requested content."""
    t = _MARKER_RE.sub(" ", text or "")
    t = _OS_RE.sub(" ", t)
    # Soft strip common kaomoji blocks but keep Chinese letters/numbers
    t = re.sub(r"[（(][^）)]{0,8}[▽∀ωヘ皿﹏︿ŏ╥ー_¬][^）)]{0,8}[）)]", " ", t)
    t = re.sub(r"[ヾゞ★☆✨💦🍚😏🥱♡♥]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def extract_sticker_tag(text: str) -> tuple[str, str | None]:
    tags = _MARKER_RE.findall(text or "")
    cleaned = _MARKER_RE.sub("", text or "").strip()
    return cleaned, (tags[-1].strip() if tags else None)


def detect_turn_override(user_text: str) -> Mode | None:
    t = user_text or ""
    if re.search(r"(这次|这回|刚才那句)?(发|用|只要)?文字", t) and not re.search(
        r"以后|都|总是|每次", t
    ):
        if "文字" in t:
            return "text"
    if re.search(r"(这次|这回).{0,4}(语音|声音|听)", t):
        return "voice"
    if re.search(r"(这次|这回).{0,4}表情", t):
        return "text_sticker"
    return None


def detect_persistent_pref(user_text: str) -> Mode | None:
    t = user_text or ""
    if re.search(r"(以后|都|总是|每次).{0,6}(发|用)?文字", t):
        return "text"
    if re.search(r"(以后|都|总是|每次).{0,6}(语音|声音)", t):
        return "voice"
    return None


def situational_emotion(text: str) -> Emotion:
    t = text or ""
    if re.search(r"(难过|伤心|哭|安慰|没事|抱抱|累了|压力)", t):
        return "comforting"
    if re.search(r"(哈哈|开心|太好了|耶|庆祝|恭喜)", t):
        return "happy"
    if re.search(r"(哼|傲娇|调皮|嘿嘿|摸鱼)", t):
        return "playful"
    return "calm"


def build_reply_plan(
    *,
    reply_text: str,
    user_text: str = "",
    preference_mode: Mode | None = None,
    force_mode: Mode | None = None,
    sticker_tag: str | None = None,
    comforting_force_text: bool = True,
) -> ReplyPlan:
    """Priority: turn override > preference > situational default."""
    cleaned, tag_from_text = extract_sticker_tag(reply_text)
    tag = sticker_tag or tag_from_text
    spoken = strip_for_speech(cleaned)
    emotion = situational_emotion(user_text + " " + cleaned)

    mode: Mode = "text"
    source = "default"

    turn = detect_turn_override(user_text)
    if force_mode:
        mode, source = force_mode, "force"
    elif turn:
        mode, source = turn, "turn_override"
    elif preference_mode:
        mode, source = preference_mode, "preference"
    else:
        if tag and emotion != "comforting":
            mode, source = "text_sticker", "situational"
        else:
            mode, source = "text", "situational"

    if comforting_force_text and emotion == "comforting" and mode == "text_sticker":
        # Keep readable text; drop playful sticker pressure
        mode = "text"
        tag = None
        source = "situational"

    keep = True
    if mode == "voice":
        keep = False  # voice-only unless text_voice
    if mode == "text_voice":
        keep = True
    if mode in ("text", "voice") and source in ("preference", "turn_override", "force"):
        tag = None

    return ReplyPlan(
        mode=mode,
        emotion=emotion,
        spoken_text=spoken,
        sticker_tag=tag,
        keep_transcript=keep,
        source=source,
    )
