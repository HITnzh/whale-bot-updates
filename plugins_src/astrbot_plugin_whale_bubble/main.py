"""Owner-only: send a short proactive bubble into a registered QQ-official group."""
from __future__ import annotations

import re

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register


def _parse_groups(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        alias, _, umo = line.partition("=")
        alias, umo = alias.strip(), umo.strip().strip("「」\"'` ")
        if alias.lower().startswith("umo"):
            continue
        if alias and umo and ":GroupMessage:" in umo:
            out[alias.casefold()] = umo
    return out


@register(
    "whale_bubble",
    "whale",
    "主人私聊让鲸鱼娘去已登记的群主动冒泡（QQ官机用 openid UMO）",
    "1.0.0",
)
class WhaleBubble(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}

    def _admins(self) -> set[str]:
        try:
            return {str(a) for a in (self.context.get_config().get("admins_id") or [])}
        except Exception:
            return set()

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        return str(event.get_sender_id()) in self._admins()

    def _groups(self) -> dict[str, str]:
        return _parse_groups(str(self.config.get("groups") or ""))

    def _resolve(self, hint: str) -> tuple[str, str] | None:
        groups = self._groups()
        key = (hint or "").strip().casefold()
        if not key:
            return None
        if key in groups:
            return hint.strip(), groups[key]
        for alias, umo in groups.items():
            if key in alias or alias in key:
                return alias, umo
        if ":GroupMessage:" in hint:
            return "umo", hint.strip()
        for alias, umo in groups.items():
            if hint.strip() and hint.strip() in umo:
                return alias, umo
        return None

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("冒泡")
    async def cmd_bubble(self, event: AstrMessageEvent):
        """用法：/冒泡 群名或别名 [可选文案]"""
        if not self._is_owner(event):
            yield event.plain_result("这个只能主人用哦～")
            return
        raw = (event.message_str or "").strip()
        raw = re.sub(r"^[/／]?冒泡\s*", "", raw, count=1).strip()
        if not raw:
            names = "、".join(sorted(self._groups())) or "（还没登记群）"
            yield event.plain_result(f"用法：/冒泡 群名 [内容]\n已登记：{names}")
            return
        parts = raw.split(None, 1)
        hint = parts[0]
        text = parts[1] if len(parts) > 1 else str(
            self.config.get("default_text") or "咕噜～本鲸鱼路过冒个泡 (｀・ω・´)"
        )
        hit = self._resolve(hint)
        if not hit:
            yield event.plain_result(
                "找不到这个群的会话。QQ 官方机器人不能用数字群号，"
                "请先在目标群里 @我 发 /sid，把 GroupMessage UMO 填进「鲸鱼娘去群冒泡」配置。"
            )
            return
        alias, umo = hit
        ok = await self._send(umo, text)
        if ok:
            yield event.plain_result(f"已经往「{alias}」冒了个泡～")
        else:
            yield event.plain_result(
                "发送失败了。常见原因：刚重启过、群里还没人说过话（要先有一条群消息），"
                "或群设置没开「机器人主动在群聊内发言」。先在群里说一句再试。"
            )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.llm_tool(name="bubble_in_group")
    async def tool_bubble(
        self,
        event: AstrMessageEvent,
        group_hint: str,
        text: str = "",
    ):
        """当主人要求去某个QQ群冒泡、打招呼、发一句时调用。必须用已登记的群名/别名。
        Args:
            group_hint(string): 群名、别名或已登记的群号提示（不要臆造数字群号发信）
            text(string): 要发到群里的短文案，可空则用默认冒泡语
        """
        if not self._is_owner(event):
            return "只有主人能让我去群里冒泡。"
        hit = self._resolve(group_hint)
        if not hit:
            return (
                "群会话未登记。请主人在目标群 @我 发送 /sid，"
                "把返回含 GroupMessage 的 UMO 写入冒泡插件配置（不能只用数字群号 754769928）。"
            )
        alias, umo = hit
        body = (text or "").strip() or str(
            self.config.get("default_text") or "咕噜～本鲸鱼路过冒个泡 (｀・ω・´)"
        )
        ok = await self._send(umo, body)
        if ok:
            return f"已发送到群「{alias}」。"
        return (
            "发送失败：可能是重启后该群还没有入站消息。请先在群里出现任意一条消息后再试；"
            "并确认群内开启了机器人主动发言。"
        )

    async def _send(self, umo: str, text: str) -> bool:
        try:
            # Prefer MessageChain().message for compatibility
            chain = MessageChain().message(text)
            return bool(await self.context.send_message(umo, chain))
        except Exception as e:
            logger.error(f"[whale_bubble] send failed to {umo}: {e}")
            return False
