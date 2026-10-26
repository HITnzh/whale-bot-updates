"""INTENT stage: pick a due life-event followup (exam/interview/trip/match).

Uses livingmemory FollowupStore via soft import / registry. Missing store → skip.
"""
from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger

from ...domain.domain_enums import HookPoint, StageResult
from ...pipeline.stage_protocol import BaseStage

# Per-user daily send guard (in-memory; process lifetime)
_SENT_TODAY: dict[str, str] = {}  # subject_id -> YYYY-MM-DD (Shanghai)


def _shanghai_day(now: float | None = None) -> str:
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=8))
    return datetime.fromtimestamp(now or time.time(), tz).strftime("%Y-%m-%d")


def _get_store():
    try:
        from astrbot_plugin_livingmemory.core.followup_store import (  # type: ignore
            get_followup_store,
        )

        return get_followup_store("default")
    except Exception:
        pass
    try:
        # Path fallback when plugin package name differs
        import importlib
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[5]  # plugins_src
        lm = root / "astrbot_plugin_livingmemory"
        if str(lm) not in sys.path:
            sys.path.insert(0, str(lm))
        mod = importlib.import_module("core.followup_store")
        return mod.get_followup_store("default")
    except Exception as e:
        logger.debug(f"[proactive/event_followup] store unavailable: {e}")
        return None


class EventFollowupIntentStage(BaseStage):
    name = "event_followup_intent"
    hook = HookPoint.INTENT
    priority = 20

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    async def handle(self, ctx) -> StageResult:
        store = _get_store()
        if store is None:
            # No memory followups → continue normal proactive flow
            return StageResult.CONTINUE

        session_id = ctx.session_id
        now = time.time()
        due = store.get_due_followups(session_id, now=now, limit=5)
        if not due:
            return StageResult.CONTINUE

        # Pick first that passes daily cap + claim
        chosen = None
        for fu in due:
            day = _shanghai_day(now)
            if _SENT_TODAY.get(fu.subject_id) == day:
                continue
            # Skip if event not yet ended (don't ask "how did it go" early)
            # reason may encode event_end; also check ctx.extra hints
            meta_end = None
            try:
                meta_end = float((fu.reason or "").split("event_end=")[-1].split(";")[0]) if "event_end=" in (fu.reason or "") else None
            except Exception:
                meta_end = None
            if meta_end is not None and now < meta_end:
                continue
            if store.claim_followup(fu.followup_id, fu.event_version):
                chosen = store.get(fu.followup_id)
                break

        if not chosen:
            # Nothing claimable now — still continue so idle chatter may happen,
            # or stop if config says event-only (default: continue)
            return StageResult.CONTINUE

        ctx.extra["event_followup"] = chosen.to_dict()
        ctx.extra["event_followup_id"] = chosen.followup_id
        # Seed prompt hint
        ctx.extra["intent_topic"] = (
            f"关心用户近期事件（版本{chosen.event_version}），"
            f"不要把未知结果说成已成功或失败；一句自然问候即可。"
            f"原因备注：{chosen.reason}"
        )
        logger.info(
            f"[proactive] claimed followup {chosen.followup_id} for {session_id}"
        )
        return StageResult.CONTINUE


class EventFollowupRecordStage(BaseStage):
    """After successful deliver, mark followup sent; on abort keep claim/uncertain."""

    name = "event_followup_record"
    hook = HookPoint.RECORD
    priority = 5

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    async def handle(self, ctx) -> StageResult:
        fid = ctx.extra.get("event_followup_id")
        if not fid:
            return StageResult.CONTINUE
        store = _get_store()
        if store is None:
            return StageResult.CONTINUE
        delivery = ctx.delivery
        ok = bool(delivery and getattr(delivery, "success", False))
        if ok:
            store.mark_followup_sent(fid, delivery_id=str(getattr(delivery, "id", "") or ""))
            fu = store.get(fid)
            if fu:
                _SENT_TODAY[fu.subject_id] = _shanghai_day()
        elif ctx.aborted:
            # Generation cancelled (user spoke) → resolve without resend loop
            store.resolve_followup(fid, outcome="cancelled")
        else:
            store.mark_uncertain(fid)
        return StageResult.CONTINUE


class EventFollowupFeature:
    name = "event_followup_feature"

    def __init__(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    def register(self, registry) -> None:
        registry.add_stage(
            hook=HookPoint.INTENT,
            stage=EventFollowupIntentStage(self.orchestrator),
            priority=20,
        )
        registry.add_stage(
            hook=HookPoint.RECORD,
            stage=EventFollowupRecordStage(self.orchestrator),
            priority=5,
        )
