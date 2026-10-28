"""Optional MiniMax speech synthesis; no enrollment or implicit voice fallback.

API contract: https://platform.minimax.io/docs/api-reference/speech-t2a-http
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import os
import re
import tempfile
import wave
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from .client import GSVRequestResult


OFFICIAL_BASE_URLS = frozenset({"https://api.minimaxi.com", "https://api.minimax.io"})
SUPPORTED_MODELS = frozenset({
    "speech-2.8-hd", "speech-2.8-turbo", "speech-2.6-hd", "speech-2.6-turbo",
    "speech-02-hd", "speech-02-turbo", "speech-01-hd", "speech-01-turbo",
})
SUPPORTED_EMOTIONS = frozenset({
    "happy", "sad", "angry", "fearful", "disgusted", "surprised", "neutral",
})
MAX_AUDIO_BYTES = 20 * 1024 * 1024
MAX_RESPONSE_BYTES = MAX_AUDIO_BYTES * 2 + 1024 * 1024


def _valid_wav(data: bytes) -> bool:
    """Require a complete PCM16 mono WAV matching the requested sample rate."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return False
    if len(data) < 46 or int.from_bytes(data[4:8], "little") + 8 != len(data):
        return False
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            if (
                audio.getcomptype() != "NONE"
                or audio.getnchannels() != 1
                or audio.getframerate() != 32000
                or audio.getsampwidth() != 2
                or audio.getnframes() < 1
            ):
                return False
            audio.setpos(audio.getnframes() - 1)
            return len(audio.readframes(1)) == 2
    except (EOFError, OSError, ValueError, wave.Error):
        return False


class MiniMaxSpeechClient:
    """A lazy, bounded client using a configured voice and environment credential."""

    def __init__(self, settings: Mapping[str, Any], audio_dir: Path):
        self.settings = dict(settings)
        self.audio_dir = Path(audio_dir)
        self._session: ClientSession | None = None
        self._closed = False

    async def close(self) -> None:
        self._closed = True
        if self._session is not None:
            await self._session.close()
            self._session = None

    @staticmethod
    def _error(text: str, message: str) -> GSVRequestResult:
        return GSVRequestResult(ok=False, error=message, text=text)

    async def inference(self, text: str, *, emotion: str | None = None) -> GSVRequestResult:
        """Synthesize one text without retries, other voices, or response URL fetches."""
        if not isinstance(text, str) or not text.strip():
            return self._error("", "MiniMax 朗读文本不能为空。")
        if len(text) >= 10000:
            return self._error(text, "MiniMax 单次朗读文本必须少于10000字符。")
        if self._closed:
            return self._error(text, "MiniMax 语音客户端已关闭。")

        base_url = str(self.settings.get("base_url") or "https://api.minimaxi.com").rstrip("/")
        if base_url not in OFFICIAL_BASE_URLS:
            return self._error(text, "MiniMax 地址仅支持官方 HTTPS 中国或国际端点。")
        model = str(self.settings.get("model") or "speech-2.8-hd")
        if model not in SUPPORTED_MODELS:
            return self._error(text, "MiniMax 语音模型不受支持，请检查模型配置。")
        voice_id = str(self.settings.get("voice_id") or "").strip()
        if not voice_id or len(voice_id) > 256 or any(ord(char) < 32 for char in voice_id):
            return self._error(text, "请先配置 MiniMax voice_id；不会自动使用其他音色。")
        env_name = str(self.settings.get("api_key_env") or "MINIMAX_API_KEY")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
            return self._error(text, "MiniMax 密钥环境变量名称无效。")
        api_key = os.environ.get(env_name, "").strip()
        if not api_key:
            return self._error(text, "MiniMax 密钥环境变量未配置，尚未调用语音服务。")
        try:
            timeout = float(self.settings.get("timeout", 60))
        except (TypeError, ValueError):
            timeout = 0
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 600:
            return self._error(text, "MiniMax 请求超时必须为大于0且不超过600的秒数。")
        if emotion is not None and emotion not in SUPPORTED_EMOTIONS:
            return self._error(text, "MiniMax 语气参数不受支持。")

        voice_setting: dict[str, Any] = {"voice_id": voice_id, "speed": 1, "vol": 1, "pitch": 0}
        if emotion is not None:
            voice_setting["emotion"] = emotion
        payload = {
            "model": model,
            "text": text,
            "stream": False,
            "output_format": "hex",
            "language_boost": "auto",
            "voice_setting": voice_setting,
            "audio_setting": {"sample_rate": 32000, "format": "wav", "channel": 1},
        }
        try:
            if self._session is None:
                self._session = ClientSession(timeout=ClientTimeout(total=timeout))
            async with self._session.post(
                f"{base_url}/v1/t2a_v2",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    return self._error(text, f"MiniMax 语音请求失败（HTTP {response.status}）。")
                if response.content_length is not None and response.content_length > MAX_RESPONSE_BYTES:
                    return self._error(text, "MiniMax 语音响应超过大小上限。")
                raw = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                        return self._error(text, "MiniMax 语音响应超过大小上限。")
                    raw.extend(chunk)
        except (ClientError, asyncio.TimeoutError):
            # Exception messages and response bodies can contain credentials or user text.
            return self._error(text, "MiniMax 语音网络请求失败或超时，请检查服务连接。")

        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            return self._error(text, "MiniMax 没有返回有效 JSON 语音结果。")
        if not isinstance(result, dict):
            return self._error(text, "MiniMax 语音响应结构无效。")
        base_resp = result.get("base_resp")
        if not isinstance(base_resp, dict) or type(base_resp.get("status_code")) is not int:
            return self._error(text, "MiniMax 语音响应缺少有效业务状态。")
        status_code = base_resp["status_code"]
        if status_code != 0:
            return self._error(text, f"MiniMax 语音服务返回业务错误（{status_code}），请检查账户与音色配置。")
        data = result.get("data")
        if not isinstance(data, dict) or type(data.get("status")) is not int or data["status"] != 2:
            return self._error(text, "MiniMax 未返回已完成的语音。")
        hex_audio = data.get("audio")
        if not isinstance(hex_audio, str) or not hex_audio or len(hex_audio) % 2:
            return self._error(text, "MiniMax 语音编码无效。")
        if len(hex_audio) > MAX_AUDIO_BYTES * 2:
            return self._error(text, "MiniMax 音频超过20 MiB大小上限。")
        if not re.fullmatch(r"[0-9a-fA-F]+", hex_audio):
            return self._error(text, "MiniMax 语音编码无效。")
        audio = bytes.fromhex(hex_audio)
        if not _valid_wav(audio):
            return self._error(text, "MiniMax 未返回完整的32000Hz单声道PCM16 WAV音频。")

        output: Path | None = None
        try:
            self.audio_dir.mkdir(parents=True, exist_ok=True)
            handle, name = tempfile.mkstemp(prefix="minimax_", suffix=".wav", dir=self.audio_dir)
            output = Path(name)
            with os.fdopen(handle, "wb") as stream:
                stream.write(audio)
        except OSError:
            if output is not None:
                output.unlink(missing_ok=True)
            return self._error(text, "MiniMax 合成结果保存失败，请检查音频目录。")
        return GSVRequestResult(ok=True, data=audio, text=text, file_path=str(output.resolve()))
