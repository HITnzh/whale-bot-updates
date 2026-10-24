"""Wake on owner probability / name mention / text-poke.

QQ official API has no poke/戳一戳 event (C2C_MSG_RECEIVE is proactive-push
opt-in, not poke). Text keywords are the only official-bot fallback.
"""
from __future__ import annotations

import random
import re
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.api.star import Context, Star, register
from astrbot.core.config import AstrBotConfig as CoreConfig

_STAR: dict[str, Any] = {"inst": None}


def _split_keywords(raw: str) -> list[str]:
    parts = re.split(r"[,，\n]+", raw or "")
    return [p.strip() for p in parts if p.strip()]


class WhaleWakeFilter(CustomFilter):
    """During waking_check: mark at/wake when owner/name/text-poke matches."""

    def filter(self, event: AstrMessageEvent, cfg: CoreConfig) -> bool:
        if event.is_at_or_wake_command:
            return False
        if event.is_private_chat():
            return False

        star: WhaleWake | None = _STAR.get("inst")
        conf = (star.config if star else {}) or {}
        text = (event.get_message_str() or event.message_str or "").strip()
        if not text:
            return False

        names = _split_keywords(str(conf.get("name_keywords") or ""))
        pokes = _split_keywords(str(conf.get("poke_text_keywords") or ""))
        mention_always = bool(conf.get("other_mention_always", True))
        text_cf = text.casefold()

        hit_name = any(n.casefold() in text_cf for n in names)
        hit_poke = any(p in text for p in pokes)
        is_owner = bool(star and star.is_owner(event))

        wake = False
        reason = ""
        if mention_always and hit_name:
            wake, reason = True, "name"
        elif hit_poke:
            wake, reason = True, "poke_text"
        elif is_owner:
            try:
                p = float(conf.get("owner_probability", 0.9))
            except (TypeError, ValueError):
                p = 0.9
            p = max(0.0, min(1.0, p))
            if random.random() < p:
                wake, reason = True, f"owner_p={p}"

        if not wake:
            return False

        event.is_at_or_wake_command = True
        event.is_wake = True
        event.set_extra("_whale_wake_reason", reason)
        logger.debug(
            f"[whale_wake] wake sender={event.get_sender_id()} reason={reason}"
        )
        # True → waking_check keeps event alive + activates this no-op handler
        return True


@register(
    "whale_wake",
    "whale",
    "群聊：主人高概率接话 / 叫名字必回；官机戳一戳仅文本兜底",
    "0.1.0",
)
class WhaleWake(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        _STAR["inst"] = self

    def is_owner(self, event: AstrMessageEvent) -> bool:
        sid = str(event.get_sender_id() or "")
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

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=10**9)
    @filter.custom_filter(WhaleWakeFilter)
    async def on_group_wake_gate(self, event: AstrMessageEvent):
        # Wake flags already set in WhaleWakeFilter; agent runs via pipeline.
        return
