"""Render cached song covers using only separation, voice conversion, and mixing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
import tempfile
import time
import wave
from collections.abc import Awaitable, Callable
from pathlib import Path

from .audio_processing import make_preview, mix_audio, run_process

PIPELINE_VERSION = "cover-stems-v1"


def _write_manifest(entry: Path, manifest: dict) -> None:
    """Publish one complete JSON state without exposing a partially written record."""
    handle, temporary = tempfile.mkstemp(prefix=".cover-", suffix=".json", dir=entry)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
        temporary_path.replace(entry / "cover.json")
    finally:
        temporary_path.unlink(missing_ok=True)


def _valid_wav(path: Path, channels: int, rate: int, width: int) -> bool:
    """Check the expected PCM format and that the final declared frame exists."""
    try:
        with wave.open(str(path), "rb") as audio:
            if (
                audio.getnchannels() != channels
                or audio.getframerate() != rate
                or audio.getsampwidth() != width
                or audio.getnframes() < 1
            ):
                return False
            audio.setpos(audio.getnframes() - 1)
            return len(audio.readframes(1)) == channels * width
    except (OSError, EOFError, wave.Error):
        return False


def _input_key(source: Path, fingerprint: dict) -> tuple[str, str]:
    """Hash source bytes plus the caller's model/settings fingerprint."""
    with source.open("rb") as stream:
        source_digest = hashlib.file_digest(stream, "sha256").hexdigest()
    serialized = json.dumps(
        {"source_sha256": source_digest, "pipeline": PIPELINE_VERSION, "fingerprint": fingerprint},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return source_digest, hashlib.sha256(serialized.encode("utf-8")).hexdigest()


async def render_cover(
    source: Path,
    cache_root: Path,
    ffmpeg: str,
    *,
    fingerprint: dict,
    separate: Callable[[Path, Path], Awaitable[tuple[Path, Path]]],
    convert: Callable[[Path], Awaitable[Path]],
    max_duration: float = 300,
) -> Path:
    """Return a QQ cover preview, reusing a finished matching cover when possible.

    Args:
        source: Existing original audio; never overwritten or deleted.
        cache_root: Directory owned by the cover cache.
        ffmpeg: FFmpeg executable; its sibling ffprobe is preferred.
        fingerprint: Model, index, separation model, and settings identities.
        separate: Produce vocals and backing from the same normalized song.
        convert: Convert only the vocals to the requested character voice.
        max_duration: Reject longer source audio instead of silently truncating it.

    Returns:
        Path to the completed mono PCM16 QQ preview.

    Raises:
        ValueError: The source has no valid audio or exceeds the duration cap.
        RuntimeError: An audio stage fails or its output is invalid.
    """
    source = Path(source).resolve()
    if not source.is_file():
        raise FileNotFoundError("原歌曲文件不存在，请重新上传或检查曲库。")
    if not math.isfinite(max_duration) or max_duration <= 0:
        raise ValueError("翻唱最长时长必须是大于零的有限数值。")
    source_digest, key = await asyncio.to_thread(_input_key, source, fingerprint)
    cache_root = Path(cache_root).resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    entry = (cache_root / key).resolve()
    if not entry.is_relative_to(cache_root):
        raise ValueError("翻唱缓存目录包含外部链接，已停止处理。")
    entry.mkdir(parents=True, exist_ok=True)
    final, preview = entry / "final.wav", entry / "qq_preview.wav"
    try:
        manifest = json.loads((entry / "cover.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    if isinstance(manifest, dict) and (
        manifest.get("status") == "complete"
        and manifest.get("key") == key
        and manifest.get("pipeline_version") == PIPELINE_VERSION
        and manifest.get("source_sha256") == source_digest
    ):
        try:
            duration = float(manifest.get("duration_seconds", 0))
        except (TypeError, ValueError):
            duration = 0
        if math.isfinite(duration) and duration > max_duration:
            raise ValueError(f"原曲长 {duration:.1f} 秒，超过翻唱上限 {max_duration:g} 秒，请先准备较短版本。")
        if duration > 0 and math.isfinite(duration) and _valid_wav(final, 2, 44100, 3) and _valid_wav(preview, 1, 24000, 2):
            return preview

    manifest = {
        "status": "running",
        "key": key,
        "pipeline_version": PIPELINE_VERSION,
        "source_sha256": source_digest,
        "source_name": source.name,
        "fingerprint": fingerprint,
        "created_at": time.time(),
    }
    _write_manifest(entry, manifest)
    try:
        ffmpeg_path = Path(ffmpeg)
        sibling = ffmpeg_path.with_name("ffprobe.exe" if ffmpeg_path.suffix.lower() == ".exe" else "ffprobe")
        ffprobe = str(sibling) if sibling.is_file() else shutil.which("ffprobe")
        if not ffprobe:
            raise FileNotFoundError("未找到 FFprobe，请恢复 FFmpeg 同目录中的 ffprobe。")
        stdout, _stderr = await run_process(
            ffprobe, "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=duration:format=duration", "-of", "json", str(source),
            timeout=30,
        )
        try:
            info = json.loads(stdout)
            streams = info.get("streams") or []
            raw_duration = info.get("format", {}).get("duration") or (streams[0].get("duration") if streams else 0)
            duration = float(raw_duration)
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("无法读取原歌曲的有效音频时长。") from error
        if not streams or not math.isfinite(duration) or duration <= 0:
            raise ValueError("原文件没有可处理的音频或有效时长。")
        if duration > max_duration:
            raise ValueError(f"原曲长 {duration:.1f} 秒，超过翻唱上限 {max_duration:g} 秒，请先准备较短版本。")
        normalized = entry / "source.wav"
        if normalized.resolve() == source:
            normalized = entry / "normalized.wav"
        if not normalized.resolve().is_relative_to(entry):
            raise ValueError("缓存音轨指向外部路径，已停止处理。")
        await run_process(
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(source), "-map", "0:a:0", "-vn", "-ar", "44100", "-ac", "2",
            "-c:a", "pcm_s16le", str(normalized), timeout=120,
        )
        if not _valid_wav(normalized, 2, 44100, 2):
            raise RuntimeError("原歌曲未能转换为有效音频。")
        with wave.open(str(normalized), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
        if duration > max_duration:
            raise ValueError(f"实际音频长 {duration:.1f} 秒，超过翻唱上限 {max_duration:g} 秒。")
        manifest["duration_seconds"] = duration
        vocals, backing = await separate(normalized, entry)
        character_vocals = await convert(vocals)
        await mix_audio(character_vocals, backing, final, ffmpeg)
        await make_preview(final, preview, ffmpeg)
        if not _valid_wav(final, 2, 44100, 3) or not _valid_wav(preview, 1, 24000, 2):
            raise RuntimeError("翻唱没有生成有效的成品和语音预览。")
        # Remove only known intermediates owned by this cache entry, never the input.
        intermediates = {normalized, Path(vocals), Path(backing), Path(character_vocals)}
        intermediates.update(entry / name for name in ("source.wav", "normalized.wav", "vocals.wav", "instrumental.wav", "rvc_vocals.wav"))
        reserved = {source, final.resolve(), preview.resolve(), (entry / "cover.json").resolve()}
        for intermediate in intermediates:
            resolved = intermediate.resolve()
            if resolved.is_relative_to(entry) and resolved not in reserved:
                intermediate.unlink(missing_ok=True)
        manifest.update(status="complete", completed_at=time.time())
        _write_manifest(entry, manifest)
        return preview
    except BaseException as error:
        manifest.update(status="failed", error_type=type(error).__name__, error=str(error)[-700:])
        _write_manifest(entry, manifest)
        raise
