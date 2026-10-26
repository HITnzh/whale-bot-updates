"""Group wake / continuity rules (offline-testable, no AstrBot import)."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

# Continuity window: 90s / max 3 turns after bot reply
WINDOW_SEC = 90.0
MAX_TURNS = 3

END_PHRASES = (
    "先不聊了",
    "不聊了",
    "不跟你聊了",
    "回去了",
    "我走了",
    "拜拜",
    "再见",
    "回见",
    "下次再说",
    "忙去了",
    "挂了",
    "下线了",
)

# Name hits that are *about* the bot, not addressing it
NAME_REPORT_RE = re.compile(
    r"(刚才|刚刚|之前).{0,8}(说|讲|回)|"
    r"(说得对|说的对|说得没错|提到过|以为是|"
    r"让.{0,4}去|叫.{0,4}去|"
    r"她说|他说|ta说|它们说)",
    re.I,
)

CONTINUATION_HINTS = (
    "为什么",
    "为啥",
    "然后呢",
    "还有呢",
    "所以",
    "就是说",
    "怎么说",
    "怎么办",
    "真的吗",
    "是吗",
    "啊？",
    "诶？",
    "呢？",
    "吗？",
    "？",
    "?",
)


@dataclass
class ParticipantState:
    last_bot_reply_at: float = 0.0
    last_bot_msg_id: str = ""
    topic_hint: str = ""
    waiting_for: str = ""  # sender_id we expect a follow-up from
    turn_count: int = 0
    ended: bool = False


@dataclass
class GroupWakeState:
    """Per-group short-term wake state (not a chat log)."""

    participants: dict[str, ParticipantState] = field(default_factory=dict)

    def get(self, sender_id: str) -> ParticipantState:
        sid = str(sender_id or "")
        if sid not in self.participants:
            self.participants[sid] = ParticipantState()
        return self.participants[sid]

    def note_bot_reply(
        self,
        sender_id: str,
        *,
        now: float | None = None,
        msg_id: str = "",
        topic_hint: str = "",
    ) -> None:
        st = self.get(sender_id)
        st.last_bot_reply_at = float(now if now is not None else time.time())
        st.last_bot_msg_id = msg_id or st.last_bot_msg_id
        if topic_hint:
            st.topic_hint = topic_hint[:80]
        st.waiting_for = str(sender_id)
        st.turn_count = 0
        st.ended = False

    def note_user_turn(self, sender_id: str) -> None:
        st = self.get(sender_id)
        st.turn_count += 1


@dataclass
class WakeDecision:
    wake: bool
    reason: str
    fuzzy: bool = False  # needs async semantic gate


class ConversationStateStore:
    """In-memory group → GroupWakeState."""

    def __init__(self) -> None:
        self._groups: dict[str, GroupWakeState] = {}

    def group(self, group_id: str) -> GroupWakeState:
        gid = str(group_id or "") or "_"
        if gid not in self._groups:
            self._groups[gid] = GroupWakeState()
        return self._groups[gid]


def _split_keywords(raw: str) -> list[str]:
    parts = re.split(r"[,，\n]+", raw or "")
    return [p.strip() for p in parts if p.strip()]


def looks_like_end(text: str) -> bool:
    t = (text or "").strip()
    return any(p in t for p in END_PHRASES)


def name_is_report_only(text: str, names: list[str]) -> bool:
    """True when a name appears but the sentence is talking *about* the bot."""
    text_cf = (text or "").casefold()
    if not any(n.casefold() in text_cf for n in names):
        return False
    # Direct address patterns: leading name / name + 你 / vocative
    for n in names:
        nc = n.casefold()
        if text_cf.startswith(nc) or re.search(
            rf"(^|[\s，,。！!？?]){re.escape(nc)}(啊|呀|哟|喂|来|帮|听|说)",
            text_cf,
        ):
            return False
    return bool(NAME_REPORT_RE.search(text or ""))


def looks_like_continuation(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if looks_like_end(t):
        return False
    # Short follow-ups / question marks / continuation hints
    if len(t) <= 24 and any(h in t for h in CONTINUATION_HINTS):
        return True
    if t.endswith(("？", "?", "呢", "吗", "嘛")):
        return True
    return any(h in t for h in ("为什么", "为啥", "然后呢", "还有呢", "所以呢"))


def in_continuity_window(
    st: ParticipantState,
    sender_id: str,
    *,
    now: float | None = None,
    window_sec: float = WINDOW_SEC,
    max_turns: int = MAX_TURNS,
) -> bool:
    now = float(now if now is not None else time.time())
    if st.ended:
        return False
    if st.waiting_for and str(st.waiting_for) != str(sender_id):
        return False
    if st.last_bot_reply_at <= 0:
        return False
    if now - st.last_bot_reply_at > window_sec:
        return False
    if st.turn_count >= max_turns:
        return False
    return True


def decide_group_wake(
    *,
    text: str,
    sender_id: str,
    group_id: str,
    store: ConversationStateStore,
    name_keywords: str = "",
    poke_keywords: str = "",
    mention_always: bool = True,
    is_at_bot: bool = False,
    is_reply_to_bot: bool = False,
    is_command: bool = False,
    is_at_other: bool = False,
    is_reply_to_other: bool = False,
    already_wake: bool = False,
    now: float | None = None,
) -> WakeDecision:
    """Rule-based group wake decision.

    Explicit @/command/reply-to-bot are expected to already set already_wake
    via AstrBot; this function still returns wake for documentation/tests.
    Owner random interrupt is intentionally removed.
    """
    now = float(now if now is not None else time.time())
    text = text or ""
    sid = str(sender_id or "")
    gstate = store.group(group_id)
    st = gstate.get(sid)

    if already_wake or is_command or is_at_bot or is_reply_to_bot:
        return WakeDecision(True, "explicit")

    # Talking to someone else — do not interrupt
    if (is_at_other or is_reply_to_other) and not is_at_bot:
        return WakeDecision(False, "directed_elsewhere")

    if looks_like_end(text):
        st.ended = True
        return WakeDecision(False, "session_end")

    # Continuity with this sender
    if in_continuity_window(st, sid, now=now) and looks_like_continuation(text):
        return WakeDecision(True, "continuity")

    names = _split_keywords(name_keywords)
    pokes = _split_keywords(poke_keywords)
    text_cf = text.casefold()
    hit_name = any(n.casefold() in text_cf for n in names)
    hit_poke = any(p in text for p in pokes)

    if hit_poke:
        return WakeDecision(True, "poke_text")

    if mention_always and hit_name:
        if name_is_report_only(text, names):
            return WakeDecision(False, "name_report_only")
        # Clear address → wake; ambiguous → fuzzy candidate (async may ignore)
        if text_cf.startswith(tuple(n.casefold() for n in names)) or re.search(
            r"(你|帮我|过来|听我说)", text
        ):
            return WakeDecision(True, "name")
        return WakeDecision(False, "name_ambiguous", fuzzy=True)

    return WakeDecision(False, "default_ignore")


def summarize_state(store: ConversationStateStore) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for gid, g in store._groups.items():
        out[gid] = {
            sid: {
                "last_bot_reply_at": p.last_bot_reply_at,
                "turn_count": p.turn_count,
                "ended": p.ended,
                "waiting_for": p.waiting_for,
            }
            for sid, p in g.participants.items()
        }
    return out
