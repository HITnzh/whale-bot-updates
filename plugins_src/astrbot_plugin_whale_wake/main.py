"""Wake on explicit call / continuity / name address, plus rare group participation.

Random participation (owner_probability / other_probability) is low by default and
shares one per-group cooldown with explicit wakes (merged from the PC 0.2.0 build).

QQ official API has no poke event; text keywords remain the poke fallback.
"""
from __future__ import annotations

import math
import random
import time
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.api.star import Context, Star, register
from astrbot.core.config import AstrBotConfig as CoreConfig

from .conversation_gate import (
    ConversationStateStore,
    decide_group_wake,
    looks_like_end,
)

_STAR: dict[str, Any] = {"inst": None}


def _message_at_targets(event: AstrMessageEvent) -> tuple[bool, bool]:
    """Return (at_bot, at_other). Best-effort; missing platform fields → False."""
    at_bot = False
    at_other = False
    try:
        self_id = str(event.get_self_id() or "")
        comps = getattr(getattr(event, "message_obj", None), "message", None)
        if not comps and hasattr(event, "get_messages"):
            comps = event.get_messages()
        for comp in comps or []:
            qq = str(getattr(comp, "qq", "") or getattr(comp, "uid", "") or "")
            # At component duck-typing
            cname = type(comp).__name__
            if cname not in ("At", "AtAll") and "At" not in cname:
                continue
            if not qq or qq.lower() == "all":
                continue
            if self_id and qq == self_id:
                at_bot = True
            else:
                at_other = True
    except Exception:
        pass
    return at_bot, at_other


def _reply_targets(event: AstrMessageEvent) -> tuple[bool, bool]:
    """Return (reply_to_bot, reply_to_other) when quote metadata exists."""
    reply_bot = False
    reply_other = False
    try:
        self_id = str(event.get_self_id() or "")
        # Common AstrBot / napcat fields
        for attr in ("reply", "quote", "reply_message"):
            obj = getattr(event.message_obj, attr, None)
            if obj is None:
                continue
            sender = (
                getattr(obj, "sender_id", None)
                or getattr(obj, "user_id", None)
                or getattr(getattr(obj, "sender", None), "user_id", None)
            )
            sid = str(sender or "")
            if not sid:
                continue
            if self_id and sid == self_id:
                reply_bot = True
            else:
                reply_other = True
    except Exception:
        pass
    return reply_bot, reply_other


class WhaleWakeFilter(CustomFilter):
    """Sync waking_check: rule decide + mark continuity; no LLM here."""

    def filter(self, event: AstrMessageEvent, cfg: CoreConfig) -> bool:
        # Already explicitly woken (@ / command) — leave alone
        if event.is_at_or_wake_command:
            # Still track continuity bookkeeping if we can
            star: WhaleWake | None = _STAR.get("inst")
            if star and not event.is_private_chat():
                star.note_explicit_wake(event)
                star.touch_group(event)
            return False
        if event.is_private_chat():
            return False

        star: WhaleWake | None = _STAR.get("inst")
        conf = (star.config if star else {}) or {}
        text = (event.get_message_str() or event.message_str or "").strip()
        if not text:
            return False

        at_bot, at_other = _message_at_targets(event)
        reply_bot, reply_other = _reply_targets(event)

        decision = decide_group_wake(
            text=text,
            sender_id=str(event.get_sender_id() or ""),
            group_id=str(event.get_group_id() or ""),
            store=star.store if star else ConversationStateStore(),
            name_keywords=str(conf.get("name_keywords") or ""),
            poke_keywords=str(conf.get("poke_text_keywords") or ""),
            mention_always=bool(conf.get("other_mention_always", True)),
            is_at_bot=at_bot,
            is_reply_to_bot=reply_bot,
            is_at_other=at_other,
            is_reply_to_other=reply_other,
            already_wake=False,
        )

        event.set_extra("_whale_wake_reason", decision.reason)
        event.set_extra("_whale_wake_fuzzy", decision.fuzzy)

        if decision.fuzzy and not decision.wake:
            # Mark candidate for async gate; do not wake sync
            event.set_extra("_whale_wake_candidate", True)
            return False

        if not decision.wake:
            # Rare random participation (PC 0.2.0): never interrupt commands or talk
            # addressed to someone else; shared per-group cooldown.
            if star is None or text.startswith("/") or at_other or reply_other:
                return False
            if not star.random_participation(event):
                return False
            decision_reason = "owner_random" if star.is_owner(event) else "group_random"
            event.set_extra("_whale_wake_reason", decision_reason)
            event.is_at_or_wake_command = True
            event.is_wake = True
            logger.debug(f"[whale_wake] wake reason={decision_reason}")
            return True

        if star:
            star.touch_group(event)
        event.is_at_or_wake_command = True
        event.is_wake = True
        if star:
            star.on_wake_accepted(event, decision.reason)
        logger.debug(
            f"[whale_wake] wake sender={event.get_sender_id()} reason={decision.reason}"
        )
        return True


@register(
    "whale_wake",
    "whale",
    "群聊接话：明确呼叫/连续对话跟进 + 低频随机接话与同群共享冷却",
    "0.3.0",
)
class WhaleWake(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.store = ConversationStateStore()
        self._last_group_activity: dict[tuple[str, str], float] = {}
        _STAR["inst"] = self

    def _group_key(self, event: AstrMessageEvent) -> tuple[str, str]:
        return (str(event.get_platform_id()), str(event.get_group_id()))

    def touch_group(self, event: AstrMessageEvent) -> None:
        self._last_group_activity[self._group_key(event)] = time.monotonic()

    def _num(self, key: str, default: float) -> float:
        try:
            v = float(self.config.get(key, default))
        except (TypeError, ValueError):
            return default
        return v if math.isfinite(v) else default

    def random_participation(self, event: AstrMessageEvent) -> bool:
        """Low-probability unsolicited reply with a shared per-group cooldown."""
        group = self._group_key(event)
        now = time.monotonic()
        cooldown = max(0.0, self._num("group_cooldown_seconds", 180))
        last = self._last_group_activity.get(group)
        if last is not None and now - last < cooldown:
            return False
        if self.is_owner(event):
            p = self._num("owner_probability", 0.1)
        else:
            p = self._num("other_probability", 0.02)
        p = max(0.0, min(1.0, p))
        if random.random() >= p:
            return False
        # Reserve the slot before generation so simultaneous messages cannot pile up.
        self._last_group_activity[group] = now
        return True

    def is_owner(self, event: AstrMessageEvent) -> bool:
        sid = str(event.get_sender_id() or "")
        if not sid.strip():
            return False
        ids: set[str] = set()
        try:
            for a in self.context.get_config().get("admins_id") or []:
                s = str(a)
                if s and s != "astrbot":
                    ids.add(s)
        except Exception:
            pass
        for line in str(self.config.get("extra_owner_ids") or "").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                ids.add(line)
        return sid in ids

    def note_explicit_wake(self, event: AstrMessageEvent) -> None:
        if looks_like_end(event.get_message_str() or event.message_str or ""):
            st = self.store.group(str(event.get_group_id() or "")).get(
                str(event.get_sender_id() or "")
            )
            st.ended = True

    def on_wake_accepted(self, event: AstrMessageEvent, reason: str) -> None:
        sid = str(event.get_sender_id() or "")
        gid = str(event.get_group_id() or "")
        st = self.store.group(gid).get(sid)
        if reason == "continuity":
            st.turn_count += 1

    @filter.on_decorating_result(priority=50)
    async def track_bot_reply(self, event: AstrMessageEvent):
        """After we reply in a group, open a continuity window for that sender."""
        try:
            if event.is_private_chat():
                return
            result = event.get_result()
            if not result or not result.chain:
                return
            # Only track when we actually woke / are answering
            reason = event.get_extra("_whale_wake_reason") or ""
            if not (event.is_wake or event.is_at_or_wake_command or reason):
                # Still track if this is an LLM result in group after we spoke
                if not result.is_llm_result():
                    return
            sid = str(event.get_sender_id() or "")
            gid = str(event.get_group_id() or "")
            self.store.group(gid).note_bot_reply(sid)
            logger.debug(f"[whale_wake] continuity window opened for {sid} in {gid}")
        except Exception as e:
            logger.debug(f"[whale_wake] track_bot_reply: {e}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=10**9)
    @filter.custom_filter(WhaleWakeFilter)
    async def on_group_wake_gate(self, event: AstrMessageEvent):
        # Fuzzy candidates: default ignore (no LLM in first version)
        if event.get_extra("_whale_wake_candidate") and not event.is_wake:
            logger.debug(
                f"[whale_wake] ignore fuzzy candidate sender={event.get_sender_id()}"
            )
            return
        return
