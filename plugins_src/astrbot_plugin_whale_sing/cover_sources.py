"""Bounded staging of audio components supplied by a messaging platform."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import url2pathname

import aiohttp
from astrbot.api.message_components import File, Record

AUDIO_EXTENSIONS = {
    ".wav", ".mp3", ".flac", ".m4a", ".ogg", ".opus", ".aac",
    ".amr", ".silk", ".wma", ".aif", ".aiff",
}
ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "voice-backends").is_dir() and (parent / "sing").is_dir()
)
LOCAL_AUDIO_ROOTS = (
    (ROOT / "data/temp").resolve(),
    (ROOT / "data/attachments").resolve(),
    Path(tempfile.gettempdir()).resolve(),
)


def find_audio_attachment(event) -> object | None:
    """Return the first real audio component, without downloading or reading it."""
    for component in event.get_messages() or []:
        if isinstance(component, Record):
            return component
        if isinstance(component, File) and Path(component.name or "").suffix.lower() in AUDIO_EXTENSIONS:
            return component
    return None


def _public_https(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("音频附件必须使用不含账号密码的 HTTPS 地址")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        raise ValueError("不允许从本机或内网地址获取音频")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if not address.is_global:
        raise ValueError("不允许从本机或内网地址获取音频")


class _PublicResolver(aiohttp.abc.AbstractResolver):
    """Resolve once and hand only public addresses to the HTTP connector."""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        answers = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM,
        )
        results = []
        for address_family, _kind, protocol, _canonical, endpoint in answers:
            address = endpoint[0]
            if not ipaddress.ip_address(address).is_global:
                raise ValueError("音频附件域名指向本机或内网，已拒绝下载")
            results.append({
                "hostname": host, "host": address, "port": port,
                "family": address_family, "proto": protocol,
                "flags": socket.AI_NUMERICHOST,
            })
        if not results:
            raise ValueError("无法解析音频附件地址")
        return results

    async def close(self):
        return None


def _audio_suffix(header: bytes) -> str:
    if header.startswith((b"#!SILK_V3", b"\x02#!SILK_V3")):
        raise ValueError("原始 Silk 语音不能直接翻唱，请发送 MP3/WAV 文件或使用平台已转换的语音")
    if header[:4] in {b"RIFF", b"RF64"} and header[8:12] == b"WAVE":
        return ".wav"
    if header.startswith(b"fLaC"):
        return ".flac"
    if header.startswith(b"OggS"):
        return ".ogg"
    if header.startswith(b"#!AMR"):
        return ".amr"
    if header[4:8] == b"ftyp":
        return ".m4a"
    if header.startswith(b"FORM") and header[8:12] in {b"AIFF", b"AIFC"}:
        return ".aiff"
    if header.startswith(bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c")):
        return ".wma"
    if header.startswith(b"ID3"):
        return ".mp3"
    if len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0:
        return ".aac" if header[1] & 0xF6 == 0xF0 else ".mp3"
    raise ValueError("附件未识别为常见音频，请发送有效的 MP3/WAV/FLAC 文件")


async def stage_audio(
    attachment: object,
    output: Path,
    *,
    max_bytes: int = 100 * 1024 * 1024,
) -> Path:
    """Stage a platform audio attachment into the current job with a size limit.

    Args:
        attachment: An actual File or Record message component.
        output: Filename inside a caller-owned job directory. Its suffix may change.
        max_bytes: Maximum compressed/source file size accepted.

    Returns:
        A new local file with an extension matching its audio header.

    Raises:
        ValueError: The component, source, size, or audio format is unsupported.
        RuntimeError: A public attachment could not be downloaded.
    """
    if not isinstance(attachment, (File, Record)):
        raise ValueError("请上传音频附件，不能用消息文本指定任意本地文件")
    if max_bytes <= 0:
        raise ValueError("音频大小上限必须大于零")
    if isinstance(attachment, File):
        if Path(attachment.name or "").suffix.lower() not in AUDIO_EXTENSIONS:
            raise ValueError("文件附件不是支持的音频类型")
        sources = [attachment.file_, attachment.url]
    else:
        sources = [attachment.file, attachment.url, attachment.path]

    local, remote = None, None
    for raw in sources:
        if not isinstance(raw, str) or not raw:
            continue
        parsed = urlsplit(raw)
        if parsed.scheme in {"http", "https"}:
            remote = remote or raw
            continue
        if parsed.scheme == "file":
            if parsed.netloc:
                raise ValueError("不允许读取网络共享路径")
            raw = url2pathname(parsed.path)
        elif parsed.scheme and not (len(parsed.scheme) == 1 and len(raw) > 2 and raw[1] == ":"):
            raise ValueError("不支持这种音频附件来源，请发送音频文件")
        if raw.startswith(("\\\\", "//")):
            raise ValueError("不允许读取网络共享路径")
        candidate = Path(raw)
        if not candidate.is_absolute():
            continue
        candidate = candidate.resolve()
        if not any(candidate.is_relative_to(root) for root in LOCAL_AUDIO_ROOTS):
            raise ValueError("音频附件必须来自平台临时文件或附件目录")
        if candidate.suffix.lower() not in AUDIO_EXTENSIONS:
            raise ValueError("平台附件的本地文件不是支持的音频类型")
        if candidate.is_file():
            local = candidate
            break

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".source-", suffix=".part", dir=output.parent)
    os.close(descriptor)
    partial = Path(temporary)
    try:
        if local is not None:
            if local.suffix.lower() == ".silk":
                raise ValueError("原始 Silk 语音不能直接翻唱，请发送 MP3/WAV 文件或使用平台已转换的语音")
            if local.stat().st_size > max_bytes:
                raise ValueError(f"音频文件超过 {max_bytes // 1024 // 1024} MiB 上限")
            if isinstance(attachment, File):
                # This option prevents an unbounded framework download if the file
                # disappears between validation and the native API call.
                native_source = await attachment.get_file(allow_return_url=True)
                resolved = None if urlsplit(native_source).scheme in {"http", "https"} else Path(native_source).resolve()
                if resolved is not None and resolved != local:
                    raise ValueError("平台附件路径在读取时发生变化，请重新上传")

            def copy_local():
                copied = 0
                with local.open("rb") as source, partial.open("wb") as destination:
                    while chunk := source.read(1024 * 1024):
                        copied += len(chunk)
                        if copied > max_bytes:
                            raise ValueError("音频超过大小上限")
                        destination.write(chunk)

            copy_task = asyncio.create_task(asyncio.to_thread(copy_local))
            try:
                await asyncio.shield(copy_task)
            except asyncio.CancelledError:
                await copy_task
                raise
        elif remote:
            _public_https(remote)
            connector = aiohttp.TCPConnector(resolver=_PublicResolver(), use_dns_cache=False)
            timeout = aiohttp.ClientTimeout(total=120, connect=10, sock_read=30)
            try:
                async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                    for _redirect in range(6):
                        _public_https(remote)
                        async with session.get(remote, allow_redirects=False) as response:
                            if response.status in {301, 302, 303, 307, 308}:
                                location = response.headers.get("Location")
                                if not location:
                                    raise ValueError("音频附件重定向缺少目标地址")
                                remote = urljoin(remote, location)
                                continue
                            if response.status != 200:
                                raise RuntimeError(f"下载音频附件失败（HTTP {response.status}）")
                            if response.content_length is not None and response.content_length > max_bytes:
                                raise ValueError("音频超过大小上限")
                            downloaded = 0
                            with partial.open("wb") as stream:
                                async for chunk in response.content.iter_chunked(1024 * 256):
                                    downloaded += len(chunk)
                                    if downloaded > max_bytes:
                                        raise ValueError("音频超过大小上限")
                                    stream.write(chunk)
                            break
                    else:
                        raise ValueError("音频附件重定向次数过多")
            except aiohttp.ClientError as error:
                raise RuntimeError("无法下载平台音频附件，请重新上传后重试") from error
            except asyncio.TimeoutError as error:
                raise RuntimeError("下载音频附件超时，请重新上传后重试") from error
        else:
            raise ValueError("找不到可读取的音频附件，请重新上传")
        with partial.open("rb") as source:
            suffix = _audio_suffix(source.read(64))
        destination = output.with_suffix(suffix)
        if local is not None and destination.resolve() == local:
            raise ValueError("暂存输出不能覆盖原始附件")
        partial.replace(destination)
        return destination
    finally:
        partial.unlink(missing_ok=True)
