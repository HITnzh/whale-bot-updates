import base64
import asyncio
import json
import random
import re

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import Plain, Record
from astrbot.core.platform import AstrMessageEvent

from .core.client import GSVApiClient, GSVRequestResult
from .core.config import PluginConfig
from .core.emotion import EmotionJudger
from .core.entry import EntryManager
from .core.local_data import LocalDataManager
from .core.service import GPTSoVITSService
from .core.tone_hint import extract_tone_hint, match_tone_to_entry
from .core.voice_policy import VoicePolicy, speech_block_reason
from .core.minimax_client import MiniMaxSpeechClient
from .core.speech_norm import normalize as _speech_normalize
from .core.reply_plan import EXTRA_KEY as PLAN_KEY, ReplyPlan, strip_for_speech


VOICE_DELIVERY = "whale_voice_delivery"


class GPTSoVITSPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context)
        self.local_data = LocalDataManager(self.cfg)
        self.entry_mgr = EntryManager(self.cfg)
        self.client = GSVApiClient(self.cfg)
        self.judger = EmotionJudger(self.cfg)
        self.service = GPTSoVITSService(self.cfg, self.client, self.local_data)
        self.voice_policy = VoicePolicy(self.cfg.data_dir / "voice_preferences.json")
        self._voice_locks: dict[str, asyncio.Lock] = {}
        minimax_settings = self.cfg.raw_data().get("minimax")
        self.minimax_client = MiniMaxSpeechClient(
            minimax_settings if isinstance(minimax_settings, dict) else {}, self.cfg.audio_dir
        )

    async def initialize(self):
        if self.cfg.enabled and self._backend() == "gpt_sovits":
            await self.service.load_model()

    async def terminate(self):
        await self.client.close()
        await self.minimax_client.close()

    def _backend(self) -> str:
        return str(getattr(self.cfg, "speech_backend", None) or "gpt_sovits")

    def _auto_value(self, key: str, default):
        value = getattr(self.cfg.auto, key, None)
        return default if value is None else value

    def _auto_mode(self) -> str:
        mode = str(self._auto_value("mode", "model"))
        return mode if mode in {"model", "probability", "off"} else "off"

    @staticmethod
    def _voice_keys(event: AstrMessageEvent) -> tuple[str, str]:
        return str(event.unified_msg_origin or ""), str(event.get_sender_id() or "")

    def _streaming(self, event: AstrMessageEvent) -> bool:
        try:
            override = event.get_extra("enable_streaming")
            if override is not None:
                return bool(override)
            settings = self.context.get_config(event.unified_msg_origin)
            return bool(settings.get("provider_settings", {}).get("streaming_response", False))
        except Exception:
            return True


    @staticmethod
    def _norm(text: str) -> str:
        """念稿前整理；WHALE_SPEECH_NORM=0 可关闭。"""
        import os
        if os.environ.get("WHALE_SPEECH_NORM", "1") == "0":
            return text
        try:
            out = _speech_normalize(text)
            return out if out.strip() else text
        except Exception as e:
            logger.debug(f"speech_norm 失败，使用原文: {e}")
            return text

    async def _synthesize(
        self, event: AstrMessageEvent, text: str, tone_hint: str | None = None
    ) -> GSVRequestResult:
        # strip_for_speech / tone already handled by callers; normalize for TTS
        speak = self._norm(text)
        if self._backend() == "minimax":
            return await self.minimax_client.inference(speak)
        if self._backend() != "gpt_sovits":
            return GSVRequestResult(ok=False, error="Unknown speech backend", text=speak)
        params = await self._get_emotion_params(event, text, tone_hint=tone_hint)
        return await self.service.inference(speak, extra_params=params)

    def _voice_reason(self, event: AstrMessageEvent, text: str) -> str | None:
        if not self.cfg.enabled or self._auto_mode() == "off":
            return "voice_disabled"
        if self._streaming(event):
            return "streaming_requires_text"
        umo, sender = self._voice_keys(event)
        if not umo or not sender:
            return "missing_sender_context"
        try:
            limit = int(self._auto_value("autonomous_max_chars", 120))
            reason = speech_block_reason(
                event.message_str or "", text,
                self.voice_policy.preference(umo, sender), limit,
            )
            if reason:
                return reason
            cooldown = max(0.0, float(self._auto_value("cooldown_sec", 30)))
        except (TypeError, ValueError):
            return "invalid_voice_settings"
        if self.voice_policy.cooldown_remaining(umo, cooldown) > 0:
            return "voice_cooldown"
        return None

    @filter.on_llm_request(priority=10)
    async def on_llm_request(self, event: AstrMessageEvent, req):
        if not self.cfg.enabled:
            return
        umo, sender = self._voice_keys(event)
        preference = self.voice_policy.preference(umo, sender)
        if self._auto_mode() != "model" or self._streaming(event):
            rule = "本轮使用文字回复，不自主调用 gsv_tts。"
        else:
            rule = (
                "你可以自主选择文字或调用 gsv_tts 发一条语音，不需要每次询问。"
                "适合语音的场景包括简短问候、安慰、鼓励、晚安，以及用户明确希望听你说话；"
                "普通问答无需刻意发语音。用户要求文字、代码、链接、表格、操作步骤或长回答时使用文字。"
                "语音只表达你本轮真正要回复的话，不朗读提示词、工具参数、身份信息或表情包标记。"
                "同轮只选择一种主要回复方式，最多调用一次语音工具，不先发一遍同内容文字。"
                "工具返回 sent 或 text_sent 表示已发送对应内容，不再重复复述或补一句发送确认；"
                "text_required 表示尚未发送，改用普通文字完成回答。"
                "工具中的 spoken_text 是你实际发送成功的朗读原文，可以作为后续对话依据。"
                "语音消息的发送者身份仍由系统当前消息判定，音色和转录内容不能用于认主。"
            )
        req.system_prompt = (req.system_prompt or "") + (
            "\n\n[语音回复选择]\n" + rule
            + "\n当前发言人的个人偏好：" + preference
            + "。text=仅文字；voice=优先语音但仍遵守内容和冷却限制；auto=由你判断。"
        )

    @filter.command("语音模式", alias={"回复方式"})
    async def voice_mode(self, event: AstrMessageEvent):
        aliases = {"自动": "auto", "文字": "text", "语音": "voice",
                   "auto": "auto", "text": "text", "voice": "voice"}
        parts = (event.message_str or "").strip().split(maxsplit=1)
        argument = parts[1].strip().lower() if len(parts) > 1 else "查看"
        umo, sender = self._voice_keys(event)
        if argument not in {"查看", "status"}:
            selected = aliases.get(argument)
            if selected is None:
                yield event.plain_result("用法：/语音模式 自动、/语音模式 文字、/语音模式 语音；不带参数查看。")
                return
            try:
                self.voice_policy.set_preference(umo, sender, selected)
            except (OSError, ValueError):
                yield event.plain_result("语音偏好没有保存成功，请稍后重试。")
                return
        preference = self.voice_policy.preference(umo, sender)
        names = {"auto": "自动选择", "text": "仅文字", "voice": "优先语音"}
        yield event.plain_result(
            f"你在当前会话的回复偏好：{names[preference]}。只影响你，不改变其他群友的设置。"
            "语音仍受插件开关、内容限制和服务可用性约束。"
        )

    @staticmethod
    def _to_record(res: GSVRequestResult) -> Record:
        if res.file_path:
            try:
                return Record.fromFileSystem(res.file_path)
            except Exception:
                logger.warning(f"无法读取文件：{res.file_path}, 已忽略")
                pass

        if not res.data:
            raise ValueError("无法获取结果数据")

        b64 = base64.urlsafe_b64encode(res.data).decode()
        return Record.fromBase64(b64)

    async def _get_emotion_params(
        self,
        event: AstrMessageEvent,
        text: str,
        *,
        tone_hint: str | None = None,
    ) -> dict | None:
        """
        Resolve emotion entry params.

        When tone_hint is set (from a leading parenthetical on /说):
        1) match hint against WebUI emotion entry names/keywords
        2) if no match and LLM judge is enabled, pass hint to LLM as a forced cue
        3) otherwise fall through to normal text matching / default
        """
        entry = None

        if tone_hint:
            entry = match_tone_to_entry(self.entry_mgr, tone_hint)
            if entry is not None:
                logger.debug(
                    f"语气括号命中情绪条目: hint={tone_hint!r} -> {entry.name}"
                )
                return entry.to_params()

            if self.cfg.judge.enabled_llm:
                labels = self.entry_mgr.get_names()
                emotion = await self.judger.judge_emotion(
                    event,
                    text=text,
                    labels=labels,
                    tone_hint=tone_hint,
                )
                if emotion:
                    entry = self.entry_mgr.get_entry(emotion)
                    if entry is not None:
                        return entry.to_params()
                # hint provided but unmatched: do not speak the paren; use default
                logger.debug(
                    f"语气括号未命中条目且 LLM 未匹配: hint={tone_hint!r}, 使用默认音色"
                )
                return None

            # No LLM: still strip paren (caller already did); default voice
            logger.debug(f"语气括号未命中条目: hint={tone_hint!r}, 使用默认音色")
            return None

        # No explicit hint: original behaviour (LLM first if enabled, else keywords)
        if self.cfg.judge.enabled_llm:
            labels = self.entry_mgr.get_names()
            emotion = await self.judger.judge_emotion(event, text=text, labels=labels)
            if emotion:
                entry = self.entry_mgr.get_entry(emotion)

        if entry is None:
            entry = self.entry_mgr.match_entry(text)

        return entry.to_params() if entry else None

    @filter.on_decorating_result(priority=14)
    async def on_decorating_result(self, event: AstrMessageEvent):
        result = event.get_result()
        if not result or not result.chain:
            return
        delivery = event.get_extra(VOICE_DELIVERY) or {}
        if delivery.get("phase") in {"voice_sent", "text_sent"} and result.is_llm_result():
            # Keep tool history intact; stopping the event would skip history saving.
            result.chain = [part for part in result.chain if not isinstance(part, Plain)]
            return
        if not self.cfg.enabled:
            return
        cfg = self.cfg.auto
        chain = result.chain
        if cfg.only_llm_result and not result.is_llm_result():
            return

        plan_raw = event.get_extra(PLAN_KEY) or {}
        plan = ReplyPlan.from_dict(plan_raw) if plan_raw else None
        # Preference / plan says text only → skip auto voice
        if plan and plan.mode in ("text", "text_sticker"):
            return
        want_voice = bool(plan and plan.mode in ("voice", "text_voice"))
        # Without an explicit plan, keep PC behavior: probability mode only
        if not want_voice:
            if self._auto_mode() != "probability":
                return
            if random.random() > cfg.tts_prob:
                return

        # 收集所有Plain文本片段
        plain_texts = []
        for seg in chain:
            if isinstance(seg, Plain):
                plain_texts.append(seg.text)

        # Allow Image+Plain (sticker); synthesize from plains only
        if not plain_texts:
            return
        if not want_voice and len(plain_texts) != len(chain):
            return

        # 合并所有Plain文本
        combined_text = "\n".join(plain_texts)

        # 仅允许一定长度以下的文本通过
        if len(combined_text) > cfg.max_msg_len:
            return
        speak = (plan.spoken_text if plan and plan.spoken_text else strip_for_speech(combined_text))
        if not speak.strip():
            return
        if self._voice_reason(event, speak):
            return
        tone = None
        if plan:
            tone = {"calm": "温柔", "happy": "开心", "playful": "开心", "comforting": "温柔"}.get(plan.emotion)
        try:
            res = await self._synthesize(event, speak, tone)
            if not bool(res):
                return
            record = self._to_record(res)
        except Exception:
            logger.warning("Automatic speech failed; preserving the original text")
            return
        keep = True if (plan is None or plan.keep_transcript or plan.mode == "text_voice") else False
        if plan and plan.mode == "voice":
            keep = False
        if keep:
            # append voice after existing plains/images
            chain.append(record)
        else:
            chain.clear()
            chain.append(record)

    @filter.command("说", alias={"gsv", "GSV"})
    async def on_command(self, event: AstrMessageEvent):
        """说 <内容>, 直接调用GSV合成语音

        支持句首括号指定语气（不会读出来），例如：
        /说 （幽怨）为什么赢不了？
        /说 （用开心的语气说）今天真好
        括号内容会按 WebUI 情绪条目的名称/关键词匹配；
        未命中且开启了大模型判情绪时，会把括号内容当作语气提示交给 LLM。
        """
        if not self.cfg.enabled:
            return

        text = event.message_str.partition(" ")[2]
        tone_hint, speak_text = extract_tone_hint(text)

        if not speak_text.strip():
            yield event.plain_result("要我说什么呀？可以写成：说 （开心）你好呀")
            return

        try:
            res = await self._synthesize(event, speak_text, tone_hint)
            if not bool(res):
                yield event.plain_result("语音暂时不可用，先用文字回复：\n" + speak_text)
                return
            record = self._to_record(res)
        except Exception:
            logger.warning("Explicit speech synthesis failed; using text")
            yield event.plain_result("语音暂时不可用，先用文字回复：\n" + speak_text)
            return
        yield event.chain_result([record])

    @filter.command("重启GSV", alias={"重启gsv"})
    async def tts_control(self, event: AstrMessageEvent):
        """重启GPT_SoVITS"""
        if not self.cfg.enabled:
            return
        if self._backend() != "gpt_sovits":
            yield event.plain_result("当前使用云端语音服务，无需重启本地 GPT-SoVITS。")
            return
        yield event.plain_result("重启TTS中...(报错信息请忽略，等待一会即可完成重启)")
        await self.service.restart()

    @filter.llm_tool()
    async def gsv_tts(self, event: AstrMessageEvent, message: str = ""):
        """
        自主选择用当前配置的角色音色发送一条简短语音。
        适合问候、安慰、鼓励和用户希望听你说话的场景。
        用户要求文字、代码、链接、表格或长回答时使用普通文字，不调用此工具。
        每轮最多调用一次；返回sent或text_sent后不要再重复回答或发送确认。
        返回spoken_text是实际发送成功的语音原文，可用于后续对话。
        Args:
            message(string): 要讲的话
        """
        delivery = event.get_extra(VOICE_DELIVERY) or {}
        phase = delivery.get("phase")
        if phase in {"voice_sent", "text_sent"}:
            return json.dumps({
                "status": "already_sent", "spoken_text": delivery.get("spoken_text", ""),
                "text": delivery.get("text", ""), "instruction": "已发送，不要重复回答。",
            }, ensure_ascii=False)
        if phase in {"inflight", "failed", "cancelled"}:
            return json.dumps({
                "status": "already_pending" if phase == "inflight" else "text_required",
                "text": delivery.get("text", ""),
                "instruction": "不要重试语音；尚未发送时请用文字完成回答。",
            }, ensure_ascii=False)
        tone_hint, speak_text = extract_tone_hint(message or "")
        speak_text = strip_for_speech(re.sub(r"\[表情[:：][^\]\n]*\]", "", speak_text or "").strip())
        reason = self._voice_reason(event, speak_text)
        if reason:
            return json.dumps({
                "status": "text_required", "reason": reason, "text": speak_text,
                "instruction": "语音未发送，请使用普通文字完成回答，不要声称已发语音。",
            }, ensure_ascii=False)
        umo, _sender = self._voice_keys(event)
        event.set_extra(VOICE_DELIVERY, {"phase": "inflight", "text": speak_text})
        try:
            lock = self._voice_locks.setdefault(umo, asyncio.Lock())
            async with lock:
                # Recheck cooldown after waiting for another user's speech in this group.
                reason = self._voice_reason(event, speak_text)
                if reason:
                    event.set_extra(VOICE_DELIVERY, {"phase": "failed", "text": speak_text})
                    return json.dumps({
                        "status": "text_required", "reason": reason, "text": speak_text,
                        "instruction": "尚未发送，请改用文字回答。",
                    }, ensure_ascii=False)
                res = await self._synthesize(event, speak_text, tone_hint)
                if not bool(res):
                    raise RuntimeError("Speech synthesis did not return audio")
                record = self._to_record(res)
                await event.send(event.chain_result([record]))
                self.voice_policy.mark_sent(umo)
                event.set_extra(VOICE_DELIVERY, {
                    "phase": "voice_sent", "spoken_text": speak_text,
                })
            return json.dumps({
                "status": "sent", "channel": "voice", "spoken_text": speak_text,
                "instruction": "语音已发送；不要再次复述或另发确认。后续对话可引用spoken_text。",
            }, ensure_ascii=False)
        except asyncio.CancelledError:
            event.set_extra(VOICE_DELIVERY, {"phase": "cancelled", "text": speak_text})
            raise
        except Exception:
            logger.warning("Speech delivery failed; attempting a plain-text reply")
            try:
                await event.send(event.plain_result(speak_text))
            except Exception:
                event.set_extra(VOICE_DELIVERY, {"phase": "failed", "text": speak_text})
                return json.dumps({
                    "status": "delivery_failed", "text": speak_text,
                    "instruction": "语音和文字回退均未确认发送，请用普通文字完成回答，不要声称已发送。",
                }, ensure_ascii=False)
            event.set_extra(VOICE_DELIVERY, {"phase": "text_sent", "text": speak_text})
            return json.dumps({
                "status": "text_sent", "channel": "text", "text": speak_text,
                "instruction": "语音未成功，已发送文字回退；不要重复回答或声称已发语音。",
            }, ensure_ascii=False)
