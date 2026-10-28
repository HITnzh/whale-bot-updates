"""Audio-only helpers for recombining converted vocals with their backing track."""

from __future__ import annotations

import asyncio
import math
import os
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path


class AudioProcessError(RuntimeError):
    """Keep a worker's exit status so callers can narrowly handle CUDA failures."""

    def __init__(self, message: str, returncode: int):
        super().__init__(message)
        self.returncode = returncode


async def _terminate_process_tree(process) -> None:
    """Stop only this worker tree, including Windows venv redirector children."""
    if os.name == "nt" and process.returncode is None and getattr(process, "pid", None) is not None:
        taskkill = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/taskkill.exe"
        try:
            killer = await asyncio.create_subprocess_exec(
                str(taskkill), "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                await asyncio.wait_for(killer.communicate(), timeout=15)
            except TimeoutError:
                killer.kill()
                await killer.communicate()
        except (OSError, ProcessLookupError):
            pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.communicate()


def find_ffmpeg(configured_path: str = "") -> str:
    """Find an explicitly configured, system, or bundled FFmpeg executable."""
    if configured_path:
        configured = Path(configured_path).expanduser()
        if configured.is_file():
            return str(configured.resolve())
        raise FileNotFoundError("配置的 FFmpeg 不存在，请检查 ffmpeg_path。")
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    for ancestor in Path(__file__).resolve().parents:
        bundled = ancestor / "voice-backends" / "RVC" / "tools"
        matches = sorted(bundled.glob("ffmpeg-*/bin/ffmpeg.exe"))
        if matches:
            return str(matches[-1])
    raise FileNotFoundError("未找到 FFmpeg，请配置 ffmpeg_path 或安装项目附带的 FFmpeg。")


async def run_process(
    *args: str | Path,
    timeout: float,
    cwd: str | Path | None = None,
) -> tuple[bytes, bytes]:
    """Run a hidden worker, collecting output and reaping it on cancellation."""
    if not args:
        raise ValueError("未指定要执行的程序。")
    options = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        process = await asyncio.create_subprocess_exec(
            *(str(arg) for arg in args),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd is not None else None,
            **options,
        )
    except OSError as error:
        raise RuntimeError(f"无法启动音频处理程序：{Path(args[0]).name}") from error
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError) as error:
        await _terminate_process_tree(process)
        if isinstance(error, asyncio.CancelledError):
            raise
        raise TimeoutError(f"音频处理超时（{timeout:g} 秒）。") from error
    if process.returncode:
        detail = stderr.decode("utf-8", errors="replace")[-700:].strip()
        raise AudioProcessError(
            f"音频处理失败（{Path(args[0]).name}，退出码 {process.returncode}）：{detail}",
            process.returncode,
        )
    return stdout, stderr


async def mix_audio(
    vocals: Path,
    instrumental: Path,
    out: Path,
    ffmpeg: str,
    *,
    vocal_gain: float = 1.0,
    instrumental_gain: float = 1.0,
    timeout: float = 120,
) -> Path:
    """Mix only the supplied stems without trimming either stem's timeline."""
    vocals, instrumental, out = Path(vocals), Path(instrumental), Path(out)
    for source in (vocals, instrumental):
        if not source.is_file():
            raise FileNotFoundError(f"缺少混音轨道：{source.name}")
        if source.resolve() == out.resolve():
            raise ValueError("混音输出不能覆盖输入轨道。")
    if any(not math.isfinite(gain) or gain < 0 for gain in (vocal_gain, instrumental_gain)):
        raise ValueError("人声和伴奏音量必须是非负有限数值。")
    out.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(suffix=".wav", prefix=".mix-", dir=out.parent)
    os.close(handle)
    temporary_path = Path(temporary)
    # Limiter latency compensation keeps the converted voice aligned to the backing.
    filters = (
        "[0:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"volume={vocal_gain:.8g}[v];"
        "[1:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"volume={instrumental_gain:.8g}[i];"
        "[v][i]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
        "alimiter=limit=0.95:level=0:latency=1[m]"
    )
    try:
        await run_process(
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", vocals, "-i", instrumental,
            "-filter_complex", filters, "-map", "[m]", "-vn",
            "-ar", "44100", "-ac", "2", "-c:a", "pcm_s24le", temporary_path,
            timeout=timeout,
        )
        with wave.open(str(temporary_path), "rb") as audio:
            if audio.getnframes() <= 0:
                raise RuntimeError("混音没有产生有效音频。")
        os.replace(temporary_path, out)
    finally:
        temporary_path.unlink(missing_ok=True)
    return out


async def make_preview(
    source: Path,
    out: Path,
    ffmpeg: str,
    *,
    timeout: float = 120,
) -> Path:
    """Render a separate mono PCM16 QQ preview, keeping the stereo master intact."""
    source, out = Path(source), Path(out)
    if not source.is_file():
        raise FileNotFoundError(f"缺少待发送音频：{source.name}")
    if source.resolve() == out.resolve():
        raise ValueError("语音预览不能覆盖立体声成品。")
    out.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(suffix=".wav", prefix=".preview-", dir=out.parent)
    os.close(handle)
    temporary_path = Path(temporary)
    try:
        await run_process(
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", source, "-map", "0:a:0", "-vn",
            "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", temporary_path,
            timeout=timeout,
        )
        with wave.open(str(temporary_path), "rb") as audio:
            if audio.getnframes() <= 0:
                raise RuntimeError("语音转换没有产生有效音频。")
        os.replace(temporary_path, out)
    finally:
        temporary_path.unlink(missing_ok=True)
    return out
