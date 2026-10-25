import base64
import random

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


class GPTSoVITSPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context)
        self.local_data = LocalDataManager(self.cfg)
        self.entry_mgr = EntryManager(self.cfg)
        self.client = GSVApiClient(self.cfg)
        self.judger = EmotionJudger(self.cfg)
        self.service = GPTSoVITSService(self.cfg, self.client, self.local_data)

    async def initialize(self):
        if self.cfg.enabled:
            await self.service.load_model()

    async def terminate(self):
        await self.client.close()

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
        """消息入口"""
        if not self.cfg.enabled:
            return
        cfg = self.cfg.auto

        result = event.get_result()
        if not result:
            return
        chain = result.chain
        if not chain:
            return
        if cfg.only_llm_result and not result.is_llm_result():
            return
        if random.random() > cfg.tts_prob:
            return

        # 收集所有Plain文本片段
        plain_texts = []
        for seg in chain:
            if isinstance(seg, Plain):
                plain_texts.append(seg.text)

        # 仅允许只含有Plain的消息链通过
        if len(plain_texts) != len(chain):
            return

        # 合并所有Plain文本
        combined_text = "\n".join(plain_texts)

        # 仅允许一定长度以下的文本通过
        if len(combined_text) > cfg.max_msg_len:
            return

        params = await self._get_emotion_params(event, combined_text)
        res = await self.service.inference(combined_text, extra_params=params)
        if not bool(res):
            return
        chain.clear()
        chain.append(self._to_record(res))

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

        params = await self._get_emotion_params(
            event, speak_text, tone_hint=tone_hint
        )
        res = await self.service.inference(speak_text, extra_params=params)

        if not bool(res):
            yield event.plain_result(res.error)
            return

        yield event.chain_result([self._to_record(res)])

    @filter.command("重启GSV", alias={"重启gsv"})
    async def tts_control(self, event: AstrMessageEvent):
        """重启GPT_SoVITS"""
        if not self.cfg.enabled:
            return
        yield event.plain_result("重启TTS中...(报错信息请忽略，等待一会即可完成重启)")
        await self.service.restart()

    @filter.llm_tool()
    async def gsv_tts(self, event: AstrMessageEvent, message: str = ""):
        """
        用语音输出要讲的话
        Args:
            message(string): 要讲的话
        """
        try:
            tone_hint, speak_text = extract_tone_hint(message or "")
            if not speak_text.strip():
                speak_text = message or ""
                tone_hint = None
            params = await self._get_emotion_params(
                event, speak_text, tone_hint=tone_hint
            )
            res = await self.service.inference(speak_text, extra_params=params)
            if not bool(res):
                return res.error
            seg = self._to_record(res)
            await event.send(event.chain_result([seg]))
        except Exception as e:
            return str(e)
